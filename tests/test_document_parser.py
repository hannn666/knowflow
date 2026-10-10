from dataclasses import FrozenInstanceError
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pymupdf
import pytest

from project.core import document_parser as module
from project.core.document_parser import DocumentVersionSource, PdfParsingError, parse_pdf_version
from project.core.document_storage import DocumentStorage


def make_pdf(texts, *, encrypted=False, image_only=False, labels=False):
    with pymupdf.open() as pdf:
        for text in texts:
            page = pdf.new_page()
            if text:
                page.insert_text((72, 72), text)
            if image_only:
                # A raster image without a text layer; no OCR should be invoked.
                pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 20, 20), False)
                pixmap.clear_with(255)
                page.insert_image(pymupdf.Rect(72, 72, 92, 92), stream=pixmap.tobytes('png'))
        if labels:
            pdf.set_page_labels([{'startpage': 0, 'prefix': '', 'style': 'r', 'firstpagenum': 1}])
        kwargs = {'encryption': pymupdf.PDF_ENCRYPT_AES_256, 'user_pw': 'test-only-viewer', 'owner_pw': 'test-only-owner'} if encrypted else {}
        return pdf.tobytes(**kwargs)


@pytest.fixture
def source():
    return DocumentVersionSource(uuid4(), uuid4(), uuid4(), 'manual.pdf')


def save_pdf(storage, source, payload):
    return storage.save(BytesIO(payload), source.knowledge_base_id, source.document_id, source.document_version_id)


def test_single_page_text_and_source(tmp_path, source):
    storage = DocumentStorage(tmp_path)
    save_pdf(storage, source, make_pdf(['Public first page']))
    result = parse_pdf_version(source, storage)
    assert result.page_count == 1 and result.has_text
    page = result.pages[0]
    assert page.page_number == 1
    assert page.page_text.strip() == 'Public first page'
    assert (page.knowledge_base_id, page.document_id, page.document_version_id, page.original_filename) == (
        source.knowledge_base_id, source.document_id, source.document_version_id, source.original_filename)
    assert 'Public first page' not in repr(page) + repr(result)
    with pytest.raises(FrozenInstanceError):
        page.page_text = 'changed'


def test_physical_pages_keep_blank_positions_and_ignore_printed_labels(tmp_path, source):
    storage = DocumentStorage(tmp_path)
    save_pdf(storage, source, make_pdf(['First page', '', 'Third page'], labels=True))
    result = parse_pdf_version(source, storage)
    assert result.page_count == 3
    assert [page.page_number for page in result.pages] == [1, 2, 3]
    assert [page.page_text.strip() for page in result.pages] == ['First page', '', 'Third page']
    assert result.has_text


@pytest.mark.parametrize('image_only', [False, True])
def test_no_text_pdf_keeps_pages_without_ocr(tmp_path, source, image_only, monkeypatch):
    storage = DocumentStorage(tmp_path)
    save_pdf(storage, source, make_pdf(['', ''], image_only=image_only))
    def forbid_ocr(*args, **kwargs):
        pytest.fail('OCR must not run')
    monkeypatch.setattr(pymupdf.Page, 'get_textpage_ocr', forbid_ocr)
    result = parse_pdf_version(source, storage)
    assert result.page_count == 2 and not result.has_text
    assert [page.page_text for page in result.pages] == ['', '']
    assert [page.page_number for page in result.pages] == [1, 2]


def test_same_name_in_different_documents_and_versions_never_mixes(tmp_path, source):
    storage = DocumentStorage(tmp_path)
    others = [
        DocumentVersionSource(source.knowledge_base_id, uuid4(), uuid4(), source.original_filename),
        DocumentVersionSource(source.knowledge_base_id, source.document_id, uuid4(), source.original_filename),
        DocumentVersionSource(uuid4(), uuid4(), uuid4(), source.original_filename),
    ]
    sources = [source, *others]
    for index, item in enumerate(sources):
        save_pdf(storage, item, make_pdf([f'Source number {index}']))
    for index, item in enumerate(sources):
        result = parse_pdf_version(item, storage)
        assert result.source == item
        assert result.pages[0].page_text.strip() == f'Source number {index}'
        assert result.pages[0].document_version_id == item.document_version_id


def test_repeated_parse_preserves_files_and_has_no_artifacts(tmp_path, source):
    storage = DocumentStorage(tmp_path)
    original = save_pdf(storage, source, make_pdf(['Repeat safely']))
    before = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()}
    first = parse_pdf_version(source, storage)
    second = parse_pdf_version(source, storage)
    after = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()}
    assert first == second
    assert before == after and list(after) == [original.relative_to(tmp_path)]


@pytest.mark.parametrize('payload', [b'%PDF-1.7\nnot a valid PDF', b''])
def test_invalid_pdf_error_has_no_path_or_contents(tmp_path, source, payload, caplog):
    storage = DocumentStorage(tmp_path)
    path = storage.path_for(source.knowledge_base_id, source.document_id, source.document_version_id)
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)
    with pytest.raises(PdfParsingError) as error:
        parse_pdf_version(source, storage)
    assert error.value.code == 'invalid_pdf'
    assert str(tmp_path) not in str(error.value) + caplog.text
    assert 'not a valid PDF' not in str(error.value) + caplog.text


