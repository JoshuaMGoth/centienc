"""CentienC — SQLite database layer with async support."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

import aiosqlite

logger = logging.getLogger("centient.database")

DB_NAME = "centient.db"


def _data_dir() -> str:
    return os.environ.get("CENTIENT_DATA_DIR", os.path.expanduser("~/.centient"))


def db_path() -> str:
    return os.path.join(_data_dir(), DB_NAME)


# ═══════════════════════════════════════════════════════════════════
#  SCHEMA
# ═══════════════════════════════════════════════════════════════════

SCHEMA = """
-- Global settings (key-value store)
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Admin users
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT DEFAULT 'admin',
    created_at    TEXT DEFAULT (datetime('now'))
);

-- Monitored servers
CREATE TABLE IF NOT EXISTS servers (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    hostname       TEXT NOT NULL,
    ip_address     TEXT,
    port           INTEGER DEFAULT 22,
    type           TEXT DEFAULT 'linux',
    check_interval INTEGER DEFAULT 60,
    enabled        INTEGER DEFAULT 1,
    created_at     TEXT DEFAULT (datetime('now'))
);

-- Proxmox nodes (API integration)
CREATE TABLE IF NOT EXISTS proxmox_nodes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    host           TEXT NOT NULL,
    port           INTEGER DEFAULT 8006,
    node           TEXT DEFAULT 'pve',
    user           TEXT NOT NULL,
    token_id       TEXT NOT NULL,
    token_secret   TEXT NOT NULL,
    ssh_port       INTEGER DEFAULT 22,
    ssh_user       TEXT DEFAULT 'root',
    ssh_password   TEXT,
    ssh_key_path   TEXT,
    verify_ssl     INTEGER DEFAULT 0,
    check_interval INTEGER DEFAULT 60,
    enabled        INTEGER DEFAULT 1,
    created_at     TEXT DEFAULT (datetime('now'))
);

-- Monitored services (port checks)
CREATE TABLE IF NOT EXISTS services (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id      INTEGER REFERENCES servers(id) ON DELETE CASCADE,
    name           TEXT NOT NULL,
    type           TEXT NOT NULL DEFAULT 'tcp',
    host           TEXT NOT NULL,
    port           INTEGER NOT NULL,
    check_interval INTEGER DEFAULT 60,
    timeout        INTEGER DEFAULT 10,
    enabled        INTEGER DEFAULT 1,
    created_at     TEXT DEFAULT (datetime('now'))
);

-- Monitored websites (HTTP checks)
CREATE TABLE IF NOT EXISTS websites (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    url             TEXT NOT NULL,
    method          TEXT DEFAULT 'GET',
    expected_status INTEGER DEFAULT 200,
    check_interval  INTEGER DEFAULT 60,
    timeout         INTEGER DEFAULT 15,
    follow_redirects INTEGER DEFAULT 1,
    verify_ssl      INTEGER DEFAULT 1,
    enabled         INTEGER DEFAULT 1,
    created_at      TEXT DEFAULT (datetime('now'))
);

