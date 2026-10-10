from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from project.db.business_models import DocumentParseStage
from test_parse_stage import stage_context


@pytest.mark.parametrize('invalid', ['status', 'summary', 'missing_parent', 'duplicate'])
def test_stage_database_constraints(stage_context, invalid):
    _, session, _, _, _, item, _ = stage_context
    version = UUID(item['document_version_id'])
    if invalid == 'duplicate':
        session.add(DocumentParseStage(document_version_id=version, attempt_id=uuid4(), status='running'))
        session.commit()
    kwargs = {'document_version_id': version, 'attempt_id': uuid4(), 'status': 'running'}
    if invalid == 'status':
        kwargs['status'] = 'ready'
    if invalid == 'summary':
        kwargs.update(status='succeeded', has_text=True, result_sha256='a' * 64,
                      finished_at=datetime.now(timezone.utc))  # Missing page_count must fail, not SQL UNKNOWN.
    if invalid == 'missing_parent':
        kwargs['document_version_id'] = uuid4()
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.add(DocumentParseStage(**kwargs))
            session.flush()
