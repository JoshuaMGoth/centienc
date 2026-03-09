"""¢entient¢ — Main FastAPI application."""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .auth import (
    create_token,
    decode_token,
    get_jwt_secret,
    hash_password,
    set_jwt_secret,
    verify_password,
)
from .database import Database
from .monitors import MonitorEngine
from .notifications import send_notification, test_channel

logger = logging.getLogger("centient.app")

STATIC_DIR = Path(__file__).parent / "static"
TEMPLATE_DIR = Path(__file__).parent / "templates"

# ═══════════════════════════════════════════════════════════════════
#  Globals (set during lifespan)
# ═══════════════════════════════════════════════════════════════════
db: Database | None = None
engine: MonitorEngine | None = None


# ═══════════════════════════════════════════════════════════════════
#  Lifespan
# ═══════════════════════════════════════════════════════════════════
@asynccontextmanager
async def lifespan(application: FastAPI):
    global db, engine
    db = Database()
    await db.init()

    # Load or generate JWT secret from DB
    stored_secret = await db.get_setting("jwt_secret", "")
    if stored_secret:
        set_jwt_secret(stored_secret)
    else:
        secret = get_jwt_secret()
        await db.set_setting("jwt_secret", secret)

    engine = MonitorEngine(db)
    await engine.start()
    logger.info("¢entient¢ v%s started", __version__)
    yield
    await engine.stop()
    logger.info("¢entient¢ shut down")


app = FastAPI(title="¢entient¢", version=__version__, lifespan=lifespan)

# Static files
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ═══════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════

def _serve_template(name: str) -> HTMLResponse:
    """Serve an HTML template file."""
    path = TEMPLATE_DIR / name
    if not path.exists():
        raise HTTPException(404, f"Template not found: {name}")
    return HTMLResponse(path.read_text(encoding="utf-8"))


async def _require_auth(request: Request) -> dict[str, Any] | None:
    """Validate auth if enabled. Returns user payload or None for open access."""
    auth_enabled = await db.get_setting("auth_enabled", "false")
    if auth_enabled != "true":
        return {"sub": 0, "username": "admin", "role": "admin"}

    # Check cookie first, then Authorization header
    token = request.cookies.get("centient_token")
    if not token:
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
    if not token:
        return None

    payload = decode_token(token)
    return payload


async def _require_auth_or_401(request: Request) -> dict[str, Any]:
    """Return user payload or raise 401."""
    user = await _require_auth(request)
    if user is None:
        raise HTTPException(401, "Authentication required")
    return user


# ═══════════════════════════════════════════════════════════════════
#  Page Routes
# ═══════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    setup_done = await db.get_setting("setup_complete", "false")
    if setup_done != "true":
        return RedirectResponse("/setup")
    auth_enabled = await db.get_setting("auth_enabled", "false")
    if auth_enabled == "true":
        user = await _require_auth(request)
        if user is None:
            return RedirectResponse("/login")
    return _serve_template("dashboard.html")


@app.get("/setup", response_class=HTMLResponse)
async def setup_page():
    setup_done = await db.get_setting("setup_complete", "false")
    if setup_done == "true":
        return RedirectResponse("/")
    return _serve_template("setup.html")


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return _serve_template("login.html")


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    setup_done = await db.get_setting("setup_complete", "false")
    if setup_done != "true":
        return RedirectResponse("/setup")
    auth_enabled = await db.get_setting("auth_enabled", "false")
    if auth_enabled == "true":
        user = await _require_auth(request)
        if user is None:
            return RedirectResponse("/login")
    return _serve_template("admin.html")


# ═══════════════════════════════════════════════════════════════════
#  Setup API
# ═══════════════════════════════════════════════════════════════════

@app.post("/api/setup")
async def do_setup(request: Request):
    setup_done = await db.get_setting("setup_complete", "false")
    if setup_done == "true":
        raise HTTPException(400, "Setup already completed")

    body = await request.json()
    mode = body.get("mode", "open")  # "open" or "secured"

    if mode == "secured":
        username = body.get("username", "").strip()
        password = body.get("password", "")
        if not username or not password:
            raise HTTPException(400, "Username and password are required for secured mode")
        pw_hash = hash_password(password)
        await db.create_user(username, pw_hash, "admin")
        await db.set_setting("auth_enabled", "true")
    else:
        await db.set_setting("auth_enabled", "false")

    # Apply optional settings from wizard
    title = body.get("title", "¢entient¢")
    theme = body.get("theme", "dark")
    interval = str(body.get("check_interval", 60))

    await db.update_settings({
        "app_title": title,
        "theme": theme,
        "check_interval": interval,
        "setup_complete": "true",
    })

    result = {"ok": True, "message": "Setup complete", "mode": mode}

    if mode == "secured":
        user = await db.get_user(username)
        token = create_token(user["id"], user["username"], user["role"])
        response = JSONResponse(result)
        response.set_cookie("centient_token", token, httponly=True, samesite="lax", max_age=86400)
        return response

    return result


