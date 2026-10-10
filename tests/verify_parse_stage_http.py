"""Real HTTP/native-parser/PostgreSQL smoke test in a newly created DB.

Does not upgrade, delete rows from, or connect the API to the business DB.
The listener uses an OS-selected free port; the manual 8000 server is untouched.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import secrets
import socket
import sys
import tempfile
from threading import Thread
import time
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alembic import command
from alembic.config import Config
import httpx
import pymupdf
from sqlalchemy import create_engine, text
import uvicorn

from project.api.app import create_app
from project.db.business_database import get_database_engine, get_database_url


REVISION = 'c842a9f713e0'


def public_pdf(texts, encrypted=False):
    with pymupdf.open() as pdf:
        for value in texts:
            page = pdf.new_page()
            if value:
                page.insert_text((72,72), value)
        options = {'encryption': pymupdf.PDF_ENCRYPT_AES_256, 'owner_pw': 'test-only-owner', 'user_pw': 'test-only-user'} if encrypted else {}
        return pdf.tobytes(**options)


def exercise_api(base_url, root):
    with httpx.Client(base_url=base_url, trust_env=False, timeout=60) as client:
        accounts = []
        for _ in range(2):
            credentials = {'email': f'parse-http-{uuid4().hex}@example.com', 'password': secrets.token_urlsafe(24)}
            assert client.post('/auth/register', json=credentials).status_code == 201
            login = client.post('/auth/login', json=credentials)
            assert login.status_code == 200
            headers = {'Authorization': f"Bearer {login.json()['access_token']}"}
            kb = client.post('/knowledge-bases', headers=headers, json={'name': 'Isolated parsing HTTP'})
            assert kb.status_code == 201
            accounts.append((headers, kb.json()['id']))
        headers, kb_id = accounts[0]

        def upload(payload, name='public.pdf'):
            response = client.post(f'/knowledge-bases/{kb_id}/documents', headers=headers,
                                   files={'file': (name, payload, 'application/pdf')})
            assert response.status_code == 201
            item = response.json()
            path = f"/knowledge-bases/{kb_id}/documents/{item['document_id']}/versions/{item['document_version_id']}/parse"
            return item, path

        payload = public_pdf(('First public page', '', 'Third public page'))
        item, path = upload(payload)
        assert client.get(path, headers=headers).json()['parse_status'] == 'not_started'
        assert client.get(path).status_code == 401
        assert client.post(path, headers=accounts[1][0]).status_code == 404
        assert client.get(path.replace(kb_id, accounts[1][1]), headers=headers).status_code == 404
        result = client.post(path, headers=headers)
        assert result.status_code == 200
        summary = result.json()
        assert summary['parse_status'] == 'succeeded' and summary['page_count'] == 3
        assert summary['has_text'] is True and 'page_text' not in result.text
        assert client.get(path, headers=headers).json() == summary
        assert client.post(path, headers=headers).json() == summary
        source_path = root / kb_id / item['document_id'] / item['document_version_id'] / 'source.pdf'
        assert source_path.read_bytes() == payload
        artifact = source_path.parent / 'parsing' / summary['attempt_id'] / 'pages.json'
        import json
        saved = json.loads(artifact.read_text(encoding='utf-8'))
        assert [page['page_number'] for page in saved['pages']] == [1, 2, 3]
        assert [page['page_text'].strip() for page in saved['pages']] == ['First public page', '', 'Third public page']
        assert client.get(path.removesuffix('/parse'), headers=headers).json()['status'] == 'pending'

        # Actual failures use the native child process, not a mock executor.
        for bad_payload, error_code in [(b'%PDF-1.7\ninvalid data', 'invalid_pdf'),
                                        (public_pdf(('Protected test',), encrypted=True), 'password_required')]:
            _, failed_path = upload(bad_payload)
            failed = client.post(failed_path, headers=headers).json()
            assert failed['parse_status'] == 'failed' and failed['error_code'] == error_code
            assert str(root) not in str(failed)

        first_parallel, path_a = upload(public_pdf(('Parallel A',)))
        second_parallel, path_b = upload(public_pdf(('Parallel B',)))
        def post_on_own_connection(path):
            with httpx.Client(base_url=base_url, trust_env=False, timeout=60) as connection:
                response = connection.post(path, headers=headers)
                assert response.status_code == 200
                return response.json()
        with ThreadPoolExecutor(max_workers=2) as pool:
            parallel = list(pool.map(post_on_own_connection, (path_a, path_b)))
        assert all(value['parse_status'] == 'succeeded' for value in parallel)
        assert parallel[0]['attempt_id'] != parallel[1]['attempt_id']
        for uploaded, summary in zip((first_parallel, second_parallel), parallel):
            assert summary['document_version_id'] == uploaded['document_version_id']
        print('PASS: real HTTP + PostgreSQL + spawned native parsers; auth/ownership, persistent pages, cached repeats, encrypted/damaged failure, different-version parallel execution')


def main():
    source = get_database_url()
    if (source.host, source.port, source.database, source.username) != ('127.0.0.1', 5432, 'knowflow', 'knowflow'):
        raise RuntimeError('Unexpected local source configuration')
    database_name = 'knowflow_test_stage_http_' + uuid4().hex
    admin = create_engine(source.set(database='postgres'), isolation_level='AUTOCOMMIT', hide_parameters=True,
                          connect_args={'connect_timeout': 5})
    created = False
    try:
        with admin.connect() as connection:
            if connection.execute(text('SELECT current_database(), current_user')).one() != ('postgres', 'knowflow'):
                raise RuntimeError('Unexpected maintenance DB identity')
            connection.execute(text('CREATE DATABASE ' + database_name))
            created = True
        target = source.set(database=database_name)
        print('Created this-run isolated HTTP database: ' + database_name)
        with tempfile.TemporaryDirectory(prefix='knowflow-stage-http-') as directory:
            root = Path(directory).resolve()
            if not root.is_relative_to(Path(tempfile.gettempdir()).resolve()):
                raise RuntimeError('Unexpected temporary root')
            old_root = os.environ.get('KNOWFLOW_UPLOAD_ROOT')
            os.environ['KNOWFLOW_UPLOAD_ROOT'] = str(root)
            get_database_engine.cache_clear()
            with patch('project.db.business_database.get_database_url', return_value=target), \
                 patch('project.api.auth_tokens.get_signing_key', return_value=secrets.token_hex(32)):
                cfg = Config(str(Path(__file__).resolve().parents[1] / 'alembic.ini'))
                command.upgrade(cfg, REVISION)
                command.check(cfg)
                with get_database_engine().connect() as connection:
                    if connection.scalar(text('SELECT current_database()')) != database_name:
                        raise RuntimeError('HTTP test DB mismatch')
                sock = socket.socket()
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
                server = uvicorn.Server(uvicorn.Config(create_app(), host='127.0.0.1', port=port,
                                                      log_level='error', access_log=False))
                thread = Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
                try:
                    thread.start()
                    deadline = time.monotonic() + 10
                    while not server.started and thread.is_alive() and time.monotonic() < deadline:
                        time.sleep(0.05)
                    if not server.started:
                        raise RuntimeError('Isolated HTTP listener did not start')
                    print('Using isolated localhost port: ' + str(port))
                    exercise_api(f'http://127.0.0.1:{port}', root)
                finally:
                    server.should_exit = True
                    thread.join(timeout=45)
                    sock.close()
                    if thread.is_alive():
                        raise RuntimeError('Isolated test listener has not stopped')
                    get_database_engine().dispose()
                    get_database_engine.cache_clear()
            if old_root is None:
                os.environ.pop('KNOWFLOW_UPLOAD_ROOT', None)
            else:
                os.environ['KNOWFLOW_UPLOAD_ROOT'] = old_root
    finally:
        if created:
            with admin.connect() as connection:
                connection.execute(text('DROP DATABASE ' + database_name))
            print('Removed only this-run isolated HTTP database: ' + database_name)
        admin.dispose()


if __name__ == '__main__':
    main()
