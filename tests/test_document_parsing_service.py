from io import BytesIO
from uuid import uuid4

import pymupdf
import pytest
from sqlalchemy import select

from project.api.document_parsing_service import parse_document_version
from project.api.document_service import DocumentNotFound
from project.core.document_storage import DocumentStorage
from project.db.business_models import Document, DocumentVersion, KnowledgeBase, User


def pdf_bytes(text):
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), text)
        return pdf.tobytes()


@pytest.fixture
def owned_version(db_session, tmp_path):
    user = User(email=f'parse-{uuid4().hex}@example.com', password_hash='test-only')
    db_session.add(user)
    db_session.flush()
    kb = KnowledgeBase(owner_id=user.id, name='Parser tests')
    document = Document(knowledge_base=kb)
    version = DocumentVersion(document=document, original_filename='shared-name.pdf')
    db_session.add(version)
    db_session.commit()
    storage = DocumentStorage(tmp_path)
    storage.save(BytesIO(pdf_bytes('Public version one')), kb.id, document.id, version.id)
    return user, kb, document, version, storage


def parse(session, context):
    user, kb, document, version, storage = context
    return parse_document_version(session, user.id, kb.id, document.id, version.id, storage)


def test_db_source_and_status_unchanged(owned_version, db_session, monkeypatch):
    user, kb, document, version, storage = owned_version
    ids = user.id, kb.id, document.id, version.id
    db_session.expunge_all()
    def forbid_write(*args, **kwargs):
        pytest.fail('Parsing must not commit or flush')
    monkeypatch.setattr(db_session, 'commit', forbid_write)
    monkeypatch.setattr(db_session, 'flush', forbid_write)
    result = parse_document_version(db_session, *ids, storage)
    assert result.pages[0].page_text.strip() == 'Public version one'
    assert result.source.original_filename == 'shared-name.pdf'
    assert (result.source.knowledge_base_id, result.source.document_id, result.source.document_version_id) == ids[1:]
    with db_session.no_autoflush:
        assert db_session.scalar(select(DocumentVersion.status).where(DocumentVersion.id == ids[3])) == 'pending'


@pytest.mark.parametrize('mismatch', ['owner', 'kb', 'document', 'version'])
def test_unauthorized_or_mismatched_source_never_reaches_storage(owned_version, db_session, monkeypatch, mismatch):
    user, kb, document, version, storage = owned_version
    ids = {'owner': user.id, 'kb': kb.id, 'document': document.id, 'version': version.id}
    ids[mismatch] = uuid4()
    def forbid_path(*args, **kwargs):
        pytest.fail('Unauthorized lookup must not access storage')
    monkeypatch.setattr(storage, 'path_for', forbid_path)
    with pytest.raises(DocumentNotFound):
        parse_document_version(db_session, ids['owner'], ids['kb'], ids['document'], ids['version'], storage)


def test_persisted_metadata_used_without_flushing_dirty_objects(owned_version, db_session, monkeypatch):
    user, kb, document, version, storage = owned_version
    owner_id, kb_id, document_id, version_id = user.id, kb.id, document.id, version.id
    version.original_filename = 'uncommitted-name.pdf'
    kb.name = 'uncommitted edit'
    def forbid_flush(*args, **kwargs):
        pytest.fail('Parsing must not flush unrelated changes')
    monkeypatch.setattr(db_session, 'flush', forbid_flush)
    result = parse_document_version(db_session, owner_id, kb_id, document_id, version_id, storage)
    assert result.source.original_filename == 'shared-name.pdf'
    assert version in db_session.dirty and kb in db_session.dirty


def test_different_versions_use_their_own_stored_files(owned_version, db_session):
    user, kb, document, first, storage = owned_version
    second = DocumentVersion(document=document, original_filename=first.original_filename)
    db_session.add(second)
    db_session.commit()
    storage.save(BytesIO(pdf_bytes('Public version two')), kb.id, document.id, second.id)
    first_result = parse(db_session, owned_version)
    second_result = parse_document_version(db_session, user.id, kb.id, document.id, second.id, storage)
    assert first_result.pages[0].page_text.strip() == 'Public version one'
    assert second_result.pages[0].page_text.strip() == 'Public version two'
    assert first_result.source.document_version_id != second_result.source.document_version_id
    assert first.status == second.status == 'pending'