# ═══════════════════════════════════════════════════════════════════
#  Auth API
# ═══════════════════════════════════════════════════════════════════

@app.post("/api/auth/login")
async def login(request: Request):
    body = await request.json()
    username = body.get("username", "")
    password = body.get("password", "")

    user = await db.get_user(username)
    if not user or not verify_password(password, user["password_hash"]):
        raise HTTPException(401, "Invalid credentials")

    token = create_token(user["id"], user["username"], user["role"])
    response = JSONResponse({"ok": True, "token": token, "username": user["username"]})
    response.set_cookie("centient_token", token, httponly=True, samesite="lax", max_age=86400)
    return response


@app.post("/api/auth/logout")
async def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie("centient_token")
    return response


@app.get("/api/auth/me")
async def auth_me(request: Request):
    user = await _require_auth_or_401(request)
    return {"ok": True, "user": {"id": user["sub"], "username": user["username"], "role": user.get("role", "admin")}}


# ═══════════════════════════════════════════════════════════════════
#  Overview / Dashboard API
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/overview")
async def api_overview(request: Request):
    await _require_auth_or_401(request)
    overview = engine.get_overview()
    settings = await db.get_all_settings()
    incidents = await db.get_recent_incidents(10)
    return {
        "ok": True,
        **overview,
        "settings": {
            "app_title": settings.get("app_title", "¢entient¢"),
            "theme": settings.get("theme", "dark"),
            "auth_enabled": settings.get("auth_enabled", "false"),
            "check_interval": settings.get("check_interval", "60"),
        },
        "incidents": incidents,
    }


# ═══════════════════════════════════════════════════════════════════
#  Server CRUD API
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/servers")
async def list_servers(request: Request):
    await _require_auth_or_401(request)
    servers = await db.list_servers()
    # Enrich with live status from cache
    for s in servers:
        cache = engine.status_cache.get(f"server:{s['id']}", {})
        s["live_status"] = cache.get("status", "unknown")
        s["response_time"] = cache.get("response_time")
    return {"ok": True, "servers": servers}


@app.post("/api/servers")
async def add_server(request: Request):
    await _require_auth_or_401(request)
    body = await request.json()
    allowed = {"name", "hostname", "ip_address", "port", "type", "ssh_user", "ssh_key_path", "check_interval", "enabled"}
    data = {k: v for k, v in body.items() if k in allowed}
    if not data.get("name") or not data.get("hostname"):
        raise HTTPException(400, "name and hostname are required")
    sid = await db.add_server(data)
    return {"ok": True, "id": sid}


@app.put("/api/servers/{server_id}")
async def update_server(server_id: int, request: Request):
    await _require_auth_or_401(request)
    body = await request.json()
    allowed = {"name", "hostname", "ip_address", "port", "type", "ssh_user", "ssh_key_path", "check_interval", "enabled"}
    data = {k: v for k, v in body.items() if k in allowed}
    ok = await db.update_server(server_id, data)
    if not ok:
        raise HTTPException(404, "Server not found")
    return {"ok": True}


@app.delete("/api/servers/{server_id}")
async def delete_server(server_id: int, request: Request):
    await _require_auth_or_401(request)
    ok = await db.delete_server(server_id)
    if not ok:
        raise HTTPException(404, "Server not found")
    # Clean cache
    engine.status_cache.pop(f"server:{server_id}", None)
    return {"ok": True}


@app.post("/api/servers/{server_id}/check")
async def check_server_now(server_id: int, request: Request):
    await _require_auth_or_401(request)
    result = await engine.check_now("server", server_id)
    return {"ok": True, **result}


@app.get("/api/servers/{server_id}/history")
async def server_history(server_id: int, request: Request):
    await _require_auth_or_401(request)
    hours = int(request.query_params.get("hours", "24"))
    history = await db.get_history("server", server_id, hours=hours)
    uptime = await db.get_uptime("server", server_id, hours=hours)
    return {"ok": True, "history": history, "uptime_pct": round(uptime, 2)}


