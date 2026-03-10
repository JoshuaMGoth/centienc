"""¢entien¢ — CLI entry point.

Modes:
  --tray      Run with a system tray icon (desktop use, binds 127.0.0.1)
  --service   Run headless as a background service (binds 0.0.0.0)
  (default)   Auto-detect: tray if a display is available, service otherwise
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading

logger = logging.getLogger("centient")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="centient",
        description="¢entien¢ — Lightweight server monitoring",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--tray", action="store_true",
        help="Run with system tray icon (desktop mode, binds 127.0.0.1)",
    )
    mode_group.add_argument(
        "--service", action="store_true",
        help="Run headless as a background service (binds 0.0.0.0)",
    )
    parser.add_argument("--host", default=None, help="Bind address (auto-detected per mode)")
    parser.add_argument("--port", type=int, default=9090, help="Listen port (default: 9090)")
    parser.add_argument("--data-dir", default=None, help="Data directory (default: ~/.centient)")
    parser.add_argument(
        "--open", action="store_true",
        help="Open dashboard in browser on start (tray mode default)",
    )
    parser.add_argument(
        "--version", action="version",
        version=f"¢entien¢ {__import__('centient').__version__}",
    )
    args = parser.parse_args()

    # Data directory
    data_dir = (
        args.data_dir
        or os.environ.get("CENTIENT_DATA_DIR")
        or os.path.expanduser("~/.centient")
    )
    os.environ["CENTIENT_DATA_DIR"] = data_dir
    os.makedirs(data_dir, exist_ok=True)

    # Mode detection
    from .tray import can_run_tray

    if args.tray:
        use_tray = True
    elif args.service:
        use_tray = False
    else:
        use_tray = can_run_tray()

    # Bind address: tray → localhost only, service → all interfaces
    host = args.host or ("127.0.0.1" if use_tray else "0.0.0.0")
    port = args.port

    if use_tray:
        _run_tray_mode(host, port, auto_open=args.open)
    else:
        _run_service_mode(host, port)


def _run_service_mode(host: str, port: int) -> None:
    """Headless service mode — just uvicorn."""
    import uvicorn

    logger.info("Starting in service mode on %s:%d", host, port)
    uvicorn.run(
        "centient.app:app",
        host=host,
        port=port,
        log_level="info",
        access_log=False,
    )


def _run_tray_mode(host: str, port: int, auto_open: bool = False) -> None:
    """Desktop tray mode — uvicorn in a thread, tray icon on the main thread."""
    import uvicorn
    from .tray import CentientTray

    logger.info("Starting in tray mode on %s:%d", host, port)

    # Start uvicorn in a background thread
    config = uvicorn.Config(
        "centient.app:app",
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    # Auto-open browser
    if auto_open:
        import webbrowser
        import time
        threading.Thread(
            target=lambda: (time.sleep(1.5), webbrowser.open(f"http://127.0.0.1:{port}")),
            daemon=True,
        ).start()

    # Run tray on main thread (required by macOS)
    tray = CentientTray(port=port, host=host)

    def on_tray_quit():
        server.should_exit = True

    tray.run(shutdown_callback=on_tray_quit)

    # Wait for server to finish
    server_thread.join(timeout=5)
    logger.info("¢entien¢ shut down")


if __name__ == "__main__":
    main()
