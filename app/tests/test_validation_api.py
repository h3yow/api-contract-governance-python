"""运行时校验语义区分、上限策略与 HTTP 入口错误归类。"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.errors import ContractError, ErrorCode
from app.limits import LimitAction, LimitConfig
from app.registry import reset_registry

CONTRACT = {
    "id": "svc",
    "version": "1.0.0",
    "fields": [
        {"name": "user_id", "type": "INTEGER", "required": True},
        {"name": "nick", "type": "STRING", "required": False},
    ],
}


def test_unregistered_revoked_mismatch_are_distinguished():
    registry = reset_registry()
    registry.register(dict(CONTRACT))

    with pytest.raises(ContractError) as exc:
        registry.validate("unknown", "1.0.0", {}, {})
    assert exc.value.code is ErrorCode.NOT_FOUND

    registry.revoke("svc", "1.0.0", 0)
    with pytest.raises(ContractError) as exc:
        registry.validate("svc", "1.0.0", {"user_id": 1}, {"user_id": 1})
    assert exc.value.code is ErrorCode.REVOKED

    registry2 = reset_registry()
    registry2.register(dict(CONTRACT))
    with pytest.raises(ContractError) as exc:
        registry2.validate(
            "svc", "1.0.0", {"user_id": 1}, {"user_id": 1},
            declared_version="2.0.0",
        )
    assert exc.value.code is ErrorCode.VERSION_MISMATCH
    assert exc.value.details["declared"] == "2.0.0"


def test_violation_reasons_are_distinguishable(registry):
    registry.register(dict(CONTRACT))
    outcome = registry.validate(
        "svc",
        "1.0.0",
        {"user_id": "not-an-int", "extra": 1},
        {},
    )
    assert outcome.valid is False
    reasons = {(v.path, v.reason) for v in outcome.violations}
    assert ("request.user_id", "wrong_type") in reasons
    assert ("request.extra", "unknown_field") in reasons
    assert ("response.user_id", "missing_required") in reasons


def test_failed_validation_does_not_pollute_state(registry):
    registry.register(dict(CONTRACT))
    before = registry.get("svc", "1.0.0")
    for payload in (
        ({"user_id": "bad"}, {}),
        ({"user_id": None}, {"user_id": None}),
        ({"user_id": 1, "x": {}}, {"user_id": 2}),
    ):
        outcome = registry.validate("svc", "1.0.0", *payload)
        assert not outcome.valid
    after = registry.get("svc", "1.0.0")
    assert after == before
    # 后续合法请求必须照常通过
    ok = registry.validate(
        "svc", "1.0.0", {"user_id": 1, "nick": "a"}, {"user_id": 2}
    )
    assert ok.valid


def test_max_fields_limit_rejects_registration():
    registry = reset_registry(LimitConfig(max_fields=2))
    with pytest.raises(ContractError) as exc:
        registry.register(
            {
                "id": "svc",
                "version": "1.0.0",
                "fields": [
                    {"name": "a", "type": "STRING"},
                    {"name": "b", "type": "STRING"},
                    {"name": "c", "type": "STRING"},
                ],
            }
        )
    assert exc.value.code is ErrorCode.LIMIT_EXCEEDED
    with pytest.raises(ContractError) as exc:
        registry.get("svc", "1.0.0")
    assert exc.value.code is ErrorCode.NOT_FOUND


def test_max_versions_limit_enforced():
    registry = reset_registry(LimitConfig(max_versions=2))
    registry.register({"id": "svc", "version": "1.0.0", "fields": []})
    registry.register({"id": "svc", "version": "2.0.0", "fields": []})
    with pytest.raises(ContractError) as exc:
        registry.register({"id": "svc", "version": "3.0.0", "fields": []})
    assert exc.value.code is ErrorCode.LIMIT_EXCEEDED
    assert len(registry.list_versions("svc")) == 2


def test_payload_nodes_limit_rejects_without_partial_state():
    registry = reset_registry(LimitConfig(max_payload_nodes=10))
    registry.register(dict(CONTRACT))
    big = {"nested": [{"k": i} for i in range(20)]}
    with pytest.raises(ContractError) as exc:
        registry.validate("svc", "1.0.0", {"user_id": 1, **big}, {"user_id": 1})
    assert exc.value.code is ErrorCode.LIMIT_EXCEEDED
    assert registry.get("svc", "1.0.0").revision == 0


def test_error_details_limit_reject_vs_truncate():
    rejecting = reset_registry(
        LimitConfig(max_error_details=1, on_limit_exceeded=LimitAction.REJECT)
    )
    rejecting.register(dict(CONTRACT))
    with pytest.raises(ContractError) as exc:
        rejecting.validate("svc", "1.0.0", {"user_id": "x", "a": 1, "b": 2}, {})
    assert exc.value.code is ErrorCode.LIMIT_EXCEEDED

    truncating = reset_registry(
        LimitConfig(max_error_details=1, on_limit_exceeded=LimitAction.TRUNCATE)
    )
    truncating.register(dict(CONTRACT))
    outcome = truncating.validate(
        "svc", "1.0.0", {"user_id": "x", "a": 1, "b": 2}, {}
    )
    assert outcome.truncated is True
    assert len(outcome.violations) == 1


# --------------------------------------------------------------------- #
# HTTP 入口
# --------------------------------------------------------------------- #
@pytest.fixture
def client():
    registry = reset_registry()
    return TestClient(create_app(registry))


def test_http_full_lifecycle_and_status_mapping(client: TestClient):
    r = client.post("/contracts", json=CONTRACT)
    assert r.status_code == 201

    r = client.post("/contracts", json=CONTRACT)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "DUPLICATE_CONTRACT"
    # 默认不暴露内部 details
    assert "details" not in r.json()["error"]

    r = client.post(
        "/contracts/svc/versions/1.0.0/validate",
        json={"request": {"user_id": "x"}, "response": {}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is False and len(body["violations"]) >= 1

    r = client.post(
        "/contracts/missing/versions/1.0.0/validate",
        json={"request": {}, "response": {}},
    )
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"

    r = client.post(
        "/contracts/svc/versions/1.0.0/validate",
        json={"request": {}, "response": {}, "declared_version": "9.9.9"},
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "VERSION_MISMATCH"

    r = client.post(
        "/contracts/svc/versions/1.0.0/revoke",
        json={"expected_revision": 0},
    )
    assert r.status_code == 200 and r.json()["revoked"] is True

    r = client.post(
        "/contracts/svc/versions/1.0.0/validate",
        json={"request": {"user_id": 1}, "response": {"user_id": 1}},
    )
    assert r.status_code == 410 and r.json()["error"]["code"] == "REVOKED"


def test_http_update_revision_conflict_and_success(client: TestClient):
    client.post("/contracts", json=CONTRACT)
    fields = CONTRACT["fields"] + [{"name": "tag", "type": "STRING"}]
    r = client.put(
        "/contracts/svc/versions/1.0.0",
        json={"expected_revision": 5, "fields": fields},
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "REVISION_CONFLICT"
    r = client.put(
        "/contracts/svc/versions/1.0.0",
        json={"expected_revision": 0, "fields": fields},
    )
    assert r.status_code == 200 and r.json()["revision"] == 1


def test_http_compatibility_endpoint(client: TestClient):
    client.post("/contracts", json=CONTRACT)
    v2 = dict(CONTRACT, version="2.0.0")
    v2["fields"] = [
        # 必填的 user_id 被删除（FIELD_REMOVED），nick 升级为必填
        {"name": "nick", "type": "STRING", "required": True},
    ]
    client.post("/contracts", json=v2)
    r = client.get(
        "/contracts/svc/compatibility?from_version=1.0.0&to_version=2.0.0"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["compatible"] is False
    kinds = {c["kind"] for c in body["changes"]}
    assert "FIELD_REMOVED" in kinds
    assert "REQUIRED_SCOPE_EXPANDED" in kinds
    assert body["basis"], "判定依据必须随结论返回"


def test_http_internal_details_only_when_enabled():
    hidden = TestClient(create_app(reset_registry()))
    hidden.post("/contracts", json={"id": "s", "version": "1.0.0", "fields": []})
    r = hidden.post("/contracts", json={"id": "s", "version": "1.0.0", "fields": []})
    assert "details" not in r.json()["error"]

    exposed = TestClient(
        create_app(reset_registry(LimitConfig(expose_internal_details=True)))
    )
    exposed.post("/contracts", json={"id": "s", "version": "1.0.0", "fields": []})
    r = exposed.post("/contracts", json={"id": "s", "version": "1.0.0", "fields": []})
    assert r.json()["error"]["details"]["existing_revision"] == 0


def test_validation_logging_prints_id_and_normalization(client: TestClient, caplog):
    client.post("/contracts", json=CONTRACT)
    with caplog.at_level(logging.INFO, logger="contractgov"):
        client.post(
            "/contracts/svc/versions/1.0.0/validate",
            json={"request": {"user_id": 1}, "response": {"user_id": 2}},
        )
    assert "svc" in caplog.text
    assert "运行时校验" in caplog.text
