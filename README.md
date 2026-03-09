# ¢entien¢

Lightweight server, service, and website monitoring that runs as a desktop tray app or headless background service. ~30 MB RAM footprint.

![Version](https://img.shields.io/badge/version-1.0.0-blue)
![Python](https://img.shields.io/badge/python-3.10+-green)
![License](https://img.shields.io/badge/license-GPL--3.0-blue)

## Features

- **Server Monitoring** — ICMP ping checks with RTT tracking
- **Service Monitoring** — TCP port connectivity checks (SSH, databases, mail, etc.)
- **Website Monitoring** — HTTP/HTTPS checks with status code validation, SSL verification, and redirect following
- **Setup Wizard** — First-run configuration with open or password-protected access
- **Admin Panel** — Full CRUD for servers, services, websites, users, and notification channels
- **Notifications** — Email (SMTP), webhooks, and Discord alerts
- **Dark / Light Theme** — Toggle from the dashboard or admin settings
- **Incident Tracking** — Automatic detection and duration tracking of outages
- **History & Uptime** — Per-target historical check data with uptime percentage
- **Cross-Platform** — Linux, macOS, Windows, Proxmox LXC
- **Two Modes** — System tray icon (desktop) or headless service
- **Ultra-Low Profile** — Single process, SQLite DB, ~30 MB RAM

## Quick Start

### One-Line Install (Linux)

```bash
curl -fsSL https://raw.githubusercontent.com/joshuagoth/centient/main/installers/universal/install.sh | sudo bash
```

### Manual Install

```bash
# Clone the repository
git clone https://github.com/joshuagoth/centient.git
cd centient

# Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install (with tray icon support)
pip install -e ".[tray]"

# Run as tray app (shows icon in system tray, opens browser)
centient --tray --open

# Or run as headless service
centient --service
```

Open `http://localhost:9090` to run the setup wizard.

### Proxmox LXC Container

```bash
# On your Proxmox VE host:
bash installers/proxmox/create-centient-lxc.sh \
    --name centient \
    --ip 10.10.10.50/24 \
    --gw 10.10.10.1 \
    --cores 1 \
    --memory 512
```

This creates and configures a Debian 12 LXC container with ¢entien¢ pre-installed.

## Platform Installers

| Platform | Script | Notes |
|----------|--------|-------|
| **Debian/Ubuntu** | `installers/debian/install.sh` | apt + systemd, `--tray` flag |
| **Arch Linux** | `installers/arch/install.sh` | pacman + systemd, `--tray` flag |
| **Universal Linux** | `installers/universal/install.sh` | Auto-detects distro |
| **macOS** | `installers/macos/install.command` | Double-clickable wrapper for `install.sh` |
| **Windows** | `installers/windows/Centienc-Installer-Setup.exe` | Standard installer `.exe` (build from Inno Setup) |
| **Proxmox LXC** | `installers/proxmox/create-centient-lxc.sh` | Full container provisioning |

### Double-Click Installers

- **Linux (`.sh`)**: Ensure the script is executable (`chmod +x`) and choose “Run in Terminal” when prompted.
- **macOS (`.command`)**: Double-click `installers/macos/install.command` in Finder.
- **Windows (`.exe`, recommended)**: Run `Centienc-Installer-Setup.exe`.
- **Windows script fallback (`.bat`)**: Double-click `installers/windows/Centienc-Installer.bat`.

### Build Windows `.exe` Installer

The current industry standard for direct Windows installs is a signed `.exe` installer.

1. Install **Inno Setup 6** on Windows.
2. Open PowerShell in `installers/windows/`.
3. Build:

```powershell
./build-exe.ps1 -Version 1.0.0
```

Output file:

- `installers/windows/dist/Centienc-Installer-Setup.exe`

## Configuration

¢entien¢ stores its data in `~/.centient` (user mode) or `/var/lib/centient` (system install).

The database (`centient.db`) and all settings are managed through the web UI. See `config/centient.example.yml` for an overview of available settings.

### CLI Options

```
centient [OPTIONS]

Modes (mutually exclusive):
  --tray       Run with system tray icon (desktop, binds 127.0.0.1)
  --service    Run headless as a background service (binds 0.0.0.0)
  (default)    Auto-detect: tray if display available, service otherwise

Options:
  --host       Bind address        (auto per mode)
  --port       Listen port         (default: 9090)
  --data-dir   Data directory      (default: ~/.centient)
  --open       Open browser on start (tray mode)
  --version    Show version
```

### Environment Variables

| Variable | Description |
|----------|-------------|
| `CENTIENT_DATA_DIR` | Override the data directory path |
| `CENTIENT_JWT_SECRET` | Override the JWT signing secret |

## API Reference

All API routes return JSON. When auth is enabled, include a `Cookie: centient_token=<jwt>` or `Authorization: Bearer <jwt>` header.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Health check (no auth) |
| GET | `/api/overview` | Dashboard overview with stats |
| POST | `/api/setup` | Complete the setup wizard |
| POST | `/api/auth/login` | Login, returns JWT in cookie |
| POST | `/api/auth/logout` | Clear auth cookie |
| GET | `/api/servers` | List servers |
| POST | `/api/servers` | Add a server |
| PUT | `/api/servers/:id` | Update a server |
| DELETE | `/api/servers/:id` | Delete a server |
| POST | `/api/servers/:id/check` | Trigger immediate check |
| GET | `/api/servers/:id/history` | Get check history |
| GET | `/api/services` | List services |
| POST | `/api/services` | Add a service |
| PUT | `/api/services/:id` | Update |
| DELETE | `/api/services/:id` | Delete |
| GET | `/api/websites` | List websites |
| POST | `/api/websites` | Add a website |
| PUT | `/api/websites/:id` | Update |
| DELETE | `/api/websites/:id` | Delete |
| GET | `/api/settings` | Get all settings |
| PUT | `/api/settings` | Update settings |
| GET | `/api/users` | List admin users |
| POST | `/api/users` | Create user |
| GET | `/api/notifications` | List notification channels |
| POST | `/api/notifications` | Add channel |
| POST | `/api/notifications/:id/test` | Send test notification |
| GET | `/api/incidents` | List recent incidents |

## Architecture

```
centient/
├── __init__.py          # Version & metadata
├── __main__.py          # CLI entry point
├── app.py               # FastAPI application + all API routes
├── auth.py              # bcrypt password hashing + JWT
├── database.py          # Async SQLite layer (aiosqlite)
├── monitors.py          # Background monitoring workers
├── notifications.py     # Email, webhook, Discord dispatchers
├── templates/
│   ├── dashboard.html   # Main monitoring dashboard
│   ├── admin.html       # Settings & administration panel
│   ├── setup.html       # First-run setup wizard
│   └── login.html       # Login page
└── static/              # Additional static assets
```

## License

¢entien¢ is free software distributed under the [GNU General Public License v3.0](https://www.gnu.org/licenses/gpl-3.0.html).

A JoshuaGoth Software · Powered by Project CloudStrap

© Joshua Goth — [GitHub](https://github.com/JoshuaMGoth)