-- Monitoring check results (historical data)
CREATE TABLE IF NOT EXISTS check_results (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    target_type   TEXT NOT NULL,
    target_id     INTEGER NOT NULL,
    status        TEXT NOT NULL,
    response_time REAL,
    status_code   INTEGER,
    details       TEXT,
    checked_at    TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_results_target ON check_results(target_type, target_id, checked_at);
CREATE INDEX IF NOT EXISTS idx_results_time   ON check_results(checked_at);

-- Notification channels
CREATE TABLE IF NOT EXISTS notification_channels (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    type       TEXT NOT NULL,
    name       TEXT NOT NULL,
    config     TEXT NOT NULL,
    enabled    INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Incidents (downtime tracking)
CREATE TABLE IF NOT EXISTS incidents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    target_type TEXT NOT NULL,
    target_id   INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',
    started_at  TEXT DEFAULT (datetime('now')),
    resolved_at TEXT,
    duration    INTEGER,
    notified    INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_incidents_target ON incidents(target_type, target_id, status);

-- Push notification tokens (Expo push)
CREATE TABLE IF NOT EXISTS push_tokens (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    token      TEXT UNIQUE NOT NULL,
    device_name TEXT,
    platform   TEXT DEFAULT 'ios',
    created_at TEXT DEFAULT (datetime('now'))
);

-- ═══════════════════════════════════════════════════════════════════
--  Visitor Analytics (Centience tracker)
-- ═══════════════════════════════════════════════════════════════════

-- Tracked sites (each gets a public site_key for the JS beacon)
CREATE TABLE IF NOT EXISTS va_sites (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    domain     TEXT NOT NULL,
    site_key   TEXT UNIQUE NOT NULL,
    enabled    INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Anonymous visitors (no PII — keyed by client-generated UUID)
CREATE TABLE IF NOT EXISTS va_visitors (
    id          TEXT PRIMARY KEY,
    site_id     INTEGER NOT NULL,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    country     TEXT DEFAULT '',
    region      TEXT DEFAULT '',
    city        TEXT DEFAULT '',
    org         TEXT DEFAULT '',
    device      TEXT DEFAULT '',
    browser     TEXT DEFAULT '',
    os          TEXT DEFAULT '',
    visits      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_va_visitors_site ON va_visitors(site_id, last_seen);

-- Sessions (30 min inactivity window, managed by the tracker)
CREATE TABLE IF NOT EXISTS va_sessions (
    id               TEXT PRIMARY KEY,
    site_id          INTEGER NOT NULL,
    visitor_id       TEXT NOT NULL,
    started_at       TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL,
    entry_path       TEXT DEFAULT '',
    current_path     TEXT DEFAULT '',
    exit_path        TEXT DEFAULT '',
    page_count       INTEGER DEFAULT 0,
    engaged_seconds  REAL DEFAULT 0,
    max_scroll       INTEGER DEFAULT 0,
    referrer         TEXT DEFAULT '',
    source           TEXT DEFAULT '',
    medium           TEXT DEFAULT '',
    campaign         TEXT DEFAULT '',
    country          TEXT DEFAULT '',
    city             TEXT DEFAULT '',
    device           TEXT DEFAULT '',
    browser          TEXT DEFAULT '',
    os               TEXT DEFAULT '',
    screen           TEXT DEFAULT '',
    lang             TEXT DEFAULT '',
    ended            INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_va_sessions_site ON va_sessions(site_id, last_seen_at);
CREATE INDEX IF NOT EXISTS idx_va_sessions_visitor ON va_sessions(visitor_id);

-- Individual page views
CREATE TABLE IF NOT EXISTS va_pageviews (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    visitor_id      TEXT NOT NULL,
    site_id         INTEGER NOT NULL,
    path            TEXT NOT NULL,
    title           TEXT DEFAULT '',
    ts              TEXT NOT NULL,
    load_ms         INTEGER,
    engaged_seconds REAL DEFAULT 0,
    scroll_depth    INTEGER DEFAULT 0,
    exited          INTEGER DEFAULT 0,
    referrer        TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_va_pv_site_ts ON va_pageviews(site_id, ts);
CREATE INDEX IF NOT EXISTS idx_va_pv_session ON va_pageviews(session_id);

-- Custom events fired by sites via the tracker
CREATE TABLE IF NOT EXISTS va_events (
    id         TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    site_id    INTEGER NOT NULL,
    name       TEXT NOT NULL,
    value      TEXT DEFAULT '',
    ts         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_va_events_site ON va_events(site_id, ts);

-- IP → geo cache (ipinfo.io lookups are cached here to stay within rate limits)
CREATE TABLE IF NOT EXISTS va_geo_cache (
    ip         TEXT PRIMARY KEY,
    country    TEXT DEFAULT '',
    country_name TEXT DEFAULT '',
    region     TEXT DEFAULT '',
    city       TEXT DEFAULT '',
    org        TEXT DEFAULT '',
    fetched_at TEXT NOT NULL
);

-- Public status pages (read-only uptime views, no analytics data)
CREATE TABLE IF NOT EXISTS status_pages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    slug       TEXT UNIQUE NOT NULL,
    title      TEXT NOT NULL,
    targets    TEXT NOT NULL DEFAULT '[]',
    created_at TEXT DEFAULT (datetime('now'))
);
"""


# ═══════════════════════════════════════════════════════════════════
#  DATABASE CLASS
# ═══════════════════════════════════════════════════════════════════

class Database:
    """Async SQLite database wrapper for CentienC."""

    def __init__(self, path: str | None = None):
        self.path = path or db_path()
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)

    async def init(self) -> None:
        """Create tables and run migrations."""
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.executescript(SCHEMA)
            await conn.commit()

            # ── Migrations (additive column additions) ──────────────
            def _has_column(columns: list[str], name: str) -> bool:
                return any(c["name"] == name for c in columns)

            # servers: migration for SSH columns
            cur = await conn.execute("PRAGMA table_info(servers)")
            server_cols = [dict(r) for r in await cur.fetchall()]
            for col, default in [
                ("ssh_user", "'root'"),
                ("ssh_port", "22"),
                ("ssh_key_path", "NULL"),
                ("ssh_password", "NULL"),
                ("sudo_password", "NULL"),
                ("monitor_flags", "'{\"system\":true,\"nginx\":false,\"pm2\":false,\"services\":false,\"fail2ban\":false}'"),
            ]:
                if not _has_column(server_cols, col):
                    await conn.execute(f"ALTER TABLE servers ADD COLUMN {col} DEFAULT {default}")
            # Drop agent columns if they exist (safe — just ignore if not present)
            await conn.commit()

            # websites: migration for server linkage columns
            cur = await conn.execute("PRAGMA table_info(websites)")
            website_cols = [dict(r) for r in await cur.fetchall()]
            for col, default in [
                ("server_id", "NULL"),
                ("log_path", "NULL"),
            ]:
                if not _has_column(website_cols, col):
                    await conn.execute(f"ALTER TABLE websites ADD COLUMN {col} DEFAULT {default}")
            await conn.commit()

            # proxmox_nodes: migration for SSH access columns used by terminal feature
            cur = await conn.execute("PRAGMA table_info(proxmox_nodes)")
            proxmox_cols = [dict(r) for r in await cur.fetchall()]
            for col, default in [
                ("ssh_port", "22"),
                ("ssh_user", "'root'"),
                ("ssh_password", "NULL"),
                ("ssh_key_path", "NULL"),
            ]:
                if not _has_column(proxmox_cols, col):
                    await conn.execute(f"ALTER TABLE proxmox_nodes ADD COLUMN {col} DEFAULT {default}")
            await conn.commit()

            # Set defaults if first run
            cursor = await conn.execute("SELECT COUNT(*) FROM settings")
            count = (await cursor.fetchone())[0]
            if count == 0:
                defaults = {
                    "setup_complete": "false",
                    "auth_enabled": "false",
                    "theme": "dark",
                    "check_interval": "60",
                    "retention_days": "7",
                    "app_title": "CentienC",
                    "notifications_enabled": "false",
                }
                for k, v in defaults.items():
                    await conn.execute(
                        "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (k, v)
                    )
                await conn.commit()

            # Ensure default admin user always exists
            cursor = await conn.execute("SELECT COUNT(*) FROM users WHERE username = 'admin'")
            admin_exists = (await cursor.fetchone())[0]
            if not admin_exists:
                from .auth import hash_password
                default_hash = hash_password("changeme")
                await conn.execute(
                    "INSERT INTO users(username, password_hash, role) VALUES('admin', ?, 'admin')",
                    (default_hash,),
                )
                await conn.commit()
                logger.info("Created default admin user (password: changeme)")

        logger.info("Database initialised at %s", self.path)

    # ── Settings ──────────────────────────────────────────────────

    async def get_setting(self, key: str, default: str = "") -> str:
        async with aiosqlite.connect(self.path) as conn:
            cursor = await conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = await cursor.fetchone()
            return row[0] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        async with aiosqlite.connect(self.path) as conn:
            await conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            await conn.commit()

    async def get_all_settings(self) -> dict[str, str]:
        async with aiosqlite.connect(self.path) as conn:
            cursor = await conn.execute("SELECT key, value FROM settings")
            rows = await cursor.fetchall()
            return {r[0]: r[1] for r in rows}

    async def update_settings(self, settings: dict[str, str]) -> None:
        async with aiosqlite.connect(self.path) as conn:
            for k, v in settings.items():
                await conn.execute(
                    "INSERT INTO settings(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (k, str(v)),
                )
            await conn.commit()

    # ── Users ─────────────────────────────────────────────────────

    async def create_user(self, username: str, password_hash: str, role: str = "admin") -> int:
        async with aiosqlite.connect(self.path) as conn:
            cursor = await conn.execute(
                "INSERT INTO users(username, password_hash, role) VALUES(?, ?, ?)",
                (username, password_hash, role),
            )
            await conn.commit()
            return cursor.lastrowid or 0

    async def get_user(self, username: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT * FROM users WHERE username = ?", (username,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def list_users(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT id, username, role, created_at FROM users ORDER BY id")
            return [dict(r) for r in await cursor.fetchall()]

    async def update_user(self, user_id: int, **fields: Any) -> bool:
        if not fields:
            return False
        sets = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [user_id]
        async with aiosqlite.connect(self.path) as conn:
            cursor = await conn.execute(f"UPDATE users SET {sets} WHERE id = ?", vals)
            await conn.commit()
            return cursor.rowcount > 0

    async def delete_user(self, user_id: int) -> bool:
        async with aiosqlite.connect(self.path) as conn:
            cursor = await conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            await conn.commit()
            return cursor.rowcount > 0

    # ── Generic CRUD helpers ──────────────────────────────────────

    async def _insert(self, table: str, data: dict[str, Any]) -> int:
        cols = ", ".join(data.keys())
        placeholders = ", ".join("?" for _ in data)
        vals = list(data.values())
        async with aiosqlite.connect(self.path) as conn:
            cursor = await conn.execute(
                f"INSERT INTO {table}({cols}) VALUES({placeholders})", vals
            )
            await conn.commit()
            return cursor.lastrowid or 0

    async def _update(self, table: str, item_id: int, data: dict[str, Any]) -> bool:
        if not data:
            return False
        sets = ", ".join(f"{k} = ?" for k in data)
        vals = list(data.values()) + [item_id]
        async with aiosqlite.connect(self.path) as conn:
            cursor = await conn.execute(f"UPDATE {table} SET {sets} WHERE id = ?", vals)
            await conn.commit()
            return cursor.rowcount > 0

    async def _delete(self, table: str, item_id: int) -> bool:
        async with aiosqlite.connect(self.path) as conn:
            cursor = await conn.execute(f"DELETE FROM {table} WHERE id = ?", (item_id,))
            await conn.commit()
            return cursor.rowcount > 0

    async def _get(self, table: str, item_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(f"SELECT * FROM {table} WHERE id = ?", (item_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def _list(self, table: str, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        where = ""
        params: list[Any] = []
        if filters:
            clauses = []
            for k, v in filters.items():
                clauses.append(f"{k} = ?")
                params.append(v)
            where = " WHERE " + " AND ".join(clauses)
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(f"SELECT * FROM {table}{where} ORDER BY id", params)
            return [dict(r) for r in await cursor.fetchall()]

    # ── Servers ───────────────────────────────────────────────────

    async def add_server(self, data: dict[str, Any]) -> int:
        return await self._insert("servers", data)

    async def update_server(self, sid: int, data: dict[str, Any]) -> bool:
        return await self._update("servers", sid, data)

    async def delete_server(self, sid: int) -> bool:
        return await self._delete("servers", sid)

    async def get_server(self, sid: int) -> dict[str, Any] | None:
        return await self._get("servers", sid)

    async def list_servers(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        return await self._list("servers", {"enabled": 1} if enabled_only else None)

    # ── Services ──────────────────────────────────────────────────

    async def add_service(self, data: dict[str, Any]) -> int:
        return await self._insert("services", data)

    async def update_service(self, sid: int, data: dict[str, Any]) -> bool:
        return await self._update("services", sid, data)

    async def delete_service(self, sid: int) -> bool:
        return await self._delete("services", sid)

    async def get_service(self, sid: int) -> dict[str, Any] | None:
        return await self._get("services", sid)

    async def list_services(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        return await self._list("services", {"enabled": 1} if enabled_only else None)

    # ── Websites ──────────────────────────────────────────────────

    async def add_website(self, data: dict[str, Any]) -> int:
        return await self._insert("websites", data)

    async def update_website(self, sid: int, data: dict[str, Any]) -> bool:
        return await self._update("websites", sid, data)

    async def delete_website(self, sid: int) -> bool:
        return await self._delete("websites", sid)

    async def get_website(self, sid: int) -> dict[str, Any] | None:
        return await self._get("websites", sid)

    async def list_websites(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        return await self._list("websites", {"enabled": 1} if enabled_only else None)

    # ── Notification Channels ─────────────────────────────────────

    async def add_channel(self, data: dict[str, Any]) -> int:
        if isinstance(data.get("config"), dict):
            data["config"] = json.dumps(data["config"])
        return await self._insert("notification_channels", data)

    async def update_channel(self, cid: int, data: dict[str, Any]) -> bool:
        if isinstance(data.get("config"), dict):
            data["config"] = json.dumps(data["config"])
        return await self._update("notification_channels", cid, data)

    async def delete_channel(self, cid: int) -> bool:
        return await self._delete("notification_channels", cid)

    async def list_channels(self) -> list[dict[str, Any]]:
        rows = await self._list("notification_channels")
        for r in rows:
            try:
                r["config"] = json.loads(r["config"])
            except (json.JSONDecodeError, TypeError):
                pass
        return rows

    # ── Check Results ─────────────────────────────────────────────

    async def record_check(
        self,
        target_type: str,
        target_id: int,
        status: str,
        response_time: float | None = None,
        status_code: int | None = None,
        details: str | None = None,
    ) -> int:
        return await self._insert("check_results", {
            "target_type": target_type,
            "target_id": target_id,
            "status": status,
            "response_time": response_time,
            "status_code": status_code,
            "details": details,
        })

    async def get_history(
        self, target_type: str, target_id: int, hours: int = 24, limit: int = 500,
    ) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT * FROM check_results "
                "WHERE target_type = ? AND target_id = ? "
                "AND checked_at >= datetime('now', ? || ' hours') "
                "ORDER BY checked_at DESC LIMIT ?",
                (target_type, target_id, f"-{hours}", limit),
            )
            return [dict(r) for r in await cursor.fetchall()]

    async def get_latest_check(self, target_type: str, target_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT * FROM check_results "
                "WHERE target_type = ? AND target_id = ? "
                "ORDER BY checked_at DESC LIMIT 1",
                (target_type, target_id),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_uptime(self, target_type: str, target_id: int, hours: int = 24) -> float:
        """Calculate uptime percentage over the given period."""
        async with aiosqlite.connect(self.path) as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) as total, "
                "SUM(CASE WHEN status = 'up' THEN 1 ELSE 0 END) as up_count "
                "FROM check_results "
                "WHERE target_type = ? AND target_id = ? "
                "AND checked_at >= datetime('now', ? || ' hours')",
                (target_type, target_id, f"-{hours}"),
            )
            row = await cursor.fetchone()
            total, up = row[0], row[1] or 0
            return (up / total * 100) if total > 0 else 100.0

    async def cleanup_old_results(self, days: int = 30) -> int:
        """Delete check results older than the given number of days."""
        async with aiosqlite.connect(self.path) as conn:
            cursor = await conn.execute(
                "DELETE FROM check_results WHERE checked_at < datetime('now', ? || ' days')",
                (f"-{days}",),
            )
            await conn.commit()
            return cursor.rowcount

    # ── Proxmox Nodes ──────────────────────────────────────────────

    async def add_proxmox_node(self, data: dict[str, Any]) -> int:
        return await self._insert("proxmox_nodes", data)

    async def update_proxmox_node(self, nid: int, data: dict[str, Any]) -> bool:
        return await self._update("proxmox_nodes", nid, data)

    async def delete_proxmox_node(self, nid: int) -> bool:
        return await self._delete("proxmox_nodes", nid)

    async def get_proxmox_node(self, nid: int) -> dict[str, Any] | None:
        return await self._get("proxmox_nodes", nid)

    async def list_proxmox_nodes(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        return await self._list("proxmox_nodes", {"enabled": 1} if enabled_only else None)

    # ── Incidents ─────────────────────────────────────────────────

    async def open_incident(self, target_type: str, target_id: int) -> int:
        """Open a new incident (or return existing open one)."""
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT id FROM incidents WHERE target_type = ? AND target_id = ? AND status = 'open'",
                (target_type, target_id),
            )
            existing = await cursor.fetchone()
            if existing:
                return existing[0]
            cursor = await conn.execute(
                "INSERT INTO incidents(target_type, target_id) VALUES(?, ?)",
                (target_type, target_id),
            )
            await conn.commit()
            return cursor.lastrowid or 0

    async def resolve_incident(self, target_type: str, target_id: int) -> bool:
        async with aiosqlite.connect(self.path) as conn:
            cursor = await conn.execute(
                "UPDATE incidents SET status = 'resolved', resolved_at = datetime('now'), "
                "duration = CAST((julianday('now') - julianday(started_at)) * 86400 AS INTEGER) "
                "WHERE target_type = ? AND target_id = ? AND status = 'open'",
                (target_type, target_id),
            )
            await conn.commit()
            return cursor.rowcount > 0

    async def resolve_incident_by_id(self, incident_id: int) -> bool:
        async with aiosqlite.connect(self.path) as conn:
            cursor = await conn.execute(
                "UPDATE incidents SET status = 'resolved', resolved_at = datetime('now'), "
                "duration = CAST((julianday('now') - julianday(started_at)) * 86400 AS INTEGER) "
                "WHERE id = ? AND status = 'open'",
                (incident_id,),
            )
            await conn.commit()
            return cursor.rowcount > 0

    async def get_recent_incidents(self, limit: int = 20) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT * FROM incidents ORDER BY started_at DESC LIMIT ?", (limit,)
            )
            return [dict(r) for r in await cursor.fetchall()]

    # ── Push Tokens ───────────────────────────────────────────────

    async def add_push_token(self, token: str, device_name: str | None = None, platform: str = "ios") -> int:
        """Register an Expo push token. Returns token row id."""
        async with aiosqlite.connect(self.path) as conn:
            # Upsert — if token already exists, just update device_name
            await conn.execute(
                "INSERT INTO push_tokens(token, device_name, platform) VALUES(?, ?, ?) "
                "ON CONFLICT(token) DO UPDATE SET device_name = excluded.device_name",
                (token, device_name, platform),
            )
            await conn.commit()
            cursor = await conn.execute("SELECT id FROM push_tokens WHERE token = ?", (token,))
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def remove_push_token(self, token: str) -> bool:
        async with aiosqlite.connect(self.path) as conn:
            cursor = await conn.execute("DELETE FROM push_tokens WHERE token = ?", (token,))
            await conn.commit()
            return cursor.rowcount > 0

    async def list_push_tokens(self) -> list[dict[str, Any]]:
        return await self._list("push_tokens")

    # ── Reporting & Historical Aggregation ────────────────────────

    async def get_daily_summary(
        self, target_type: str, target_id: int, days: int = 7,
    ) -> list[dict[str, Any]]:
        """Per-day aggregation: uptime %, avg response time, check count, incidents."""
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                """
                SELECT date(checked_at) as day,
                       COUNT(*) as checks,
                       SUM(CASE WHEN status='up' THEN 1 ELSE 0 END) as up_count,
                       ROUND(AVG(response_time), 2) as avg_response,
                       ROUND(MIN(response_time), 2) as min_response,
                       ROUND(MAX(response_time), 2) as max_response
                FROM check_results
                WHERE target_type = ? AND target_id = ?
                  AND checked_at >= datetime('now', ? || ' days')
                GROUP BY date(checked_at)
                ORDER BY day
                """,
                (target_type, target_id, f"-{days}"),
            )
            rows = [dict(r) for r in await cursor.fetchall()]
            for r in rows:
                r["uptime_pct"] = round(r["up_count"] / r["checks"] * 100, 2) if r["checks"] else 100.0
            return rows

    async def get_hourly_summary(
        self, target_type: str, target_id: int, days: int = 1,
    ) -> list[dict[str, Any]]:
        """Per-hour aggregation for detailed drill-down charts."""
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                """
                SELECT strftime('%Y-%m-%d %H:00', checked_at) as hour,
                       COUNT(*) as checks,
                       SUM(CASE WHEN status='up' THEN 1 ELSE 0 END) as up_count,
                       ROUND(AVG(response_time), 2) as avg_response
                FROM check_results
                WHERE target_type = ? AND target_id = ?
                  AND checked_at >= datetime('now', ? || ' days')
                GROUP BY strftime('%Y-%m-%d %H:00', checked_at)
                ORDER BY hour
                """,
                (target_type, target_id, f"-{days}"),
            )
            rows = [dict(r) for r in await cursor.fetchall()]
            for r in rows:
                r["uptime_pct"] = round(r["up_count"] / r["checks"] * 100, 2) if r["checks"] else 100.0
            return rows

    async def get_all_targets_summary(self, days: int = 7) -> list[dict[str, Any]]:
        """Cross-target summary: uptime %, avg response, incident count per target."""
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                """
                SELECT target_type, target_id,
                       COUNT(*) as checks,
                       SUM(CASE WHEN status='up' THEN 1 ELSE 0 END) as up_count,
                       ROUND(AVG(response_time), 2) as avg_response,
                       ROUND(MAX(response_time), 2) as max_response,
                       MIN(checked_at) as first_check,
                       MAX(checked_at) as last_check
                FROM check_results
                WHERE checked_at >= datetime('now', ? || ' days')
                GROUP BY target_type, target_id
                ORDER BY target_type, target_id
                """,
                (f"-{days}",),
            )
            rows = [dict(r) for r in await cursor.fetchall()]
            for r in rows:
                r["uptime_pct"] = round(r["up_count"] / r["checks"] * 100, 2) if r["checks"] else 100.0
            return rows

    async def get_incidents_for_period(
        self, days: int = 7, target_type: str | None = None, target_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """All incidents within a time range, optionally filtered by target."""
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            query = "SELECT * FROM incidents WHERE started_at >= datetime('now', ? || ' days')"
            params: list[Any] = [f"-{days}"]
            if target_type:
                query += " AND target_type = ?"
                params.append(target_type)
            if target_id is not None:
                query += " AND target_id = ?"
                params.append(target_id)
            query += " ORDER BY started_at DESC"
            cursor = await conn.execute(query, params)
            return [dict(r) for r in await cursor.fetchall()]

    async def get_status_timeline(
        self, target_type: str, target_id: int, days: int = 7,
    ) -> list[dict[str, Any]]:
        """Status change events for timeline visualisation."""
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                """
                SELECT checked_at, status, response_time, status_code, details
                FROM check_results
                WHERE target_type = ? AND target_id = ?
                  AND checked_at >= datetime('now', ? || ' days')
                ORDER BY checked_at
                """,
                (target_type, target_id, f"-{days}"),
            )
            rows = [dict(r) for r in await cursor.fetchall()]
            # Compress to status-change events only
            changes = []
            prev_status = None
            for r in rows:
                if r["status"] != prev_status:
                    changes.append(r)
                    prev_status = r["status"]
            return changes
    # ══════════════════════════════════════════════════════════════
    #  Visitor Analytics
    # ══════════════════════════════════════════════════════════════

    @staticmethod
    def _va_ts(dt: datetime | None = None) -> str:
        """UTC ISO-8601 timestamp that sorts correctly as a string."""
        dt = dt or datetime.now(timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"

    # ── Tracked sites ─────────────────────────────────────────────

    async def va_add_site(self, name: str, domain: str, site_key: str) -> int:
        return await self._insert("va_sites", {
            "name": name, "domain": domain, "site_key": site_key,
        })

    async def va_update_site(self, sid: int, data: dict[str, Any]) -> bool:
        return await self._update("va_sites", sid, data)

    async def va_delete_site(self, sid: int) -> bool:
        return await self._delete("va_sites", sid)

    async def va_get_site(self, sid: int) -> dict[str, Any] | None:
        return await self._get("va_sites", sid)

    async def va_list_sites(self) -> list[dict[str, Any]]:
        return await self._list("va_sites")

    async def va_get_site_by_key(self, site_key: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT * FROM va_sites WHERE site_key = ? AND enabled = 1", (site_key,)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    # ── Ingestion ─────────────────────────────────────────────────

    async def va_upsert_visitor(self, vid: str, site_id: int, info: dict[str, Any]) -> None:
        now = self._va_ts()
        async with aiosqlite.connect(self.path) as conn:
            await conn.execute(
                "INSERT INTO va_visitors(id, site_id, first_seen, last_seen, device, browser, os) "
                "VALUES(?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET last_seen = excluded.last_seen, "
                "device = COALESCE(NULLIF(excluded.device, ''), va_visitors.device), "
                "browser = COALESCE(NULLIF(excluded.browser, ''), va_visitors.browser), "
                "os = COALESCE(NULLIF(excluded.os, ''), va_visitors.os)",
                (vid, site_id, now, now,
                 info.get("device", ""), info.get("browser", ""), info.get("os", "")),
            )
            await conn.commit()

    async def va_touch_visitor(self, vid: str, site_id: int) -> None:
        async with aiosqlite.connect(self.path) as conn:
            await conn.execute(
                "UPDATE va_visitors SET last_seen = ?, visits = visits + 1 "
                "WHERE id = ? AND site_id = ?",
                (self._va_ts(), vid, site_id),
            )
            await conn.commit()

    async def va_upsert_session(self, sid: str, site_id: int, visitor_id: str,
                                info: dict[str, Any]) -> None:
        now = self._va_ts()
        async with aiosqlite.connect(self.path) as conn:
            await conn.execute(
                "INSERT INTO va_sessions(id, site_id, visitor_id, started_at, last_seen_at, "
                "entry_path, current_path, page_count, referrer, source, medium, campaign, "
                "device, browser, os, screen, lang) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET last_seen_at = excluded.last_seen_at, "
                "current_path = COALESCE(NULLIF(excluded.current_path, ''), va_sessions.current_path)",
                (sid, site_id, visitor_id, now, now,
                 info.get("path", ""), info.get("path", ""), 0,
                 info.get("referrer", "")[:500], info.get("source", "")[:120],
                 info.get("medium", "")[:120], info.get("campaign", "")[:120],
                 info.get("device", ""), info.get("browser", ""), info.get("os", ""),
                 info.get("screen", "")[:20], info.get("lang", "")[:20]),
            )
            # Inherit geo from the visitor record if enrichment already ran
            await conn.execute(
                "UPDATE va_sessions SET country = (SELECT country FROM va_visitors WHERE id = ?), "
                "city = (SELECT city FROM va_visitors WHERE id = ?) "
                "WHERE id = ? AND country = ''",
                (visitor_id, visitor_id, sid),
            )
            await conn.commit()

    async def va_update_session_activity(self, sid: str, site_id: int,
                                         engaged: float, scroll: int,
                                         path: str = "", exited: bool = False,
                                         exit_path: str = "") -> None:
        now = self._va_ts()
        async with aiosqlite.connect(self.path) as conn:
            await conn.execute(
                "UPDATE va_sessions SET last_seen_at = ?, "
                "engaged_seconds = MAX(engaged_seconds, ?), "
                "max_scroll = MAX(max_scroll, ?), "
                "current_path = CASE WHEN ? != '' THEN ? ELSE current_path END, "
                "ended = CASE WHEN ? THEN 1 ELSE ended END, "
                "exit_path = CASE WHEN ? != '' THEN ? ELSE exit_path END "
                "WHERE id = ? AND site_id = ?",
                (now, engaged, scroll, path, path, int(exited), exit_path, exit_path,
                 sid, site_id),
            )
            await conn.commit()

    async def va_add_pageview(self, pv_id: str, session_id: str, visitor_id: str,
                              site_id: int, path: str, title: str, load_ms: int | None,
                              engaged: float, scroll: int, exited: bool,
                              referrer: str) -> None:
        now = self._va_ts()
        async with aiosqlite.connect(self.path) as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO va_pageviews(id, session_id, visitor_id, site_id, "
                "path, title, ts, load_ms, engaged_seconds, scroll_depth, exited, referrer) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (pv_id, session_id, visitor_id, site_id, path[:1024], title[:240], now,
                 load_ms, engaged, scroll, int(exited), referrer[:500]),
            )
            await conn.execute(
                "UPDATE va_sessions SET page_count = page_count + 1 WHERE id = ? AND site_id = ?",
                (session_id, site_id),
            )
            await conn.commit()

    async def va_update_pageview_engagement(self, session_id: str, site_id: int,
                                            path: str, engaged: float,
                                            scroll: int, exited: bool) -> None:
        """Update the most recent pageview for a session+path with live engagement."""
        async with aiosqlite.connect(self.path) as conn:
            await conn.execute(
                "UPDATE va_pageviews SET engaged_seconds = MAX(engaged_seconds, ?), "
                "scroll_depth = MAX(scroll_depth, ?), exited = MAX(exited, ?) "
                "WHERE id = (SELECT id FROM va_pageviews WHERE session_id = ? AND site_id = ? "
                "AND path = ? ORDER BY ts DESC LIMIT 1)",
                (engaged, scroll, int(exited), session_id, site_id, path[:1024]),
            )
            await conn.commit()

    async def va_add_event(self, ev_id: str, session_id: str, site_id: int,
                           name: str, value: str) -> None:
        async with aiosqlite.connect(self.path) as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO va_events(id, session_id, site_id, name, value, ts) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (ev_id, session_id, site_id, name[:80], value[:253], self._va_ts()),
            )
            await conn.commit()

    # ── Geo cache ─────────────────────────────────────────────────

    async def va_geo_get(self, ip: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT * FROM va_geo_cache WHERE ip = ?", (ip,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def va_geo_put(self, ip: str, data: dict[str, Any]) -> None:
        async with aiosqlite.connect(self.path) as conn:
            await conn.execute(
                "INSERT INTO va_geo_cache(ip, country, country_name, region, city, org, fetched_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(ip) DO UPDATE SET country=excluded.country, "
                "country_name=excluded.country_name, region=excluded.region, "
                "city=excluded.city, org=excluded.org, fetched_at=excluded.fetched_at",
                (ip, data.get("country", ""), data.get("country_name", ""),
                 data.get("region", ""), data.get("city", ""), data.get("org", ""),
                 self._va_ts()),
            )
            await conn.commit()

    async def va_set_visitor_geo(self, vid: str, site_id: int, geo: dict[str, Any]) -> None:
        async with aiosqlite.connect(self.path) as conn:
            await conn.execute(
                "UPDATE va_visitors SET country=?, region=?, city=?, org=? WHERE id=? AND site_id=?",
                (geo.get("country", ""), geo.get("region", ""), geo.get("city", ""),
                 geo.get("org", ""), vid, site_id),
            )
            await conn.execute(
                "UPDATE va_sessions SET country=?, city=? WHERE visitor_id=? AND site_id=? "
                "AND country = ''",
                (geo.get("country", ""), geo.get("city", ""), vid, site_id),
            )
            await conn.commit()

    # ── Dashboard queries ─────────────────────────────────────────

    async def va_live_sessions(self, site_id: int, within_seconds: int = 90,
                               limit: int = 100) -> list[dict[str, Any]]:
        """Sessions seen within the last N seconds (active visitors)."""
        cutoff = self._va_ts(datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() - within_seconds,
            tz=timezone.utc))
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT * FROM va_sessions "
                "WHERE site_id = ? AND last_seen_at >= ? "
                "ORDER BY last_seen_at DESC LIMIT ?",
                (site_id, cutoff, limit),
            )
            return [dict(r) for r in await cursor.fetchall()]

    async def va_minute_counts(self, site_id: int, minutes: int = 60) -> list[dict[str, Any]]:
        """Pageviews per minute for the real-time chart."""
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT substr(ts, 1, 16) AS minute, COUNT(*) AS count "
                "FROM va_pageviews WHERE site_id = ? "
                "AND ts >= ? GROUP BY minute ORDER BY minute",
                (site_id, self._va_ts(datetime.fromtimestamp(
                    datetime.now(timezone.utc).timestamp() - minutes * 60,
                    tz=timezone.utc))),
            )
            return [dict(r) for r in await cursor.fetchall()]

    async def va_summary(self, site_id: int, hours: int) -> dict[str, Any]:
        cutoff = self._va_ts(datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() - hours * 3600, tz=timezone.utc))
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            totals = await (await conn.execute(
                "SELECT COUNT(DISTINCT visitor_id) AS visitors, COUNT(*) AS pageviews "
                "FROM va_pageviews WHERE site_id = ? AND ts >= ?",
                (site_id, cutoff))).fetchone()
            sess = await (await conn.execute(
                "SELECT COUNT(*) AS n, AVG(engaged_seconds) AS avg_engaged, "
                "AVG(CAST(max_scroll AS REAL)) AS avg_scroll, "
                "SUM(CASE WHEN page_count <= 1 THEN 1 ELSE 0 END) AS bounces "
                "FROM va_sessions WHERE site_id = ? AND started_at >= ?",
                (site_id, cutoff))).fetchone()
            hourly = await (await conn.execute(
                "SELECT substr(ts, 12, 2) AS hour, COUNT(*) AS count "
                "FROM va_pageviews WHERE site_id = ? AND ts >= ? GROUP BY hour",
                (site_id, cutoff))).fetchall()
            daily = await (await conn.execute(
                "SELECT substr(ts, 1, 10) AS day, COUNT(*) AS count "
                "FROM va_pageviews WHERE site_id = ? AND ts >= ? GROUP BY day ORDER BY day",
                (site_id, cutoff))).fetchall()
            return {
                "visitors": totals["visitors"] or 0,
                "sessions": sess["n"] or 0,
                "pageviews": totals["pageviews"] or 0,
                "avg_engaged_seconds": round(sess["avg_engaged"] or 0, 1),
                "avg_scroll": round(sess["avg_scroll"] or 0, 1),
                "bounce_rate": round((sess["bounces"] or 0) / max(sess["n"] or 1, 1) * 100, 1),
                "hourly": {r["hour"]: r["count"] for r in hourly},
                "daily": [dict(r) for r in daily],
            }

    async def va_page_stats(self, site_id: int, hours: int, limit: int = 50) -> list[dict[str, Any]]:
        cutoff = self._va_ts(datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() - hours * 3600, tz=timezone.utc))
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT path, COUNT(*) AS views, COUNT(DISTINCT visitor_id) AS visitors, "
                "AVG(engaged_seconds) AS avg_engaged, AVG(CAST(scroll_depth AS REAL)) AS avg_scroll, "
                "SUM(CASE WHEN exited = 1 THEN 1 ELSE 0 END) AS exits, "
                "AVG(load_ms) AS avg_load_ms "
                "FROM va_pageviews WHERE site_id = ? AND ts >= ? "
                "GROUP BY path ORDER BY views DESC LIMIT ?",
                (site_id, cutoff, limit),
            )
            return [dict(r) for r in await cursor.fetchall()]

    async def va_breakdown(self, site_id: int, hours: int, column: str) -> list[dict[str, Any]]:
        """Top values for a session column (device/browser/os/country/city/source...)."""
        allowed = {"device", "browser", "os", "country", "city", "source", "medium", "campaign"}
        if column not in allowed:
            return []
        cutoff = self._va_ts(datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() - hours * 3600, tz=timezone.utc))
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                f"SELECT COALESCE(NULLIF({column}, ''), 'unknown') AS name, COUNT(*) AS count "
                f"FROM va_sessions WHERE site_id = ? AND started_at >= ? "
                f"GROUP BY name ORDER BY count DESC LIMIT 20",
                (site_id, cutoff),
            )
            return [dict(r) for r in await cursor.fetchall()]

    async def va_session_list(self, site_id: int, hours: int, limit: int = 100) -> list[dict[str, Any]]:
        cutoff = self._va_ts(datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() - hours * 3600, tz=timezone.utc))
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT * FROM va_sessions WHERE site_id = ? AND started_at >= ? "
                "ORDER BY started_at DESC LIMIT ?",
                (site_id, cutoff, limit),
            )
            return [dict(r) for r in await cursor.fetchall()]

    # ── Status Pages ──────────────────────────────────────────────

    async def sp_add(self, slug: str, title: str, targets: list[dict[str, Any]]) -> int:
        return await self._insert("status_pages", {
            "slug": slug, "title": title, "targets": json.dumps(targets),
        })

    async def sp_update(self, pid: int, data: dict[str, Any]) -> bool:
        if "targets" in data and not isinstance(data["targets"], str):
            data["targets"] = json.dumps(data["targets"])
        return await self._update("status_pages", pid, data)

    async def sp_delete(self, pid: int) -> bool:
        return await self._delete("status_pages", pid)

    async def sp_get(self, pid: int) -> dict[str, Any] | None:
        row = await self._get("status_pages", pid)
        if row:
            try:
                row["targets"] = json.loads(row.get("targets") or "[]")
            except (json.JSONDecodeError, TypeError):
                row["targets"] = []
        return row

    async def sp_get_by_slug(self, slug: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT * FROM status_pages WHERE slug = ?", (slug,))
            row = await cursor.fetchone()
            if not row:
                return None
            d = dict(row)
            try:
                d["targets"] = json.loads(d.get("targets") or "[]")
            except (json.JSONDecodeError, TypeError):
                d["targets"] = []
            return d

    async def sp_list(self) -> list[dict[str, Any]]:
        rows = await self._list("status_pages")
        for r in rows:
            try:
                r["targets"] = json.loads(r.get("targets") or "[]")
            except (json.JSONDecodeError, TypeError):
                r["targets"] = []
        return rows

    async def va_cleanup(self, days: int = 90) -> int:
        """Delete visitor-analytics rows older than the retention window."""
        cutoff = self._va_ts(datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() - days * 86400, tz=timezone.utc))
        async with aiosqlite.connect(self.path) as conn:
            total = 0
            for table, col in (("va_pageviews", "ts"), ("va_events", "ts"),
                               ("va_sessions", "started_at"), ("va_visitors", "last_seen")):
                cursor = await conn.execute(
                    f"DELETE FROM {table} WHERE {col} < ?", (cutoff,))
                total += cursor.rowcount or 0
            await conn.commit()
            return total
