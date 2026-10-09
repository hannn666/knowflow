import asyncio
from uuid import uuid4

import pytest
from starlette import formparsers

from project.api.app import create_app
from project.api import upload_limits


def invoke_upload(chunks, headers):
    messages = [{"type": "http.request", "body": chunk, "more_body": i < len(chunks) - 1}
                for i, chunk in enumerate(chunks)]
    sent = []
    calls = 0
    async def receive():
        nonlocal calls
        calls += 1
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}
    async def send(message):
        sent.append(message)
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "POST", "scheme": "http", "path": f"/knowledge-bases/{uuid4()}/documents",
             "query_string": b"", "root_path": "", "headers": headers,
             "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 8000)}
    asyncio.run(create_app()(scope, receive, send))
    return next(item["status"] for item in sent if item["type"] == "http.response.start"), calls


@pytest.mark.parametrize("claimed_length", [None, b"1"])
def test_actual_receive_bytes_limited_and_parser_files_closed(monkeypatch, claimed_length):
    monkeypatch.setattr(upload_limits, "MAX_REQUEST_BYTES", 256)
    opened = []
    actual_factory = formparsers.SpooledTemporaryFile
    def track_file(*args, **kwargs):
        file = actual_factory(*args, **kwargs)
        opened.append(file)
        return file
    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", track_file)
    first = b'--test\r\nContent-Disposition: form-data; name="file"; filename="a.pdf"\r\nContent-Type: application/pdf\r\n\r\n%PDF-'
    headers = [(b"content-type", b"multipart/form-data; boundary=test")]
    if claimed_length is not None:
        headers.append((b"content-length", claimed_length))
    status, calls = invoke_upload([first, b"x" * 300, b"not read"], headers)
    assert status == 413
    assert calls == 2
    assert opened and all(file.closed for file in opened)


def test_large_declared_length_rejected_without_reading():
    status, calls = invoke_upload([b"unread"], [(b"content-length", str(upload_limits.MAX_REQUEST_BYTES + 1).encode())])
    assert status == 413
    assert calls == 0


@pytest.mark.parametrize("length", [b"invalid", b"-1"])
def test_malformed_content_length_rejected(length):
    status, calls = invoke_upload([b"unread"], [(b"content-length", length)])
    assert status == 400
    assert calls == 0


def test_long_numeric_length_is_rejected_without_integer_conversion():
    status, calls = invoke_upload([b"unread"], [(b"content-length", b"9" * 5000)])
    assert status == 413
    assert calls == 0


def test_duplicate_lengths_rejected():
    status, calls = invoke_upload([b"unread"], [(b"content-length", b"1"), (b"content-length", b"2")])
    assert status == 400
    assert calls == 0


def test_exact_body_limit_allowed(monkeypatch):
    first = b'--test\r\nContent-Disposition: form-data; name="file"; filename="a.pdf"\r\nContent-Type: application/pdf\r\n\r\n%PDF-'
    last = b'\r\n--test--\r\n'
    body = first + b"x" * 20 + last
    monkeypatch.setattr(upload_limits, "MAX_REQUEST_BYTES", len(body))
    status, _ = invoke_upload([body], [(b"content-type", b"multipart/form-data; boundary=test")])
    assert status == 401  # Body parsing completed; authentication, not size, rejects it.
