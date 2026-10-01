"""API 契约治理服务的 HTTP 入口。

错误语义：GovernanceError.reason 映射为稳定的 HTTP 状态码与原因码，
对外只暴露 reason + 安全消息，不泄漏内部实现细节。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from contract_governance import (
    ContractSpec,
    FieldSpec,
    FieldType,
    GovernanceError,
    GovernanceLimits,
    GovernanceService,
    Reason,
)

app = FastAPI(title="API Contract Governance")
service = GovernanceService(GovernanceLimits())

_STATUS_BY_REASON = {
    Reason.CONTRACT_EXISTS: 409,
    Reason.CONTRACT_NOT_FOUND: 404,
    Reason.CONTRACT_REVOKED: 409,
    Reason.ILLEGAL_VERSION_TRANSITION: 409,
    Reason.VERSION_MISMATCH: 409,
    Reason.INVALID_CONTRACT: 422,
    Reason.LIMIT_EXCEEDED: 413,
    Reason.VALIDATION_FAILED: 422,
    Reason.VALIDATION_TIMEOUT: 408,
    Reason.BREAKING_CHANGE_REJECTED: 409,
    Reason.RELEASE_RECORD_NOT_FOUND: 404,
}


@app.exception_handler(GovernanceError)
async def governance_error_handler(_: Request, exc: GovernanceError) -> JSONResponse:
    status = _STATUS_BY_REASON.get(exc.reason, 400)
    return JSONResponse(
        status_code=status,
        content={"error": {"reason": exc.reason.value, "message": exc.message}},
    )


class FieldIn(BaseModel):
    name: str
    type: FieldType
    required: bool = False


class RegisterIn(BaseModel):
    contract_id: str = Field(min_length=1)
    fields: list[FieldIn] = []
    # 缺省/None 表示老的单侧契约；传 [] 表示成对但响应侧无字段约束。
    response_fields: list[FieldIn] | None = None


class UpdateIn(BaseModel):
    version: int = Field(ge=2)
    fields: list[FieldIn] = []
    response_fields: list[FieldIn] | None = None


class PublishIn(BaseModel):
    version: int = Field(ge=2)
    fields: list[FieldIn] = []
    response_fields: list[FieldIn] | None = None
    accept_breaking: bool = False


class ValidateIn(BaseModel):
    version: int = Field(ge=1)
    payload: Any
    timeout_seconds: float | None = None


class ValidatePairIn(BaseModel):
    version: int = Field(ge=1)
    request_payload: Any
    response_payload: Any
    timeout_seconds: float | None = None


def _to_spec(fields: list[FieldIn], response_fields: list[FieldIn] | None = None) -> ContractSpec:
    return ContractSpec(
        fields=tuple(FieldSpec(name=f.name, type=f.type, required=f.required) for f in fields),
        response_fields=(
            None
            if response_fields is None
            else tuple(
                FieldSpec(name=f.name, type=f.type, required=f.required)
                for f in response_fields
            )
        ),
    )


def _fields_out(fields: Any) -> list[dict[str, Any]]:
    return [
        {"name": f.name, "type": f.type.value, "required": f.required}
        for f in fields
    ]


def _contract_out(c: Any) -> dict[str, Any]:
    out = {
        "contract_id": c.contract_id,
        "version": c.version,
        "status": c.status.value,
        "paired": c.spec.is_paired,
        "fields": _fields_out(c.spec.fields),
    }
    if c.spec.is_paired:
        out["response_fields"] = _fields_out(c.spec.response_fields)
    return out


@app.get("/")
def read_root() -> dict[str, str]:
    return {"Hello": "World"}


@app.post("/contracts", status_code=201)
def register(body: RegisterIn) -> dict[str, Any]:
    return _contract_out(
        service.register(body.contract_id, _to_spec(body.fields, body.response_fields))
    )


@app.get("/contracts/{contract_id}")
def get_contract(contract_id: str) -> dict[str, Any]:
    return _contract_out(service.get(contract_id))


@app.put("/contracts/{contract_id}")
def update_contract(contract_id: str, body: UpdateIn) -> dict[str, Any]:
    return _contract_out(
        service.update(
            contract_id, body.version, _to_spec(body.fields, body.response_fields)
        )
    )


@app.post("/contracts/{contract_id}/releases", status_code=201)
def publish_release(contract_id: str, body: PublishIn) -> dict[str, Any]:
    decision = service.publish(
        contract_id,
        body.version,
        _to_spec(body.fields, body.response_fields),
        accept_breaking=body.accept_breaking,
    )
    return _decision_out(decision)


@app.get("/contracts/{contract_id}/releases/{version}")
def get_release(contract_id: str, version: int) -> dict[str, Any]:
    return _decision_out(service.get_release_record(contract_id, version))


@app.delete("/contracts/{contract_id}")
def revoke_contract(contract_id: str) -> dict[str, Any]:
    return _contract_out(service.revoke(contract_id))


@app.post("/contracts/{contract_id}/validate")
def validate(contract_id: str, body: ValidateIn) -> dict[str, Any]:
    result = service.validate(
        contract_id, body.version, body.payload, timeout_seconds=body.timeout_seconds
    )
    return {
        "ok": result.ok,
        "contract_id": result.contract_id,
        "version": result.version,
        "issues": [
            {"path": i.path, "code": i.code, "message": i.message} for i in result.issues
        ],
    }


@app.post("/contracts/{contract_id}/validate-pair")
def validate_pair(contract_id: str, body: ValidatePairIn) -> dict[str, Any]:
    result = service.validate_pair(
        contract_id,
        body.version,
        body.request_payload,
        body.response_payload,
        timeout_seconds=body.timeout_seconds,
    )

    def side_out(side_result: Any) -> dict[str, Any]:
        return {
            "side": side_result.side.value,
            "ok": side_result.ok,
            "issues": [
                {"path": i.path, "code": i.code, "message": i.message}
                for i in side_result.issues
            ],
        }

    return {
        "ok": result.ok,
        "contract_id": result.contract_id,
        "version": result.version,
        "request": side_out(result.request),
        "response": side_out(result.response),
    }


def _changes_out(changes: Any) -> list[dict[str, Any]]:
    return [
        {"kind": c.kind.value, "field": c.field_name, "detail": c.detail, "side": c.side.value if c.side else None}
        for c in changes
    ]


def _decision_out(d: Any) -> dict[str, Any]:
    return {
        "contract_id": d.contract_id,
        "from_version": d.from_version,
        "to_version": d.to_version,
        "verdict": d.verdict.value,
        "request_verdict": d.request_verdict.value,
        "response_verdict": d.response_verdict.value if d.response_verdict else None,
        "allowed": d.allowed,
        "accepted_breaking": d.accepted_breaking,
        "changes": _changes_out(d.changes),
    }


@app.get("/contracts/{contract_id}/compatibility")
def compatibility(contract_id: str, from_version: int, to_version: int) -> dict[str, Any]:
    report = service.check_compatibility(contract_id, from_version, to_version)
    return {
        "contract_id": report.contract_id,
        "from_version": report.from_version,
        "to_version": report.to_version,
        "verdict": report.verdict.value,
        "request_verdict": report.request_verdict.value,
        "response_verdict": report.response_verdict.value if report.response_verdict else None,
        "changes": _changes_out(report.changes),
    }
