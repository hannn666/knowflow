from contextlib import contextmanager
from io import BytesIO
from uuid import UUID, uuid4

import pymupdf
import pytest
from sqlalchemy import select

from project.api import auth_tokens
from project.core.document_parser import parse_pdf_version
from project.core.document_storage import DocumentStorage, get_document_storage
from project.core.parse_artifacts import ParseArtifactStore
from project.core.parse_executor import ParseExecutionError, get_parse_executor
from project.db.business_models import DocumentParseStage, DocumentVersion


def public_pdf(texts=('Public first page', '', 'Public third page')):
    with pymupdf.open() as pdf:
        for text in texts:
            page = pdf.new_page()
            if text:
                page.insert_text((72, 72), text)
        return pdf.tobytes()


class InlineTestExecutor:
    """Control-flow fixture only; real spawned-process cases are separate."""
    def __init__(self):
        self.calls = 0
        self.error = None

    @contextmanager
    def slot(self):
        yield

    def run(self, source, artifacts, reservation):
        self.calls += 1
        if self.error:
            raise ParseExecutionError(self.error)
        parsed = parse_pdf_version(source, artifacts.storage)
        return artifacts.publish(reservation, parsed)


@pytest.fixture
def stage_context(client, db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(auth_tokens, 'load_dotenv', lambda *_: None)
    monkeypatch.setenv('KNOWFLOW_JWT_SECRET', 'ab' * 32)
    storage = DocumentStorage(tmp_path)
    executor = InlineTestExecutor()
    client.app.dependency_overrides[get_document_storage] = lambda: storage
    client.app.dependency_overrides[get_parse_executor] = lambda: executor
    accounts = []
    for _ in range(2):
        credentials = {'email': f'stage-{uuid4().hex}@example.com', 'password': 'test-only parsing stage password'}
        user = client.post('/auth/register', json=credentials)
        assert user.status_code == 201
        login = client.post('/auth/login', json=credentials)
        assert login.status_code == 200
        headers = {'Authorization': f"Bearer {login.json()['access_token']}"}
        kb = client.post('/knowledge-bases', headers=headers, json={'name': 'Stage tests'})
        assert kb.status_code == 201
        accounts.append((headers, kb.json()['id']))
    headers, kb = accounts[0]
    uploaded = client.post(f'/knowledge-bases/{kb}/documents', headers=headers,
                           files={'file': ('public.pdf', public_pdf(), 'application/pdf')})
    assert uploaded.status_code == 201
    item = uploaded.json()
    path = f"/knowledge-bases/{kb}/documents/{item['document_id']}/versions/{item['document_version_id']}/parse"
    return client, db_session, storage, executor, accounts, item, path


def test_not_started_is_not_a_version_status(stage_context):
    client, session, _, executor, accounts, item, path = stage_context
    response = client.get(path, headers=accounts[0][0])
    assert response.status_code == 200
    assert response.json()['parse_status'] == 'not_started'
    assert response.json()['page_count'] is None
    assert executor.calls == 0
    assert session.get(DocumentParseStage, UUID(item['document_version_id'])) is None
    assert session.get(DocumentVersion, UUID(item['document_version_id'])).status == 'pending'


def test_success_persists_pages_and_cached_repeat_is_read_only(stage_context):
    client, session, storage, executor, accounts, item, path = stage_context
    headers = accounts[0][0]
    first = client.post(path, headers=headers)
    assert first.status_code == 200
    body = first.json()
    assert body['parse_status'] == 'succeeded'
    assert body['page_count'] == 3 and body['has_text'] is True
    assert body['error_code'] is None and body['finished_at']
    assert 'page_text' not in first.text
    source = session.get(DocumentVersion, UUID(item['document_version_id']))
    from project.core.document_parser import DocumentVersionSource
    metadata = DocumentVersionSource(UUID(item['knowledge_base_id']), source.document_id, source.id, source.original_filename)
    record = session.get(DocumentParseStage, source.id)
    parsed = ParseArtifactStore(storage).read(metadata, record.attempt_id, record.result_sha256)
    assert [page.page_number for page in parsed.pages] == [1, 2, 3]
    assert [page.page_text.strip() for page in parsed.pages] == ['Public first page', '', 'Public third page']
    original = storage.path_for(metadata.knowledge_base_id, metadata.document_id, metadata.document_version_id)
    before = original.read_bytes()
    def forbid_write(*args, **kwargs):
        pytest.fail('Successful repeats must not publish again')
    executor.run = forbid_write
    second = client.post(path, headers=headers)
    assert second.status_code == 200 and second.json() == body
    assert client.get(path, headers=headers).json() == body
    assert original.read_bytes() == before
    session.expire_all()
    assert session.get(DocumentVersion, source.id).status == 'pending'


@pytest.mark.parametrize('code', ['source_missing', 'invalid_pdf', 'password_required', 'parse_timeout', 'resource_limit'])
def test_failed_stage_has_safe_queryable_error(stage_context, code):
    client, session, storage, executor, accounts, item, path = stage_context
    executor.error = code
    response = client.post(path, headers=accounts[0][0])
    assert response.status_code == 200
    body = response.json()
    assert body['parse_status'] == 'failed' and body['error_code'] == code
    assert body['error_message'] and body['page_count'] is None
    assert str(storage.root) not in response.text
    assert client.get(path, headers=accounts[0][0]).json() == body
    assert session.get(DocumentVersion, UUID(item['document_version_id'])).status == 'pending'
    assert not list(storage.root.rglob('pages.json'))


def test_explicit_retry_after_failure_uses_new_attempt(stage_context):
    client, _, storage, executor, accounts, _, path = stage_context
    executor.error = 'invalid_pdf'
    first = client.post(path, headers=accounts[0][0]).json()
    executor.error = None
    second = client.post(path, headers=accounts[0][0]).json()
    assert first['parse_status'] == 'failed' and second['parse_status'] == 'succeeded'
    assert first['attempt_id'] != second['attempt_id']
    assert len(list(storage.root.rglob('pages.json'))) == 1


@pytest.mark.parametrize('method', ['get', 'post'])
@pytest.mark.parametrize('authorization', [None, 'Bearer invalid-token'])
def test_stage_requires_auth(stage_context, method, authorization):
    client, _, storage, executor, _, _, path = stage_context
    headers = {} if authorization is None else {'Authorization': authorization}
    assert getattr(client, method)(path, headers=headers).status_code == 401
    assert executor.calls == 0 and not list(storage.root.rglob('pages.json'))


def test_complete_ownership_chain_for_trigger_and_query(stage_context):
    client, _, storage, executor, accounts, item, path = stage_context
    own_headers, kb = accounts[0]
    other_headers, other_kb = accounts[1]
    second_kb = client.post('/knowledge-bases', headers=own_headers, json={'name': 'Other own KB'}).json()['id']
    cases = [(other_headers, path), (own_headers, path.replace(kb, other_kb)),
             (own_headers, path.replace(kb, second_kb)),
             (own_headers, path.replace(item['document_id'], str(uuid4()))),
             (own_headers, path.replace(item['document_version_id'], str(uuid4())))]
    for headers, wrong_path in cases:
        for method in (client.get, client.post):
            response = method(wrong_path, headers=headers)
            assert response.status_code == 404
            assert response.json() == {'detail': 'Document resource not found'}
    assert executor.calls == 0 and not list(storage.root.rglob('pages.json'))


def test_trigger_body_cannot_supply_path_or_options(stage_context):
    client, _, _, executor, accounts, _, path = stage_context
    response = client.post(path, headers=accounts[0][0], json={'path': 'private-marker-path', 'owner_id': str(uuid4())})
    assert response.status_code == 422 and 'private-marker-path' not in response.text
    assert executor.calls == 0


def test_running_returns_409_without_starting_another_attempt(stage_context):
    client, session, _, executor, accounts, item, path = stage_context
    stage = DocumentParseStage(document_version_id=UUID(item['document_version_id']), attempt_id=uuid4(), status='running')
    session.add(stage)
    session.commit()
    assert client.get(path, headers=accounts[0][0]).json()['parse_status'] == 'running'
    assert client.post(path, headers=accounts[0][0]).status_code == 409
    assert executor.calls == 0


def test_artifact_write_failure_does_not_touch_original(stage_context, monkeypatch):
    client, session, storage, _, accounts, item, path = stage_context
    original = storage.path_for(UUID(item['knowledge_base_id']), UUID(item['document_id']), UUID(item['document_version_id']))
    before = original.read_bytes()
    def fail_publish(self, *args):
        raise OSError('sensitive-path-sentinel')
    monkeypatch.setattr(ParseArtifactStore, 'publish', fail_publish)
    response = client.post(path, headers=accounts[0][0])
    assert response.json()['parse_status'] == 'failed'
    assert response.json()['error_code'] == 'artifact_write_failed'
    assert 'sensitive-path-sentinel' not in response.text
    assert original.read_bytes() == before
    assert not list(storage.root.rglob('pages.part'))
    assert session.get(DocumentVersion, UUID(item['document_version_id'])).status == 'pending'


def test_missing_or_corrupt_artifact_is_not_reported_success(stage_context):
    client, _, storage, _, accounts, _, path = stage_context
    assert client.post(path, headers=accounts[0][0]).json()['parse_status'] == 'succeeded'
    artifact = next(storage.root.rglob('pages.json'))
    artifact.write_bytes(b'{incomplete')
    queried = client.get(path, headers=accounts[0][0])
    assert queried.json()['parse_status'] == 'failed'
    assert queried.json()['error_code'] == 'artifact_unavailable'


def test_other_version_artifacts_are_independent(stage_context):
    client, _, storage, _, accounts, _, path = stage_context
    headers, kb = accounts[0]
    first = client.post(path, headers=headers).json()
    uploaded = client.post(f'/knowledge-bases/{kb}/documents', headers=headers,
                           files={'file': ('public.pdf', public_pdf(('Other version text',)), 'application/pdf')}).json()
    other_path = f"/knowledge-bases/{kb}/documents/{uploaded['document_id']}/versions/{uploaded['document_version_id']}/parse"
    second = client.post(other_path, headers=headers).json()
    assert first['attempt_id'] != second['attempt_id']
    assert first['page_count'] == 3 and second['page_count'] == 1
    assert len(list(storage.root.rglob('pages.json'))) == 2
    assert client.get(path, headers=headers).json() == first


def test_acknowledgement_loss_after_success_preserves_committed_artifact(stage_context, monkeypatch):
    client, session, storage, _, accounts, _, path = stage_context
    actual_commit = session.commit
    commits = 0
    def commit_with_lost_ack():
        nonlocal commits
        commits += 1
        actual_commit()
        if commits == 2:
            raise RuntimeError('completion acknowledgement lost')
    monkeypatch.setattr(session, 'commit', commit_with_lost_ack)
    assert client.post(path, headers=accounts[0][0]).status_code == 503
    assert len(list(storage.root.rglob('pages.json'))) == 1
    queried = client.get(path, headers=accounts[0][0])
    assert queried.json()['parse_status'] == 'succeeded'


def test_unconfirmed_completion_does_not_report_success(stage_context, monkeypatch):
    client, session, storage, _, accounts, _, path = stage_context
    actual_commit = session.commit
    commits = 0
    def fail_completion():
        nonlocal commits
        commits += 1
        if commits == 2:
            raise RuntimeError('commit not acknowledged')
        actual_commit()
    monkeypatch.setattr(session, 'commit', fail_completion)
    assert client.post(path, headers=accounts[0][0]).status_code == 503
    assert client.get(path, headers=accounts[0][0]).json()['parse_status'] == 'running'
    assert len(list(storage.root.rglob('pages.json'))) == 1
    assert client.post(path, headers=accounts[0][0]).status_code == 409


def test_uncertain_worker_shutdown_keeps_claim_and_files(stage_context):
    from project.core.parse_executor import ParseExecutionUncertain
    client, _, storage, executor, accounts, _, path = stage_context
    def cannot_stop(*args):
        raise ParseExecutionUncertain
    executor.run = cannot_stop
    assert client.post(path, headers=accounts[0][0]).status_code == 503
    assert client.get(path, headers=accounts[0][0]).json()['parse_status'] == 'running'
    assert list(storage.root.rglob('parsing'))


def test_real_missing_original_is_a_persisted_failure(stage_context):
    client, _, storage, _, accounts, item, path = stage_context
    storage.path_for(UUID(item['knowledge_base_id']), UUID(item['document_id']), UUID(item['document_version_id'])).unlink()
    response = client.post(path, headers=accounts[0][0])
    assert response.json()['parse_status'] == 'failed'
    assert response.json()['error_code'] == 'source_missing'
