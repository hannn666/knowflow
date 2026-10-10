"""Read-only, page-preserving PDF extraction for trusted version metadata.

No OCR, Markdown artifacts, database writes, or indexing. The internal service
must obtain Source from an authorized database lookup, never client metadata.
"""
from dataclasses import dataclass, field
from uuid import UUID

import pymupdf

from project.core.document_storage import DocumentStorage, MAX_FILE_BYTES


@dataclass(frozen=True, slots=True)
class DocumentVersionSource:
    knowledge_base_id: UUID
    document_id: UUID
    document_version_id: UUID
    original_filename: str


@dataclass(frozen=True, slots=True)
class ParsedPage(DocumentVersionSource):
    page_number: int
    page_text: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ParsedPdf:
    source: DocumentVersionSource
    pages: tuple[ParsedPage, ...] = field(repr=False)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def has_text(self) -> bool:
        return any(page.page_text.strip() for page in self.pages)


class PdfParsingError(Exception):
    """Safe error codes; never contain source paths or PDF contents."""
    def __init__(self, code: str, message: str, page_number: int | None = None):
        super().__init__(message)
        self.code = code
        self.page_number = page_number


def parse_pdf_version(source: DocumentVersionSource, storage: DocumentStorage) -> ParsedPdf:
    """Internal adapter: Source and the storage root must be server-controlled.

    Page numbers are 1-based physical PDF positions, not printed page labels.
    Empty/image-only pages remain in the result; has_text is false if none have
    extractable text. Extraction does not assert processing or retrieval readiness.
    """
    try:
        path = storage.path_for(source.knowledge_base_id, source.document_id, source.document_version_id)
        for component in (path, path.parent, path.parent.parent, path.parent.parent.parent):
            is_junction = getattr(component, "is_junction", lambda: False)
            if component.is_symlink() or is_junction():
                raise ValueError("Linked source location")
    except (ValueError, OSError):
        raise PdfParsingError("unsafe_source", "Invalid version source location") from None
    try:
        # A bounded snapshot avoids passing a filesystem path to the native
        # parser and cannot consume more than the accepted original-file limit.
        with path.open("rb") as original:
            payload = original.read(MAX_FILE_BYTES + 1)
    except FileNotFoundError:
        raise PdfParsingError("source_missing", "Version source file is missing") from None
    except OSError:
        raise PdfParsingError("source_unreadable", "Unable to read version source file") from None
    if len(payload) > MAX_FILE_BYTES:
        raise PdfParsingError("source_too_large", "Version source exceeds the PDF size limit")
    try:
        pdf = pymupdf.open(stream=payload, filetype="pdf")
    except Exception:
        raise PdfParsingError("invalid_pdf", "Unable to open PDF source") from None
    with pdf:
        if pdf.is_repaired:
            raise PdfParsingError("invalid_pdf", "PDF requiring repair is unsupported")
        if not pdf.is_pdf or pdf.page_count == 0:
            raise PdfParsingError("invalid_pdf", "Source is not a PDF with pages")
        if pdf.needs_pass:
            raise PdfParsingError("password_required", "Password-protected PDF is unsupported")
        pages = []
        for index in range(pdf.page_count):
            try:
                page_text = pdf.load_page(index).get_text("text", sort=True)
            except Exception:
                raise PdfParsingError("text_extraction_failed", "Unable to extract PDF page text", index + 1) from None
            pages.append(ParsedPage(
                knowledge_base_id=source.knowledge_base_id, document_id=source.document_id,
                document_version_id=source.document_version_id, original_filename=source.original_filename,
                page_number=index + 1, page_text=page_text,
            ))
    return ParsedPdf(source=source, pages=tuple(pages))
