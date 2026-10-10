"""SQL-authoritative stage state with unique/CAS claims, not process locks."""
from datetime import datetime, timezone
import logging
from uuid import uuid4

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from project.api.document_parsing_service import get_authorized_parse_source
from project.api.parse_stage_schemas import ParseStageResponse
from project.core.parse_artifacts import ParseArtifactError, ParseArtifactStore
from project.core.parse_executor import ParseExecutionUncertain
from project.db.business_models import DocumentParseStage


logger = logging.getLogger(__name__)
TABLE = DocumentParseStage.__table__
ERROR_MESSAGES = {
    'unsafe_source': 'The version source location is unavailable.',
    'source_missing': 'The original PDF is missing.',
    'source_unreadable': 'The original PDF cannot be read.',
    'source_too_large': 'The original PDF exceeds the size budget.',
    'invalid_pdf': 'The PDF is damaged or requires repair.',
    'password_required': 'Password-protected PDF is unsupported.',
    'text_extraction_failed': 'PDF page text could not be extracted.',
    'resource_limit': 'The parsing resource budget was exceeded.',
    'parse_timeout': 'The parser exceeded its time budget.',
    'worker_failure': 'The parser process failed.',
    'artifact_write_failed': 'The parsing result could not be saved.',
    'artifact_unavailable': 'The persisted parsing result is unavailable.',
    'artifact_invalid': 'The persisted parsing result failed validation.',
    'processing_failed': 'The parsing stage could not be completed.',
}


class ParseStageBusy(Exception):
    pass


class ParseStageUnavailable(Exception):
    pass


def _check_session(session):
    if session.new or session.dirty or session.deleted:
        raise ParseStageUnavailable('Stage operations require a clean request session')


def _load(session, version_id):
    with session.no_autoflush:
        row = session.execute(select(TABLE).where(TABLE.c.document_version_id == version_id)).mappings().one_or_none()
    return dict(row) if row is not None else None


def _utc(value):
    if value is None:
        return None
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc))


def _response(source, record):
    if record is None:
        return ParseStageResponse(**{
            'knowledge_base_id': source.knowledge_base_id, 'document_id': source.document_id,
            'document_version_id': source.document_version_id, 'original_filename': source.original_filename,
            'parse_status': 'not_started',
        })
    code = record['error_code']
    if code is not None and code not in ERROR_MESSAGES:
        code = 'processing_failed'
    return ParseStageResponse(
        knowledge_base_id=source.knowledge_base_id, document_id=source.document_id,
        document_version_id=source.document_version_id, original_filename=source.original_filename,
        parse_status=record['status'], attempt_id=record['attempt_id'],
        page_count=record['page_count'], has_text=record['has_text'],
        error_code=code, error_message=ERROR_MESSAGES.get(code),
        started_at=_utc(record['started_at']), finished_at=_utc(record['finished_at']),
    )


def _commit(session):
    try:
        session.commit()
    except BaseException:
        try:
            session.rollback()
        except Exception:
            logger.warning('Parse-stage rollback could not be confirmed')
        raise


def _transition(session, record, values):
    result = session.execute(update(TABLE).where(
        TABLE.c.document_version_id == record['document_version_id'],
        TABLE.c.attempt_id == record['attempt_id'], TABLE.c.status == record['status'],
    ).values(**values))
    if result.rowcount != 1:
        session.rollback()
        raise ParseStageBusy
    # Commit acknowledgement may be lost. Callers must not remove published
    # files on an exception here: the row may already reference that artifact.
    _commit(session)
    return {**record, **values}


def _claim(session, source, current):
    now = datetime.now(timezone.utc)
    record = {
        'document_version_id': source.document_version_id, 'attempt_id': uuid4(),
        'status': 'running', 'started_at': now, 'finished_at': None,
        'page_count': None, 'has_text': None, 'result_sha256': None, 'error_code': None,
    }
    if current is not None:
        if current['status'] != 'failed':
            raise ParseStageBusy
        return _transition(session, current, record)
    try:
        session.execute(insert(TABLE).values(**record))
        _commit(session)
    except IntegrityError:
        session.rollback()
        if _load(session, source.document_version_id) is not None:
            raise ParseStageBusy from None
        raise ParseStageUnavailable from None
    return record


def _failed_values(code):
    return {
        'status': 'failed', 'finished_at': datetime.now(timezone.utc),
        'page_count': None, 'has_text': None, 'result_sha256': None,
        'error_code': code if isinstance(code, str) and code in ERROR_MESSAGES else 'processing_failed',
    }


def _validated_record(session, source, record, artifacts):
    if record is not None and record['status'] == 'succeeded':
        try:
            parsed = artifacts.read(source, record['attempt_id'], record['result_sha256'])
            if parsed.page_count != record['page_count'] or parsed.has_text is not record['has_text']:
                raise ParseArtifactError('artifact_invalid')
        except ParseArtifactError as error:
            record = _transition(session, record, _failed_values(error.code))
    return record


def get_parse_stage(session, owner_id, kb_id, document_id, version_id, storage):
    source = get_authorized_parse_source(session, owner_id, kb_id, document_id, version_id)
    _check_session(session)
    record = _validated_record(session, source, _load(session, version_id), ParseArtifactStore(storage))
    return _response(source, record)


def trigger_parse_stage(session, owner_id, kb_id, document_id, version_id, storage, executor):
    source = get_authorized_parse_source(session, owner_id, kb_id, document_id, version_id)
    _check_session(session)
    artifacts = ParseArtifactStore(storage)
    current = _validated_record(session, source, _load(session, version_id), artifacts)
    if current is not None and current['status'] == 'succeeded':
        return _response(source, current)  # Successful repeats are read-only/idempotent.
    if current is not None and current['status'] == 'running':
        raise ParseStageBusy
    with executor.slot():
        record = _claim(session, source, current)
        reservation = None
        try:
            reservation = artifacts.reserve(source, record['attempt_id'])
            digest = executor.run(source, artifacts, reservation)
            parsed = artifacts.read(source, record['attempt_id'], digest)
        except ParseExecutionUncertain:
            logger.warning('Parser shutdown uncertain for attempt %s; running state and files retained', record['attempt_id'])
            raise ParseStageUnavailable from None
        except Exception as error:
            code = getattr(error, 'code', 'artifact_write_failed' if isinstance(error, OSError) else 'processing_failed')
            failed = _transition(session, record, _failed_values(code))
            if reservation is not None:
                artifacts.discard(reservation)
            return _response(source, failed)
        completed = _transition(session, record, {
            'status': 'succeeded', 'finished_at': datetime.now(timezone.utc),
            'page_count': parsed.page_count, 'has_text': parsed.has_text,
            'result_sha256': digest, 'error_code': None,
        })
        return _response(source, completed)