# ═══════════════════════════════════════════════════════════════════
#  Service CRUD API
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/services")
async def list_services(request: Request):
    await _require_auth_or_401(request)
    services = await db.list_services()
    for s in services:
        cache = engine.status_cache.get(f"service:{s['id']}", {})
        s["live_status"] = cache.get("status", "unknown")
        s["response_time"] = cache.get("response_time")
    return {"ok": True, "services": services}


@app.post("/api/services")
async def add_service(request: Request):
    await _require_auth_or_401(request)
    body = await request.json()
    allowed = {"name", "server_id", "type", "host", "port", "check_interval", "timeout", "enabled"}
    data = {k: v for k, v in body.items() if k in allowed}
    if not data.get("name") or not data.get("host") or not data.get("port"):
        raise HTTPException(400, "name, host and port are required")
    sid = await db.add_service(data)
    return {"ok": True, "id": sid}


@app.put("/api/services/{service_id}")
async def update_service(service_id: int, request: Request):
    await _require_auth_or_401(request)
    body = await request.json()
    allowed = {"name", "server_id", "type", "host", "port", "check_interval", "timeout", "enabled"}
    data = {k: v for k, v in body.items() if k in allowed}
    ok = await db.update_service(service_id, data)
    if not ok:
        raise HTTPException(404, "Service not found")
    return {"ok": True}


@app.delete("/api/services/{service_id}")
async def delete_service(service_id: int, request: Request):
    await _require_auth_or_401(request)
    ok = await db.delete_service(service_id)
    if not ok:
        raise HTTPException(404, "Service not found")
    engine.status_cache.pop(f"service:{service_id}", None)
    return {"ok": True}


@app.post("/api/services/{service_id}/check")
async def check_service_now(service_id: int, request: Request):
    await _require_auth_or_401(request)
    result = await engine.check_now("service", service_id)
    return {"ok": True, **result}


@app.get("/api/services/{service_id}/history")
async def service_history(service_id: int, request: Request):
    await _require_auth_or_401(request)
    hours = int(request.query_params.get("hours", "24"))
    history = await db.get_history("service", service_id, hours=hours)
    uptime = await db.get_uptime("service", service_id, hours=hours)
    return {"ok": True, "history": history, "uptime_pct": round(uptime, 2)}


# ═══════════════════════════════════════════════════════════════════
#  Website CRUD API
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/websites")
async def list_websites(request: Request):
    await _require_auth_or_401(request)
    websites = await db.list_websites()
    for w in websites:
        cache = engine.status_cache.get(f"website:{w['id']}", {})
        w["live_status"] = cache.get("status", "unknown")
        w["response_time"] = cache.get("response_time")
        w["status_code"] = cache.get("status_code")
    return {"ok": True, "websites": websites}


@app.post("/api/websites")
async def add_website(request: Request):
    await _require_auth_or_401(request)
    body = await request.json()
    allowed = {"name", "url", "method", "expected_status", "check_interval", "timeout", "follow_redirects", "verify_ssl", "enabled"}
    data = {k: v for k, v in body.items() if k in allowed}
    if not data.get("name") or not data.get("url"):
        raise HTTPException(400, "name and url are required")
    wid = await db.add_website(data)
    return {"ok": True, "id": wid}


@app.put("/api/websites/{website_id}")
async def update_website(website_id: int, request: Request):
    await _require_auth_or_401(request)
    body = await request.json()
    allowed = {"name", "url", "method", "expected_status", "check_interval", "timeout", "follow_redirects", "verify_ssl", "enabled"}
    data = {k: v for k, v in body.items() if k in allowed}
    ok = await db.update_website(website_id, data)
    if not ok:
        raise HTTPException(404, "Website not found")
    return {"ok": True}


@app.delete("/api/websites/{website_id}")
async def delete_website(website_id: int, request: Request):
    await _require_auth_or_401(request)
    ok = await db.delete_website(website_id)
    if not ok:
        raise HTTPException(404, "Website not found")
    engine.status_cache.pop(f"website:{website_id}", None)
    return {"ok": True}


@app.post("/api/websites/{website_id}/check")
async def check_website_now(website_id: int, request: Request):
    await _require_auth_or_401(request)
    result = await engine.check_now("website", website_id)
    return {"ok": True, **result}


@app.get("/api/websites/{website_id}/history")
async def website_history(website_id: int, request: Request):
    await _require_auth_or_401(request)
    hours = int(request.query_params.get("hours", "24"))
    history = await db.get_history("website", website_id, hours=hours)
    uptime = await db.get_uptime("website", website_id, hours=hours)
    return {"ok": True, "history": history, "uptime_pct": round(uptime, 2)}


