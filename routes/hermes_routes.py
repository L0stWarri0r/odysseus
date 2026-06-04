import os

from fastapi import APIRouter, HTTPException, Request

from src.hermes_control import HermesRequestContext, evaluate
from src.hermes_control.continuity import build_continuity_inventory
from src.hermes_control.maintenance import build_maintenance_status


def _require_admin(request: Request) -> None:
    """Route-local admin gate that avoids importing core.middleware in tests.

    app.py stamps trusted loopback tool calls as current_user="internal-tool";
    cookie sessions carry the actual username; bearer API tokens carry
    current_user="api" and are intentionally not admin here.
    """
    if os.getenv("AUTH_ENABLED", "true").lower() == "false":
        return
    if getattr(request.state, "current_user", None) == "internal-tool":
        return

    auth_mgr = getattr(request.app.state, "auth_manager", None)
    if not auth_mgr or not getattr(auth_mgr, "is_configured", False):
        raise HTTPException(403, "Admin only")
    user = getattr(request.state, "current_user", None)
    if not user or not auth_mgr.is_admin(user):
        raise HTTPException(403, "Admin only")


def setup_hermes_routes() -> APIRouter:
    router = APIRouter(tags=["hermes"])

    @router.post("/api/hermes/preflight")
    async def hermes_preflight(context: HermesRequestContext):
        return evaluate(context).model_dump(mode="json")

    @router.get("/api/hermes/continuity/inventory")
    async def hermes_continuity_inventory():
        return build_continuity_inventory()

    @router.get("/api/hermes/maintenance/status")
    async def hermes_maintenance_status(request: Request):
        _require_admin(request)
        return build_maintenance_status()

    return router
