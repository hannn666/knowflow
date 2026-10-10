from io import BytesIO
import multiprocessing
import os
import time
from uuid import uuid4

import pymupdf
import pytest

from project.core.document_parser import DocumentVersionSource
from project.core.document_storage import DocumentStorage
from project.core.parse_artifacts import ParseArtifactStore
from project.core.parse_executor import BoundedParseExecutor, ParseCapacityUnavailable, ParseExecutionError


def slow_test_worker(source, root, reservation, sender):
    time.sleep(10)


def crashing_test_worker(source, root, reservation, sender):
    os._exit(3)


@pytest.fixture
def executor_context(tmp_path):
    source = DocumentVersionSource(uuid4(), uuid4(), uuid4(), 'public.pdf')
    storage = DocumentStorage(tmp_path)
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72,72), 'Real child-process text')
        payload = pdf.tobytes()
    storage.save(BytesIO(payload), source.knowledge_base_id, source.document_id, source.document_version_id)
    store = ParseArtifactStore(storage)
    reservation = store.reserve(source, uuid4())
    return source, store, reservation


def test_real_spawned_parser_process_publishes_complete_result(executor_context):
    source, store, reservation = executor_context
    executor = BoundedParseExecutor(timeout_seconds=15)
    with executor.slot():
        digest = executor.run(source, store, reservation)
    assert store.read(source, reservation.attempt_id, digest).pages[0].page_text.strip() == 'Real child-process text'


def test_timeout_terminates_only_own_child_and_releases_slot(executor_context):
    source, store, reservation = executor_context
    executor = BoundedParseExecutor(timeout_seconds=0.2, max_processes=1, worker_target=slow_test_worker)
    before = {child.pid for child in multiprocessing.active_children()}
    with pytest.raises(ParseExecutionError) as error:
        with executor.slot():
            executor.run(source, store, reservation)
    assert error.value.code == 'parse_timeout'
    assert {child.pid for child in multiprocessing.active_children()} == before
    with executor.slot():
        pass
    assert not list(store.storage.root.rglob('pages.json'))


def test_real_worker_crash_is_failure(executor_context):
    source, store, reservation = executor_context
    executor = BoundedParseExecutor(timeout_seconds=15, worker_target=crashing_test_worker)
    with pytest.raises(ParseExecutionError) as error:
        with executor.slot():
            executor.run(source, store, reservation)
    assert error.value.code == 'worker_failure'


def test_capacity_limit_is_explicit_not_a_queue():
    executor = BoundedParseExecutor(max_processes=1)
    with executor.slot():
        with pytest.raises(ParseCapacityUnavailable):
            with executor.slot():
                pytest.fail('No second slot available')


def test_page_and_text_budgets_are_enforced(executor_context):
    from project.core.document_parser import PdfParsingError, parse_pdf_version
    source, store, _ = executor_context
    with pytest.raises(PdfParsingError) as error:
        parse_pdf_version(source, store.storage, max_text_characters=1)
    assert error.value.code == 'resource_limit'
    with pytest.raises(PdfParsingError) as error:
        parse_pdf_version(source, store.storage, max_pages=0)
    assert error.value.code == 'resource_limit'
