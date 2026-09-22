"""FastAPI HTTP 入口。

错误响应统一形如::

    {"error": {"code": "REVOKED", "message": "..."}}

``code`` 始终暴露以保证原因可区分；``details`` 仅在
``LimitConfig.expose_internal_details=True`` 时返回，避免泄漏内部细节。
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .errors import ContractError, ErrorCode
from .registry import Registry, get_registry
from .service import GovernanceService

_STATUS_MAP: dict[ErrorCode, int] = {
    ErrorCode.INVALID_CONTRACT: 400,
    ErrorCode.DUPLICATE_CONTRACT: 409,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.REVOKED: 410,
    ErrorCode.VERSION_NOT_NEWER: 422,
    ErrorCode.REVISION_CONFLICT: 409,
    ErrorCode.VERSION_MISMATCH: 422,
    ErrorCode.VERSION_NOT_ADJACENT: 422,
    ErrorCode.LIMIT_EXCEEDED: 413,
    ErrorCode.INTERNAL: 500,
}


def create_app(registry: Registry | None = None) -> FastAPI:
    registry = registry or get_registry()
    service = GovernanceService(registry)
    app = FastAPI(title="API 契约治理服务", version="0.1.0")

    @app.exception_handler(ContractError)
    async def _handle_contract_error(_request: Request, exc: ContractError):
        body: dict = {"code": exc.code.value, "message": exc.message}
        # 详情暴露程度受配置控制，默认不回传内部 details
        if registry._limits.expose_internal_details and exc.details:
            body["details"] = exc.details
        return JSONResponse(
            status_code=_STATUS_MAP.get(exc.code, 500),
            content={"error": body},
        )

    @app.post("/contracts", status_code=201)
    def register_contract(payload: dict):
        return service.register_contract(payload)

    @app.get("/contracts/{contract_id}/versions/{version}")
    def get_contract(contract_id: str, version: str):
        return service.get_contract(contract_id, version)

    @app.put("/contracts/{contract_id}/versions/{version}")
    def update_contract(contract_id: str, version: str, body: dict):
        return service.update_contract(contract_id, version, body)

    @app.post("/contracts/{contract_id}/versions/{version}/revoke")
    def revoke_contract(contract_id: str, version: str, body: dict = None):
        return service.revoke_contract(contract_id, version, body or {})

    @app.post("/contracts/{contract_id}/versions/{version}/validate")
    def validate_call(contract_id: str, version: str, body: dict = None):
        return service.validate_call(contract_id, version, body or {})

    @app.get("/contracts/{contract_id}/compatibility")
    def check_compatibility(contract_id: str, from_version: str, to_version: str):
        return service.check_compatibility(contract_id, from_version, to_version)

    return app


app = create_app()
