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
