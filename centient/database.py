"""¢entient¢ — SQLite database layer with async support."""

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
    ssh_user       TEXT,
    ssh_key_path   TEXT,
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
"""


# ═══════════════════════════════════════════════════════════════════
#  DATABASE CLASS
# ═══════════════════════════════════════════════════════════════════

class Database:
    """Async SQLite database wrapper for ¢entient¢."""

    def __init__(self, path: str | None = None):
        self.path = path or db_path()
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)

    async def init(self) -> None:
        """Create tables and run migrations."""
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.executescript(SCHEMA)
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
                    "retention_days": "30",
                    "app_title": "¢entient¢",
                    "notifications_enabled": "false",
                }
                for k, v in defaults.items():
                    await conn.execute(
                        "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (k, v)
                    )
                await conn.commit()
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

    async def get_recent_incidents(self, limit: int = 20) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT * FROM incidents ORDER BY started_at DESC LIMIT ?", (limit,)
            )
            return [dict(r) for r in await cursor.fetchall()]
