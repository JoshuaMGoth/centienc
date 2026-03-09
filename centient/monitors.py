"""¢entien¢ — Background monitoring workers.

Runs async checks against servers (ICMP ping), services (TCP port),
and websites (HTTP request) at configurable intervals.
"""

from __future__ import annotations

import asyncio
import logging
import platform
import time
from typing import Any

import httpx

from .database import Database

logger = logging.getLogger("centient.monitors")


class MonitorEngine:
    """Runs background monitoring loops for all configured targets."""

    def __init__(self, db: Database):
        self.db = db
        self._tasks: list[asyncio.Task[None]] = []
        self._running = False
        # In-memory status cache (target_type:target_id -> latest status)
        self.status_cache: dict[str, dict[str, Any]] = {}

    async def start(self) -> None:
        """Start all monitoring loops."""
        self._running = True
        self._tasks = [
            asyncio.create_task(self._server_loop()),
            asyncio.create_task(self._service_loop()),
            asyncio.create_task(self._website_loop()),
            asyncio.create_task(self._cleanup_loop()),
        ]
        logger.info("Monitor engine started (%d workers)", len(self._tasks))

    async def stop(self) -> None:
        """Stop all monitoring loops."""
        self._running = False
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()
        logger.info("Monitor engine stopped")

    # ── Server monitoring (ICMP ping) ────────────────────────────

    async def _server_loop(self) -> None:
        while self._running:
            try:
                servers = await self.db.list_servers(enabled_only=True)
                tasks = [self._check_server(s) for s in servers]
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Server loop error: %s", e)
            interval = int(await self.db.get_setting("check_interval", "60"))
            await asyncio.sleep(max(interval, 10))

    async def _check_server(self, server: dict[str, Any]) -> None:
        hostname = server.get("ip_address") or server["hostname"]
        start = time.monotonic()
        try:
            status, rtt = await self._ping(hostname)
        except Exception as e:
            status, rtt = "down", None
            logger.debug("Ping failed for %s: %s", hostname, e)

        elapsed = round((time.monotonic() - start) * 1000, 2) if rtt is None else rtt
        cache_key = f"server:{server['id']}"

        # Track incidents
        prev = self.status_cache.get(cache_key, {}).get("status")
        if status == "down" and prev != "down":
            await self.db.open_incident("server", server["id"])
        elif status == "up" and prev == "down":
            await self.db.resolve_incident("server", server["id"])

        self.status_cache[cache_key] = {
            "status": status,
            "response_time": elapsed if status == "up" else None,
            "last_check": time.time(),
            "name": server["name"],
        }
        await self.db.record_check("server", server["id"], status, elapsed if status == "up" else None)

    async def _ping(self, host: str) -> tuple[str, float | None]:
        """Ping a host and return (status, rtt_ms)."""
        is_windows = platform.system().lower() == "windows"
        flag = "-n" if is_windows else "-c"
        timeout_flag = "-w" if is_windows else "-W"
        cmd = ["ping", flag, "1", timeout_flag, "3", host]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            if proc.returncode == 0:
                output = stdout.decode("utf-8", errors="ignore")
                # Parse RTT from ping output
                rtt = self._parse_ping_rtt(output)
                return "up", rtt
            return "down", None
        except (asyncio.TimeoutError, OSError):
            return "down", None

    @staticmethod
    def _parse_ping_rtt(output: str) -> float | None:
        """Extract average RTT from ping output."""
        import re
        # Linux: rtt min/avg/max/mdev = 1.234/2.345/3.456/0.123 ms
        m = re.search(r"rtt min/avg/max/mdev = [\d.]+/([\d.]+)/", output)
        if m:
            return float(m.group(1))
        # macOS: round-trip min/avg/max/stddev = 1.234/2.345/3.456/0.123 ms
        m = re.search(r"round-trip min/avg/max/stddev = [\d.]+/([\d.]+)/", output)
        if m:
            return float(m.group(1))
        # Windows: Average = 2ms
        m = re.search(r"Average = (\d+)ms", output)
        if m:
            return float(m.group(1))
        # Try to find time= from individual ping
        m = re.search(r"time[=<]([\d.]+)\s*ms", output)
        if m:
            return float(m.group(1))
        return None

    # ── Service monitoring (TCP port check) ──────────────────────

    async def _service_loop(self) -> None:
        while self._running:
            try:
                services = await self.db.list_services(enabled_only=True)
                tasks = [self._check_service(s) for s in services]
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Service loop error: %s", e)
            interval = int(await self.db.get_setting("check_interval", "60"))
            await asyncio.sleep(max(interval, 10))

    async def _check_service(self, service: dict[str, Any]) -> None:
        host = service["host"]
        port = service["port"]
        timeout = service.get("timeout", 10)
        start = time.monotonic()

        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=timeout,
            )
            writer.close()
            await writer.wait_closed()
            status = "up"
        except (asyncio.TimeoutError, OSError, ConnectionRefusedError):
            status = "down"

        elapsed = round((time.monotonic() - start) * 1000, 2)
        cache_key = f"service:{service['id']}"

        prev = self.status_cache.get(cache_key, {}).get("status")
        if status == "down" and prev != "down":
            await self.db.open_incident("service", service["id"])
        elif status == "up" and prev == "down":
            await self.db.resolve_incident("service", service["id"])

        self.status_cache[cache_key] = {
            "status": status,
            "response_time": elapsed if status == "up" else None,
            "last_check": time.time(),
            "name": service["name"],
            "type": service["type"],
            "host": host,
            "port": port,
        }
        await self.db.record_check("service", service["id"], status, elapsed if status == "up" else None)

    # ── Website monitoring (HTTP check) ──────────────────────────

    async def _website_loop(self) -> None:
        while self._running:
            try:
                websites = await self.db.list_websites(enabled_only=True)
                tasks = [self._check_website(w) for w in websites]
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Website loop error: %s", e)
            interval = int(await self.db.get_setting("check_interval", "60"))
            await asyncio.sleep(max(interval, 10))

    async def _check_website(self, website: dict[str, Any]) -> None:
        url = website["url"]
        method = website.get("method", "GET").upper()
        timeout = website.get("timeout", 15)
        expected = website.get("expected_status", 200)
        follow = bool(website.get("follow_redirects", 1))
        verify_ssl = bool(website.get("verify_ssl", 1))
        start = time.monotonic()

        status = "down"
        status_code = None
        details = None
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=follow,
                verify=verify_ssl,
            ) as client:
                resp = await client.request(method, url)
                status_code = resp.status_code
                if expected and status_code == expected:
                    status = "up"
                elif not expected and 200 <= status_code < 400:
                    status = "up"
                else:
                    status = "warning"
                    details = f"Expected {expected}, got {status_code}"
        except httpx.TimeoutException:
            details = "Connection timed out"
        except httpx.ConnectError as e:
            details = f"Connection error: {e}"
        except Exception as e:
            details = str(e)

        elapsed = round((time.monotonic() - start) * 1000, 2)
        cache_key = f"website:{website['id']}"

        prev = self.status_cache.get(cache_key, {}).get("status")
        if status == "down" and prev != "down":
            await self.db.open_incident("website", website["id"])
        elif status in ("up", "warning") and prev == "down":
            await self.db.resolve_incident("website", website["id"])

        self.status_cache[cache_key] = {
            "status": status,
            "response_time": elapsed,
            "status_code": status_code,
            "last_check": time.time(),
            "name": website["name"],
            "url": url,
            "details": details,
        }
        await self.db.record_check(
            "website", website["id"], status, elapsed, status_code, details
        )

    # ── Cleanup loop ─────────────────────────────────────────────

    async def _cleanup_loop(self) -> None:
        """Periodically clean up old check results."""
        while self._running:
            try:
                days = int(await self.db.get_setting("retention_days", "30"))
                deleted = await self.db.cleanup_old_results(days)
                if deleted:
                    logger.info("Cleaned up %d old check results (>%d days)", deleted, days)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Cleanup error: %s", e)
            await asyncio.sleep(3600)  # Run hourly

    # ── Manual check ─────────────────────────────────────────────

    async def check_now(self, target_type: str, target_id: int) -> dict[str, Any]:
        """Run an immediate check on a specific target."""
        if target_type == "server":
            item = await self.db.get_server(target_id)
            if item:
                await self._check_server(item)
        elif target_type == "service":
            item = await self.db.get_service(target_id)
            if item:
                await self._check_service(item)
        elif target_type == "website":
            item = await self.db.get_website(target_id)
            if item:
                await self._check_website(item)
        cache_key = f"{target_type}:{target_id}"
        return self.status_cache.get(cache_key, {"status": "unknown"})

    # ── Aggregate status ─────────────────────────────────────────

    def get_overview(self) -> dict[str, Any]:
        """Get a summary of all monitored targets from the cache."""
        servers = []
        services = []
        websites = []
        for key, val in self.status_cache.items():
            ttype, tid = key.split(":", 1)
            entry = {"id": int(tid), **val}
            if ttype == "server":
                servers.append(entry)
            elif ttype == "service":
                services.append(entry)
            elif ttype == "website":
                websites.append(entry)

        total = len(servers) + len(services) + len(websites)
        up = sum(1 for s in servers + services + websites if s.get("status") == "up")
        down = sum(1 for s in servers + services + websites if s.get("status") == "down")
        warning = sum(1 for s in servers + services + websites if s.get("status") == "warning")

        return {
            "servers": servers,
            "services": services,
            "websites": websites,
            "stats": {
                "total": total,
                "up": up,
                "down": down,
                "warning": warning,
                "uptime_pct": round((up / total * 100) if total > 0 else 100, 1),
            },
        }
