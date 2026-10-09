"""Local original-file storage. Filenames are never used as paths."""
import logging
import os
from pathlib import Path
from typing import BinaryIO
from uuid import UUID

MAX_FILE_BYTES = 10 * 1024 * 1024


CHUNK_BYTES = 64 * 1024
_DEFAULT_ROOT = Path(__file__).resolve().parents[2] / "uploads"
logger = logging.getLogger(__name__)


class UploadRejected(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def validate_filename(filename: str | None, content_type: str | None) -> str:
    if not filename or not filename.strip() or len(filename) > 255:
        raise UploadRejected(422, "Filename must contain 1 to 255 characters")
    if any(ord(char) < 32 or ord(char) == 127 or char in '<>:"/\\|?*' for char in filename):
        raise UploadRejected(422, "Invalid display filename")
    if not filename.lower().endswith(".pdf"):
        raise UploadRejected(415, "Only PDF uploads are accepted")
    if content_type not in {None, "application/pdf", "application/octet-stream"}:
        raise UploadRejected(415, "Only PDF uploads are accepted")
    return filename


class DocumentStorage:
    def __init__(self, root: Path):
        if not root.is_absolute():
            raise ValueError("Upload root must be absolute")
        self.root = root.resolve()

    def path_for(self, knowledge_base_id: UUID, document_id: UUID, version_id: UUID) -> Path:
        if not all(isinstance(value, UUID) for value in (knowledge_base_id, document_id, version_id)):
            raise ValueError("Storage identifiers must be UUIDs")
        path = self.root / str(knowledge_base_id) / str(document_id) / str(version_id) / "source.pdf"
        # The root is trusted configuration. Refuse symlinked child directories.
        for parent in (path.parent, path.parent.parent, path.parent.parent.parent):
            if parent.is_symlink():
                raise ValueError("Symlinked upload directory")
        if not path.resolve().is_relative_to(self.root):
            raise ValueError("Upload path escapes storage root")
        return path

    def save(self, stream: BinaryIO, knowledge_base_id: UUID, document_id: UUID, version_id: UUID) -> Path:
        final = self.path_for(knowledge_base_id, document_id, version_id)
        final.parent.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive reservation: a collision never overwrites or cleans another request.
        final.parent.mkdir(exist_ok=False)
        temporary = final.with_name("source.part")
        try:
            total = 0
            header = b""
            with temporary.open("xb") as output:
                while chunk := stream.read(CHUNK_BYTES):
                    total += len(chunk)
                    if total > MAX_FILE_BYTES:
                        raise UploadRejected(413, "PDF file too large")
                    header = (header + chunk[:5])[:5]
                    output.write(chunk)
            if total == 0:
                raise UploadRejected(422, "Empty file")
            if header != b"%PDF-":
                raise UploadRejected(415, "Invalid PDF header")
            temporary.replace(final)
            return final
        except BaseException:
            self.discard(final)
            raise

    def discard(self, final: Path) -> None:
        # Called only with a path successfully reserved by this request.
        if final.name != "source.pdf" or not final.resolve().is_relative_to(self.root):
            raise ValueError("Invalid cleanup target")
        try:
            final.with_name("source.part").unlink(missing_ok=True)
            final.unlink(missing_ok=True)
            final.parent.rmdir()
        except OSError:
            logger.warning("Could not clean this upload's files; manual reconciliation required")


def get_document_storage() -> DocumentStorage:
    configured = os.getenv("KNOWFLOW_UPLOAD_ROOT")
    return DocumentStorage(Path(configured) if configured else _DEFAULT_ROOT)
