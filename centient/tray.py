"""CentienC — System tray icon (cross-platform via pystray).

Provides a lightweight tray icon that shows monitoring status,
lets you open the dashboard in a browser, and quit the service.
"""

from __future__ import annotations

import io
import logging
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
    img = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Background rounded rect
    draw.rounded_rectangle(
        [2, 2, ICON_SIZE - 2, ICON_SIZE - 2],
        radius=14,
        fill=COLOR_BG,
    )
    # "C" letter
    draw.text(
        (ICON_SIZE // 2, ICON_SIZE // 2 - 8),
        "C",
        fill=COLOR_ACCENT,
        anchor="mm",
    )
    # Status dot (bottom-right)
    dot_r = 8
    cx, cy = ICON_SIZE - 14, ICON_SIZE - 14
    draw.ellipse([cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r], fill=color)
    return img


class CentientTray:
    """System tray icon for CentienC."""

    def __init__(self, port: int = 9099, host: str = "127.0.0.1"):
        self.port = port
        self.host = host
        self._icon = None
        self._shutdown_event = threading.Event()

    def _url(self) -> str:
        h = "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host
        return f"http://{h}:{self.port}"

    def _on_open_dashboard(self, icon: Any, item: Any) -> None:
        webbrowser.open(self._url())

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
            from PIL import Image
        except ImportError:
            logger.warning(
                "pystray or Pillow not installed — running without tray icon. "
                "Install with: pip install pystray Pillow"
            )
            return

        menu = pystray.Menu(
            pystray.MenuItem("Open Dashboard", self._on_open_dashboard, default=True),
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
