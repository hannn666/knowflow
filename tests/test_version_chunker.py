from dataclasses import replace
from uuid import uuid4

import pytest

from project.core.document_parser import DocumentVersionSource, ParsedPage, ParsedPdf
from project.core.version_chunker import ChunkingConfig, ChunkingError, ParseBinding, chunk_parsed_version


@pytest.fixture
def chunk_context():
    source = DocumentVersionSource(uuid4(), uuid4(), uuid4(), "public.pdf")
    binding = ParseBinding(source, uuid4(), "a" * 64)
    config = ChunkingConfig(min_parent_size=128, max_parent_size=256, child_chunk_size=128, child_chunk_overlap=32)
    def parsed(texts):
        return ParsedPdf(source, tuple(ParsedPage(source.knowledge_base_id, source.document_id,
                         source.document_version_id, source.original_filename, index, text)
                         for index, text in enumerate(texts, 1)))
    return binding, config, parsed


def test_single_page_short_text_keeps_exact_source_and_parent_relation(chunk_context):
    binding, config, parsed = chunk_context
    result = chunk_parsed_version(parsed(["Public short page"]), binding, config)
    assert len(result.parents) == len(result.children) == 1
    parent, child = result.parents[0], result.children[0]
    assert parent.text == child.text == "Public short page"
    assert parent.page_number == child.page_number == parent.order == child.order == 1
    assert child.parent_id == parent.chunk_id and parent.parent_id is None
    for item in (parent, child):
        assert (item.knowledge_base_id, item.document_id, item.document_version_id, item.original_filename) == (
            binding.source.knowledge_base_id, binding.source.document_id, binding.source.document_version_id, "public.pdf")
    assert "Public short page" not in repr(result) + repr(parent) + repr(child)


def test_multiple_pages_preserve_blanks_and_never_merge_short_pages(chunk_context):
    binding, config, parsed = chunk_context
    result = chunk_parsed_version(parsed(["第一页公开内容", "  \n\t", "Third public page"]), binding, config)
    assert result.page_count == 3 and result.blank_page_numbers == (2,)
    assert [item.page_number for item in result.parents] == [1, 3]
    assert [item.page_number for item in result.children] == [1, 3]
    assert [item.text for item in result.parents] == ["第一页公开内容", "Third public page"]
    assert [item.order for item in result.parents] == [1, 2]


def test_long_pages_split_with_bounded_sizes_and_overlap(chunk_context):
    binding, config, parsed = chunk_context
    text = "ABCDEFGHIJKLMNOPQRSTUVWXYZ" * 100
    result = chunk_parsed_version(parsed([text, "汉字测试" * 500]), binding, config)
    assert len(result.parents) > 2 and len(result.children) > len(result.parents)
    assert all(len(item.text) <= config.max_parent_size for item in result.parents)
    assert all(len(item.text) <= config.child_chunk_size for item in result.children)
    first_parent_children = [item for item in result.children if item.parent_id == result.parents[0].chunk_id]
    assert len(first_parent_children) > 1
    assert first_parent_children[0].text[-32:] == first_parent_children[1].text[:32]
    for child in result.children:
        parent = next(item for item in result.parents if item.chunk_id == child.parent_id)
        assert child.page_number == parent.page_number and child.text in parent.text
    assert {item.page_number for item in result.parents} == {1, 2}


def test_all_blank_pages_have_valid_empty_result(chunk_context):
    binding, config, parsed = chunk_context
    result = chunk_parsed_version(parsed(["", " "]), binding, config)
    assert result.page_count == 2 and result.blank_page_numbers == (1, 2)
    assert result.parents == result.children == ()


def test_same_binding_and_config_have_stable_result_and_ids(chunk_context):
    binding, config, parsed = chunk_context
    pdf = parsed(["Public deterministic content " * 50])
    first = chunk_parsed_version(pdf, binding, config)
    assert chunk_parsed_version(pdf, binding, config) == first
    changed_config = replace(config, child_chunk_overlap=16)
    changed_attempt = replace(binding, attempt_id=uuid4())
    changed_digest = replace(binding, sha256="b" * 64)
    for other_binding, other_config in [(binding, changed_config), (changed_attempt, config), (changed_digest, config)]:
        other = chunk_parsed_version(pdf, other_binding, other_config)
        assert other_binding.fingerprint(other_config) != binding.fingerprint(config)
        assert {item.chunk_id for item in other.parents + other.children}.isdisjoint(
            item.chunk_id for item in first.parents + first.children)


@pytest.mark.parametrize("change", [dict(child_chunk_size=0), dict(child_chunk_overlap=128),
                                    dict(min_parent_size=300, max_parent_size=256), dict(child_chunk_size=True),
                                    dict(headers=[["#", "H1"]])])
def test_invalid_configuration_rejected(chunk_context, change):
    _, config, _ = chunk_context
    with pytest.raises(ChunkingError) as error:
        replace(config, **change)
    assert error.value.code == "invalid_chunking_config"


def test_output_budget_rejects_excessive_chunks(chunk_context, monkeypatch):
    from project.core import version_chunker as module
    binding, config, parsed = chunk_context
    monkeypatch.setattr(module, "MAX_CHUNKS", 1)
    with pytest.raises(ChunkingError) as error:
        chunk_parsed_version(parsed(["Public text"]), binding, config)
    assert error.value.code == "chunk_resource_limit"
