from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from project.core.chunk_artifacts import ChunkArtifactStore
from project.core.document_storage import DocumentStorage
from project.core.version_chunker import ChunkingError, canonical_json, chunk_parsed_version
from test_version_chunker import chunk_context


@pytest.fixture
def artifact_context(chunk_context, tmp_path):
    binding, config, parsed = chunk_context
    result = chunk_parsed_version(parsed(["Public persisted page", "", "Other public page"]), binding, config)
    return ChunkArtifactStore(DocumentStorage(tmp_path)), result


def test_atomic_publish_read_restart_and_cached_repeat(artifact_context):
    store, result = artifact_context
    assert store.publish(result) == result
    path = store.path_for(result.binding, result.config)
    before = path.read_bytes(), path.stat().st_mtime_ns
    assert ChunkArtifactStore(DocumentStorage(store.storage.root)).read(result.binding, result.config) == result
    assert store.publish(result) == result
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert not list(store.storage.root.rglob("*.part"))


def test_parse_attempt_and_config_results_use_separate_paths(artifact_context):
    store, result = artifact_context
    first_path = store.path_for(result.binding, result.config)
    assert first_path != store.path_for(replace(result.binding, attempt_id=uuid4()), result.config)
    assert first_path != store.path_for(result.binding, replace(result.config, child_chunk_overlap=16))
    assert "public.pdf" not in str(first_path.relative_to(store.storage.root))


def test_incomplete_or_corrupt_cache_is_rejected_and_preserved(artifact_context):
    store, result = artifact_context
    store.publish(result)
    path = store.path_for(result.binding, result.config)
    path.write_bytes(b"{partial")
    for operation in (lambda: store.read(result.binding, result.config), lambda: store.publish(result)):
        with pytest.raises(ChunkingError) as error:
            operation()
        assert error.value.code == "chunk_artifact_invalid"
    assert path.read_bytes() == b"{partial"
    assert not list(store.storage.root.rglob("*.part"))


@pytest.mark.parametrize("fault", ["checksum", "source", "parent", "page", "order"])
def test_integrity_and_metadata_validation(artifact_context, fault):
    store, result = artifact_context
    store.publish(result)
    path = store.path_for(result.binding, result.config)
    envelope = json.loads(path.read_bytes())
    payload = envelope["payload"]
    if fault == "checksum":
        payload["children"][0]["text"] = "corrupted content"
    else:
        if fault == "source":
            payload["children"][0]["document_version_id"] = str(uuid4())
        if fault == "parent":
            payload["children"][0]["parent_id"] = str(uuid4())
        if fault == "page":
            payload["children"][0]["page_number"] = 2
        if fault == "order":
            payload["children"][0]["order"] = True
        envelope["sha256"] = hashlib.sha256(canonical_json(payload)).hexdigest()
    path.write_bytes(canonical_json(envelope))
    with pytest.raises(ChunkingError) as error:
        store.read(result.binding, result.config)
    assert error.value.code == "chunk_artifact_invalid"


def test_write_failure_publishes_no_partial_and_keeps_other_result(artifact_context, monkeypatch):
    import os
    store, result = artifact_context
    store.publish(result)
    original = store.path_for(result.binding, result.config).read_bytes()
    second = replace(result, config=replace(result.config, child_chunk_overlap=16))
    # Regenerate IDs for the alternate configuration rather than forging a result.
    from project.core.document_parser import ParsedPage, ParsedPdf
    source = second.binding.source
    pages = tuple(ParsedPage(**vars_source(source), page_number=i, page_text=text)
                  for i, text in enumerate(["Public persisted page", "", "Other public page"], 1))
    second = chunk_parsed_version(ParsedPdf(source, pages), second.binding, second.config)
    def fail_link(*args):
        raise OSError("private-path-sentinel")
    monkeypatch.setattr(os, "link", fail_link)
    with pytest.raises(ChunkingError) as error:
        store.publish(second)
    assert error.value.code == "chunk_artifact_write_failed" and "private-path" not in str(error.value)
    assert not store.path_for(second.binding, second.config).exists()
    assert store.path_for(result.binding, result.config).read_bytes() == original
    assert not list(store.storage.root.rglob("*.part"))


def vars_source(source):
    from dataclasses import asdict
    return asdict(source)


def test_concurrent_identical_publish_has_one_complete_winner(artifact_context):
    store, result = artifact_context
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.publish(result), range(4)))
    assert all(item == result for item in results)
    assert len(list(store.storage.root.rglob("chunks.json"))) == 1
    assert not list(store.storage.root.rglob("*.part"))


def test_linked_chunk_location_rejected(artifact_context, monkeypatch):
    store, result = artifact_context
    path = store.path_for(result.binding, result.config)
    real_junction = getattr(Path, "is_junction", lambda self: False)
    monkeypatch.setattr(Path, "is_junction", lambda self: self == path.parent or real_junction(self), raising=False)
    with pytest.raises(ChunkingError) as error:
        store.publish(result)
    assert error.value.code == "chunk_artifact_unavailable"


def _publish_actor(root, result, ready, output):
    try:
        if not ready.wait(timeout=30):
            raise RuntimeError("test gate timeout")
        saved = ChunkArtifactStore(DocumentStorage(Path(root))).publish(result)
        output.put(("ok", str(saved.children[0].chunk_id)))
    except Exception as error:
        output.put(("failure", type(error).__name__))


def test_two_real_processes_publish_one_identical_artifact(artifact_context):
    import multiprocessing
    store, result = artifact_context
    context = multiprocessing.get_context("spawn")
    ready, output = context.Event(), context.Queue()
    processes = [context.Process(target=_publish_actor, args=(str(store.storage.root), result, ready, output)) for _ in range(2)]
    try:
        for process in processes:
            process.start()
        ready.set()
        outcomes = [output.get(timeout=30) for _ in processes]
        assert outcomes == [("ok", str(result.children[0].chunk_id))] * 2
        for process in processes:
            process.join(timeout=30)
            assert process.exitcode == 0 and not process.is_alive()
        assert store.read(result.binding, result.config) == result
        assert len(list(store.storage.root.rglob("chunks.json"))) == 1
        assert not list(store.storage.root.rglob("*.part"))
    finally:
        for process in processes:
            if process.pid is not None:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
                if not process.is_alive():
                    process.close()
        output.close()
        output.join_thread()


def test_existing_foreign_temporary_file_is_never_deleted(artifact_context, monkeypatch):
    from project.core import chunk_artifacts as module
    store, result = artifact_context
    identity = uuid4()
    path = store.path_for(result.binding, result.config)
    path.parent.mkdir(parents=True)
    foreign = path.with_name(f".chunks-{identity.hex}.part")
    foreign.write_bytes(b"other request owned file")
    monkeypatch.setattr(module, "uuid4", lambda: identity)
    with pytest.raises(ChunkingError) as error:
        store.publish(result)
    assert error.value.code == "chunk_artifact_write_failed"
    assert foreign.read_bytes() == b"other request owned file"
    assert not path.exists()
