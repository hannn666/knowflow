from dataclasses import replace
from uuid import uuid4

import pytest

from project.core.document_parser import DocumentVersionSource, ParsedPage, ParsedPdf
from project.core.document_storage import DocumentStorage
from project.core.parse_artifacts import ParseArtifactError, ParseArtifactStore


@pytest.fixture
def artifact_context(tmp_path):
    source = DocumentVersionSource(uuid4(), uuid4(), uuid4(), 'public.pdf')
    store = ParseArtifactStore(DocumentStorage(tmp_path))
    page = ParsedPage(source.knowledge_base_id, source.document_id, source.document_version_id,
                      source.original_filename, 1, 'Public text')
    return store, source, ParsedPdf(source, (page,))


def test_atomic_publish_and_restart_read(artifact_context):
    store, source, result = artifact_context
    reservation = store.reserve(source, uuid4())
    digest = store.publish(reservation, result)
    fresh = ParseArtifactStore(DocumentStorage(store.storage.root))
    assert fresh.read(source, reservation.attempt_id, digest) == result
    assert not list(store.storage.root.rglob('pages.part'))
    assert not list(store.storage.root.rglob('source.pdf'))


def test_attempt_collision_never_overwrites_or_discards(artifact_context):
    store, source, result = artifact_context
    reservation = store.reserve(source, uuid4())
    digest = store.publish(reservation, result)
    with pytest.raises(FileExistsError):
        store.reserve(source, reservation.attempt_id)
    with pytest.raises(ParseArtifactError):
        store.publish(reservation, result)
    assert store.read(source, reservation.attempt_id, digest) == result


@pytest.mark.parametrize('mismatch', ['version', 'page_number', 'boolean_page_number'])
def test_bad_source_or_page_metadata_rejected(artifact_context, mismatch):
    store, source, result = artifact_context
    page = result.pages[0]
    if mismatch == 'version':
        page = replace(page, document_version_id=uuid4())
    else:
        page = replace(page, page_number=3 if mismatch == 'page_number' else True)
    reservation = store.reserve(source, uuid4())
    with pytest.raises(ParseArtifactError) as error:
        store.publish(reservation, ParsedPdf(source, (page,)))
    assert error.value.code == 'artifact_invalid'
    assert not list(store.storage.root.rglob('pages.json'))


def test_rename_failure_leaves_no_published_partial_artifact(artifact_context, monkeypatch):
    from pathlib import Path
    store, source, result = artifact_context
    reservation = store.reserve(source, uuid4())
    def deny_replace(*args, **kwargs):
        raise OSError('test-only disk fault')
    monkeypatch.setattr(Path, 'replace', deny_replace)
    with pytest.raises(OSError):
        store.publish(reservation, result)
    assert not list(store.storage.root.rglob('pages.json'))
    assert list(store.storage.root.rglob('pages.part'))
    store.discard(reservation)
    assert not list(store.storage.root.rglob('pages.part'))


def test_digest_failure_is_detected(artifact_context):
    store, source, result = artifact_context
    reservation = store.reserve(source, uuid4())
    digest = store.publish(reservation, result)
    store.path_for(source, reservation.attempt_id).write_bytes(b'partial')
    with pytest.raises(ParseArtifactError) as error:
        store.read(source, reservation.attempt_id, digest)
    assert error.value.code == 'artifact_unavailable'


def test_cleanup_does_not_touch_other_attempt(artifact_context):
    store, source, result = artifact_context
    first = store.reserve(source, uuid4())
    digest = store.publish(first, result)
    second = store.reserve(source, uuid4())
    store.discard(second)
    assert store.read(source, first.attempt_id, digest) == result
