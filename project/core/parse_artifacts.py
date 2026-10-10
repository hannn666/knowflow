"""Version/attempt isolated parsing artifacts; stage truth remains in SQL."""
from dataclasses import asdict, dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
from uuid import UUID

from project.core.document_parser import DocumentVersionSource, ParsedPage, ParsedPdf
from project.core.document_storage import DocumentStorage


MAX_PARSED_PAGES = 1000
MAX_TEXT_CHARACTERS = 2_000_000
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
logger = logging.getLogger(__name__)


class ParseArtifactError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ArtifactReservation:
    source: DocumentVersionSource
    attempt_id: UUID


class ParseArtifactStore:
    def __init__(self, storage: DocumentStorage):
        self.storage = storage

    def path_for(self, source: DocumentVersionSource, attempt_id: UUID) -> Path:
        if not isinstance(attempt_id, UUID):
            raise ParseArtifactError('artifact_unavailable')
        original = self.storage.path_for(source.knowledge_base_id, source.document_id, source.document_version_id)
        path = original.parent / 'parsing' / str(attempt_id) / 'pages.json'
        for component in (path, *list(path.parents)[:5]):
            if component.is_symlink() or getattr(component, 'is_junction', lambda: False)():
                raise ParseArtifactError('artifact_unavailable')
        if not path.resolve().is_relative_to(self.storage.root):
            raise ParseArtifactError('artifact_unavailable')
        return path

    def reserve(self, source: DocumentVersionSource, attempt_id: UUID) -> ArtifactReservation:
        path = self.path_for(source, attempt_id)
        path.parent.parent.mkdir(parents=True, exist_ok=True)
        # Existing attempts are never overwritten or claimed for cleanup.
        path.parent.mkdir(exist_ok=False)
        return ArtifactReservation(source, attempt_id)

    @staticmethod
    def validate(result: ParsedPdf, expected: DocumentVersionSource) -> None:
        if result.source != expected or not 1 <= result.page_count <= MAX_PARSED_PAGES:
            raise ParseArtifactError('artifact_invalid')
        count = 0
        for index, page in enumerate(result.pages, 1):
            if (page.knowledge_base_id, page.document_id, page.document_version_id, page.original_filename) != (
                expected.knowledge_base_id, expected.document_id, expected.document_version_id, expected.original_filename
            ) or type(page.page_number) is not int or page.page_number != index or not isinstance(page.page_text, str):
                raise ParseArtifactError('artifact_invalid')
            count += len(page.page_text)
        if count > MAX_TEXT_CHARACTERS:
            raise ParseArtifactError('resource_limit')

    def publish(self, reservation: ArtifactReservation, result: ParsedPdf) -> str:
        self.validate(result, reservation.source)
        payload = {
            'schema_version': 1, 'source': asdict(result.source),
            'page_count': result.page_count, 'has_text': result.has_text,
            'pages': [asdict(page) for page in result.pages],
        }
        encoded = json.dumps(payload, ensure_ascii=False, default=str).encode('utf-8')
        if len(encoded) > MAX_ARTIFACT_BYTES:
            raise ParseArtifactError('resource_limit')
        final = self.path_for(reservation.source, reservation.attempt_id)
        temporary = final.with_name('pages.part')
        if final.exists():
            raise ParseArtifactError('artifact_write_failed')
        with temporary.open('xb') as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(final)
        return hashlib.sha256(encoded).hexdigest()

    def read(self, source: DocumentVersionSource, attempt_id: UUID, digest: str) -> ParsedPdf:
        try:
            path = self.path_for(source, attempt_id)
            with path.open('rb') as input_file:
                encoded = input_file.read(MAX_ARTIFACT_BYTES + 1)
            if len(encoded) > MAX_ARTIFACT_BYTES or hashlib.sha256(encoded).hexdigest() != digest:
                raise ParseArtifactError('artifact_unavailable')
            payload = json.loads(encoded)
            if type(payload['schema_version']) is not int or payload['schema_version'] != 1 or payload['source'] != json.loads(json.dumps(asdict(source), default=str)):
                raise ParseArtifactError('artifact_invalid')
            pages = tuple(ParsedPage(
                knowledge_base_id=UUID(item['knowledge_base_id']), document_id=UUID(item['document_id']),
                document_version_id=UUID(item['document_version_id']), original_filename=item['original_filename'],
                page_number=item['page_number'], page_text=item['page_text'],
            ) for item in payload['pages'])
            result = ParsedPdf(source, pages)
            self.validate(result, source)
            if type(payload['page_count']) is not int or payload['page_count'] != result.page_count or payload['has_text'] is not result.has_text:
                raise ParseArtifactError('artifact_invalid')
            return result
        except ParseArtifactError:
            raise
        except Exception:
            raise ParseArtifactError('artifact_unavailable') from None

    def discard(self, reservation: ArtifactReservation) -> None:
        # Only callers holding a successful exclusive reservation may clean it.
        try:
            path = self.path_for(reservation.source, reservation.attempt_id)
            path.with_name('pages.part').unlink(missing_ok=True)
            path.unlink(missing_ok=True)
            path.parent.rmdir()
        except (OSError, ValueError, ParseArtifactError):
            logger.warning('Parse artifact cleanup incomplete for attempt %s', reservation.attempt_id)
