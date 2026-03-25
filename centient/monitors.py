"""CentienC — Background monitoring workers.

Agentless monitoring: ping/TCP, services (TCP port), websites (HTTP),
and Proxmox nodes (API).  Nothing is installed on monitored hosts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import re
import time
from typing import Any

import asyncssh
import httpx

from .database import Database
from .notifications import send_notification

SEP = "---CENTIENT-SEP---"

logger = logging.getLogger("centient.monitors")


class MonitorEngine:
    """Runs background monitoring loops for all configured targets."""

    def __init__(self, db: Database):
        self.db = db
        self._tasks: list[asyncio.Task[None]] = []
        self._running = False
        self.status_cache: dict[str, dict[str, Any]] = {}
        self._ssh_conns: dict[int, asyncssh.SSHClientConnection] = {}

    async def start(self) -> None:
        """Start all monitoring loops."""
        self._running = True
        self._tasks = [
            asyncio.create_task(self._server_loop()),
            asyncio.create_task(self._service_loop()),
            asyncio.create_task(self._website_loop()),
            asyncio.create_task(self._ssh_loop()),
            asyncio.create_task(self._proxmox_loop()),
            asyncio.create_task(self._cleanup_loop()),
        ]
        logger.info("Monitor engine started (%d workers)", len(self._tasks))

    async def stop(self) -> None:
        """Stop all monitoring loops."""
        self._running = False
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()
        # Close any open SSH connections
        for conn in self._ssh_conns.values():
            try:
                conn.close()
            except Exception:
                pass
        self._ssh_conns.clear()
        logger.info("Monitor engine stopped")

    # ══════════════════════════════════════════════════════════════
    #  Server monitoring (ICMP ping)
    # ══════════════════════════════════════════════════════════════

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
            interval = int(await self.db.get_setting("check_interval", "10"))
            await asyncio.sleep(max(interval, 5))

    async def _check_server(self, server: dict[str, Any]) -> None:
        hostname = server.get("ip_address") or server["hostname"]
        start = time.monotonic()
        try:
            status, rtt = await self._ping(hostname)
        except Exception:
            status, rtt = "down", None

        elapsed = round((time.monotonic() - start) * 1000, 2) if rtt is None else rtt
        cache_key = f"server:{server['id']}"

        prev = self.status_cache.get(cache_key, {}).get("status")
        if status == "down" and prev != "down":
            await self.db.open_incident("server", server["id"])
            await send_notification(self.db, "server", server["name"], "down", f"{hostname} is unreachable")
        elif status == "up" and prev == "down":
            await self.db.resolve_incident("server", server["id"])
            await send_notification(self.db, "server", server["name"], "up", "Server is back online")

        self.status_cache[cache_key] = {
            "status": status,
            "response_time": elapsed if status == "up" else None,
            "last_check": time.time(),
            "name": server["name"],
        }
        await self.db.record_check("server", server["id"], status, elapsed if status == "up" else None)

    async def _ping(self, host: str) -> tuple[str, float | None]:
        is_windows = platform.system().lower() == "windows"
        flag = "-n" if is_windows else "-c"
        timeout_flag = "-w" if is_windows else "-W"
        cmd = ["ping", flag, "1", timeout_flag, "3", host]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
            if proc.returncode == 0:
                output = stdout.decode("utf-8", errors="ignore")
                rtt = self._parse_ping_rtt(output)
                return "up", rtt
            # ICMP may be blocked (unprivileged LXC container, Docker, restricted env).
            # Fall back to TCP port probing so servers are still detected.
            err_text = stderr.decode("utf-8", errors="ignore").lower()
            if any(kw in err_text for kw in ("permission denied", "operation not permitted", "socket")):
                logger.debug("ICMP blocked for %s, falling back to TCP probe", host)
                return await self._tcp_probe(host)
            return "down", None
        except FileNotFoundError:
            logger.debug("ping binary unavailable for %s, falling back to TCP probe", host)
            return await self._tcp_probe(host)
        except PermissionError:
            logger.debug("ICMP PermissionError for %s, falling back to TCP probe", host)
            return await self._tcp_probe(host)
        except asyncio.TimeoutError:
            return "down", None
        except OSError:
            logger.debug("ICMP probe failed to start for %s, falling back to TCP probe", host)
            return await self._tcp_probe(host)

    async def _tcp_probe(self, host: str, ports: tuple[int, ...] = (22, 80, 443, 3389, 8080)) -> tuple[str, float | None]:
        """TCP connectivity fallback used when ICMP ping is unavailable (e.g. unprivileged LXC)."""
        start = time.monotonic()
        for port in ports:
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port), timeout=3.0,
                )
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
                rtt = round((time.monotonic() - start) * 1000, 2)
                return "up", rtt
            except (asyncio.TimeoutError, OSError, ConnectionRefusedError):
                continue
        return "down", None

    @staticmethod
    def _parse_ping_rtt(output: str) -> float | None:
        for pattern in [
            r"rtt min/avg/max/mdev = [\d.]+/([\d.]+)/",
            r"round-trip min/avg/max/stddev = [\d.]+/([\d.]+)/",
            r"Average = (\d+)ms",
            r"time[=<]([\d.]+)\s*ms",
        ]:
            m = re.search(pattern, output)
            if m:
                return float(m.group(1))
        return None

    # ══════════════════════════════════════════════════════════════
    #  Service monitoring (TCP port check)
    # ══════════════════════════════════════════════════════════════

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
            interval = int(await self.db.get_setting("check_interval", "10"))
            await asyncio.sleep(max(interval, 5))

    async def _check_service(self, service: dict[str, Any]) -> None:
        host = service["host"]
        port = service["port"]
        timeout = service.get("timeout", 10)
        start = time.monotonic()

        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=timeout,
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
            await send_notification(self.db, "service", service["name"], "down", f"{host}:{port} is unreachable")
        elif status == "up" and prev == "down":
            await self.db.resolve_incident("service", service["id"])
            await send_notification(self.db, "service", service["name"], "up", f"{host}:{port} is back online")

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

    # ══════════════════════════════════════════════════════════════
    #  Website monitoring (HTTP check)
    # ══════════════════════════════════════════════════════════════

    async def _website_loop(self) -> None:
        while self._running:
            try:
                websites = await self.db.list_websites(enabled_only=True)
                if websites:
                    # Stagger checks to avoid 429s — run up to 2 concurrently
                    sem = asyncio.Semaphore(2)

                    async def _throttled(w: dict) -> None:
                        async with sem:
                            await self._check_website(w)
                            await asyncio.sleep(1.5)   # pause between each check

                    await asyncio.gather(
                        *[_throttled(w) for w in websites],
                        return_exceptions=True,
                    )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Website loop error: %s", e)
            interval = int(await self.db.get_setting("check_interval", "10"))
            await asyncio.sleep(max(interval, 5))

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
                timeout=timeout, follow_redirects=follow, verify=verify_ssl,
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
        if status in ("down", "warning") and prev not in ("down", "warning"):
            await self.db.open_incident("website", website["id"])
            await send_notification(
                self.db, "website", website["name"], status,
                details or f"{url} returned HTTP {status_code}" if status_code else details,
            )
        elif status == "up" and prev in ("down", "warning"):
            await self.db.resolve_incident("website", website["id"])
            await send_notification(self.db, "website", website["name"], "up", f"{url} is back online")

        self.status_cache[cache_key] = {
            "status": status,
            "response_time": elapsed,
            "status_code": status_code,
            "last_check": time.time(),
            "name": website["name"],
            "url": url,
            "details": details,
            # Preserve website linkage metadata so dashboard cards can enable drill-down.
            "server_id": website.get("server_id"),
            "log_path": website.get("log_path"),
        }
        await self.db.record_check(
            "website", website["id"], status, elapsed, status_code, details
        )

    # ══════════════════════════════════════════════════════════════
    #  SSH-based remote metrics collection (agentless)
    # ══════════════════════════════════════════════════════════════

    async def _ssh_loop(self) -> None:
        """Poll each server with SSH credentials for rich metrics."""
        while self._running:
            try:
                servers = await self.db.list_servers(enabled_only=True)
                tasks = [
                    self._poll_ssh(s) for s in servers
                    if s.get("ssh_user")
                ]
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("SSH loop error: %s", e)
            interval = int(await self.db.get_setting("check_interval", "10"))
            await asyncio.sleep(max(interval, 5))

    async def _get_ssh_conn(self, server: dict[str, Any]) -> asyncssh.SSHClientConnection:
        """Get or create a persistent SSH connection."""
        sid = server["id"]
        conn = self._ssh_conns.get(sid)
        if conn is not None:
            try:
                result = await asyncio.wait_for(conn.run("echo ok", check=False), timeout=3)
                if result.stdout and result.stdout.strip() == "ok":
                    return conn
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass
            self._ssh_conns.pop(sid, None)

        host = server.get("ip_address") or server["hostname"]
        port = server.get("ssh_port") or server.get("port") or 22
        user = server.get("ssh_user", "root")
        key_path = server.get("ssh_key_path")
        password = server.get("ssh_password")

        connect_kwargs: dict[str, Any] = {
            "host": host,
            "port": int(port),
            "username": user,
            "known_hosts": None,
            "connect_timeout": 8,
        }

        if key_path:
            connect_kwargs["client_keys"] = [key_path]
        elif password:
            connect_kwargs["password"] = password

        conn = await asyncssh.connect(**connect_kwargs)
        self._ssh_conns[sid] = conn
        return conn

    async def _ssh_cmd(self, conn: asyncssh.SSHClientConnection, cmd: str, timeout: int = 12) -> str:
        """Run a command over SSH and return stdout."""
        result = await asyncio.wait_for(conn.run(cmd, check=False), timeout=timeout)
        return result.stdout or ""

    async def _ssh_sudo_cmd(self, conn: asyncssh.SSHClientConnection, cmd: str,
                            sudo_pass: str | None = None, timeout: int = 15) -> str:
        """Run a sudo command over SSH."""
        if sudo_pass:
            remote = f'echo "{sudo_pass}" | sudo -S {cmd} 2>/dev/null'
        else:
            remote = f'sudo {cmd} 2>/dev/null'
        return await self._ssh_cmd(conn, remote, timeout)

    async def _poll_ssh(self, server: dict[str, Any]) -> None:
        """Fetch metrics from a server via SSH."""
        sid = server["id"]
        cache_key = f"ssh:{sid}"

        raw_flags = server.get("monitor_flags") or "{}"
        try:
            flags = json.loads(raw_flags) if isinstance(raw_flags, str) else raw_flags
        except (json.JSONDecodeError, TypeError):
            flags = {}

        results: dict[str, Any] = {
            "server_id": sid,
            "last_update": time.time(),
            "error": None,
        }

        try:
            conn = await self._get_ssh_conn(server)
            sudo_pass = server.get("sudo_password")
            is_macos = str(server.get("type", "")).lower() == "macos"

            if flags.get("system", flags.get("system_metrics", True)):
                if is_macos:
                    results["metrics"] = await self._ssh_system_metrics_macos(conn)
                else:
                    metrics = await self._ssh_system_metrics(conn)
                    # Auto-fallback to macOS collection if Linux /proc failed
                    if metrics.get("error") and "Incomplete" in str(metrics["error"]):
                        metrics = await self._ssh_system_metrics_macos(conn)
                    results["metrics"] = metrics

            if flags.get("pm2", False):
                results["pm2"] = await self._ssh_pm2(conn)

            if flags.get("services", flags.get("systemd", flags.get("systemd_services", False))):
                results["services"] = await self._ssh_services(conn)

            if flags.get("nginx", flags.get("web_server", False)):
                results["nginx"] = await self._ssh_nginx(conn, sudo_pass)

            if flags.get("fail2ban", False):
                results["fail2ban"] = await self._ssh_fail2ban(conn, sudo_pass)

        except Exception as exc:
            results["error"] = str(exc)
            logger.warning("SSH poll failed for server %d: %s", sid, exc)
            self._ssh_conns.pop(sid, None)

        self.status_cache[cache_key] = results

    async def _ssh_system_metrics(self, conn: asyncssh.SSHClientConnection) -> dict[str, Any]:
        """Collect CPU, memory, disk, connections via a single batched SSH command."""
        batch_cmd = (
            f"cat /proc/loadavg && echo '{SEP}' && "
            f"nproc && echo '{SEP}' && "
            f"cat /proc/meminfo && echo '{SEP}' && "
            f"df -BG / | tail -1 && echo '{SEP}' && "
            f"cat /proc/uptime && echo '{SEP}' && "
            f"ss -tn state established 2>/dev/null | tail -n +2 && echo '{SEP}' && "
            f"cat /proc/net/dev"
        )
        raw = await self._ssh_cmd(conn, batch_cmd)
        parts = raw.split(SEP)
        if len(parts) < 7:
            return {"error": "Incomplete system metrics response"}

        result: dict[str, Any] = {}

        # CPU load
        try:
            load_parts = parts[0].strip().split()
            cores = int(parts[1].strip())
            load1 = float(load_parts[0])
            result["cpu_pct"] = round(load1 / cores * 100, 1)
            result["load_1m"] = load1
            result["cores"] = cores
        except (ValueError, IndexError):
            result["cpu_pct"] = 0
            result["cores"] = 1

        # Memory
        try:
            mem_text = parts[2].strip()
            mem = {}
            for line in mem_text.split("\n"):
                if ":" in line:
                    k, v = line.split(":", 1)
                    mem[k.strip()] = int(v.strip().split()[0])
            total = mem.get("MemTotal", 1)
            avail = mem.get("MemAvailable", total)
            used = total - avail
            result["memory"] = {
                "total_mb": round(total / 1024),
                "used_mb": round(used / 1024),
                "used_pct": round(used / total * 100, 1),
            }
        except (ValueError, IndexError):
            result["memory"] = {"total_mb": 0, "used_mb": 0, "used_pct": 0}

        # Disk
        try:
            disk_line = parts[3].strip().split()
            total_g = int(disk_line[1].rstrip("G"))
            used_g = int(disk_line[2].rstrip("G"))
            result["disk"] = {
                "total_gb": total_g,
                "used_gb": used_g,
                "used_pct": round(used_g / total_g * 100, 1) if total_g > 0 else 0,
            }
        except (ValueError, IndexError):
            result["disk"] = {"total_gb": 0, "used_gb": 0, "used_pct": 0}

        # Uptime
        try:
            uptime_secs = float(parts[4].strip().split()[0])
            result["uptime"] = int(uptime_secs)
        except (ValueError, IndexError):
            result["uptime"] = 0

        # Connections
        try:
            conn_lines = [l for l in parts[5].strip().split("\n") if l.strip()]
            result["connections"] = len(conn_lines)
            result["raw_connections"] = parts[5].strip()
        except Exception:
            result["connections"] = 0
            result["raw_connections"] = ""

        # Network I/O
        try:
            net_lines = parts[6].strip().split("\n")
            for line in net_lines:
                if "lo:" in line or "Inter-" in line or "face" in line:
                    continue
                cols = line.split()
                if len(cols) >= 10:
                    result["net_rx_bytes"] = int(cols[1])
                    result["net_tx_bytes"] = int(cols[9])
                    break
        except Exception:
            pass

        return result

    async def _ssh_system_metrics_macos(self, conn: asyncssh.SSHClientConnection) -> dict[str, Any]:
        """Collect CPU, memory, disk, connections for macOS via SSH."""
        batch_cmd = (
            f"sysctl -n hw.ncpu && echo '{SEP}' && "
            f"sysctl -n vm.loadavg && echo '{SEP}' && "
            f"vm_stat && echo '{SEP}' && "
            f"sysctl -n hw.memsize && echo '{SEP}' && "
            f"df -g / | tail -1 && echo '{SEP}' && "
            f"sysctl -n kern.boottime && echo '{SEP}' && "
            f"netstat -an 2>/dev/null | grep -c ESTABLISHED"
        )
        raw = await self._ssh_cmd(conn, batch_cmd)
        parts = raw.split(SEP)
        if len(parts) < 7:
            return {"error": "Incomplete macOS system metrics response"}

        result: dict[str, Any] = {}

        # CPU cores + load
        try:
            cores = int(parts[0].strip())
            # vm.loadavg format: "{ 1.23 4.56 7.89 }"
            load_str = parts[1].strip().strip("{}").strip()
            load1 = float(load_str.split()[0])
            result["cpu_pct"] = round(load1 / cores * 100, 1)
            result["load_1m"] = load1
            result["cores"] = cores
        except (ValueError, IndexError):
            result["cpu_pct"] = 0
            result["cores"] = 1

        # Memory from vm_stat + hw.memsize
        try:
            page_size = 16384  # Apple Silicon default
            vm_text = parts[2].strip()
            # Try to extract page size from vm_stat header
            for line in vm_text.split("\n"):
                if "page size of" in line:
                    ps_match = re.search(r"page size of (\d+)", line)
                    if ps_match:
                        page_size = int(ps_match.group(1))
                    break
            vm = {}
            for line in vm_text.split("\n"):
                if ":" in line:
                    k, v = line.split(":", 1)
                    v_clean = v.strip().rstrip(".")
                    if v_clean.isdigit():
                        vm[k.strip()] = int(v_clean)
            total_bytes = int(parts[3].strip())
            total_mb = round(total_bytes / 1048576)
            free_pages = vm.get("Pages free", 0) + vm.get("Pages speculative", 0)
            inactive_pages = vm.get("Pages inactive", 0)
            purgeable_pages = vm.get("Pages purgeable", 0)
            avail_mb = round((free_pages + inactive_pages + purgeable_pages) * page_size / 1048576)
            used_mb = total_mb - avail_mb
            result["memory"] = {
                "total_mb": total_mb,
                "used_mb": max(0, used_mb),
                "used_pct": round(max(0, used_mb) / total_mb * 100, 1) if total_mb > 0 else 0,
            }
        except (ValueError, IndexError):
            result["memory"] = {"total_mb": 0, "used_mb": 0, "used_pct": 0}

        # Disk
        try:
            disk_cols = parts[4].strip().split()
            # df -g on macOS: Filesystem Gblocks Used Available Capacity ...
            total_g = int(disk_cols[1])
            used_g = int(disk_cols[2])
            result["disk"] = {
                "total_gb": total_g,
                "used_gb": used_g,
                "used_pct": round(used_g / total_g * 100, 1) if total_g > 0 else 0,
            }
        except (ValueError, IndexError):
            result["disk"] = {"total_gb": 0, "used_gb": 0, "used_pct": 0}

        # Uptime from kern.boottime
        try:
            bt_str = parts[5].strip()
            sec_match = re.search(r"sec\s*=\s*(\d+)", bt_str)
            if sec_match:
                boot_epoch = int(sec_match.group(1))
                result["uptime"] = int(time.time()) - boot_epoch
            else:
                result["uptime"] = 0
        except (ValueError, IndexError):
            result["uptime"] = 0

        # Connections
        try:
            result["connections"] = int(parts[6].strip())
        except (ValueError, IndexError):
            result["connections"] = 0

        return result

    async def _ssh_pm2(self, conn: asyncssh.SSHClientConnection) -> list[dict[str, Any]]:
        """Get PM2 process list via SSH."""
        raw = await self._ssh_cmd(conn, "pm2 jlist 2>/dev/null || echo '[]'")
        try:
            match = re.search(r'\[.*\]', raw, re.DOTALL)
            data = json.loads(match.group(0)) if match else []
        except (json.JSONDecodeError, AttributeError):
            return []

        procs = []
        for p in data:
            env = p.get("pm2_env", {})
            monit = p.get("monit", {})
            procs.append({
                "name": p.get("name", "?"),
                "status": env.get("status", "unknown"),
                "cpu": monit.get("cpu", 0),
                "memory_mb": round(monit.get("memory", 0) / 1048576, 1),
                "restarts": env.get("restart_time", 0),
                "uptime": env.get("pm_uptime", 0),
            })
        return procs

    async def _ssh_services(self, conn: asyncssh.SSHClientConnection) -> list[dict[str, Any]]:
        """Get systemd services via SSH — notable services only."""
        raw = await self._ssh_cmd(conn,
            "systemctl list-units --type=service --all --plain --no-legend 2>/dev/null"
        )
        services = []
        for line in raw.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split(None, 4)
            if len(parts) < 4:
                continue
            name = parts[0].replace(".service", "")
            active = parts[2]
            sub = parts[3]
            ok = active == "active"
            services.append({"name": name, "active": active, "sub": sub, "ok": ok})
        return services

    async def _ssh_nginx(self, conn: asyncssh.SSHClientConnection, sudo_pass: str | None = None) -> dict[str, Any]:
        """Parse recent nginx access logs via SSH."""
        cmd = "tail -n 5000 /var/log/nginx/*access*.log 2>/dev/null || tail -n 5000 /var/log/nginx/*.log 2>/dev/null || echo ''"
        if sudo_pass:
            raw = await self._ssh_sudo_cmd(conn, cmd, sudo_pass)
        else:
            raw = await self._ssh_cmd(conn, cmd)

        window = 300
        cutoff = time.time() - window
        sites: dict[str, dict[str, Any]] = {}
        recent_requests: list[dict[str, Any]] = []
        total_requests = 0

        # Support two formats:
        #   1) vhost-prefixed: "joshuagoth.com 1.2.3.4 - - [ts] ..."
        #   2) standard combined: "1.2.3.4 - - [ts] ..."
        log_re = re.compile(
            r'(?:(?P<vhost>[a-zA-Z0-9._-]+)\s+)?'
            r'(?P<ip>[\d.]+)\s+\S+\s+\S+\s+'
            r'\[(?P<ts>[^\]]+)\]\s+'
            r'"(?P<method>\S+)\s+(?P<path>\S+)\s+\S+"\s+'
            r'(?P<status>\d+)\s+(?P<bytes>\d+)\s+'
            r'"(?P<ref>[^"]*)"\s+"(?P<ua>[^"]*)"'
        )
        # When tail reads multiple files it emits "==> /path/to/file <=="
        # headers. Extract a site name from the filename to use as fallback.
        file_header_re = re.compile(r'^==> .*/(?P<fname>[^/]+?)(?:\.access)?\.log <==$')
        # Generic filenames that should NOT be used as site names
        _GENERIC_LOG_NAMES = {"access", "error", "default", "nginx", "combined", "main"}
        current_file_site: str | None = None

        from datetime import datetime
        for line in raw.strip().split("\n"):
            fh = file_header_re.match(line.strip())
            if fh:
                fname = fh.group("fname")
                # Don't use generic log names like 'access' as site names
                current_file_site = None if fname in _GENERIC_LOG_NAMES else fname
                continue
            m = log_re.search(line)
            if not m:
                continue
            try:
                ts_str = m.group("ts")
                dt = datetime.strptime(ts_str, "%d/%b/%Y:%H:%M:%S %z")
                ts = dt.timestamp()
            except (ValueError, TypeError):
                continue
            if ts < cutoff:
                continue

            total_requests += 1
            status_code = int(m.group("status"))
            ip = m.group("ip")
            method = m.group("method")
            path = m.group("path")
            ua = m.group("ua")

            site_name = m.group("vhost") or current_file_site or "default"
            site = sites.setdefault(site_name, {
                "total_requests": 0,
                "status_codes": {},
                "unique_ips": set(),
                "rpm": 0,
            })
            site["total_requests"] += 1
            code_bucket = str(status_code)
            site["status_codes"][code_bucket] = site["status_codes"].get(code_bucket, 0) + 1
            # Only count real visitors (exclude monitoring/bot user agents)
            if not self._BOT_PATTERNS.search(ua):
                site["unique_ips"].add(ip)

            if len(recent_requests) < 50:
                recent_requests.append({
                    "ts": ts,
                    "method": method,
                    "path": path,
                    "status": status_code,
                    "ip": ip,
                    "site": site_name,
                    "ua": m.group("ua"),
                })

        rpm = total_requests / (window / 60) if total_requests > 0 else 0

        serialized_sites: dict[str, Any] = {}
        for name, data in sites.items():
            serialized_sites[name] = {
                "total_requests": data["total_requests"],
                "status_codes": data["status_codes"],
                "unique_visitors": len(data["unique_ips"]),
                "rpm": round(data["total_requests"] / (window / 60), 1),
                "period_minutes": window // 60,
            }

        recent_requests.sort(key=lambda r: r.get("ts", 0), reverse=True)

        return {
            "sites": serialized_sites,
            "totals": {"rpm": round(rpm, 1), "total_requests": total_requests},
            "recent_requests": recent_requests[:50],
        }

    # ══════════════════════════════════════════════════════════════
    #  Web server detection & per-site log parsing
    # ══════════════════════════════════════════════════════════════

    # Common log paths per web server type
    _LOG_PATHS = {
        "nginx": [
            "/var/log/nginx/*access*.log",
            "/var/log/nginx/*.log",
        ],
        "apache": [
            "/var/log/apache2/*access*.log",
            "/var/log/apache2/*.log",
            "/var/log/httpd/*access_log*",
            "/var/log/httpd/*.log",
        ],
    }

    async def detect_web_server(self, server: dict[str, Any]) -> dict[str, Any]:
        """Auto-detect web server type and available log files on a server."""
        conn = await self._get_ssh_conn(server)
        sudo_pass = server.get("sudo_password")

        # Detect installed web servers
        detect_cmd = (
            "which nginx 2>/dev/null && echo '---NGINX---'; "
            "which apache2 2>/dev/null && echo '---APACHE2---'; "
            "which httpd 2>/dev/null && echo '---HTTPD---'; "
            "echo '---DONE---'"
        )
        raw = await self._ssh_cmd(conn, detect_cmd)

        web_servers = []
        if "---NGINX---" in raw:
            web_servers.append("nginx")
        if "---APACHE2---" in raw or "---HTTPD---" in raw:
            web_servers.append("apache")

        # Find available log files
        log_files = []
        for ws_type in web_servers:
            for glob_path in self._LOG_PATHS[ws_type]:
                ls_cmd = f"ls -1 {glob_path} 2>/dev/null"
                if sudo_pass:
                    result = await self._ssh_sudo_cmd(conn, ls_cmd, sudo_pass)
                else:
                    result = await self._ssh_cmd(conn, ls_cmd)
                for line in result.strip().split("\n"):
                    line = line.strip()
                    if line and not line.startswith("ls:") and line.endswith(".log"):
                        log_files.append({
                            "path": line,
                            "web_server": ws_type,
                        })

        return {
            "web_servers": web_servers,
            "log_files": log_files,
            "suggested_path": log_files[0]["path"] if log_files else None,
        }

    async def get_site_detail(
        self,
        server: dict[str, Any],
        website: dict[str, Any],
        minutes: int = 5,
    ) -> dict[str, Any]:
        """Get detailed traffic stats for a specific website from its server logs.

        Returns top pages, status code breakdown, recent requests, user agents,
        and server system metrics — similar to the unified monitor drill-down.
        """
        conn = await self._get_ssh_conn(server)
        sudo_pass = server.get("sudo_password")

        # Determine which log file to read
        log_path = website.get("log_path")
        if not log_path:
            # Auto-detect: try nginx first, then apache
            log_path = "/var/log/nginx/*access*.log"

        cmd = f"tail -n 15000 {log_path} 2>/dev/null || echo ''"
        if sudo_pass:
            raw = await self._ssh_sudo_cmd(conn, cmd, sudo_pass)
        else:
            raw = await self._ssh_cmd(conn, cmd)

        # Extract the hostname from the website URL for filtering
        url_host = (website.get("url") or "").replace("https://", "").replace("http://", "").split("/")[0].lower()

        from datetime import datetime as dt
        window = minutes * 60
        cutoff = time.time() - window

        # Reuse the same regex as _ssh_nginx
        log_re = re.compile(
            r'(?:(?P<vhost>[a-zA-Z0-9._-]+)\s+)?'
            r'(?P<ip>[\d.]+)\s+\S+\s+\S+\s+'
            r'\[(?P<ts>[^\]]+)\]\s+'
            r'"(?P<method>\S+)\s+(?P<path>\S+)\s+\S+"\s+'
            r'(?P<status>\d+)\s+(?P<bytes>\d+)\s+'
            r'"(?P<ref>[^"]*)"\s+"(?P<ua>[^"]*)"'
        )
        file_header_re = re.compile(r'^==> .*/(?P<fname>[^/]+?)(?:\.access)?\.log <==$')
        _GENERIC_LOG_NAMES = {"access", "error", "default", "nginx", "combined", "main"}
        current_file_site: str | None = None

        pages: dict[str, int] = {}
        status_codes: dict[str, int] = {"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0}
        unique_ips: set[str] = set()
        user_agents: dict[str, int] = {"Browser": 0, "Bot": 0, "Monitor": 0}
        device_types: dict[str, int] = {"desktop": 0, "mobile": 0, "tablet": 0}
        recent_requests: list[dict[str, Any]] = []
        total_requests = 0
        total_bytes = 0

        for line in raw.strip().split("\n"):
            fh = file_header_re.match(line.strip())
            if fh:
                fname = fh.group("fname")
                current_file_site = None if fname in _GENERIC_LOG_NAMES else fname
                continue

            m = log_re.search(line)
            if not m:
                continue

            try:
                ts_str = m.group("ts")
                ts_dt = dt.strptime(ts_str, "%d/%b/%Y:%H:%M:%S %z")
                ts = ts_dt.timestamp()
            except (ValueError, TypeError):
                continue
            if ts < cutoff:
                continue

            # Filter to this specific site
            vhost = m.group("vhost") or current_file_site or "default"
            vhost_lower = vhost.lower()
            # Match by exact hostname, or by prefix (e.g. "25cent" matches "25cent.cloud")
            if url_host and vhost_lower != url_host:
                prefix = url_host.split(".")[0]
                if vhost_lower != prefix and not url_host.startswith(vhost_lower + "."):
                    continue

            total_requests += 1
            status_code = int(m.group("status"))
            ip = m.group("ip")
            method = m.group("method")
            path = m.group("path")
            ua = m.group("ua")
            bsent = int(m.group("bytes")) if m.group("bytes").isdigit() else 0
            total_bytes += bsent

            # Status code buckets
            bucket = f"{status_code // 100}xx"
            if bucket in status_codes:
                status_codes[bucket] += 1

            # Page tracking (skip static assets)
            if not self._STATIC_EXTS.search(path):
                pages[path] = pages.get(path, 0) + 1

            # Visitor tracking (exclude bots)
            if not self._BOT_PATTERNS.search(ua):
                unique_ips.add(ip)

            # User agent classification
            if self._BOT_PATTERNS.search(ua):
                if "httpx" in ua.lower() or "monitoring" in ua.lower():
                    user_agents["Monitor"] += 1
                else:
                    user_agents["Bot"] += 1
            else:
                user_agents["Browser"] += 1
                # Device type classification (only for browsers)
                ua_lower = ua.lower()
                if "ipad" in ua_lower or "tablet" in ua_lower or ("android" in ua_lower and "mobile" not in ua_lower):
                    device_types["tablet"] += 1
                elif self._MOBILE_UA.search(ua):
                    device_types["mobile"] += 1
                else:
                    device_types["desktop"] += 1

            recent_requests.append({
                "ts": ts,
                "method": method,
                "path": path,
                "status": status_code,
                "ip": ip,
                "ua": ua,
                "bytes": bsent,
                "site": url_host,
            })

        recent_requests.sort(key=lambda r: r.get("ts", 0), reverse=True)

        top_pages = sorted(pages.items(), key=lambda x: -x[1])[:20]

        # Get server system metrics from cache
        ssh_data = self.status_cache.get(f"ssh:{server['id']}", {})
        system_metrics = ssh_data.get("metrics", {})
        services = ssh_data.get("services", [])
        pm2 = ssh_data.get("pm2", [])
        fail2ban = ssh_data.get("fail2ban", {})

        # Parse active connections by port from cached ss output
        connection_types: dict[str, int] = {}
        raw_connections = system_metrics.get("raw_connections", "")
        if raw_connections:
            for cline in raw_connections.strip().split("\n"):
                parts = cline.split()
                if len(parts) >= 4:
                    local_addr = parts[3]
                    # Extract port from address like "0.0.0.0:80" or "[::]:443"
                    port_str = local_addr.rsplit(":", 1)[-1] if ":" in local_addr else ""
                    if port_str.isdigit():
                        port_label = port_str
                        connection_types[port_label] = connection_types.get(port_label, 0) + 1

        return {
            "traffic": {
                "name": website.get("name", url_host),
                "requests": total_requests,
                "unique_visitors": len(unique_ips),
                "req_per_min": round(total_requests / max(minutes, 1), 1),
                "top_pages": [{"path": p, "hits": h} for p, h in top_pages],
                "status_codes": status_codes,
                "user_agents": user_agents,
                "device_types": device_types,
                "bytes": total_bytes,
                "recent_requests": recent_requests[:50],
                "period_minutes": minutes,
            },
            "connection_types": connection_types,
            "system": system_metrics,
            "server_name": server.get("name", "Unknown"),
            "services": services,
            "pm2": pm2,
            "fail2ban": fail2ban,
        }

    # ══════════════════════════════════════════════════════════════
    #  Web Analytics (deep nginx log parse)
    # ══════════════════════════════════════════════════════════════

    _BOT_PATTERNS = re.compile(
        r'bot|spider|crawler|scrapy|curl|wget|python-requests|python-urllib|'
        r'go-http|java/|perl/|libwww|nmap|masscan|zgrab|censys|shodan|'
        r'dataforseo|semrush|ahrefs|mj12bot|dotbot|blexbot|yandexbot|'
        r'bingbot|googlebot|duckduckbot|baiduspider|slurp|facebot|'
        r'twitterbot|applebot|linkedinbot|headlesschrome|phantom|selenium|'
        r'scaninfo|nuclei|nikto|sqlmap',
        re.IGNORECASE,
    )
    _STATIC_EXTS = re.compile(
        r'\.(css|js|png|jpg|jpeg|gif|svg|ico|woff|woff2|ttf|eot|map|webp|'
        r'mp4|mp3|pdf|zip|gz|tar|exe|dmg|deb|apk|xml|txt|json|webmanifest|'
        r'otf|avif)(\?.*)?$',
        re.IGNORECASE,
    )
    _MOBILE_UA = re.compile(r'Mobile|Android|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini', re.IGNORECASE)

    async def get_analytics(self, server: dict[str, Any], days: int = 30) -> dict[str, Any]:
        """Public method: fetch analytics from a server via SSH."""
        empty: dict[str, Any] = {
            "pages": [], "topics": [], "sections": [], "traffic_by_day": [],
            "traffic_by_hour": [0] * 24, "status_codes": {},
            "top_ips": [], "referrers": [], "devices": {"mobile": 0, "desktop": 0, "bot": 0},
            "totals": {"views": 0, "unique_visitors": 0, "avg_daily": 0},
            "error": None,
        }
        try:
            conn = await self._get_ssh_conn(server)
            sudo_pass = server.get("sudo_password")
            return await self._ssh_analytics(conn, sudo_pass, days=days)
        except Exception as exc:
            logger.warning("Analytics SSH error for server %s: %s", server.get("id"), exc)
            empty["error"] = str(exc)
            return empty

    async def _ssh_analytics(
        self,
        conn: asyncssh.SSHClientConnection,
        sudo_pass: str | None,
        days: int = 30,
    ) -> dict[str, Any]:
        """Deep-read nginx logs and return structured analytics data."""
        from datetime import datetime, timezone

        # Read current + rotated logs (including gzip-compressed)
        cmd = (
            "(zcat /var/log/nginx/*.gz 2>/dev/null; "
            "cat /var/log/nginx/*access*.log 2>/dev/null) "
            "| tail -n 2000000 || echo ''"
        )
        if sudo_pass:
            raw = await self._ssh_sudo_cmd(conn, cmd, sudo_pass, timeout=120)
        else:
            raw = await self._ssh_cmd(conn, cmd, timeout=120)

        cutoff = time.time() - days * 86400

        log_re = re.compile(
            r'(?:(?P<vhost>[a-zA-Z0-9._-]+)\s+)?'
            r'(?P<ip>[\d.a-fA-F:]+)\s+\S+\s+\S+\s+'
            r'\[(?P<ts>[^\]]+)\]\s+'
            r'"(?P<method>\S+)\s+(?P<path>\S+)\s+\S+"\s+'
            r'(?P<status>\d+)\s+\S+\s+'
            r'"(?P<ref>[^"]*)"\s+"(?P<ua>[^"]*)"'
        )

        page_counts: dict[str, int] = {}
        section_counts: dict[str, int] = {}
        topic_counts: dict[str, int] = {}
        day_counts: dict[str, int] = {}
        hour_counts: list[int] = [0] * 24
        status_codes: dict[str, int] = {}
        ip_counts: dict[str, int] = {}
        referrer_counts: dict[str, int] = {}
        unique_ips: set[str] = set()
        devices = {"mobile": 0, "desktop": 0, "bot": 0}

        for line in raw.split("\n"):
            m = log_re.search(line)
            if not m:
                continue

            ua = m.group("ua")
            method = m.group("method")
            path = m.group("path").split("?")[0]  # strip query string
            status = m.group("status")
            ip = m.group("ip")
            ref = m.group("ref")

            # Parse timestamp
            try:
                dt = datetime.strptime(m.group("ts"), "%d/%b/%Y:%H:%M:%S %z")
                ts = dt.timestamp()
            except (ValueError, TypeError):
                continue

            if ts < cutoff:
                continue

            # Classify device / bot
            if self._BOT_PATTERNS.search(ua):
                devices["bot"] += 1
                continue  # exclude bots from all counts

            # Skip non-GET requests for page analytics
            if method not in ("GET", "HEAD"):
                pass  # still count status/ip for non-GET

            # Skip static assets for page/topic counts
            is_static = bool(self._STATIC_EXTS.search(path))

            day_key = dt.strftime("%Y-%m-%d")
            day_counts[day_key] = day_counts.get(day_key, 0) + 1
            hour_counts[dt.hour] += 1

            status_bucket = f"{status[0]}xx"
            status_codes[status_bucket] = status_codes.get(status_bucket, 0) + 1

            ip_counts[ip] = ip_counts.get(ip, 0) + 1
            unique_ips.add(ip)

            # Device type
            if self._MOBILE_UA.search(ua):
                devices["mobile"] += 1
            else:
                devices["desktop"] += 1

            # Referrer (skip empty, self, and common noise)
            if ref and ref != "-" and not ref.startswith("/"):
                try:
                    from urllib.parse import urlparse
                    parsed_ref = urlparse(ref)
                    ref_host = parsed_ref.netloc.lower().replace("www.", "")
                    if ref_host and len(ref_host) > 3:
                        referrer_counts[ref_host] = referrer_counts.get(ref_host, 0) + 1
                except Exception:
                    pass

            if is_static:
                continue

            # Normalize path
            clean_path = path.rstrip("/") or "/"
            if not clean_path:
                clean_path = "/"

            page_counts[clean_path] = page_counts.get(clean_path, 0) + 1

            # Section: first path segment
            parts = [p for p in clean_path.split("/") if p]
            if parts:
                section = "/" + parts[0]
            else:
                section = "/"
            section_counts[section] = section_counts.get(section, 0) + 1

            # Topics: extract slug from 2nd segment (blog posts, tools, etc.)
            if len(parts) >= 2:
                slug = parts[1]
                # Convert slug to readable topic name
                topic = slug.replace("-", " ").replace("_", " ").title()
                if topic and len(topic) > 1:
                    topic_counts[topic] = topic_counts.get(topic, 0) + 1

        # Sort and limit results
        def top_n(d: dict[str, int], n: int = 20) -> list[dict[str, Any]]:
            return [{"name": k, "count": v} for k, v in sorted(d.items(), key=lambda x: -x[1])[:n]]

        total_views = sum(day_counts.values())
        avg_daily = round(total_views / max(days, 1), 1)

        # Build daily traffic ordered by date
        from datetime import timedelta
        end_dt = datetime.now(tz=timezone.utc)
        traffic_by_day = []
        for i in range(days - 1, -1, -1):
            dk = (end_dt - timedelta(days=i)).strftime("%Y-%m-%d")
            traffic_by_day.append({"date": dk, "count": day_counts.get(dk, 0)})

        return {
            "pages": top_n(page_counts, 25),
            "topics": top_n(topic_counts, 20),
            "sections": top_n(section_counts, 15),
            "traffic_by_day": traffic_by_day,
            "traffic_by_hour": hour_counts,
            "status_codes": status_codes,
            "top_ips": top_n(ip_counts, 15),
            "referrers": top_n(referrer_counts, 15),
            "devices": devices,
            "totals": {
                "views": total_views,
                "unique_visitors": len(unique_ips),
                "avg_daily": avg_daily,
            },
            "error": None,
        }

    async def _ssh_fail2ban(self, conn: asyncssh.SSHClientConnection, sudo_pass: str | None = None) -> dict[str, Any]:
        """Query fail2ban status via SSH."""
        if sudo_pass:
            raw_status = await self._ssh_sudo_cmd(conn, "fail2ban-client status", sudo_pass)
        else:
            raw_status = await self._ssh_cmd(conn, "fail2ban-client status 2>/dev/null || sudo fail2ban-client status 2>/dev/null || echo ''")

        total_banned = 0
        active_bans = 0
        jails: list[dict[str, Any]] = []

        jail_match = re.search(r'Jail list:\s*(.+)', raw_status)
        if jail_match:
            jail_names = [j.strip() for j in jail_match.group(1).split(",") if j.strip()]
            for jail in jail_names:
                if sudo_pass:
                    jail_raw = await self._ssh_sudo_cmd(conn, f"fail2ban-client status {jail}", sudo_pass)
                else:
                    jail_raw = await self._ssh_cmd(conn, f"fail2ban-client status {jail} 2>/dev/null || sudo fail2ban-client status {jail} 2>/dev/null || echo ''")

                currently = 0
                total = 0
                m = re.search(r'Currently banned:\s*(\d+)', jail_raw)
                if m:
                    currently = int(m.group(1))
                m = re.search(r'Total banned:\s*(\d+)', jail_raw)
                if m:
                    total = int(m.group(1))

                banned_ips = []
                m = re.search(r'Banned IP list:\s*(.+)', jail_raw)
                if m:
                    banned_ips = m.group(1).strip().split()

                active_bans += currently
                total_banned += total
                jails.append({
                    "name": jail,
                    "currently_banned": currently,
                    "total_banned": total,
                    "banned_ips": banned_ips[:20],
                })

        return {
            "total_banned": total_banned,
            "active_bans": active_bans,
            "jails": jails,
        }

    # ══════════════════════════════════════════════════════════════
    #  Proxmox monitoring (API)
    # ══════════════════════════════════════════════════════════════

    async def _proxmox_loop(self) -> None:
        """Poll each configured Proxmox node for container/VM stats."""
        while self._running:
            try:
                nodes = await self.db.list_proxmox_nodes(enabled_only=True)
                tasks = [self._poll_proxmox(n) for n in nodes]
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Proxmox loop error: %s", e)
            interval = int(await self.db.get_setting("check_interval", "10"))
            await asyncio.sleep(max(interval * 3, 30))

    async def _poll_proxmox(self, node: dict[str, Any]) -> None:
        """Fetch LXC/VM stats from a Proxmox node via API token."""
        host = node["host"]
        port = node.get("port", 8006)
        pve_node = node.get("node", "pve")
        user = node["user"]
        token_id = node["token_id"]
        token_secret = node["token_secret"]
        verify_ssl = bool(node.get("verify_ssl", 0))
        nid = node["id"]
        cache_key = f"proxmox:{nid}"
        previous = self.status_cache.get(cache_key, {})

        base = f"https://{host}:{port}/api2/json"
        auth_header = f"PVEAPIToken={user}!{token_id}={token_secret}"
        headers = {"Authorization": auth_header}

        results: dict[str, Any] = {
            "node_id": nid,
            "name": node["name"],
            "last_update": time.time(),
            "status": "unknown",
            "containers": previous.get("containers", []),
            "vms": previous.get("vms", []),
            "node_status": previous.get("node_status", {}),
            "error": None,
        }

        try:
            async with httpx.AsyncClient(timeout=15.0, verify=verify_ssl) as client:
                # Node status (may 403 if token lacks Sys.Audit — that's fine, we still fetch guests)
                try:
                    r = await client.get(f"{base}/nodes/{pve_node}/status", headers=headers)
                    if r.status_code == 200:
                        ns = r.json().get("data", {})
                        cpu_val = ns.get("cpu", 0)
                        if isinstance(cpu_val, (int, float)):
                            cpu_pct = round(cpu_val * 100, 1)
                        else:
                            cpu_pct = 0.0
                        mem_info = ns.get("memory", {})
                        results["node_status"] = {
                            "cpu": cpu_pct,
                            "mem_used": mem_info.get("used", 0),
                            "mem_total": mem_info.get("total", 1),
                            "uptime": ns.get("uptime", 0),
                        }
                        results["status"] = "up"
                    elif r.status_code == 401:
                        results["error"] = "Authentication failed — check API token credentials"
                        results["status"] = "down"
                        logger.warning("Proxmox %s auth failed (401)", host)
                    elif r.status_code == 403:
                        # Token works but lacks Sys.Audit for node stats.
                        # Mark as "up" (token is valid) and try fetching guests.
                        results["status"] = "up"
                        results["error"] = (
                            "Node stats unavailable (403) — token lacks Sys.Audit permission. "
                            "In Proxmox: Datacenter → Permissions → Add, "
                            "Path=/, Role=PVEAuditor, assign to root@pam!monitor."
                        )
                        logger.info("Proxmox %s: node status 403 — fetching guests anyway", host)
                    else:
                        results["error"] = f"HTTP {r.status_code} from Proxmox API"
                        results["status"] = "down"
                        logger.warning("Proxmox %s returned %d", host, r.status_code)
                except Exception as exc:
                    results["status"] = "down"
                    results["error"] = f"Connection error: {exc}"
                    logger.debug("Proxmox node status error: %s", exc)

                # Fetch guests if node is reachable (including partial-403 access)
                if results["status"] == "up":
                    fresh_containers: list[dict[str, Any]] = []
                    fresh_vms: list[dict[str, Any]] = []

                    # LXC containers
                    try:
                        r = await client.get(f"{base}/nodes/{pve_node}/lxc", headers=headers)
                        if r.status_code == 200:
                            for ct in r.json().get("data", []):
                                vmid = ct.get("vmid")
                                try:
                                    rs = await client.get(
                                        f"{base}/nodes/{pve_node}/lxc/{vmid}/status/current",
                                        headers=headers,
                                    )
                                    cdata = rs.json().get("data", {}) if rs.status_code == 200 else {}
                                except Exception:
                                    cdata = {}
                                ipv4 = None
                                try:
                                    ri = await client.get(
                                        f"{base}/nodes/{pve_node}/lxc/{vmid}/interfaces",
                                        headers=headers,
                                    )
                                    if ri.status_code == 200:
                                        iface_data = ri.json().get("data", [])
                                        for iface in iface_data:
                                            for addr in iface.get("inet", []) or []:
                                                ip = str(addr).split("/")[0]
                                                if ip and not ip.startswith("127."):
                                                    ipv4 = ip
                                                    break
                                            if ipv4:
                                                break
                                except Exception:
                                    ipv4 = None
                                maxmem = cdata.get("maxmem", 1) or 1
                                maxdisk = cdata.get("maxdisk", 1) or 1
                                fresh_containers.append({
                                    "vmid": vmid,
                                    "name": ct.get("name", f"CT{vmid}"),
                                    "status": ct.get("status", "unknown"),
                                    "ipv4": ipv4,
                                    "cpu_pct": round(cdata.get("cpu", 0) * 100, 1),
                                    "mem_used": cdata.get("mem", 0),
                                    "mem_total": maxmem,
                                    "mem_pct": round(cdata.get("mem", 0) / maxmem * 100, 1),
                                    "disk_used": cdata.get("disk", 0),
                                    "disk_total": maxdisk,
                                    "disk_pct": round(cdata.get("disk", 0) / maxdisk * 100, 1),
                                    "uptime": cdata.get("uptime", 0),
                                })
                            results["containers"] = fresh_containers
                    except Exception as exc:
                        logger.debug("Proxmox LXC list error: %s", exc)

                    # VMs
                    try:
                        r = await client.get(f"{base}/nodes/{pve_node}/qemu", headers=headers)
                        if r.status_code == 200:
                            for vm in r.json().get("data", []):
                                vmid = vm.get("vmid")
                                try:
                                    rs = await client.get(
                                        f"{base}/nodes/{pve_node}/qemu/{vmid}/status/current",
                                        headers=headers,
                                    )
                                    vdata = rs.json().get("data", {}) if rs.status_code == 200 else {}
                                except Exception:
                                    vdata = {}
                                maxmem = vdata.get("maxmem", 1) or 1
                                fresh_vms.append({
                                    "vmid": vmid,
                                    "name": vm.get("name", f"VM{vmid}"),
                                    "status": vm.get("status", "unknown"),
                                    "cpu_pct": round(vdata.get("cpu", 0) * 100, 1),
                                    "mem_used": vdata.get("mem", 0),
                                    "mem_total": maxmem,
                                    "mem_pct": round(vdata.get("mem", 0) / maxmem * 100, 1),
                                    "uptime": vdata.get("uptime", 0),
                                })
                            results["vms"] = fresh_vms
                    except Exception as exc:
                        logger.debug("Proxmox VM list error: %s", exc)

        except Exception as exc:
            results["error"] = str(exc)
            results["status"] = "down"
            logger.warning("Proxmox poll failed for node %d (%s): %s", nid, host, exc)

        self.status_cache[cache_key] = results

    # ══════════════════════════════════════════════════════════════
    #  Cleanup loop
    # ══════════════════════════════════════════════════════════════

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
            await asyncio.sleep(3600)

    # ══════════════════════════════════════════════════════════════
    #  Manual check
    # ══════════════════════════════════════════════════════════════

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

    # ══════════════════════════════════════════════════════════════
    #  Aggregate status
    # ══════════════════════════════════════════════════════════════

    def get_overview(self) -> dict[str, Any]:
        """Get a summary of all monitored targets from the cache."""
        servers = []
        services = []
        websites = []
        proxmox_nodes = []

        for key, val in self.status_cache.items():
            ttype, tid = key.split(":", 1)
            entry = {"id": int(tid), **val}
            if ttype == "server":
                servers.append(entry)
            elif ttype == "service":
                services.append(entry)
            elif ttype == "website":
                websites.append(entry)
            elif ttype == "proxmox":
                proxmox_nodes.append(entry)

        # Merge SSH data into server entries
        for srv in servers:
            ssh_data = self.status_cache.get(f"ssh:{srv['id']}")
            if ssh_data:
                srv["ssh"] = ssh_data

        total = len(servers) + len(services) + len(websites)
        up = sum(1 for s in servers + services + websites if s.get("status") == "up")
        down = sum(1 for s in servers + services + websites if s.get("status") == "down")
        warning = sum(1 for s in servers + services + websites if s.get("status") == "warning")

        # Build a mapping from nginx site short-names to full website hostnames.
        # e.g. "25cent" (from 25cent.access.log) -> "25cent.cloud" (from website URL)
        site_alias: dict[str, str] = {}
        website_hosts: list[str] = []
        for w in websites:
            host = (w.get("url") or "").replace("https://", "").replace("http://", "").split("/")[0].lower()
            if host:
                website_hosts.append(host)
        for host in website_hosts:
            # Map the prefix before first dot, e.g. "25cent" -> "25cent.cloud"
            prefix = host.split(".")[0]
            if prefix and prefix != host:
                site_alias.setdefault(prefix, host)

        # Aggregate nginx + fail2ban across all SSH-monitored servers
        all_recent_requests: list[dict[str, Any]] = []
        total_rpm = 0.0
        fail2ban_total = 0
        fail2ban_active = 0
        device_types: dict[str, int] = {"desktop": 0, "mobile": 0, "tablet": 0}
        connection_types: dict[str, int] = {}
        for srv in servers:
            ssh = srv.get("ssh") or {}
            nginx = ssh.get("nginx") or {}
            for req in nginx.get("recent_requests", []):
                # Normalize site names to match configured website hostnames
                raw_site = req.get("site", "default")
                normalized = site_alias.get(raw_site, raw_site)
                all_recent_requests.append({**req, "site": normalized, "server_name": srv.get("name", "")})

                # Classify device type from user agent
                ua = req.get("ua", "")
                if ua and not self._BOT_PATTERNS.search(ua):
                    ua_lower = ua.lower()
                    if "ipad" in ua_lower or "tablet" in ua_lower or ("android" in ua_lower and "mobile" not in ua_lower):
                        device_types["tablet"] += 1
                    elif self._MOBILE_UA.search(ua):
                        device_types["mobile"] += 1
                    else:
                        device_types["desktop"] += 1

            total_rpm += (nginx.get("totals") or {}).get("rpm", 0)
            fb = ssh.get("fail2ban") or {}
            fail2ban_total += fb.get("total_banned", 0)
            fail2ban_active += fb.get("active_bans", 0)

            # Aggregate connection types from SSH metrics
            raw_conns = (ssh.get("metrics") or {}).get("raw_connections", "")
            if raw_conns:
                for line in raw_conns.strip().splitlines():
                    parts = line.split()
                    if len(parts) >= 4:
                        local_addr = parts[3]
                        port = local_addr.rsplit(":", 1)[-1] if ":" in local_addr else ""
                        if port.isdigit() and int(port) < 10000:
                            connection_types[port] = connection_types.get(port, 0) + 1

        all_recent_requests.sort(key=lambda r: r.get("ts", 0), reverse=True)

        return {
            "servers": servers,
            "services": services,
            "websites": websites,
            "proxmox_nodes": proxmox_nodes,
            "nginx": {
                "total_rpm": round(total_rpm, 1),
                "recent_requests": all_recent_requests[:50],
            },
            "fail2ban": {
                "total_banned": fail2ban_total,
                "active_bans": fail2ban_active,
            },
            "device_types": device_types,
            "connection_types": connection_types,
            "stats": {
                "total": total,
                "up": up,
                "down": down,
                "warning": warning,
                "uptime_pct": round((up / total * 100) if total > 0 else 100, 1),
            },
        }
