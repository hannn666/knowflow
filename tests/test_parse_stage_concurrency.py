"""Real PostgreSQL/process concurrency test; never commits into business DB."""
from contextlib import contextmanager
from io import BytesIO
import multiprocessing
from pathlib import Path
from uuid import uuid4

import pymupdf
import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from project.api.parse_stage_service import ParseStageBusy, trigger_parse_stage
from project.core.document_parser import parse_pdf_version
from project.core.document_storage import DocumentStorage
from project.db.business_database import get_database_url
from project.db.business_models import Document, DocumentParseStage, DocumentVersion, KnowledgeBase, User


class ProcessGateExecutor:
    def __init__(self, started, release):
        self.started = started
        self.release = release

    @contextmanager
    def slot(self):
        yield

    def run(self, source, artifacts, reservation):
        self.started.set()
        if not self.release.wait(timeout=30):
            raise RuntimeError('test gate timeout')
        result = parse_pdf_version(source, artifacts.storage)
        return artifacts.publish(reservation, result)


def database_actor(url, ids, root, started, release, output):
    engine = create_engine(url, hide_parameters=True)
    try:
        with Session(engine) as session:
            try:
                result = trigger_parse_stage(session, *ids, DocumentStorage(Path(root)), ProcessGateExecutor(started, release))
                output.put(('succeeded', str(result.attempt_id)))
            except ParseStageBusy:
                output.put(('busy', None))
            except Exception as error:
                output.put(('unexpected', type(error).__name__))
    finally:
        engine.dispose()


def test_same_version_claim_is_exclusive_across_real_processes(tmp_path):
    import os
    if os.getenv('KNOWFLOW_TEST_POSTGRES') != '1':
        pytest.skip('Requires the isolated PostgreSQL verifier')
    url = get_database_url()
    if not url.database.startswith('knowflow_test_'):
        pytest.skip('This concurrency case only commits into a generated isolated test database')
    engine = create_engine(url, hide_parameters=True)
    ids = uuid4(), uuid4(), uuid4(), uuid4()
    storage = DocumentStorage(tmp_path)
    with Session(engine) as session:
        session.add(User(id=ids[0], email=f'concurrency-{uuid4().hex}@example.com', password_hash='test-only'))
        session.flush()
        session.add(KnowledgeBase(id=ids[1], owner_id=ids[0], name='Concurrent parse'))
        session.flush()
        session.add(Document(id=ids[2], knowledge_base_id=ids[1]))
        session.flush()
        session.add(DocumentVersion(id=ids[3], document_id=ids[2], original_filename='public.pdf'))
        session.commit()
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72,72), 'Real cross-process parse test')
        payload = pdf.tobytes()
    storage.save(BytesIO(payload), *ids[1:])
    context = multiprocessing.get_context('spawn')
    started, release = context.Event(), context.Event()
    output = context.Queue()
    processes = []
    try:
        first = context.Process(target=database_actor, args=(url, ids, str(tmp_path), started, release, output))
        processes.append(first)
        first.start()
        assert started.wait(timeout=20)
        with Session(engine) as session:
            assert session.get(DocumentParseStage, ids[3]).status == 'running'
        second = context.Process(target=database_actor, args=(url, ids, str(tmp_path), started, release, output))
        processes.append(second)
        second.start()
        assert output.get(timeout=20) == ('busy', None)
        release.set()
        outcome = output.get(timeout=20)
        assert outcome[0] == 'succeeded'
        for process in processes:
            process.join(timeout=20)
            assert not process.is_alive() and process.exitcode == 0
        with Session(engine) as session:
            record = session.get(DocumentParseStage, ids[3])
            assert record.status == 'succeeded' and str(record.attempt_id) == outcome[1]
            assert session.get(DocumentVersion, ids[3]).status == 'pending'
        assert len(list(tmp_path.rglob('pages.json'))) == 1
    finally:
        release.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
            if not process.is_alive():
                process.close()
        output.close()
        output.join_thread()
        with Session(engine) as session:
            session.execute(delete(DocumentParseStage).where(DocumentParseStage.document_version_id == ids[3]))
            session.execute(delete(DocumentVersion).where(DocumentVersion.id == ids[3]))
            session.execute(delete(Document).where(Document.id == ids[2]))
            session.execute(delete(KnowledgeBase).where(KnowledgeBase.id == ids[1], KnowledgeBase.owner_id == ids[0]))
            session.execute(delete(User).where(User.id == ids[0]))
            session.commit()
        engine.dispose()
