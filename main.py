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


class UpdateIn(BaseModel):
    version: int = Field(ge=2)
    fields: list[FieldIn] = []


class ValidateIn(BaseModel):
    version: int = Field(ge=1)
    payload: Any


def _to_spec(fields: list[FieldIn]) -> ContractSpec:
    return ContractSpec(
        fields=tuple(FieldSpec(name=f.name, type=f.type, required=f.required) for f in fields)
    )


def _contract_out(c: Any) -> dict[str, Any]:
    return {
        "contract_id": c.contract_id,
        "version": c.version,
        "status": c.status.value,
        "fields": [
            {"name": f.name, "type": f.type.value, "required": f.required}
            for f in c.spec.fields
        ],
    }


@app.get("/")
def read_root() -> dict[str, str]:
    return {"Hello": "World"}


@app.post("/contracts", status_code=201)
def register(body: RegisterIn) -> dict[str, Any]:
    return _contract_out(service.register(body.contract_id, _to_spec(body.fields)))


@app.get("/contracts/{contract_id}")
def get_contract(contract_id: str) -> dict[str, Any]:
    return _contract_out(service.get(contract_id))


@app.put("/contracts/{contract_id}")
def update_contract(contract_id: str, body: UpdateIn) -> dict[str, Any]:
    return _contract_out(service.update(contract_id, body.version, _to_spec(body.fields)))


@app.delete("/contracts/{contract_id}")
def revoke_contract(contract_id: str) -> dict[str, Any]:
    return _contract_out(service.revoke(contract_id))


@app.post("/contracts/{contract_id}/validate")
def validate(contract_id: str, body: ValidateIn) -> dict[str, Any]:
    result = service.validate(contract_id, body.version, body.payload)
    return {
        "ok": result.ok,
        "contract_id": result.contract_id,
        "version": result.version,
        "issues": [
            {"path": i.path, "code": i.code, "message": i.message} for i in result.issues
        ],
    }


@app.get("/contracts/{contract_id}/compatibility")
def compatibility(contract_id: str, from_version: int, to_version: int) -> dict[str, Any]:
    report = service.check_compatibility(contract_id, from_version, to_version)
    return {
        "contract_id": report.contract_id,
        "from_version": report.from_version,
        "to_version": report.to_version,
        "verdict": report.verdict.value,
        "changes": [
            {"kind": c.kind.value, "field": c.field_name, "detail": c.detail}
            for c in report.changes
        ],
    }
