"""Owned, deterministic document and version lists on isolated test databases."""
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from project.db.business_models import DocumentVersion
from test_document_api import upload_context, post


def listing(kb):
    return f"/knowledge-bases/{kb}/documents"


def versions(kb, doc):
    return f"{listing(kb)}/{doc}/versions"


def test_list_documents_and_versions_latest_and_stable_paging(upload_context, db_session):
    client, accounts, _ = upload_context
    headers, kb = accounts[0]
    items = [post(client, headers, kb, name=f"public-{i}.pdf").json() for i in range(3)]
    new_version = DocumentVersion(
        document_id=UUID(items[0]["document_id"]), original_filename="second-version.pdf",
        created_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )
    db_session.add(new_version)
    db_session.commit()
    first = client.get(listing(kb), headers=headers)
    assert first.status_code == 200 and first.headers["cache-control"] == "no-store"
    rows = first.json()
    assert len(rows) == 3
    assert {row["id"] for row in rows} == {item["document_id"] for item in items}
    assert [(row["created_at"], row["id"]) for row in rows] == sorted(
        [(row["created_at"], row["id"]) for row in rows], reverse=True)
    assert all(row["knowledge_base_id"] == kb for row in rows)
    latest = next(row["latest_version"] for row in rows if row["id"] == items[0]["document_id"])
    assert latest["document_version_id"] == str(new_version.id)
    assert latest["original_filename"] == "second-version.pdf"
    assert latest["status"] == "pending"
    assert all("path" not in row and "storage" not in row for row in rows)
    assert [row["id"] for offset in range(3) for row in
            client.get(listing(kb) + f"?limit=1&offset={offset}", headers=headers).json()] == [
        row["id"] for row in rows]
    assert client.get(listing(kb) + "?limit=1&offset=3", headers=headers).json() == []
    version_result = client.get(versions(kb, items[0]["document_id"]), headers=headers)
    assert version_result.status_code == 200
    assert version_result.headers["cache-control"] == "no-store"
    listed = version_result.json()
    assert [v["document_version_id"] for v in listed] == [str(new_version.id), items[0]["document_version_id"]]
    assert all(v["document_id"] == items[0]["document_id"] and v["knowledge_base_id"] == kb for v in listed)
    assert client.get(versions(kb, items[0]["document_id"]) + "?limit=1&offset=1", headers=headers).json() == [listed[1]]


def test_list_ownership_and_id_mismatch_are_indistinguishable(upload_context):
    client, accounts, _ = upload_context
    headers, kb = accounts[0]
    other_headers, other_kb = accounts[1]
    own = post(client, headers, kb).json()
    foreign = post(client, other_headers, other_kb, name="foreign.pdf").json()
    own_rows = client.get(listing(kb), headers=headers).json()
    other_rows = client.get(listing(other_kb), headers=other_headers).json()
    assert [row["id"] for row in own_rows] == [own["document_id"]]
    assert [row["id"] for row in other_rows] == [foreign["document_id"]]
    missing_list = client.get(listing(str(uuid4())), headers=headers)
    for request_headers, request_kb in ((other_headers, kb), (headers, other_kb)):
        response = client.get(listing(request_kb), headers=request_headers)
        assert response.status_code == 404 and response.json() == missing_list.json()
    missing_versions = client.get(versions(kb, uuid4()), headers=headers)
    for request_headers, request_kb, doc in (
        (other_headers, kb, own["document_id"]),
        (headers, other_kb, foreign["document_id"]),
        (headers, kb, foreign["document_id"]),
        (headers, other_kb, own["document_id"]),
    ):
        response = client.get(versions(request_kb, doc), headers=request_headers)
        assert response.status_code == 404 and response.json() == missing_versions.json()


@pytest.mark.parametrize("suffix", ["", f"/{uuid4()}/versions"])
def test_lists_require_authentication(upload_context, suffix):
    client, accounts, _ = upload_context
    kb = accounts[0][1]
    assert client.get(listing(kb) + suffix).status_code == 401
    assert client.get(listing(kb) + suffix, headers={"Authorization": "Bearer invalid"}).status_code == 401


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1", "limit=garbage"])
def test_list_page_validation(upload_context, query):
    client, accounts, _ = upload_context
    headers, kb = accounts[0]
    assert client.get(listing(kb) + "?" + query, headers=headers).status_code == 422
    assert client.get(versions(kb, uuid4()) + "?" + query, headers=headers).status_code == 422
