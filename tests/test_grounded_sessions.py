"""Real HTTP/JWT/session/source gates with a controlled clock and fake inference.

Uses only the existing isolated SQLite, generated public PDF and in-memory index.
Real PostgreSQL visibility is covered separately, not inferred from SQLite.
"""
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from sqlalchemy.exc import OperationalError

from project.api import auth_tokens, auth_session_service as auth, grounded_routes
from project.db.business_models import AuthSession
from test_grounded_qa import qa_context, indexed
from test_parse_stage import stage_context


@pytest.fixture
def running_qa(qa_context, monkeypatch):
    client, db, _, accounts, _, service, llm = qa_context
    headers, _, path = indexed(qa_context)
    owner = auth_tokens.decode_access_identity(headers['Authorization'].split()[1]).user_id
    base = datetime.now(timezone.utc).replace(microsecond=0)
    clock = [base]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0] if tz else clock[0].replace(tzinfo=None)

    monkeypatch.setattr(auth_tokens, 'datetime', Clock)
    monkeypatch.setattr(jwt.api_jwt, 'datetime', Clock)
    monkeypatch.setattr(auth, 'utcnow', lambda: clock[0])
    bundle = auth.start_session(db, owner)
    headers = {'Authorization': 'Bearer ' + bundle.access_token}
    clock[0] = base + timedelta(minutes=14)
    return client, db, path, llm, bundle, clock, base, accounts, service


def ask(run):
    client, _, path, _, bundle, *_ = run
    return client.post(path+'/ask', headers={'Authorization': 'Bearer '+bundle.access_token},
                       json={'question': 'When did the Apollo project launch?'})


@pytest.mark.parametrize('rotation', [False, True])
def test_minute_14_to_17_expiry_and_rotation_preserve_same_session(running_qa, rotation):
    _, db, _, llm, bundle, clock, base, *_ = running_qa
    generate = llm.generate

    def finish(*args, **kwargs):
        assert not db.in_transaction(), 'No transaction/row lock during inference'
        clock[0] = base + timedelta(minutes=17)
        with pytest.raises(jwt.ExpiredSignatureError):
            auth_tokens.decode_access_identity(bundle.access_token)
        if rotation:
            renewed = auth.rotate_refresh(db, bundle.refresh_token)
            assert renewed.session_id == bundle.session_id
        return generate(*args, **kwargs)

    llm.generate = finish
    response = ask(running_qa)
    assert response.status_code == 200, response.text
    assert response.json()['citations'][0]['page_number'] == 1
    assert response.headers['x-knowflow-verified-session'] == str(bundle.session_id)
    assert not db.in_transaction()
    row = db.get(AuthSession, bundle.session_id)
    assert response.headers['x-knowflow-verified-user'] == str(row.user_id)
    assert auth.aware(row.idle_expires_at) == base + timedelta(minutes=44)


@pytest.mark.parametrize('change', ['logout', 'revoke', 'replay', 'idle', 'absolute', 'wrong_user', 'deleted'])
def test_session_lost_during_inference_never_releases_evidence(running_qa, change):
    _, db, _, llm, bundle, clock, base, accounts, _ = running_qa
    generate = llm.generate

    def finish(*args, **kwargs):
        assert not db.in_transaction()
        if change == 'logout':
            auth.revoke_session(db, bundle.refresh_token)
        elif change == 'replay':
            auth.rotate_refresh(db, bundle.refresh_token)
            with pytest.raises(auth.InvalidSession):
                auth.rotate_refresh(db, bundle.refresh_token)
        elif change in ('idle', 'absolute'):
            # At admission idle is minute 44. Isolate absolute expiry by setting
            # its deadline before admission in the test setup below.
            clock[0] = base + timedelta(minutes=44 if change == 'idle' else 16)
        else:
            row = db.get(AuthSession, bundle.session_id)
            if change == 'revoke':
                row.revoked_at, row.revocation_reason = clock[0], 'logout'
            if change == 'deleted': db.delete(row)
            if change == 'wrong_user':
                row.user_id = auth_tokens.decode_access_identity(accounts[1][0]['Authorization'].split()[1]).user_id
            db.commit()
        return generate(*args, **kwargs)

    if change == 'absolute':
        row = db.get(AuthSession, bundle.session_id)
        row.absolute_expires_at = row.idle_expires_at = base + timedelta(minutes=16)
        db.commit()
    llm.generate = finish
    response = ask(running_qa)
    assert response.status_code == 401, response.text
    assert response.headers['cache-control'] == 'no-store'
    assert 'x-knowflow-verified-session' not in response.headers
    assert all(value not in response.text for value in ('2024-04-15', 'citations', 'Apollo'))
    assert len(llm.calls) == 1


def test_expired_at_entry_never_runs_model(running_qa):
    running_qa[5][0] = running_qa[6] + timedelta(minutes=17)
    assert ask(running_qa).status_code == 401
    assert not running_qa[3].calls


def test_release_database_failure_is_safe_503(running_qa, monkeypatch):
    def unavailable(*_):
        raise OperationalError('private-db-sentinel', {}, Exception('private-db-sentinel'))
    monkeypatch.setattr(grounded_routes, 'check_result_session', unavailable)
    response = ask(running_qa)
    assert response.status_code == 503
    assert all(x not in response.text for x in ('sentinel', 'citations', 'Apollo'))


def test_refusal_also_requires_live_session(running_qa):
    _, db, _, llm, bundle, *_ = running_qa
    def refuse(*args, **kwargs):
        auth.revoke_session(db, bundle.refresh_token)
        return '{"status":"insufficient_evidence","quotes":[]}'
    llm.generate = refuse
    assert ask(running_qa).status_code == 401


def test_retry_retains_no_transaction_and_final_gate(running_qa):
    _, db, _, llm, bundle, *_ = running_qa
    calls = []
    def generate(*args, retry=False):
        assert not db.in_transaction()
        calls.append(retry)
        if retry:
            auth.revoke_session(db, bundle.refresh_token)
            return '{"status":"insufficient_evidence","quotes":[]}'
        return 'invalid'
    llm.generate = generate
    assert ask(running_qa).status_code == 401
    assert calls == [False, True]
