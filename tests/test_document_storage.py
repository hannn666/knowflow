from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pytest

from project.core import document_storage as module
from project.core.document_storage import CHUNK_BYTES, DocumentStorage, UploadRejected, get_document_storage


PDF = b"%PDF-1.7\nsmall test body"


def test_block_reads_and_exact_file_limit(tmp_path):
    class CountingStream(BytesIO):
        def read(self, size=-1):
            assert size == CHUNK_BYTES
            return super().read(size)
    storage = DocumentStorage(tmp_path)
    content = PDF + b"x" * (module.MAX_FILE_BYTES - len(PDF))
    path = storage.save(CountingStream(content), uuid4(), uuid4(), uuid4())
    assert path.read_bytes() == content
    assert not list(tmp_path.rglob("*.part"))


@pytest.mark.parametrize("content,code", [(b"", 422), (b"not PDF", 415)])
def test_rejected_content_is_cleaned(tmp_path, content, code):
    with pytest.raises(UploadRejected) as error:
        DocumentStorage(tmp_path).save(BytesIO(content), uuid4(), uuid4(), uuid4())
    assert error.value.status_code == code
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]


def test_file_above_limit_is_cleaned(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "MAX_FILE_BYTES", len(PDF))
    with pytest.raises(UploadRejected) as error:
        DocumentStorage(tmp_path).save(BytesIO(PDF + b"x"), uuid4(), uuid4(), uuid4())
    assert error.value.status_code == 413
    assert not list(tmp_path.rglob("*.pdf"))
    assert not list(tmp_path.rglob("*.part"))


def test_collision_preserves_other_request(tmp_path):
    storage = DocumentStorage(tmp_path)
    ids = uuid4(), uuid4(), uuid4()
    original = storage.save(BytesIO(PDF), *ids)
    with pytest.raises(FileExistsError):
        storage.save(BytesIO(PDF + b"new"), *ids)
    assert original.read_bytes() == PDF


def test_read_failure_closes_and_removes_partial_file(tmp_path):
    class BrokenStream(BytesIO):
        def read(self, size):
            if self.tell():
                raise OSError("test-only read error")
            return super().read(size)
    with pytest.raises(OSError):
        DocumentStorage(tmp_path).save(BrokenStream(PDF), uuid4(), uuid4(), uuid4())
    assert not list(tmp_path.rglob("*.part"))
    assert not list(tmp_path.rglob("*.pdf"))


def test_storage_root_is_stable_and_can_be_isolated(tmp_path, monkeypatch):
    monkeypatch.delenv("KNOWFLOW_UPLOAD_ROOT", raising=False)
    expected = Path(__file__).resolve().parents[1] / "uploads"
    monkeypatch.chdir(tmp_path)
    assert get_document_storage().root == expected
    monkeypatch.setenv("KNOWFLOW_UPLOAD_ROOT", str(tmp_path))
    assert get_document_storage().root == tmp_path
    monkeypatch.setenv("KNOWFLOW_UPLOAD_ROOT", "relative-directory")
    with pytest.raises(ValueError):
        get_document_storage()


def test_concurrent_storage_saves_never_overwrite(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    storage = DocumentStorage(tmp_path)
    knowledge_base_id = uuid4()
    def save_one(index):
        payload = PDF + str(index).encode()
        path = storage.save(BytesIO(payload), knowledge_base_id, uuid4(), uuid4())
        return path, payload
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(save_one, range(4)))
    assert len({path for path, _ in results}) == 4
    assert all(path.read_bytes() == payload for path, payload in results)
