"""GET /v1/metrics: Prometheus exposition format (plan §84).

No separate listener: the bridge is already a long-running FastAPI process,
so its metrics are one more route on the same app.
"""

from fastapi import APIRouter, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from mt5_bridge.api.app_state import get_state

router = APIRouter(prefix="/v1", tags=["metrics"])


@router.get("/metrics")
async def metrics(*, request: Request) -> Response:
    state = get_state(request)
    payload = generate_latest(state.metrics_registry)
    return Response(content=payload, media_type=CONTENT_TYPE_LATEST)