def test_internal_parser_not_directly_exposed_or_coupled_to_rag():
    import ast
    from pathlib import Path
    from project.api.app import create_app
    root = Path(__file__).resolve().parents[1]
    for name in ['project/core/document_parser.py', 'project/api/document_parsing_service.py']:
        tree = ast.parse((root / name).read_text(encoding='utf-8-sig'))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            if isinstance(node, ast.ImportFrom):
                imports.append(node.module or '')
        assert not any('qdrant' in item or 'rag_agent' in item or 'document_manager' in item or 'utils' == item for item in imports)
    assert not any(getattr(route, 'endpoint', None) is parse_document_version for route in create_app().routes)


def test_existing_cross_user_and_cross_kb_records_denied(owned_version, db_session, monkeypatch):
    user, kb, document, version, storage = owned_version
    other_user = User(email=f'other-parse-{uuid4().hex}@example.com', password_hash='test-only')
    db_session.add(other_user)
    db_session.flush()
    other_kb = KnowledgeBase(owner_id=other_user.id, name='Other private KB')
    own_second_kb = KnowledgeBase(owner_id=user.id, name='Another own KB')
    other_document = Document(knowledge_base=other_kb)
    other_version = DocumentVersion(document=other_document, original_filename='shared-name.pdf')
    db_session.add_all([other_version, own_second_kb])
    db_session.commit()
    cases = [
        (user.id, other_kb.id, other_document.id, other_version.id),
        (other_user.id, kb.id, document.id, version.id),
        (user.id, own_second_kb.id, document.id, version.id),
    ]
    def forbid_path(*args, **kwargs):
        pytest.fail('Denied parsing must not touch any PDF')
    monkeypatch.setattr(storage, 'path_for', forbid_path)
    for ids in cases:
        with pytest.raises(DocumentNotFound):
            parse_document_version(db_session, *ids, storage)


@pytest.mark.parametrize('status', ['pending', 'processing', 'failed', 'ready'])
def test_parser_never_changes_processing_status(owned_version, db_session, status):
    user, kb, document, version, storage = owned_version
    version.status = status
    db_session.commit()
    version_id = version.id
    parse(db_session, owned_version)
    db_session.expire_all()
    assert db_session.get(DocumentVersion, version_id).status == status


def test_real_pdf_upload_then_internal_parse(client, db_session, tmp_path, monkeypatch):
    from project.api import auth_tokens
    from project.core.document_storage import get_document_storage
    from uuid import UUID
    monkeypatch.setattr(auth_tokens, 'load_dotenv', lambda *_: None)
    monkeypatch.setenv('KNOWFLOW_JWT_SECRET', 'ab' * 32)
    storage = DocumentStorage(tmp_path)
    client.app.dependency_overrides[get_document_storage] = lambda: storage
    credentials = {'email': f'parse-api-{uuid4().hex}@example.com', 'password': 'test-only PDF parsing integration'}
    registered = client.post('/auth/register', json=credentials)
    assert registered.status_code == 201
    login = client.post('/auth/login', json=credentials)
    assert login.status_code == 200
    headers = {'Authorization': f"Bearer {login.json()['access_token']}"}
    created = client.post('/knowledge-bases', headers=headers, json={'name': 'Real PDF parsing'})
    assert created.status_code == 201
    kb_id = UUID(created.json()['id'])
    payload = pdf_bytes('Public uploaded PDF content')
    uploaded = client.post(f'/knowledge-bases/{kb_id}/documents', headers=headers,
                           files={'file': ('public.pdf', payload, 'application/pdf')})
    assert uploaded.status_code == 201
    item = uploaded.json()
    parsed = parse_document_version(db_session, UUID(registered.json()['id']), kb_id,
                                    UUID(item['document_id']), UUID(item['document_version_id']), storage)
    assert parsed.pages[0].page_text.strip() == 'Public uploaded PDF content'
    assert parsed.source.original_filename == 'public.pdf'
    queried = client.get(f"/knowledge-bases/{kb_id}/documents/{item['document_id']}/versions/{item['document_version_id']}", headers=headers)
    assert queried.status_code == 200 and queried.json()['status'] == 'pending'
    assert storage.path_for(kb_id, UUID(item['document_id']), UUID(item['document_version_id'])).read_bytes() == payload
