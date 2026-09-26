"""服务端业务模块。"""

from __future__ import annotations

from fastapi import FastAPI

from .delegation_routers import router as delegation_router
from .routers import router

app = FastAPI(
    title="Practice Hours Guard",
    version="0.1.0",
    description=(
        "Event-sourced practice-hours compliance service. Check-ins, mentor "
        "confirmations and leave corrections are append-only; compliance is "
        "derived by replay and can be frozen into an immutable snapshot. "
        "Internship confirmations support time-boxed, non-circular delegation "
        "chains with audited actor/responsibility/grant-version provenance."
    ),
)

app.include_router(router)
app.include_router(delegation_router)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}
