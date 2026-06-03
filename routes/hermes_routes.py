from fastapi import APIRouter

from src.hermes_control import HermesRequestContext, evaluate
from src.hermes_control.continuity import build_continuity_inventory


def setup_hermes_routes() -> APIRouter:
    router = APIRouter(tags=["hermes"])

    @router.post("/api/hermes/preflight")
    async def hermes_preflight(context: HermesRequestContext):
        return evaluate(context).model_dump(mode="json")

    @router.get("/api/hermes/continuity/inventory")
    async def hermes_continuity_inventory():
        return build_continuity_inventory()

    return router
