"""CentienC — System tray icon (cross-platform via pystray).

Provides a lightweight tray icon that shows monitoring status,
lets you open the dashboard in a browser, control the background service,
and see a LAN-friendly URL for mobile app setup.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import webbrowser
from typing import Any

logger = logging.getLogger("centient.tray")

# We draw a tiny icon in pure Python (no external image files needed).
# Falls back gracefully if pystray/PIL aren't available.

ICON_SIZE = 64
COLOR_GREEN = (63, 185, 80)
COLOR_RED = (248, 81, 73)
COLOR_YELLOW = (210, 153, 34)
COLOR_BG = (22, 27, 34)
COLOR_ACCENT = (88, 166, 255)
_ICON_FILE = Path(__file__).resolve().parent / "static" / "centienc-logo.png"
_LAUNCHD_LABEL = "com.centient.monitor"
_WINDOWS_TASK_NAME = "centient"


def _has_display() -> bool:
    """Check if a graphical display is available."""
    import os, sys
    if sys.platform == "win32":
        return True
    if sys.platform == "darwin":
        return True
    # Linux/BSD — check for DISPLAY or WAYLAND_DISPLAY
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _create_icon_image(color: tuple[int, int, int] = COLOR_GREEN):
    """Create a small status icon using PIL."""
    from PIL import Image, ImageDraw

    if _ICON_FILE.exists():
        img = Image.open(_ICON_FILE).convert("RGBA").resize((ICON_SIZE, ICON_SIZE), Image.Resampling.LANCZOS)
    else:
        img = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle(
            [2, 2, ICON_SIZE - 2, ICON_SIZE - 2],
            radius=14,
            fill=COLOR_BG,
        )
        draw.text(
            (ICON_SIZE // 2, ICON_SIZE // 2 - 8),
            "C",
            fill=COLOR_ACCENT,
            anchor="mm",
        )

    draw = ImageDraw.Draw(img)
    # Status dot (bottom-right)
    dot_r = 8
    cx, cy = ICON_SIZE - 14, ICON_SIZE - 14
    draw.ellipse([cx - dot_r - 2, cy - dot_r - 2, cx + dot_r + 2, cy + dot_r + 2], fill=(0, 0, 0, 160))
    draw.ellipse([cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r], fill=color)
    return img


def _detect_local_ip() -> str:
    """Best-effort local LAN IP for onboarding mobile clients."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
    except Exception:
        pass

    try:
        hostname_ip = socket.gethostbyname(socket.gethostname())
        if hostname_ip and not hostname_ip.startswith("127."):
            return hostname_ip
    except Exception:
        pass

    return "127.0.0.1"


class CentientTray:
    """System tray icon for CentienC."""

    def __init__(self, port: int = 9099, host: str = "127.0.0.1"):
        self.port = port
        self.host = host
        self._icon = None
        self._shutdown_event = threading.Event()
        self._local_ip = _detect_local_ip()

    def _url(self) -> str:
        h = "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host
        return f"http://{h}:{self.port}"

    def _lan_url(self) -> str:
        return f"http://{self._local_ip}:{self.port}"

    def _on_open_dashboard(self, icon: Any, item: Any) -> None:
        webbrowser.open(self._url())

    def _on_open_dashboard_lan(self, icon: Any, item: Any) -> None:
        webbrowser.open(self._lan_url())

    def _notify(self, title: str, message: str) -> None:
        if self._icon and hasattr(self._icon, "notify"):
            try:
                self._icon.notify(message, title)
                return
            except Exception:
                pass
        logger.info("%s: %s", title, message)

    def _run_command(self, command: list[str]) -> bool:
        try:
            subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception as e:
            logger.warning("Command failed: %s (%s)", " ".join(command), e)
            return False

    def _service_control(self, action: str) -> bool:
        if sys.platform == "win32":
            if action == "start":
                return self._run_command(["schtasks", "/Run", "/TN", _WINDOWS_TASK_NAME])
            if action == "stop":
                return self._run_command(["schtasks", "/End", "/TN", _WINDOWS_TASK_NAME])
            if action == "restart":
                self._run_command(["schtasks", "/End", "/TN", _WINDOWS_TASK_NAME])
                return self._run_command(["schtasks", "/Run", "/TN", _WINDOWS_TASK_NAME])
            return False

        if sys.platform == "darwin":
            uid = str(os.getuid())
            target = f"gui/{uid}/{_LAUNCHD_LABEL}"
            if action == "start":
                ok = self._run_command(["launchctl", "start", _LAUNCHD_LABEL])
                return ok or self._run_command(["launchctl", "kickstart", target])
            if action == "stop":
                return self._run_command(["launchctl", "stop", _LAUNCHD_LABEL])
            if action == "restart":
                return self._run_command(["launchctl", "kickstart", "-k", target])
            return False

        return False

    def _on_start_service(self, icon: Any, item: Any) -> None:
        if self._service_control("start"):
            self._notify("CentienC", "Service started")
        else:
            self._notify("CentienC", "Unable to start service")

    def _on_stop_service(self, icon: Any, item: Any) -> None:
        if self._service_control("stop"):
            self._notify("CentienC", "Service stopped")
        else:
            self._notify("CentienC", "Unable to stop service")

    def _on_restart_service(self, icon: Any, item: Any) -> None:
        if self._service_control("restart"):
            self._notify("CentienC", "Service restarted")
        else:
            self._notify("CentienC", "Unable to restart service")

    def _on_quit(self, icon: Any, item: Any) -> None:
        logger.info("Quit requested from tray")
        self._shutdown_event.set()
        if self._icon:
            self._icon.stop()

    def update_status(self, status: str = "ok") -> None:
        """Update tray icon color: ok=green, warning=yellow, critical=red."""
        if not self._icon:
            return
        colors = {"ok": COLOR_GREEN, "warning": COLOR_YELLOW, "critical": COLOR_RED}
        color = colors.get(status, COLOR_GREEN)
        try:
            self._icon.icon = _create_icon_image(color)
        except Exception:
            pass

    def run(self, shutdown_callback=None) -> None:
        """Run the tray icon (blocking). Call from a thread."""
        try:
            import pystray
        except ImportError:
            logger.warning(
                "pystray or Pillow not installed — running without tray icon. "
                "Install with: pip install pystray Pillow"
            )
            return

        menu = pystray.Menu(
            pystray.MenuItem("Open Dashboard", self._on_open_dashboard, default=True),
            pystray.MenuItem("Open Dashboard (LAN)", self._on_open_dashboard_lan),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(f"Local URL: {self._url()}", None, enabled=False),
            pystray.MenuItem(f"LAN URL: {self._lan_url()}", None, enabled=False),
            pystray.MenuItem("Tip: Use LAN URL for iPhone setup", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Start Service", self._on_start_service),
            pystray.MenuItem("Stop Service", self._on_stop_service),
            pystray.MenuItem("Restart Service", self._on_restart_service),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(f"Port {self.port}", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._on_quit),
        )

        self._icon = pystray.Icon(
            name="CentienC",
            icon=_create_icon_image(),
            title="CentienC",
            menu=menu,
        )

        logger.info("Tray icon started")
        self._icon.run()  # blocks until stopped

        # After tray exits, trigger shutdown
        if shutdown_callback:
            shutdown_callback()

    @property
    def should_quit(self) -> bool:
        return self._shutdown_event.is_set()


def can_run_tray() -> bool:
    """Check if tray mode is possible (display + pystray available)."""
    if not _has_display():
        return False
    try:
        import pystray
        from PIL import Image
        return True
    except ImportError:
        return False
