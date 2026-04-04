"""centienc_pro — CentienC Pro plugin.

Registers analytics and reports API routes when a valid Pro license is active.
Called by centient.app lifespan: register_pro(app, db, engine)

Keep this package in a PRIVATE repository / private wheel index.
Distribute only to customers with a valid CentienC Pro license.

Install alongside centient to unlock Pro features:
    pip install centienc-pro
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

logger = logging.getLogger("centienc_pro")

# Must match the centient version this plugin was built for.
# Bump together whenever centient makes breaking API changes.
_PLUGIN_VERSION = "1.8.0"
_CENTIENT_MIN = "1.8.0"


def register_pro(app=None, db=None, engine=None):
    """Register Pro routes (analytics + reports) into the running CentienC app.

    Called dynamically by centient.app during lifespan startup when a valid
    Pro license is detected. Imports centient utilities here (not at module
    level) to avoid circular imports during package load.
    """
    from centient.app import _require_auth_or_401, _require_pro
    from centient.reports import build_report_data, export_csv, export_html, export_pdf_html

    # Sanity-check version compatibility at load time so mismatches are obvious
    try:
        from centient import __version__ as _core_ver
        from packaging.version import Version
        if Version(_core_ver) < Version(_CENTIENT_MIN):
            logger.warning(
                "centienc_pro %s requires centient>=%s but found %s — "
                "pro features may misbehave. Upgrade centient.",
                _PLUGIN_VERSION, _CENTIENT_MIN, _core_ver,
            )
    except Exception:
        pass

    router = APIRouter()

    # ── Analytics ───────────────────────────────────────────────────────────

    @router.get("/api/analytics")
    async def api_analytics(request: Request):
        """Deep nginx log parse for web analytics.

        Query params:
            server_id  (int)           – SSH server to read from
            website_id (int, optional) – auto-selects server and log_path
            days       (int, default 30)
        """
        await _require_auth_or_401(request)
        await _require_pro(request)
        days = max(1, min(int(request.query_params.get("days", "30")), 365))

        log_path: str | None = None
        website_id_str = request.query_params.get("website_id", "").strip()
        server_id_str = request.query_params.get("server_id", "").strip()

        website: dict | None = None
        if website_id_str:
            try:
                website_id_int = int(website_id_str)
            except ValueError:
                raise HTTPException(400, "website_id must be an integer")
            websites = await db.list_websites()
            website = next((w for w in websites if w["id"] == website_id_int), None)
            if not website:
                raise HTTPException(404, "Website not found")
            lp = website.get("log_path") or ""
            # Validate log_path to prevent path traversal
            if lp and lp.startswith("/") and ".." not in lp and "\x00" not in lp:
                log_path = lp
            if not server_id_str:
                server_id_str = str(website.get("server_id") or "")

        if not server_id_str:
            servers_all = await db.list_servers()
            if len(servers_all) == 1:
                server_id_str = str(servers_all[0]["id"])
            else:
                return {
                    "ok": False,
                    "error": "No server linked to this website. Open Admin → Websites and set a Server, or link an SSH server to enable traffic analytics.",
                    "pages": [], "topics": [], "sections": [], "traffic_by_day": [],
                    "traffic_by_hour": [0] * 24, "status_codes": {}, "top_ips": [],
                    "referrers": [], "devices": {"mobile": 0, "desktop": 0, "bot": 0, "tablet": 0},
                    "totals": {"views": 0, "unique_visitors": 0, "avg_daily": 0, "peak_hour": 0},
                    "traffic_by_weekday": [0] * 7, "heatmap": [[0] * 24 for _ in range(7)],
                    "browsers": [], "traffic_sources": {"search": 0, "social": 0, "direct": 0, "referral": 0},
                    "search_queries": [], "new_vs_returning": {"new": 0, "returning": 0},
                    "trending_pages": [], "page_timelines": {}, "unique_by_day": [],
                    "error_breakdown": {}, "hack_attempts": [], "attacking_ips": [],
                    "scanner_uas": [], "bot_categories": {}, "method_distribution": {},
                    "http_versions": {},
                }

        try:
            server_id_int = int(server_id_str)
        except ValueError:
            raise HTTPException(400, "server_id must be an integer")

        servers = await db.list_servers()
        server = next((s for s in servers if s["id"] == server_id_int), None)
        if not server:
            raise HTTPException(404, "Server not found")

        if not log_path and website:
            url = website.get("url") or ""
            url_host = url.replace("https://", "").replace("http://", "").split("/")[0].split(":")[0].lower()
            try:
                detection = await engine.detect_web_server(server, url_host=url_host)
                suggested = detection.get("suggested_path")
                if suggested and suggested.startswith("/") and ".." not in suggested and "\x00" not in suggested:
                    log_path = suggested
                    await db.update_website(website["id"], {"log_path": suggested})
                    logger.info("Auto-detected log path %s for website %s", suggested, website.get("name"))
            except Exception as exc:
                logger.debug("Log path auto-detection failed for %s: %s", website.get("name"), exc)

        logger.info("ANALYTICS: server=%s log_path=%r website=%s",
                    server.get("name"), log_path, website.get("name") if website else None)
        result = await engine.get_analytics(server, days=days, log_path=log_path)
        return {"ok": True, **result}

    # ── Reports ─────────────────────────────────────────────────────────────

    @router.get("/api/reports/summary")
    async def api_reports_summary(request: Request):
        """Overview report data for all targets."""
        await _require_auth_or_401(request)
        await _require_pro(request)
        days = max(1, min(int(request.query_params.get("days", "7")), 365))
        data = await build_report_data(db, engine, "overview", days)
        return {"ok": True, **data}

    @router.get("/api/reports/detail/{target_type}/{target_id}")
    async def api_reports_detail(request: Request, target_type: str, target_id: int):
        """Drillable detail report for a single target."""
        await _require_auth_or_401(request)
        await _require_pro(request)
        if target_type not in ("server", "website", "service"):
            raise HTTPException(400, "target_type must be server, website, or service")
        days = max(1, min(int(request.query_params.get("days", "7")), 365))

        if target_type == "server":
            targets = {s["id"]: s["name"] for s in await db.list_servers()}
        elif target_type == "website":
            targets = {w["id"]: w["name"] for w in await db.list_websites()}
        else:
            targets = {s["id"]: s["name"] for s in await db.list_services()}
        target_name = targets.get(target_id, f"#{target_id}")

        daily_raw = await db.get_daily_summary(target_type, target_id, days)
        hourly_raw = await db.get_hourly_summary(target_type, target_id, min(days, 3))
        timeline_raw = await db.get_status_timeline(target_type, target_id, days)
        incidents_raw = await db.get_incidents_for_period(days, target_type, target_id)
        incidents = [{**inc, "target_name": target_name} for inc in incidents_raw]

        daily_data = [
            {
                "date": d["day"],
                "uptime_pct": d["uptime_pct"],
                "avg_response": d["avg_response"],
                "min_response": d["min_response"],
                "max_response": d["max_response"],
                "checks": d["checks"],
                "up_checks": d["up_count"],
                "down_checks": d["checks"] - d["up_count"],
            }
            for d in daily_raw
        ]
        hourly_data = [
            {
                "hour": int(h["hour"].split(" ")[1].split(":")[0]),
                "avg_response": h["avg_response"],
                "checks": h["checks"],
            }
            for h in hourly_raw
        ]
        recent_checks = [
            {
                "timestamp": t["checked_at"],
                "status": t["status"],
                "response_time": t["response_time"],
            }
            for t in timeline_raw[:50]
        ]

        total_checks = sum(d["checks"] for d in daily_data)
        up_checks = sum(d["up_checks"] for d in daily_data)
        all_avg = [d["avg_response"] for d in daily_data if d["avg_response"] is not None]
        all_max = [d["max_response"] for d in daily_data if d["max_response"] is not None]
        totals = {
            "uptime_pct": round(up_checks / total_checks * 100, 2) if total_checks else 100.0,
            "total_checks": total_checks,
            "avg_response": round(sum(all_avg) / len(all_avg), 2) if all_avg else None,
            "max_response": max(all_max) if all_max else None,
            "total_incidents": len(incidents),
        }

        return {
            "ok": True,
            "target_type": target_type,
            "target_id": target_id,
            "target_name": target_name,
            "days": days,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "totals": totals,
            "daily_data": daily_data,
            "hourly_data": hourly_data,
            "recent_checks": recent_checks,
            "incidents": incidents,
        }

    @router.get("/api/reports/incidents")
    async def api_reports_incidents(request: Request):
        """Incident report, optionally filtered by target."""
        await _require_auth_or_401(request)
        await _require_pro(request)
        days = max(1, min(int(request.query_params.get("days", "7")), 365))
        tt = request.query_params.get("target_type")
        tid_str = request.query_params.get("target_id")
        tid = int(tid_str) if tid_str else None
        if tt and tt not in ("server", "website", "service"):
            raise HTTPException(400, "target_type must be server, website, or service")
        data = await build_report_data(db, engine, "incidents", days, tt, tid)
        return {"ok": True, **data}

    @router.get("/api/reports/bans")
    async def api_reports_bans(request: Request):
        """Fail2ban report across all servers."""
        await _require_auth_or_401(request)
        await _require_pro(request)
        days = max(1, min(int(request.query_params.get("days", "7")), 365))
        data = await build_report_data(db, engine, "bans", days)
        return {"ok": True, **data}

    @router.get("/api/reports/export")
    async def api_reports_export(request: Request):
        """Export a report as CSV, HTML, or PDF."""
        await _require_auth_or_401(request)
        await _require_pro(request)
        fmt = request.query_params.get("format", "csv").lower()
        if fmt not in ("csv", "html", "pdf"):
            raise HTTPException(400, "format must be csv, html, or pdf")
        report_type = request.query_params.get("report_type", "overview")
        days = max(1, min(int(request.query_params.get("days", "7")), 365))
        tt = request.query_params.get("target_type")
        tid_str = request.query_params.get("target_id")
        tid = int(tid_str) if tid_str else None

        data = await build_report_data(db, engine, report_type, days, tt, tid)
        settings = await db.get_all_settings()
        title = f"{settings.get('app_title', 'CentienC')} — {report_type.title()} Report"

        if fmt == "csv":
            content = export_csv(data)
            return Response(
                content, media_type="text/csv",
                headers={"Content-Disposition": f'attachment; filename="centienc-{report_type}-report.csv"'},
            )
        elif fmt == "html":
            content = export_html(data, title)
            return Response(
                content, media_type="text/html",
                headers={"Content-Disposition": f'attachment; filename="centienc-{report_type}-report.html"'},
            )
        else:  # pdf
            content = export_pdf_html(data, title)
            return HTMLResponse(content)

    app.include_router(router)
    logger.info("centienc_pro v%s: analytics + reports routes registered", _PLUGIN_VERSION)