def test_missing_file_error_is_explicit(tmp_path, source):
    with pytest.raises(PdfParsingError) as error:
        parse_pdf_version(source, DocumentStorage(tmp_path))
    assert error.value.code == 'source_missing'


def test_unreadable_file_error_is_explicit(tmp_path, source, monkeypatch):
    storage = DocumentStorage(tmp_path)
    path = save_pdf(storage, source, make_pdf(['Public text']))
    actual_open = Path.open
    def deny_open(self, *args, **kwargs):
        if self == path:
            raise PermissionError('private filesystem details')
        return actual_open(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', deny_open)
    with pytest.raises(PdfParsingError) as error:
        parse_pdf_version(source, storage)
    assert error.value.code == 'source_unreadable'
    assert 'private filesystem details' not in str(error.value)


def test_password_protected_pdf_is_rejected(tmp_path, source):
    storage = DocumentStorage(tmp_path)
    save_pdf(storage, source, make_pdf(['Protected test text'], encrypted=True))
    with pytest.raises(PdfParsingError) as error:
        parse_pdf_version(source, storage)
    assert error.value.code == 'password_required'


def test_oversized_source_is_rejected_before_native_parser(tmp_path, source, monkeypatch):
    storage = DocumentStorage(tmp_path)
    path = save_pdf(storage, source, make_pdf(['Public text']))
    monkeypatch.setattr(module, 'MAX_FILE_BYTES', path.stat().st_size - 1)
    def forbid_open(*args, **kwargs):
        pytest.fail('Native parser must not open oversized input')
    monkeypatch.setattr(module.pymupdf, 'open', forbid_open)
    with pytest.raises(PdfParsingError) as error:
        parse_pdf_version(source, storage)
    assert error.value.code == 'source_too_large'


def test_linked_source_is_rejected_before_reading(tmp_path, source, monkeypatch):
    storage = DocumentStorage(tmp_path)
    path = save_pdf(storage, source, make_pdf(['Public text']))
    actual_is_symlink = Path.is_symlink
    monkeypatch.setattr(Path, 'is_symlink', lambda self: self == path or actual_is_symlink(self))
    with pytest.raises(PdfParsingError) as error:
        parse_pdf_version(source, storage)
    assert error.value.code == 'unsafe_source'


def test_page_failure_is_safe_closes_pdf_and_returns_no_partial_result(tmp_path, source, monkeypatch, caplog):
    storage = DocumentStorage(tmp_path)
    save_pdf(storage, source, make_pdf(['Public first page', 'Private test sentinel']))
    actual_open = pymupdf.open
    actual_get_text = pymupdf.Page.get_text
    opened = []
    def record_open(*args, **kwargs):
        pdf = actual_open(*args, **kwargs)
        opened.append(pdf)
        return pdf
    def fail_second_page(self, *args, **kwargs):
        if self.number == 1:
            raise RuntimeError('Private test sentinel')
        return actual_get_text(self, *args, **kwargs)
    monkeypatch.setattr(pymupdf, 'open', record_open)
    monkeypatch.setattr(pymupdf.Page, 'get_text', fail_second_page)
    with pytest.raises(PdfParsingError) as error:
        parse_pdf_version(source, storage)
    assert error.value.code == 'text_extraction_failed'
    assert error.value.page_number == 2
    assert 'Private test sentinel' not in str(error.value) + caplog.text
    assert opened and all(pdf.is_closed for pdf in opened)


def test_junction_source_directory_is_rejected(tmp_path, source, monkeypatch):
    storage = DocumentStorage(tmp_path)
    path = save_pdf(storage, source, make_pdf(['Public text']))
    monkeypatch.setattr(Path, 'is_junction', lambda self: self == path.parent.parent, raising=False)
    with pytest.raises(PdfParsingError) as error:
        parse_pdf_version(source, storage)
    assert error.value.code == 'unsafe_source'


def test_chinese_text_is_extracted_from_real_text_layer(tmp_path, source):
    storage = DocumentStorage(tmp_path)
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), '公开测试：第一页面内容', fontname='china-s')
        payload = pdf.tobytes()
    save_pdf(storage, source, payload)
    result = parse_pdf_version(source, storage)
    assert ''.join(result.pages[0].page_text.split()) == '公开测试：第一页面内容'
    assert result.pages[0].page_number == 1


def test_damaged_pdf_that_native_library_repairs_is_not_silently_accepted(tmp_path, source):
    storage = DocumentStorage(tmp_path)
    original = make_pdf(['Public damaged PDF test'])
    damaged = original[:original.rfind(b'startxref')]
    path = save_pdf(storage, source, damaged)
    with pymupdf.open(stream=damaged, filetype='pdf') as pdf:
        assert pdf.is_repaired
    with pytest.raises(PdfParsingError) as error:
        parse_pdf_version(source, storage)
    assert error.value.code == 'invalid_pdf'
    assert 'repair' in str(error.value)
    assert path.read_bytes() == damaged