# ═══════════════════════════════════════════════════════════════════
#  Settings API
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/settings")
async def get_settings(request: Request):
    await _require_auth_or_401(request)
    settings = await db.get_all_settings()
    # Remove sensitive keys
    settings.pop("jwt_secret", None)
    return {"ok": True, "settings": settings}


@app.put("/api/settings")
async def update_settings(request: Request):
    await _require_auth_or_401(request)
    body = await request.json()
    safe_keys = {"app_title", "theme", "check_interval", "retention_days", "notifications_enabled", "auth_enabled"}
    data = {k: str(v) for k, v in body.items() if k in safe_keys}
    await db.update_settings(data)
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════════
#  Users API
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/users")
async def list_users(request: Request):
    user = await _require_auth_or_401(request)
    users = await db.list_users()
    return {"ok": True, "users": users}


@app.post("/api/users")
async def add_user(request: Request):
    await _require_auth_or_401(request)
    body = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "")
    role = body.get("role", "admin")
    if not username or not password:
        raise HTTPException(400, "username and password are required")
    existing = await db.get_user(username)
    if existing:
        raise HTTPException(409, "User already exists")
    pw_hash = hash_password(password)
    uid = await db.create_user(username, pw_hash, role)
    return {"ok": True, "id": uid}


@app.put("/api/users/{user_id}")
async def update_user(user_id: int, request: Request):
    await _require_auth_or_401(request)
    body = await request.json()
    fields = {}
    if "username" in body:
        fields["username"] = body["username"].strip()
    if "password" in body and body["password"]:
        fields["password_hash"] = hash_password(body["password"])
    if "role" in body:
        fields["role"] = body["role"]
    if not fields:
        raise HTTPException(400, "No fields to update")
    ok = await db.update_user(user_id, **fields)
    if not ok:
        raise HTTPException(404, "User not found")
    return {"ok": True}


@app.delete("/api/users/{user_id}")
async def delete_user(user_id: int, request: Request):
    cur = await _require_auth_or_401(request)
    if cur["sub"] == user_id:
        raise HTTPException(400, "Cannot delete yourself")
    ok = await db.delete_user(user_id)
    if not ok:
        raise HTTPException(404, "User not found")
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════════
#  Notification Channels API
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/notifications")
async def list_channels(request: Request):
    await _require_auth_or_401(request)
    channels = await db.list_channels()
    return {"ok": True, "channels": channels}


@app.post("/api/notifications")
async def add_channel(request: Request):
    await _require_auth_or_401(request)
    body = await request.json()
    allowed = {"type", "name", "config", "enabled"}
    data = {k: v for k, v in body.items() if k in allowed}
    if not data.get("type") or not data.get("name") or not data.get("config"):
        raise HTTPException(400, "type, name and config are required")
    cid = await db.add_channel(data)
    return {"ok": True, "id": cid}


@app.put("/api/notifications/{channel_id}")
async def update_channel(channel_id: int, request: Request):
    await _require_auth_or_401(request)
    body = await request.json()
    allowed = {"type", "name", "config", "enabled"}
    data = {k: v for k, v in body.items() if k in allowed}
    ok = await db.update_channel(channel_id, data)
    if not ok:
        raise HTTPException(404, "Channel not found")
    return {"ok": True}


@app.delete("/api/notifications/{channel_id}")
async def delete_channel(channel_id: int, request: Request):
    await _require_auth_or_401(request)
    ok = await db.delete_channel(channel_id)
    if not ok:
        raise HTTPException(404, "Channel not found")
    return {"ok": True}


@app.post("/api/notifications/{channel_id}/test")
async def test_notification(channel_id: int, request: Request):
    await _require_auth_or_401(request)
    channels = await db.list_channels()
    channel = next((c for c in channels if c["id"] == channel_id), None)
    if not channel:
        raise HTTPException(404, "Channel not found")
    result = await test_channel(channel)
    return result


# ═══════════════════════════════════════════════════════════════════
#  Incidents API
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/incidents")
async def list_incidents(request: Request):
    await _require_auth_or_401(request)
    limit = int(request.query_params.get("limit", "50"))
    incidents = await db.get_recent_incidents(limit)
    return {"ok": True, "incidents": incidents}


# ═══════════════════════════════════════════════════════════════════
#  Health check (no auth)
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "version": __version__,
        "product": "¢entient¢",
    }
