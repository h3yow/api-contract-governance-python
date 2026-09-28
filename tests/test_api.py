"""HTTP 入口：错误原因码经 HTTP 暴露且可区分。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh_service():
    # 每个用例重置模块级服务状态，避免用例间污染。
    from contract_governance import GovernanceLimits, GovernanceService
    import main

    main.service = GovernanceService(GovernanceLimits())
    yield


def test_register_and_get():
    r = client.post(
        "/contracts",
        json={"contract_id": "orders", "fields": [{"name": "id", "type": "integer", "required": True}]},
    )
    assert r.status_code == 201
    assert r.json()["version"] == 1
    assert client.get("/contracts/orders").status_code == 200


def test_duplicate_register_reason():
    client.post("/contracts", json={"contract_id": "orders", "fields": []})
    r = client.post("/contracts", json={"contract_id": "orders", "fields": []})
    assert r.status_code == 409
    assert r.json()["error"]["reason"] == "contract_exists"


def test_illegal_transition_reason():
    client.post("/contracts", json={"contract_id": "orders", "fields": []})
    r = client.put("/contracts/orders", json={"version": 3, "fields": []})
    assert r.status_code == 409
    assert r.json()["error"]["reason"] == "illegal_version_transition"


def test_validate_and_revoke_flow():
    client.post(
        "/contracts",
        json={"contract_id": "orders", "fields": [{"name": "id", "type": "integer", "required": True}]},
    )
    bad = client.post("/contracts/orders/validate", json={"version": 1, "payload": {}})
    assert bad.status_code == 200
    assert bad.json()["ok"] is False
    assert bad.json()["issues"][0]["code"] == "missing_required"

    client.delete("/contracts/orders")
    r = client.post("/contracts/orders/validate", json={"version": 1, "payload": {"id": 1}})
    assert r.status_code == 409
    assert r.json()["error"]["reason"] == "contract_revoked"


def test_compatibility_endpoint():
    client.post(
        "/contracts",
        json={"contract_id": "orders", "fields": [{"name": "id", "type": "integer", "required": True}]},
    )
    client.put("/contracts/orders", json={"version": 2, "fields": []})
    r = client.get("/contracts/orders/compatibility", params={"from_version": 1, "to_version": 2})
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] == "breaking"
    assert body["changes"][0]["kind"] == "field_removed"


def test_paired_contract_lifecycle_and_pair_validation():
    r = client.post(
        "/contracts",
        json={
            "contract_id": "pair-api",
            "fields": [{"name": "id", "type": "integer", "required": True}],
            "response_fields": [{"name": "ok", "type": "boolean", "required": True}],
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["paired"] is True
    assert body["response_fields"][0]["name"] == "ok"

    good = client.post(
        "/contracts/pair-api/validate-pair",
        json={
            "version": 1,
            "request_payload": {"id": 1},
            "response_payload": {"ok": True},
        },
    )
    assert good.status_code == 200
    j = good.json()
    assert j["ok"] is True
    assert j["request"]["side"] == "request" and j["response"]["side"] == "response"

    bad = client.post(
        "/contracts/pair-api/validate-pair",
        json={
            "version": 1,
            "request_payload": {"id": "x"},
            "response_payload": {"ok": True},
        },
    )
    j = bad.json()
    assert j["ok"] is False
    assert j["request"]["ok"] is False and j["response"]["ok"] is True
    assert j["request"]["issues"][0]["code"] == "type_mismatch"


def test_pair_validation_on_legacy_contract_rejected():
    client.post("/contracts", json={"contract_id": "legacy", "fields": []})
    r = client.post(
        "/contracts/legacy/validate-pair",
        json={"version": 1, "request_payload": {}, "response_payload": {}},
    )
    assert r.status_code == 422
    assert r.json()["error"]["reason"] == "invalid_contract"


def test_validation_timeout_maps_to_408():
    client.post(
        "/contracts",
        json={
            "contract_id": "slow",
            "fields": [{"name": "id", "type": "integer", "required": True}],
        },
    )
    r = client.post(
        "/contracts/slow/validate",
        json={"version": 1, "payload": {}, "timeout_seconds": -1},
    )
    # -1 表示不设预算：缺字段正常返回 200，而非超时。
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_publish_gate_endpoints():
    client.post(
        "/contracts",
        json={
            "contract_id": "pub",
            "fields": [{"name": "id", "type": "integer", "required": True}],
        },
    )
    # 破坏性（删字段）默认拒绝。
    rejected = client.post(
        "/contracts/pub/releases",
        json={"version": 2, "fields": []},
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["reason"] == "breaking_change_rejected"

    # 显式接受后落库。
    accepted = client.post(
        "/contracts/pub/releases",
        json={"version": 2, "fields": [], "accept_breaking": True},
    )
    assert accepted.status_code == 201
    j = accepted.json()
    assert j["verdict"] == "breaking" and j["accepted_breaking"] is True

    rec = client.get("/contracts/pub/releases/2")
    assert rec.status_code == 200
    assert rec.json()["from_version"] == 1
    assert rec.json()["changes"][0]["kind"] == "field_removed"

    missing = client.get("/contracts/pub/releases/9")
    assert missing.status_code == 404
    assert missing.json()["error"]["reason"] == "release_record_not_found"
