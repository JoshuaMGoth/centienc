"""CentienC — Main FastAPI application."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import ipaddress
import json
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import asyncssh
import httpx
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
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
from .reports import build_report_data, export_csv, export_html, export_pdf_html

logger = logging.getLogger("centient.app")

# ── GitHub Updates ─────────────────────────────────────────────────────────────
# Set GITHUB_TOKEN to a PAT with repo scope so /api/updates can fetch releases
# from a private repo.  Not required for public repos.
_GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "")


STATIC_DIR = Path(__file__).parent / "static"
TEMPLATE_DIR = Path(__file__).parent / "templates"

# ═══════════════════════════════════════════════════════════════════
#  Globals (set during lifespan)
# ═══════════════════════════════════════════════════════════════════
db: Database | None = None
engine: MonitorEngine | None = None

# WebSocket connection manager
_ws_clients: set[WebSocket] = set()
_ws_broadcast_task: asyncio.Task | None = None
_va_cleanup_task: asyncio.Task | None = None


async def _build_overview_payload() -> dict[str, Any]:
    """Build the dashboard payload consistently for HTTP and WebSocket clients."""
    if engine is None or db is None:
        return {
            "servers": [],
            "services": [],
            "proxmox_nodes": [],
            "containers": [],
            "vms": [],
            "incidents": [],
            "settings": {
                "app_title": "CentienC",
                "theme": "dark",
                "auth_enabled": "false",
                "check_interval": "60",
            },
        }

    monitor_engine = engine
    database = db

    overview = monitor_engine.get_overview()
    settings = await database.get_all_settings()
    incidents = await database.get_recent_incidents(10)

    # Merge DB-sourced targets with cache so newly-added items appear immediately
    # (before the first monitoring cycle populates status_cache)
    for ttype, list_fn in [("server", database.list_servers),
                           ("service", database.list_services),
                           ("website", database.list_websites)]:
        key = ttype + "s" if ttype != "website" else "websites"
        cache_ids = {t["id"] for t in overview.get(key, [])}
        db_items = await list_fn()
        for item in db_items:
            if item["id"] not in cache_ids:
                cache = monitor_engine.status_cache.get(f"{ttype}:{item['id']}", {})
                item["status"] = cache.get("status", "unknown")
                item["response_time"] = cache.get("response_time")
                item["last_check"] = cache.get("last_check")
                item["host"] = item.get("ip_address") or item.get("hostname") or item.get("url", "")
                overview[key].append(item)

    db_pve_nodes = await database.list_proxmox_nodes()
    cache_ids = {n["id"] for n in overview.get("proxmox_nodes", [])}
    for node in db_pve_nodes:
        if node["id"] not in cache_ids:
            cache = monitor_engine.status_cache.get(f"proxmox:{node['id']}", {})
            node["live_status"] = cache.get("status", "unknown")
            node["containers"] = cache.get("containers", [])
            node["vms"] = cache.get("vms", [])
            node["node_status"] = cache.get("node_status", {})
            node["last_update"] = cache.get("last_update")
            node["error"] = cache.get("error")
            overview["proxmox_nodes"].append(node)

    return {
        **overview,
        "settings": {
            "app_title": settings.get("app_title", "CentienC"),
            "theme": settings.get("theme", "dark"),
            "auth_enabled": settings.get("auth_enabled", "false"),
            "check_interval": settings.get("check_interval", "60"),
        },
        "incidents": incidents,
        "license": {"mode": "open_source", "pro_features": True},
    }


async def _va_cleanup_loop() -> None:
    """Purge visitor-analytics data older than the retention window, hourly."""
    while True:
        try:
            await asyncio.sleep(3600)
            if db is not None:
                removed = await db.va_cleanup(90)
                if removed:
                    logger.info("VA cleanup removed %d rows", removed)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.debug("VA cleanup error: %s", exc)


async def _ws_broadcast_loop() -> None:
    """Periodically push overview data to all connected WebSocket clients."""
    while True:
        try:
            await asyncio.sleep(5)
            if not _ws_clients or engine is None or db is None:
                continue
            overview = await _build_overview_payload()
            payload = json.dumps({
                "type": "overview",
                **overview,
            }, default=str)
            dead: set[WebSocket] = set()
            for ws in _ws_clients.copy():
                try:
                    await ws.send_text(payload)
                except Exception:
                    dead.add(ws)
            _ws_clients.difference_update(dead)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("WS broadcast error: %s", e)


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

    # Ensure there is always at least one admin user (default: admin / changeme).
    # This runs on every startup so that headless installs and migrated databases
    # always have a usable login without going through the setup wizard.
    existing_users = await db.list_users()
    if not existing_users:
        default_hash = hash_password("changeme")
        await db.create_user("admin", default_hash, "admin")
        await db.update_settings({
            "auth_enabled": "true",
            "setup_complete": "true",
        })
        logger.info("Created default admin account (username: admin, password: changeme). Change this immediately.")

    engine = MonitorEngine(db)
    await engine.start()

    global _ws_broadcast_task, _va_cleanup_task
    _ws_broadcast_task = asyncio.create_task(_ws_broadcast_loop())
    _va_cleanup_task = asyncio.create_task(_va_cleanup_loop())

    logger.info("CentienC v%s started", __version__)
    yield
    if _ws_broadcast_task:
        _ws_broadcast_task.cancel()
    if _va_cleanup_task:
        _va_cleanup_task.cancel()
    await engine.stop()
    logger.info("CentienC shut down")


app = FastAPI(title="CentienC", version=__version__, lifespan=lifespan)

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


async def _require_ws_auth(websocket: WebSocket) -> dict[str, Any] | None:
    """Validate auth for websocket routes when auth is enabled."""
    auth_enabled = await db.get_setting("auth_enabled", "false")
    if auth_enabled != "true":
        return {"sub": 0, "username": "admin", "role": "admin"}

    token = websocket.query_params.get("token")
    if not token:
        token = websocket.cookies.get("centient_token")
    if not token:
        return None
    return decode_token(token)


async def _fetch_vulnerability_summary(settings: dict[str, Any] | None = None) -> dict[str, int | str]:
    """Fetch vulnerability summary from a Jarvis API endpoint.

    This is best-effort and always returns a stable result shape.
    """
    settings = settings or {}
    base_candidates = [
        settings.get("jarvis_api_base_url", ""),
        os.getenv("JARVIS_API_BASE_URL", ""),
        "http://127.0.0.1:8787",
    ]

    default = {
        "total": 0,
        "active": 0,
        "critical": 0,
        "high": 0,
        "unreviewed": 0,
        "acknowledged": 0,
        "dismissed": 0,
        "fixed": 0,
        "source": "unavailable",
    }

    async with httpx.AsyncClient(timeout=3.0) as client:
        for base in base_candidates:
            if not base:
                continue
            url = f"{base.rstrip('/')}/api/deps/vulnerabilities"
            try:
                resp = await client.get(url)
                if resp.status_code != 200:
                    continue
                payload = resp.json()
                summary = payload.get("summary") or {}
                total = int(summary.get("total", 0) or 0)
                fixed = int(summary.get("fixed", 0) or 0)
                dismissed = int(summary.get("dismissed", 0) or 0)
                active = max(total - fixed - dismissed, 0)
                return {
                    "total": total,
                    "active": active,
                    "critical": int(summary.get("critical", 0) or 0),
                    "high": int(summary.get("high", 0) or 0),
                    "unreviewed": int(summary.get("unreviewed", 0) or 0),
                    "acknowledged": int(summary.get("acknowledged", 0) or 0),
                    "dismissed": dismissed,
                    "fixed": fixed,
                    "source": base,
                }
            except Exception:
                continue

    return default


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
    auth_enabled = await db.get_setting("auth_enabled", "false")
    if auth_enabled != "true":
        return RedirectResponse("/")
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


@app.get("/map", response_class=HTMLResponse)
async def map_page(request: Request):
    setup_done = await db.get_setting("setup_complete", "false")
    if setup_done != "true":
        return RedirectResponse("/setup")
    auth_enabled = await db.get_setting("auth_enabled", "false")
    if auth_enabled == "true":
        user = await _require_auth(request)
        if user is None:
            return RedirectResponse("/login")
    return _serve_template("map.html")


@app.get("/tv", response_class=HTMLResponse)
async def tv_page(request: Request):
    setup_done = await db.get_setting("setup_complete", "false")
    if setup_done != "true":
        return RedirectResponse("/setup")
    auth_enabled = await db.get_setting("auth_enabled", "false")
    if auth_enabled == "true":
        user = await _require_auth(request)
        if user is None:
            return RedirectResponse("/login")
    return _serve_template("tv.html")


@app.get("/analytics", response_class=HTMLResponse)
async def analytics_page(request: Request):
    # Analytics page removed — all analytics are now consolidated in Reports
    return RedirectResponse("/reports")


@app.get("/reports", response_class=HTMLResponse)
async def reports_page(request: Request):
    setup_done = await db.get_setting("setup_complete", "false")
    if setup_done != "true":
        return RedirectResponse("/setup")
    auth_enabled = await db.get_setting("auth_enabled", "false")
    if auth_enabled == "true":
        user = await _require_auth(request)
        if user is None:
            return RedirectResponse("/login")
    return _serve_template("reports.html")


@app.get("/troubleshoot", response_class=HTMLResponse)
async def troubleshoot_page(request: Request):
    setup_done = await db.get_setting("setup_complete", "false")
    if setup_done != "true":
        return RedirectResponse("/setup")
    auth_enabled = await db.get_setting("auth_enabled", "false")
    if auth_enabled == "true":
        user = await _require_auth(request)
        if user is None:
            return RedirectResponse("/login")
    return _serve_template("troubleshoot.html")


@app.get("/updates", response_class=HTMLResponse)
async def updates_page(request: Request):
    setup_done = await db.get_setting("setup_complete", "false")
    if setup_done != "true":
        return RedirectResponse("/setup")
    auth_enabled = await db.get_setting("auth_enabled", "false")
    if auth_enabled == "true":
        user = await _require_auth(request)
        if user is None:
            return RedirectResponse("/login")
    return _serve_template("updates.html")


@app.get("/visitors", response_class=HTMLResponse)
async def visitors_page(request: Request):
    setup_done = await db.get_setting("setup_complete", "false")
    if setup_done != "true":
        return RedirectResponse("/setup")
    auth_enabled = await db.get_setting("auth_enabled", "false")
    if auth_enabled == "true":
        user = await _require_auth(request)
        if user is None:
            return RedirectResponse("/login")
    return _serve_template("visitors.html")


# ═══════════════════════════════════════════════════════════════════
#  WebSocket endpoint
# ═══════════════════════════════════════════════════════════════════

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_clients.add(ws)
    try:
        # Send initial data immediately
        if engine and db:
            overview = await _build_overview_payload()
            await ws.send_text(json.dumps({
                "type": "overview",
                **overview,
            }, default=str))
        # Keep alive — client can send pings
        while True:
            msg = await ws.receive_text()
            if msg == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        _ws_clients.discard(ws)


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
    title = body.get("title", "CentienC")
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
    overview = await _build_overview_payload()
    return {
        "ok": True,
        **overview,
    }


# ═══════════════════════════════════════════════════════════════════
#  Server CRUD API
# ═══════════════════════════════════════════════════════════════════

def _sanitize_server(server: dict) -> dict:
    sanitized = dict(server)
    sanitized.pop("ssh_password", None)
    sanitized.pop("sudo_password", None)
    return sanitized


def _sanitize_proxmox_node(node: dict) -> dict:
    sanitized = dict(node)
    token_secret = sanitized.pop("token_secret", None)
    ssh_password = sanitized.pop("ssh_password", None)
    sanitized["has_token_secret"] = bool(token_secret)
    sanitized["has_ssh_password"] = bool(ssh_password)
    return sanitized

@app.get("/api/servers")
async def list_servers(request: Request):
    await _require_auth_or_401(request)
    servers = await db.list_servers()
    # Enrich with live status from cache
    for s in servers:
        cache = engine.status_cache.get(f"server:{s['id']}", {})
        s["live_status"] = cache.get("status", "unknown")
        s["response_time"] = cache.get("response_time")
    return {"ok": True, "servers": [_sanitize_server(s) for s in servers]}


@app.post("/api/servers")
async def add_server(request: Request):
    await _require_auth_or_401(request)
    body = await request.json()
    allowed = {"name", "hostname", "ip_address", "port", "type",
               "check_interval", "enabled",
               "ssh_user", "ssh_port", "ssh_key_path", "ssh_password",
               "sudo_password", "monitor_flags"}
    data = {k: v for k, v in body.items() if k in allowed}
    if not data.get("name") or not data.get("hostname"):
        raise HTTPException(400, "name and hostname are required")
    # Serialize monitor_flags dict to JSON string
    if "monitor_flags" in data and isinstance(data["monitor_flags"], dict):
        data["monitor_flags"] = json.dumps(data["monitor_flags"])
    sid = await db.add_server(data)

    # Auto-discover nginx sites if server has SSH credentials
    discovered_sites = []
    if data.get("ssh_user"):
        try:
            server = await db.get_server(sid)
            if server:
                sites = await engine.discover_nginx_sites(server)
                # Get existing website URLs to avoid duplicates
                existing = {w["url"].rstrip("/").lower() for w in await db.list_websites()}
                for site in sites:
                    if site["url"].rstrip("/").lower() not in existing:
                        wid = await db.add_website({
                            "name": site["name"],
                            "url": site["url"],
                            "server_id": sid,
                            "enabled": 1,
                        })
                        discovered_sites.append({"id": wid, **site})
        except Exception as e:
            logger.warning("Auto-discovery failed for server %d: %s", sid, e)

    return {"ok": True, "id": sid, "discovered_sites": discovered_sites}


@app.put("/api/servers/{server_id}")
async def update_server(server_id: int, request: Request):
    await _require_auth_or_401(request)
    body = await request.json()
    allowed = {"name", "hostname", "ip_address", "port", "type",
               "check_interval", "enabled",
               "ssh_user", "ssh_port", "ssh_key_path", "ssh_password",
               "sudo_password", "monitor_flags"}
    data = {k: v for k, v in body.items() if k in allowed}
    if "monitor_flags" in data and isinstance(data["monitor_flags"], dict):
        data["monitor_flags"] = json.dumps(data["monitor_flags"])
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


@app.post("/api/servers/{server_id}/test-ssh")
async def test_ssh_connection(server_id: int, request: Request):
    """Test SSH connectivity to a server and return connection info or error."""
    await _require_auth_or_401(request)
    servers = await db.list_servers()
    server = next((s for s in servers if s["id"] == server_id), None)
    if not server:
        raise HTTPException(404, "Server not found")
    if not server.get("ssh_user"):
        return {"ok": False, "error": "No SSH credentials configured for this server."}
    try:
        conn = await engine._get_ssh_conn(server)
        result = await conn.run("echo ok && whoami && hostname", check=False)
        return {"ok": True, "output": result.stdout.strip()}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/api/servers/generate-ssh-key")
async def generate_ssh_key(request: Request):
    """Generate an ed25519 SSH keypair for CentienC outgoing SSH connections.

    The keypair is created in the running user's ~/.ssh/ directory if it does
    not already exist.  Returns the public key so the admin can copy it into
    authorized_keys on monitored servers.
    """
    await _require_auth_or_401(request)
    import subprocess  # stdlib — already available

    home = Path.home()
    ssh_dir = home / ".ssh"
    key_path = ssh_dir / "id_ed25519"
    pub_path = ssh_dir / "id_ed25519.pub"

    if not key_path.exists():
        ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        import socket
        comment = f"centienc@{socket.gethostname()}"
        proc = subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", "", "-C", comment],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            return {"ok": False, "error": proc.stderr.strip() or "ssh-keygen failed"}

    if not pub_path.exists():
        return {"ok": False, "error": "Key file not found after generation"}

    public_key = pub_path.read_text().strip()
    return {"ok": True, "public_key": public_key, "key_path": str(key_path)}


@app.get("/api/servers/{server_id}/history")
async def server_history(server_id: int, request: Request):
    await _require_auth_or_401(request)
    hours = int(request.query_params.get("hours", "24"))
    history = await db.get_history("server", server_id, hours=hours)
    uptime = await db.get_uptime("server", server_id, hours=hours)
    return {"ok": True, "history": history, "uptime_pct": round(uptime, 2)}


# ═══════════════════════════════════════════════════════════════════
#  Proxmox Nodes CRUD API
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/proxmox")
async def list_proxmox_nodes(request: Request):
    await _require_auth_or_401(request)
    nodes = await db.list_proxmox_nodes()
    # Enrich with live cache data
    for n in nodes:
        cache = engine.status_cache.get(f"proxmox:{n['id']}", {})
        n["live_status"] = cache.get("status", "unknown")
        n["containers"] = cache.get("containers", [])
        n["vms"] = cache.get("vms", [])
        n["node_status"] = cache.get("node_status", {})
        n["last_update"] = cache.get("last_update")
        n["error"] = cache.get("error")
    return {"ok": True, "nodes": [_sanitize_proxmox_node(n) for n in nodes]}


@app.post("/api/proxmox")
async def add_proxmox_node(request: Request):
    await _require_auth_or_401(request)
    body = await request.json()
    allowed = {"name", "host", "port", "node", "user", "token_id", "token_secret",
               "verify_ssl", "check_interval", "enabled",
               "ssh_port", "ssh_user", "ssh_password", "ssh_key_path"}
    data = {k: v for k, v in body.items() if k in allowed}

    # Smart-parse: if user field contains "!" and token_id is empty,
    # split "user!tokenname" into separate user and token_id fields.
    user_val = str(data.get("user", "")).strip()
    token_id_val = str(data.get("token_id", "")).strip()
    if "!" in user_val and not token_id_val:
        parts = user_val.split("!", 1)
        data["user"] = parts[0]
        data["token_id"] = parts[1]

    required = {"name", "host", "user", "token_id", "token_secret"}
    missing = required - set(data.keys())
    if missing:
        raise HTTPException(400, f"Required fields missing: {', '.join(missing)}")
    nid = await db.add_proxmox_node(data)
    return {"ok": True, "id": nid}


@app.put("/api/proxmox/{node_id}")
async def update_proxmox_node(node_id: int, request: Request):
    await _require_auth_or_401(request)
    body = await request.json()
    allowed = {"name", "host", "port", "node", "user", "token_id", "token_secret",
               "verify_ssl", "check_interval", "enabled",
               "ssh_port", "ssh_user", "ssh_password", "ssh_key_path"}
    data = {k: v for k, v in body.items() if k in allowed}
    if data.get("token_secret") in (None, ""):
        data.pop("token_secret", None)
    if data.get("token_id") in (None, ""):
        data.pop("token_id", None)
    if data.get("ssh_password") in (None, ""):
        data.pop("ssh_password", None)
    ok = await db.update_proxmox_node(node_id, data)
    if not ok:
        raise HTTPException(404, "Proxmox node not found")
    return {"ok": True}


@app.delete("/api/proxmox/{node_id}")
async def delete_proxmox_node(node_id: int, request: Request):
    await _require_auth_or_401(request)
    ok = await db.delete_proxmox_node(node_id)
    if not ok:
        raise HTTPException(404, "Proxmox node not found")
    engine.status_cache.pop(f"proxmox:{node_id}", None)
    return {"ok": True}


@app.post("/api/proxmox/{node_id}/refresh")
async def refresh_proxmox_node(node_id: int, request: Request):
    await _require_auth_or_401(request)
    node = await db.get_proxmox_node(node_id)
    if not node:
        raise HTTPException(404, "Proxmox node not found")
    await engine._poll_proxmox(node)
    cache = engine.status_cache.get(f"proxmox:{node_id}", {})
    return {"ok": True, **cache}


@app.websocket("/api/terminal/{node_id}/{vmid}")
async def terminal_websocket(websocket: WebSocket, node_id: int, vmid: int):
    """Interactive terminal websocket via SSH to Proxmox host and pct/qm shell."""
    user = await _require_ws_auth(websocket)
    if user is None:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    conn: asyncssh.SSHClientConnection | None = None
    process: asyncssh.SSHClientProcess | None = None

    try:
        node = await db.get_proxmox_node(node_id)
        if not node:
            await websocket.send_json({"type": "error", "message": "Proxmox node not found"})
            await websocket.close(code=1008)
            return

        ssh_host = node.get("host")
        ssh_port = int(node.get("ssh_port") or 22)
        ssh_user = node.get("ssh_user") or "root"
        ssh_password = node.get("ssh_password")
        ssh_key_path = node.get("ssh_key_path")

        connect_kwargs: dict[str, Any] = {
            "host": ssh_host,
            "port": ssh_port,
            "username": ssh_user,
            "known_hosts": None,
        }
        if ssh_key_path:
            connect_kwargs["client_keys"] = [ssh_key_path]
        if ssh_password:
            connect_kwargs["password"] = ssh_password

        conn = await asyncssh.connect(**connect_kwargs)

        vm_type = websocket.query_params.get("vm_type", "lxc").lower()
        if vm_type == "qemu":
            command = f"qm terminal {vmid}"
        else:
            command = f"pct enter {vmid}"

        process = await conn.create_process(command, term_type="xterm-256color", term_size=(24, 80))
        await websocket.send_json({"type": "connected", "node_id": node_id, "vmid": vmid, "vm_type": vm_type})

        async def stream_output(reader: asyncssh.SSHReader):
            while not reader.at_eof():
                chunk = await reader.read(4096)
                if not chunk:
                    break
                await websocket.send_json({"type": "output", "data": chunk})

        async def accept_input():
            while True:
                message = await websocket.receive_text()
                try:
                    payload = json.loads(message)
                except Exception:
                    continue
                if payload.get("type") == "input" and process:
                    process.stdin.write(payload.get("data", ""))

        tasks = [
            asyncio.create_task(stream_output(process.stdout)),
            asyncio.create_task(stream_output(process.stderr)),
            asyncio.create_task(accept_input()),
            asyncio.create_task(process.wait()),
        ]

        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()

        exit_status = None
        for task in done:
            try:
                result = task.result()
                if isinstance(result, int):
                    exit_status = result
            except Exception:
                pass

        await websocket.send_json({"type": "exit", "code": exit_status})
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("Terminal websocket error for node=%s vmid=%s: %s", node_id, vmid, exc)
        try:
            await websocket.send_json({"type": "error", "message": str(exc)})
        except Exception:
            pass
    finally:
        try:
            if process:
                process.close()
        except Exception:
            pass
        try:
            if conn:
                conn.close()
                await conn.wait_closed()
        except Exception:
            pass


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
    allowed = {"name", "url", "method", "expected_status", "check_interval", "timeout", "follow_redirects", "verify_ssl", "enabled", "server_id", "log_path"}
    data = {k: v for k, v in body.items() if k in allowed}
    if not data.get("name") or not data.get("url"):
        raise HTTPException(400, "name and url are required")
    # Normalize: null server_id means basic monitor (no SSH)
    if data.get("server_id") in (None, "", 0, "null"):
        data["server_id"] = None
        data["log_path"] = None
    wid = await db.add_website(data)
    return {"ok": True, "id": wid}


@app.put("/api/websites/{website_id}")
async def update_website(website_id: int, request: Request):
    await _require_auth_or_401(request)
    body = await request.json()
    allowed = {"name", "url", "method", "expected_status", "check_interval", "timeout", "follow_redirects", "verify_ssl", "enabled", "server_id", "log_path"}
    data = {k: v for k, v in body.items() if k in allowed}
    if data.get("server_id") in (None, "", 0, "null"):
        data["server_id"] = None
        data["log_path"] = None
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


@app.get("/api/websites/{website_id}/detail")
async def website_detail(website_id: int, request: Request):
    """Drill-down view for a website with SSH log access.

    Returns traffic stats, top pages, status codes, recent requests,
    and server system metrics — similar to the unified monitor drill-down.
    """
    await _require_auth_or_401(request)
    website = await db.get_website(website_id)
    if not website:
        raise HTTPException(404, "Website not found")

    minutes = int(request.query_params.get("minutes", "5"))
    result: dict[str, Any] = {
        "ok": True,
        "website": website,
        "has_logs": False,
        "drilldown_reason": None,
    }

    # Always include the latest website status + basic uptime trend.
    cache = engine.status_cache.get(f"website:{website_id}", {}) if engine else {}
    result["live"] = {
        "status": cache.get("status", "unknown"),
        "response_time": cache.get("response_time"),
        "status_code": cache.get("status_code"),
        "details": cache.get("details"),
        "last_check": cache.get("last_check"),
    }
    uptime_24h = await db.get_uptime("website", website_id, hours=24)
    recent_history = await db.get_history("website", website_id, hours=6)
    result["uptime_pct_24h"] = round(uptime_24h, 2)
    result["recent_checks"] = recent_history[-50:]

    # If website is linked to a server, fetch SSH log data
    server_id = website.get("server_id")
    if not server_id:
        result["drilldown_reason"] = "Website is not linked to a server."
        return result

    servers = await db.list_servers()
    server = next((s for s in servers if s["id"] == server_id), None)
    if not server or not server.get("ssh_user"):
        result["drilldown_reason"] = "Linked server is missing SSH credentials."
        return result

    try:
        detail = await engine.get_site_detail(server, website, minutes=minutes)
        result["has_logs"] = True
        result.update(detail)
    except Exception as exc:
        logger.warning("Site detail failed for website %d: %s", website_id, exc)
        result["drilldown_reason"] = str(exc)
        result["error"] = str(exc)

    return result


@app.post("/api/servers/{server_id}/detect-web-server")
async def detect_web_server(server_id: int, request: Request):
    """Auto-detect web server type and log paths on a server.

    Optional query param ``url`` — used to score the best log-path suggestion
    for a specific website URL (e.g. ``?url=https://joshuagoth.com``).
    """
    await _require_auth_or_401(request)
    url = request.query_params.get("url", "")
    url_host = url.replace("https://", "").replace("http://", "").split("/")[0].lower() if url else ""
    servers = await db.list_servers()
    server = next((s for s in servers if s["id"] == server_id), None)
    if not server:
        raise HTTPException(404, "Server not found")
    if not server.get("ssh_user"):
        raise HTTPException(400, "Server has no SSH credentials")

    try:
        result = await engine.detect_web_server(server, url_host=url_host)
        return {"ok": True, **result}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


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
    safe_keys = {"app_title", "theme", "check_interval", "retention_days",
                 "notifications_enabled", "auth_enabled"}
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
    if str(cur["sub"]) == str(user_id):
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


@app.patch("/api/incidents/{incident_id}/resolve")
async def resolve_incident_by_id(incident_id: int, request: Request):
    await _require_auth_or_401(request)
    resolved = await db.resolve_incident_by_id(incident_id)
    if not resolved:
        return JSONResponse({"ok": False, "error": "Incident not found or already resolved"}, status_code=404)
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════════
#  Push Token Registration (Expo Push Notifications)
# ═══════════════════════════════════════════════════════════════════

@app.post("/api/push-tokens")
async def register_push_token(request: Request):
    """Register an Expo push token for this device."""
    await _require_auth_or_401(request)
    body = await request.json()
    token = body.get("token", "").strip()
    if not token or not token.startswith("ExponentPushToken["):
        raise HTTPException(400, "Valid Expo push token required")
    device_name = body.get("device_name", "")
    platform = body.get("platform", "ios")
    tid = await db.add_push_token(token, device_name, platform)
    return {"ok": True, "id": tid}


@app.delete("/api/push-tokens")
async def unregister_push_token(request: Request):
    """Remove an Expo push token (device unregister)."""
    await _require_auth_or_401(request)
    body = await request.json()
    token = body.get("token", "").strip()
    if not token:
        raise HTTPException(400, "token is required")
    ok = await db.remove_push_token(token)
    return {"ok": ok}


@app.get("/api/push-tokens")
async def list_push_tokens(request: Request):
    """List all registered push tokens."""
    await _require_auth_or_401(request)
    tokens = await db.list_push_tokens()
    return {"ok": True, "tokens": tokens}


@app.post("/api/push-tokens/test")
async def test_push(request: Request):
    """Send a test push to all registered devices."""
    await _require_auth_or_401(request)
    from .notifications import _send_expo_push
    await _send_expo_push(db, "test", "Test Notification", "up", "Push notifications are working!")
    return {"ok": True, "message": "Test push sent to all registered devices"}


# ═══════════════════════════════════════════════════════════════════
#  Web Analytics API
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/analytics")
async def api_analytics(request: Request):
    """Deep nginx log parse for web analytics.

    Query params:
        server_id  (int)           – SSH server to read from
        website_id (int, optional) – auto-selects server and log_path
        days       (int, default 30)
    """
    await _require_auth_or_401(request)
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
    # Annotate top IPs with cached geo data (non-blocking; misses stay bare)
    for entry in result.get("top_ips", []):
        try:
            geo = await db.va_geo_get(entry["name"])
            if geo and geo.get("country"):
                entry["geo"] = {
                    "country": geo.get("country", ""),
                    "city": geo.get("city", ""),
                    "org": geo.get("org", ""),
                }
        except Exception:
            pass
    return {"ok": True, **result}


# ── Reports ──────────────────────────────────────────────────────────────────

@app.get("/api/reports/summary")
async def api_reports_summary(request: Request):
    """Overview report data for all targets."""
    await _require_auth_or_401(request)
    days = max(1, min(int(request.query_params.get("days", "7")), 365))
    data = await build_report_data(db, engine, "overview", days)
    return {"ok": True, **data}


@app.get("/api/reports/detail/{target_type}/{target_id}")
async def api_reports_detail(request: Request, target_type: str, target_id: int):
    """Drillable detail report for a single target."""
    await _require_auth_or_401(request)
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


@app.get("/api/reports/incidents")
async def api_reports_incidents(request: Request):
    """Incident report, optionally filtered by target."""
    await _require_auth_or_401(request)
    days = max(1, min(int(request.query_params.get("days", "7")), 365))
    tt = request.query_params.get("target_type")
    tid_str = request.query_params.get("target_id")
    tid = int(tid_str) if tid_str else None
    if tt and tt not in ("server", "website", "service"):
        raise HTTPException(400, "target_type must be server, website, or service")
    data = await build_report_data(db, engine, "incidents", days, tt, tid)
    return {"ok": True, **data}


@app.get("/api/reports/bans")
async def api_reports_bans(request: Request):
    """Fail2ban report across all servers."""
    await _require_auth_or_401(request)
    days = max(1, min(int(request.query_params.get("days", "7")), 365))
    data = await build_report_data(db, engine, "bans", days)
    return {"ok": True, **data}


@app.get("/api/reports/export")
async def api_reports_export(request: Request):
    """Export a report as CSV, HTML, or PDF."""
    await _require_auth_or_401(request)
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


# ═══════════════════════════════════════════════════════════════════
#  Updates / GitHub releases (no auth — public data)
# ═══════════════════════════════════════════════════════════════════

_updates_cache: dict[str, Any] = {"ts": 0.0, "data": None}
_UPDATES_TTL = 300  # cache for 5 minutes

@app.get("/api/updates")
async def api_updates():
    """Proxy GitHub releases for the /updates page (no auth required)."""
    import time
    now = time.monotonic()
    if _updates_cache["data"] and now - _updates_cache["ts"] < _UPDATES_TTL:
        return _updates_cache["data"]
    try:
        gh_headers: dict[str, str] = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if _GITHUB_TOKEN:
            gh_headers["Authorization"] = f"Bearer {_GITHUB_TOKEN}"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.github.com/repos/JoshuaMGoth/centienc/releases",
                headers=gh_headers,
            )
        if resp.status_code == 404:
            hint = " (repo is private — set GITHUB_TOKEN env var)" if not _GITHUB_TOKEN else ""
            return {"ok": False, "error": f"GitHub returned 404{hint}"}
        if resp.status_code != 200:
            return {"ok": False, "error": f"GitHub returned {resp.status_code}"}
        releases = resp.json()
        result: dict[str, Any] = {
            "ok": True,
            "releases": [
                {
                    "tag_name": r.get("tag_name"),
                    "name": r.get("name"),
                    "published_at": r.get("published_at"),
                    "body": r.get("body"),
                    "html_url": r.get("html_url"),
                    "prerelease": r.get("prerelease", False),
                }
                for r in releases
            ],
        }
        _updates_cache["ts"] = now
        _updates_cache["data"] = result
        return result
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ═══════════════════════════════════════════════════════════════════
#  Visitor Analytics — JS beacon tracker + live visitor data
# ═══════════════════════════════════════════════════════════════════

_TRACK_KINDS = {"pv", "beat", "exit", "evt"}
_track_rate: dict[str, list[float]] = {}
_geo_inflight: set[str] = set()

_VA_UA_BROWSER = [
    ("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"),
    ("Chrome/", "Chrome"), ("Safari/", "Safari"),
]
_VA_UA_OS = [
    ("Windows", "Windows"), ("Android", "Android"), ("iPhone", "iOS"),
    ("iPad", "iOS"), ("Mac OS X", "macOS"), ("Linux", "Linux"),
    ("CrOS", "ChromeOS"),
]
_VA_BOT_RE = re.compile(
    r"bot|spider|crawler|slurp|facebookexternal|preview|headless|phantom|"
    r"selenium|lighthouse|pingdom|uptime|monitor|curl|wget|python-requests|"
    r"go-http|java/|perl/|libwww|nmap|masscan|zgrab|semrush|ahrefs|mj12bot|"
    r"dotbot|blexbot|yandex|baiduspider|facebot|twitterbot|applebot|"
    r"linkedinbot|scaninfo|nuclei|nikto|sqlmap",
    re.IGNORECASE,
)
_VA_MOBILE_RE = re.compile(r"Mobile|Android|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini", re.IGNORECASE)
_VA_TABLET_RE = re.compile(r"iPad|Tablet|PlayBook|Silk", re.IGNORECASE)


def _va_parse_ua(ua: str) -> tuple[str, str, str]:
    """Return (device, browser, os) from a User-Agent string."""
    ua = (ua or "")[:1024]
    if _VA_TABLET_RE.search(ua):
        device = "Tablet"
    elif _VA_MOBILE_RE.search(ua):
        device = "Mobile"
    else:
        device = "Desktop"
    browser = next((name for token, name in _VA_UA_BROWSER if token in ua), "Other")
    os_name = next((name for token, name in _VA_UA_OS if token in ua), "Other")
    return device, browser, os_name


def _va_client_ip(request: Request) -> str:
    """Client IP. Only trusts forwarding headers from loopback proxies."""
    host = request.client.host if request.client else ""
    if host in ("127.0.0.1", "::1"):
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()[:45]
    return host


def _va_rate_ok(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _track_rate.get(ip, []) if now - t < 60]
    if len(hits) >= 240:
        _track_rate[ip] = hits
        return False
    hits.append(now)
    _track_rate[ip] = hits
    if len(_track_rate) > 10000:
        for k in [k for k, v in _track_rate.items() if not v or now - v[-1] > 300]:
            _track_rate.pop(k, None)
    return True


def _va_public_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
        return addr.is_global
    except ValueError:
        return False


async def _va_geo_enrich(ip: str, visitor_id: str, site_id: int) -> None:
    """Fetch + cache geo data for an IP, then stamp it on the visitor/session."""
    if ip in _geo_inflight or not _va_public_ip(ip):
        return
    _geo_inflight.add(ip)
    try:
        cached = await db.va_geo_get(ip)
        if cached is None or not cached.get("fetched_at"):
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    r = await client.get(f"https://ipinfo.io/{ip}/json")
                    data = r.json() if r.status_code == 200 else {}
            except Exception:
                data = {}
            geo = {
                "country": (data.get("country") or "")[:8],
                "country_name": (data.get("country_name") or "")[:80],
                "region": (data.get("region") or "")[:80],
                "city": (data.get("city") or "")[:80],
                "org": (data.get("org") or "")[:120],
            }
            await db.va_geo_put(ip, geo)
            cached = geo
        if cached.get("country") or cached.get("city"):
            await db.va_set_visitor_geo(visitor_id, site_id, cached)
    except Exception as exc:
        logger.debug("VA geo enrich failed for %s: %s", ip, exc)
    finally:
        _geo_inflight.discard(ip)


@app.get("/centience-track.js")
async def tracker_js():
    from fastapi.responses import FileResponse
    return FileResponse(STATIC_DIR / "centience-track.js", media_type="application/javascript")


@app.post("/api/track/{site_key}")
async def track_ingest(site_key: str, request: Request):
    """Public beacon endpoint — receives events from centience-track.js."""
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > 16384:
                raise HTTPException(413, "Payload too large")
        except ValueError:
            pass
    site = await db.va_get_site_by_key(site_key[:64])
    if not site:
        raise HTTPException(404, "Unknown tracking site")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "Invalid payload")

    kind = str(body.get("k", ""))[:8]
    if kind not in _TRACK_KINDS:
        raise HTTPException(400, "Unknown event kind")

    ip = _va_client_ip(request)
    if not _va_rate_ok(ip):
        return {"ok": True}  # silently drop — never punish real visitors

    ua = request.headers.get("user-agent", "")
    if _VA_BOT_RE.search(ua):
        return {"ok": True}

    vid = str(body.get("v", ""))[:64]
    sid = str(body.get("s", ""))[:64]
    if not vid or not sid:
        raise HTTPException(400, "Missing visitor/session id")

    device, browser, os_name = _va_parse_ua(ua)
    site_id = site["id"]
    raw_path = str(body.get("path", "/"))[:1024]
    if not raw_path.startswith("/"):
        raw_path = "/" + raw_path.lstrip("/")
    # Group pages without query strings (UTMs are captured separately)
    path = raw_path.split("?", 1)[0] or "/"

    def _f(key: str, default: float = 0.0) -> float:
        try:
            v = float(body.get(key, default) or 0)
            return max(0.0, min(v, 86400 * 7))
        except (TypeError, ValueError):
            return default

    def _i(key: str) -> int | None:
        try:
            v = int(float(body.get(key, 0) or 0))
            return max(0, min(v, 600000))
        except (TypeError, ValueError):
            return None

    client_ts = body.get("ts")
    try:
        client_ms = float(client_ts)
        if client_ms <= 0 or abs(time.time() * 1000 - client_ms) > 86400000:
            client_ms = 0
    except (TypeError, ValueError):
        client_ms = 0

    visitor_info = {
        "device": device, "browser": browser, "os": os_name,
    }
    await db.va_upsert_visitor(vid, site_id, visitor_info)

    if kind == "pv":
        await db.va_touch_visitor(vid, site_id)
        session_info = {
            "path": path.split("?")[0] or "/",
            "referrer": str(body.get("ref", ""))[:500],
            "source": str(body.get("utm_source", ""))[:120],
            "medium": str(body.get("utm_medium", ""))[:120],
            "campaign": str(body.get("utm_campaign", ""))[:120],
            "device": device, "browser": browser, "os": os_name,
            "screen": str(body.get("screen", ""))[:20],
            "lang": str(body.get("lang", ""))[:20],
        }
        await db.va_upsert_session(sid, site_id, vid, session_info)
        await db.va_add_pageview(
            str(body.get("id") or secrets.token_hex(16)), sid, vid, site_id,
            path, str(body.get("title", ""))[:240], _i("load"),
            _f("eng"), int(_f("scroll")), False,
            str(body.get("ref", ""))[:500],
        )
        # Kick off geo enrichment in the background (cached per-IP)
        asyncio.create_task(_va_geo_enrich(ip, vid, site_id))
    elif kind in ("beat", "exit"):
        eng = _f("eng")
        seng = _f("seng") or eng
        scroll = int(_f("scroll"))
        exited = kind == "exit"
        await db.va_update_session_activity(
            sid, site_id, seng, scroll, path.split("?")[0] or "/", exited,
            path.split("?")[0] or "/" if exited else "",
        )
        await db.va_update_pageview_engagement(
            sid, site_id, path.split("?")[0] or "/", eng, scroll, exited,
        )
    elif kind == "evt":
        name = re.sub(r"[^a-zA-Z0-9_.:-]", "", str(body.get("name", "")))[:80]
        if name:
            await db.va_add_event(
                str(body.get("id") or secrets.token_hex(16)), sid, site_id,
                name, str(body.get("value", ""))[:253],
            )

    return {"ok": True}


def _va_hours(request: Request, default: int = 168) -> int:
    try:
        return max(1, min(int(request.query_params.get("hours", default)), 24 * 365))
    except ValueError:
        return default


def _va_site_param(request: Request) -> int:
    try:
        return int(request.query_params.get("site_id", "0"))
    except ValueError:
        return 0


@app.get("/api/va/sites")
async def va_sites_list(request: Request):
    await _require_auth_or_401(request)
    return {"ok": True, "sites": await db.va_list_sites()}


@app.post("/api/va/sites")
async def va_sites_create(request: Request):
    await _require_auth_or_401(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")
    name = str(body.get("name", "")).strip()[:120]
    domain = str(body.get("domain", "")).strip()[:253]
    if not name or not domain:
        raise HTTPException(400, "name and domain are required")
    site_key = secrets.token_urlsafe(18)
    sid = await db.va_add_site(name, domain, site_key)
    return {"ok": True, "id": sid, "site_key": site_key}


@app.put("/api/va/sites/{site_id}")
async def va_sites_update(site_id: int, request: Request):
    await _require_auth_or_401(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")
    data: dict[str, Any] = {}
    if "name" in body:
        data["name"] = str(body["name"])[:120]
    if "domain" in body:
        data["domain"] = str(body["domain"])[:253]
    if "enabled" in body:
        data["enabled"] = 1 if body["enabled"] else 0
    if "rotate_key" in body and body["rotate_key"]:
        data["site_key"] = secrets.token_urlsafe(18)
    if not data:
        raise HTTPException(400, "Nothing to update")
    await db.va_update_site(site_id, data)
    site = await db.va_get_site(site_id)
    return {"ok": True, "site": site}


@app.delete("/api/va/sites/{site_id}")
async def va_sites_delete(site_id: int, request: Request):
    await _require_auth_or_401(request)
    await db.va_delete_site(site_id)
    return {"ok": True}


@app.get("/api/va/live")
async def va_live(request: Request):
    """Currently-active visitors (heartbeat within the last 90 seconds)."""
    await _require_auth_or_401(request)
    site_id = _va_site_param(request)
    if not site_id:
        raise HTTPException(400, "site_id is required")
    sessions = await db.va_live_sessions(site_id)
    now = time.time()
    out = []
    for s in sessions:
        try:
            from datetime import datetime, timezone
            last = datetime.strptime(
                s["last_seen_at"], "%Y-%m-%dT%H:%M:%S.%fZ"
            ).replace(tzinfo=timezone.utc).timestamp()
        except Exception:
            last = now - 30
        out.append({
            "id": s["id"], "visitor_id": s["visitor_id"],
            "path": s.get("current_path") or s.get("entry_path") or "/",
            "entry_path": s.get("entry_path", ""),
            "started_at": s.get("started_at", ""),
            "last_seen_at": s.get("last_seen_at", ""),
            "seconds_ago": max(0, int(now - last)),
            "page_count": s.get("page_count", 0),
            "engaged_seconds": s.get("engaged_seconds", 0),
            "max_scroll": s.get("max_scroll", 0),
            "country": s.get("country", ""), "city": s.get("city", ""),
            "device": s.get("device", ""), "browser": s.get("browser", ""),
            "os": s.get("os", ""), "screen": s.get("screen", ""),
            "source": s.get("source", ""), "medium": s.get("medium", ""),
            "campaign": s.get("campaign", ""), "referrer": s.get("referrer", ""),
        })
    return {"ok": True, "live": out, "count": len(out)}


@app.get("/api/va/data")
async def va_data(request: Request):
    """Aggregated visitor analytics for a tracked site and period."""
    await _require_auth_or_401(request)
    site_id = _va_site_param(request)
    if not site_id:
        raise HTTPException(400, "site_id is required")
    hours = _va_hours(request)
    summary, pages, sessions = await asyncio.gather(
        db.va_summary(site_id, hours),
        db.va_page_stats(site_id, hours),
        db.va_session_list(site_id, hours, limit=100),
    )
    breakdowns = await asyncio.gather(*[
        db.va_breakdown(site_id, hours, col)
        for col in ("device", "browser", "os", "country", "city", "source", "medium")
    ])
    keys = ("device", "browser", "os", "country", "city", "source", "medium")
    breakdown = dict(zip(keys, breakdowns))
    minutes = await db.va_minute_counts(site_id, minutes=60)
    return {
        "ok": True, "hours": hours, "summary": summary, "pages": pages,
        "sessions": sessions, "breakdowns": breakdown, "minutes": minutes,
    }


@app.post("/api/va/cleanup")
async def va_cleanup(request: Request):
    await _require_auth_or_401(request)
    try:
        body = await request.json()
        days = int(body.get("days", 90))
    except Exception:
        days = 90
    days = max(7, min(days, 365))
    removed = await db.va_cleanup(days)
    return {"ok": True, "removed": removed}


# ═══════════════════════════════════════════════════════════════════
#  Health check (no auth)
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "version": __version__,
        "product": "CentienC",
    }


# ═══════════════════════════════════════════════════════════════════
#  Update Check — compares local version against latest GitHub release
# ═══════════════════════════════════════════════════════════════════

_update_cache: dict[str, Any] = {}

@app.get("/api/update-check")
async def update_check(request: Request = None):
    """Check GitHub for a newer release. Cached for 15 minutes."""
    import time
    from packaging.version import Version

    now = time.time()
    # Allow cache bust with ?fresh=1
    force = False
    if request and request.query_params.get("fresh"):
        force = True
    if not force and _update_cache.get("ts", 0) > now - 900:
        return _update_cache["data"]

    current = __version__
    result = {
        "ok": True,
        "current_version": current,
        "latest_version": current,
        "update_available": False,
        "release_url": "https://github.com/JoshuaMGoth/centienc/releases",
    }

    try:
        import httpx
        uc_headers: dict[str, str] = {"Accept": "application/vnd.github.v3+json"}
        if _GITHUB_TOKEN:
            uc_headers["Authorization"] = f"Bearer {_GITHUB_TOKEN}"
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://api.github.com/repos/JoshuaMGoth/centienc/releases/latest",
                headers=uc_headers,
            )
            if r.status_code == 200:
                data = r.json()
                tag = data.get("tag_name", "").lstrip("v")
                if tag:
                    result["latest_version"] = tag
                    result["release_url"] = data.get("html_url", result["release_url"])
                    try:
                        if Version(tag) > Version(current):
                            result["update_available"] = True
                    except Exception:
                        # Fall back to string comparison
                        if tag != current:
                            result["update_available"] = True
    except Exception as exc:
        logger.debug("Update check failed: %s", exc)

    _update_cache["ts"] = now
    _update_cache["data"] = result
    return result


# ═══════════════════════════════════════════════════════════════════
#  One-Click Updater — upgrade CentienC from GitHub
# ═══════════════════════════════════════════════════════════════════

_update_lock = asyncio.Lock()
_update_progress: dict[str, Any] = {"status": "idle"}


@app.get("/api/update-progress")
async def update_progress():
    """Poll this to see how the update is progressing."""
    return {"ok": True, **_update_progress}


@app.post("/api/update-install")
async def update_install(request: Request):
    """Download & install the latest CentienC release, then restart.

    This runs pip install --upgrade in a subprocess, waits for it
    to finish, then signals the process to restart via systemd.
    """
    global _update_progress

    if _update_lock.locked():
        return {"ok": False, "error": "Update already in progress"}

    async with _update_lock:
        import subprocess
        import sys
        import time

        _update_progress = {"status": "checking", "step": "Checking for updates…"}

        # 1. Verify an update is actually available (force fresh check)
        _update_cache.clear()
        check = await update_check()
        if not check.get("update_available"):
            _update_progress = {"status": "idle"}
            return {"ok": False, "error": "Already running the latest version",
                    "current_version": check.get("current_version")}

        latest = check.get("latest_version", "unknown")
        _update_progress = {"status": "downloading", "step": f"Downloading CentienC v{latest}…",
                            "latest_version": latest}

        # 2. pip install --upgrade from GitHub
        pip_exe = os.path.join(os.path.dirname(sys.executable), "pip")
        if not os.path.exists(pip_exe):
            pip_exe = sys.executable + " -m pip"
        cmd = [
            sys.executable, "-m", "pip", "install", "--upgrade", "--no-cache-dir",
            "centient @ git+https://github.com/JoshuaMGoth/centienc.git",
        ]
        logger.info("Update: running %s", " ".join(cmd))

        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=300,
            )
            if proc.returncode != 0:
                logger.error("pip upgrade failed: %s", proc.stderr)
                _update_progress = {"status": "error", "step": "pip install failed",
                                    "detail": proc.stderr[-500:] if proc.stderr else ""}
                return {"ok": False, "error": "pip install failed",
                        "detail": proc.stderr[-500:] if proc.stderr else ""}
        except subprocess.TimeoutExpired:
            _update_progress = {"status": "error", "step": "pip install timed out"}
            return {"ok": False, "error": "pip install timed out after 5 minutes"}

        _update_progress = {"status": "restarting", "step": f"Installed v{latest} — restarting service…",
                            "latest_version": latest}

        # 3. Clear the update cache so the next check is fresh
        _update_cache.clear()

        # 4. Schedule a graceful restart
        #    Try PM2 first, then systemd, then SIGHUP self-restart
        async def _do_restart():
            await asyncio.sleep(1)  # give time for the response to be sent
            # Try PM2 reload (most common deployment method)
            try:
                for name in ("centienc", "centient"):
                    r = subprocess.run(
                        ["pm2", "reload", name, "--update-env"],
                        capture_output=True, timeout=15,
                    )
                    if r.returncode == 0:
                        return
            except FileNotFoundError:
                pass
            except Exception:
                pass
            # Try systemd
            try:
                for svc in ("centienc", "centient"):
                    r = subprocess.run(
                        ["sudo", "systemctl", "restart", svc],
                        capture_output=True, timeout=10,
                    )
                    if r.returncode == 0:
                        return
            except Exception:
                pass
            # Not running under PM2 or systemd — send SIGHUP to ourselves
            import signal
            os.kill(os.getpid(), signal.SIGHUP)

        asyncio.create_task(_do_restart())

        return {"ok": True, "message": f"Update to v{latest} installed. Restarting…",
                "new_version": latest}


# ═══════════════════════════════════════════════════════════════════
#  IP Geo-Lookup API
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/ip-lookup")
async def ip_lookup_api(request: Request):
    """Return geo/org data for an IP via ipinfo.io."""
    import httpx
    ip = request.query_params.get("ip", "").strip()
    if not ip:
        raise HTTPException(400, "ip parameter required")
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"https://ipinfo.io/{ip}/json")
            data = r.json()
        return {
            "ok": True,
            "ip": data.get("ip", ip),
            "city": data.get("city", ""),
            "region": data.get("region", ""),
            "country": data.get("country", ""),
            "loc": data.get("loc", ""),        # "lat,lng"
            "org": data.get("org", ""),
            "hostname": data.get("hostname", ""),
            "timezone": data.get("timezone", ""),
        }
    except Exception as exc:
        logger.warning("IP lookup failed for %s: %s", ip, exc)
        return {"ok": False, "ip": ip, "error": str(exc)}


@app.get("/ip", response_class=HTMLResponse)
async def ip_lookup(request: Request):
    ip = request.query_params.get("ip", "")
    if ip:
        return RedirectResponse(f"https://ipinfo.io/{ip}")
    return HTMLResponse("<h1>IP lookup requires ?ip= parameter</h1>", status_code=400)
