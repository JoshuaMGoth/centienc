"""centienc_pro — Private Pro plugin skeleton for CentienC

This package demonstrates a minimal `register_pro(app, db, engine)` hook
that the public CentienC core will import when a valid Pro license is present.

The real Pro package should implement report exporters, additional routes,
and any proprietary analytics. Keep this package private and distribute
via a private wheel or private package index.
"""

from fastapi import APIRouter


def register_pro(app=None, db=None, engine=None):
    """Register Pro functionality into the running CentienC app.

    Called dynamically by CentienC core when a valid license is active.
    """
    router = APIRouter()

    @router.get("/pro/hello")
    async def _hello():
        return {"ok": True, "message": "centienc_pro active"}

    app.include_router(router, prefix="/pro")

    # Example: register a reports exporter or attach to engine/db
    try:
        if engine and hasattr(engine, "register_exporter"):
            engine.register_exporter("pro_csv", lambda data: ("text/csv", ""))
    except Exception:
        pass
