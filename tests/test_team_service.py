"""Exercise real team authentication, imports, receipts and concurrent requests."""

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from zhiwiki.service.api import create_service_app
from zhiwiki.service.common import ServiceSettings
from zhiwiki.service.worker import process_job


@pytest.fixture
def team(tmp_path):
    settings = ServiceSettings(data_dir=str(tmp_path), cookie_secure=False, user_rpm=6000,
                               global_rpm=60000, user_burst=600, global_burst=6000)
    app = create_service_app(settings)
    services = app.state.services
    admin = services.auth.create_user("admin", "admin-test-password", True)
    kb = services.auth.create_kb("Shared books", "kb_shared")
    reader = services.auth.create_user("reader", "reader-test-password")
    services.auth.grant(reader, kb, "reader")
    credential = services.auth.issue(reader, scopes=["retrieve", "read", "validate"], kb_ids=[kb])
    with TestClient(app) as client:
        login = client.post("/v1/sessions", json={"username": "admin", "password": "admin-test-password"})
        assert login.status_code == 200, login.text
        client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        yield client, services, {"admin": admin, "reader": reader, "token": credential["token"], "token_id": credential["token_id"], "kb": kb}


def headers(token=None, key="test-request-0001", **extra):
    return {**({"Authorization": "Bearer " + token} if token else {}), "Idempotency-Key": key, **extra}


def publish_text(team, text="Return policy: seven days. Keep the receipt.", mode="new", book=None, expected=None):
    client, services, identities = team
    fields = {"knowledge_base_id": identities["kb"], "mode": mode, "logical_source_key": "policy"}
    if book:
        fields.update(document_id=book, expected_revision_id=expected)
    response = client.post("/v1/workspaces/default/uploads?knowledge_base_id=" + identities["kb"],
                           headers=headers(key="upload-" + str(time.time_ns())), data=fields,
                           files={"file": ("policy.txt", text.encode(), "text/plain")})
    assert response.status_code == 202, response.text
    job = services.jobs.claim()
    assert job is not None
    process_job(services.settings, job["id"], job["fence"])
    result = services.jobs.get(services.auth.principal(client.cookies["zw_session"]), job["id"])
    assert result["status"] == "awaiting_publish", result
    path = "/v1/workspaces/default/documents/{}/revisions/{}".format(result["document_id"], result["revision_id"])
    review = client.post(path + ":review", json={"note": "Checked source text and applicable conditions"})
    assert review.status_code == 200, review.text
    conditional = {"If-None-Match": "*"} if mode == "new" else {"If-Match": '"' + expected + '"'}
    response = client.post(path + ":publish", headers=headers(key="publish-" + str(time.time_ns()), **conditional))
    assert response.status_code == 200, response.text
    return result


def retrieve(team, token=None, key="retrieve-0001", **overrides):
    client, _, identities = team
    payload = {"query": "Return policy", "knowledge_base_ids": [identities["kb"]], **overrides}
    return client.post("/v1/workspaces/default/retrievals", headers=headers(token or identities["token"], key), json=payload)


def test_authenticated_complete_tool_chain_and_idempotency(team):
    publish_text(team)
    client, _, identities = team
    response = retrieve(team)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["usage"]["model_calls"] == 0 and result["status"] == "ok"
    first = result["evidence"][0]
    assert first["text"] == "Return policy: seven days. Keep the receipt."
    replay = retrieve(team)
    assert replay.json()["retrieval_id"] == result["retrieval_id"]
    conflict = retrieve(team, query="Another query")
    assert conflict.status_code == 409
    read = client.post("/v1/workspaces/default/evidence:read", headers=headers(identities["token"], "read-00001"),
                       json={"retrieval_ids": [result["retrieval_id"]], "evidence_ids": [first["evidence_id"]]})
    assert read.status_code == 200, read.text
    view = read.json()["evidence"][0]
    validated = client.post("/v1/workspaces/default/citations:validate", headers=headers(identities["token"]),
                            json={"retrieval_ids": [read.json()["retrieval_id"]], "citations": [{"evidence_id": view["evidence_id"], "text_sha256": view["text_sha256"]}]})
    assert validated.status_code == 200, validated.text
    assert validated.json()["semantic_support"] == "not_checked"


def test_reader_cannot_upload_publish_or_bypass_legacy(team):
    client, _, identities = team
    read_headers = headers(identities["token"])
    assert client.post("/api/documents", headers=read_headers, json={}).status_code == 410
    assert client.get("/api/runs/unknown", headers=read_headers).status_code == 410
    response = client.post("/v1/workspaces/default/uploads?knowledge_base_id=" + identities["kb"], headers=read_headers,
                           data={"knowledge_base_id": identities["kb"], "mode": "new", "logical_source_key": "evil"}, files={"file": ("a.txt", b"text")})
    assert response.status_code == 403
    assert client.get("/v1/workspaces/other/knowledge-bases", headers=read_headers).status_code == 404


def test_acl_revocation_and_cross_user_receipt(team):
    publish_text(team)
    client, services, identities = team
    result = retrieve(team).json()
    second = services.auth.create_user("second", "second-test-password")
    services.auth.grant(second, identities["kb"], "reader")
    token = services.auth.issue(second, kb_ids=[identities["kb"]])["token"]
    denied = client.get("/v1/workspaces/default/retrievals/" + result["retrieval_id"], headers=headers(token))
    assert denied.status_code == 403
    client.delete("/v1/workspaces/default/knowledge-bases/" + identities["kb"] + "/members/" + identities["reader"])
    assert retrieve(team, key="retrieve-after-revoke").status_code == 403
    assert client.get("/v1/workspaces/default/retrievals/" + result["retrieval_id"], headers=headers(identities["token"])).status_code == 403


def test_new_revision_invalidates_old_receipt_atomically(team):
    original = publish_text(team)
    old = retrieve(team).json()
    publish_text(team, "Return policy: fourteen days.", "update", original["document_id"], original["revision_id"])
    client, _, identities = team
    previous = client.get("/v1/workspaces/default/retrievals/" + old["retrieval_id"], headers=headers(identities["token"]))
    assert previous.status_code == 409
    latest = retrieve(team, key="retrieve-current").json()
    assert "fourteen" in latest["evidence"][0]["text"]


def test_twenty_identities_complete_without_429(team):
    publish_text(team)
    client, services, identities = team
    tokens = []
    for index in range(20):
        user = services.auth.create_user("user{:02}".format(index), "concurrency-test-password")
        services.auth.grant(user, identities["kb"], "reader")
        tokens.append(services.auth.issue(user, kb_ids=[identities["kb"]])["token"])
    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(lambda pair: retrieve(team, token=pair[1], key="burst-user-{:02}".format(pair[0])), enumerate(tokens)))
    assert [response.status_code for response in results] == [200] * 20
    assert all(response.json()["status"] == "ok" for response in results)
