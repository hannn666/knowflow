from pathlib import Path
import subprocess
import sys

from project.document_chunker import DocumentChunker


def test_existing_file_entry_retains_parent_child_contract(tmp_path):
    text = "# Public heading\n" + "Public paragraph content. " * 300 + "\n## Other heading\nShort public ending."
    path = tmp_path / "legacy.md"
    path.write_text(text, encoding="utf-8")
    chunker = DocumentChunker()
    file_result = chunker.create_chunks_single(path)
    memory_result = chunker.create_chunks_text(text, source_id="legacy", source_name="legacy.pdf")
    assert file_result == memory_result
    parents, children = file_result
    assert len(parents) > 1 and children
    assert [identity for identity, _ in parents] == [f"legacy_p{index}" for index in range(len(parents))]
    assert all(item.metadata["source"] == "legacy.pdf" for _, item in parents)
    assert {item.metadata["parent_id"] for item in children} == {identity for identity, _ in parents}
    assert all(len(item.page_content) <= 4000 for _, item in parents)


def test_legacy_top_level_import_used_by_gradio_still_works():
    project_path = Path(__file__).resolve().parents[1] / "project"
    script = "from document_chunker import DocumentChunker; p,c=DocumentChunker().create_chunks_text('Public compatibility test',source_id='legacy',source_name='legacy.pdf'); assert p[0][0]=='legacy_p0' and c[0].metadata['parent_id']=='legacy_p0'"
    result = subprocess.run([sys.executable, "-c", script], cwd=project_path, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
