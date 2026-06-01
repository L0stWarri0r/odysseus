from fastapi import APIRouter

from src.hermes_control import HermesRequestContext, evaluate


def setup_hermes_routes() -> APIRouter:
    router = APIRouter(tags=["hermes"])

    @router.post("/api/hermes/preflight")
    async def hermes_preflight(context: HermesRequestContext):
        return evaluate(context).model_dump(mode="json")

    return router
