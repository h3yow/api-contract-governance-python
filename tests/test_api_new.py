"""HTTP 入口：成对契约、成对校验、发布门禁、决策查询、预算原因码。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh_service():
    from contract_governance import GovernanceLimits, GovernanceService
    import main

    main.service = GovernanceService(GovernanceLimits())
    yield


def test_paired_contract_register_and_pair_validate():
    r = client.post(
        "/contracts",
        json={
            "contract_id": "echo",
            "request_fields": [
                {"name": "id", "type": "integer", "required": True},
            ],
            "response_fields": [
                {"name": "code", "type": "integer", "required": True},
                {"name": "ok", "type": "boolean"},
            ],
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["request_fields"][0]["name"] == "id"
    assert body["response_fields"][1]["name"] == "ok"

    ok = client.post(
        "/contracts/echo/validate-pair",
        json={"version": 1, "request": {"id": 1}, "response": {"code": 0, "ok": True}},
    )
    assert ok.status_code == 200
    data = ok.json()
    assert data["ok"] is True
    assert [s["side"] for s in data["sides"]] == ["request", "response"]

    bad = client.post(
        "/contracts/echo/validate-pair",
        json={"version": 1, "request": {}, "response": {"code": "x"}},
    )
    assert bad.status_code == 200
    data = bad.json()
    assert data["ok"] is False
    by_side = {s["side"]: s for s in data["sides"]}
    assert by_side["request"]["issues"][0]["code"] == "missing_required"
    assert by_side["response"]["issues"][0]["code"] == "type_mismatch"


def test_side_mismatch_reason_over_http():
    client.post(
        "/contracts",
        json={"contract_id": "echo", "request_fields": [{"name": "id", "type": "integer"}]},
    )
    # 成对契约走单侧入口
    r = client.post("/contracts/echo/validate", json={"version": 1, "payload": {}})
    assert r.status_code == 422
    assert r.json()["error"]["reason"] == "validation_side_mismatch"


def test_publish_gate_endpoints():
    client.post(
        "/contracts",
        json={
            "contract_id": "orders",
            "fields": [
                {"name": "id", "type": "integer", "required": True},
                {"name": "name", "type": "string"},
            ],
        },
    )
    breaking = {"version": 2, "fields": [{"name": "id", "type": "integer", "required": True}]}

    # 默认拒绝破坏性变更
    r = client.post("/contracts/orders/publish", json=breaking)
    assert r.status_code == 409
    assert r.json()["error"]["reason"] == "breaking_change_rejected"

    # 决策尚未落库
    d = client.get("/contracts/orders/decisions/2")
    assert d.status_code == 404
    assert d.json()["error"]["reason"] == "decision_not_found"

    # 显式接受
    accepted = client.post(
        "/contracts/orders/publish", json={**breaking, "accept_breaking": True}
    )
    assert accepted.status_code == 201
    assert accepted.json()["version"] == 2

    d = client.get("/contracts/orders/decisions/2")
    assert d.status_code == 200
    body = d.json()
    assert body["verdict"] == "breaking"
    assert body["breaking_accepted"] is True
    assert body["changes"][0]["kind"] == "field_removed"
    assert body["changes"][0]["side"] is None


def test_publish_illegal_transition_distinct_reason():
    client.post(
        "/contracts",
        json={"contract_id": "orders", "fields": [{"name": "id", "type": "integer"}]},
    )
    r = client.post(
        "/contracts/orders/publish",
        json={"version": 9, "fields": [], "accept_breaking": True},
    )
    assert r.status_code == 409
    assert r.json()["error"]["reason"] == "illegal_version_transition"


def test_budget_exceeded_mapped_to_504():
    from contract_governance import GovernanceLimits, GovernanceService
    import main

    n = 200_000
    main.service = GovernanceService(
        GovernanceLimits(
            max_fields_per_contract=1_000_000,
            max_validation_issues=1_000_000,
            validation_timeout=0.005,
        )
    )
    client.post(
        "/contracts",
        json={
            "contract_id": "big",
            "fields": [
                {"name": f"f{i}", "type": "string", "required": True}
                for i in range(n)
            ],
        },
    )
    r = client.post("/contracts/big/validate", json={"version": 1, "payload": {}})
    assert r.status_code == 504
    err = r.json()["error"]
    assert err["reason"] == "budget_exceeded"
    assert err["message"] == "validation rejected: time budget exceeded"
