<p align="center">
  <img src="centienc-logo.png" alt="CentienC logo" width="120">
</p>

<h1 align="center">CentienC</h1>

<p align="center">
  <strong>Lightweight, self-hosted server &amp; infrastructure monitoring dashboard</strong><br>
  <em>Inspired by Prometheus · Customized for web developers in mind</em>
</p>

<p align="center">
  <a href="https://github.com/JoshuaMGoth/centienc/releases"><img src="https://img.shields.io/github/v/release/JoshuaMGoth/centienc?style=flat-square&color=blue" alt="Release"></a>
  <img src="https://img.shields.io/badge/python-3.10+-green?style=flat-square" alt="Python">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-GPL--3.0-blue?style=flat-square" alt="License"></a>
  <img src="https://img.shields.io/badge/RAM-~30MB-brightgreen?style=flat-square" alt="RAM">
  <a href="https://joshuagoth.com/downloads/centienc/"><img src="https://img.shields.io/badge/downloads-joshuagoth.com-orange?style=flat-square" alt="Downloads"></a>
</p>

---

## About

**CentienC** is a professional-grade, self-hosted monitoring dashboard that tracks your servers, services, websites, and Proxmox infrastructure — all from a single lightweight process. Inspired by **Prometheus**, CentienC was customized for web developers in mind — delivering real-time infrastructure visibility without the complexity of deploying and configuring multiple services.

There are no separate agents to install on your monitored machines. CentienC connects to your servers via SSH to collect system metrics (CPU, RAM, disk, network, nginx, PM2, systemd, fail2ban) in real time. For Proxmox users, API token integration gives you container and VM stats alongside your servers.

### Key Features

- **Server Monitoring** — ICMP ping, SSH-based CPU / RAM / disk / network / load metrics
- **Website Monitoring** — HTTP/HTTPS checks with status code validation and SSL verification
- **Service Monitoring** — TCP port connectivity checks (SSH, databases, mail, etc.)
- **Proxmox Integration** — Container and VM metrics via API token
- **Nginx Live Feed** — Real-time request log with per-site traffic breakdown
- **IP Geolocation** — Click any IP in the feed to see a map with city, ISP, and org data
- **Fail2ban & Firewall** — View banned IPs and active jail stats
- **PM2 Process Monitoring** — See all PM2 processes across your servers
- **Setup Wizard** — First-run configuration with open-access or password-protected modes
- **Admin Panel** — Full CRUD for servers, services, websites, users, and notification channels
- **Notifications** — Email (SMTP), webhooks, and Discord alerts on incidents
- **Dark / Light Theme** — Toggle from the dashboard or admin settings
- **Incident Tracking** — Automatic detection and duration tracking of outages
- **Update Notifications** — Dashboard alerts you when a new version is available
- **Cross-Platform** — Linux, macOS, Windows, Proxmox LXC
- **Two Modes** — System tray icon (desktop) or headless service
- **Ultra-Low Footprint** — Single process, SQLite database, ~30 MB RAM

---

## Quick Start

### One-Line Install (Linux)

**Debian / Ubuntu:**
```bash
curl -fsSL https://raw.githubusercontent.com/JoshuaMGoth/centienc/main/installers/debian/install.sh | sudo bash
```

**Arch Linux:**
```bash
curl -fsSL https://raw.githubusercontent.com/JoshuaMGoth/centienc/main/installers/arch/install.sh | sudo bash
```

**Fedora / RHEL / CentOS:**
```bash
curl -fsSL https://raw.githubusercontent.com/JoshuaMGoth/centienc/main/installers/fedora/install.sh | sudo bash
```

**Universal (auto-detects distro):**
```bash
curl -fsSL https://raw.githubusercontent.com/JoshuaMGoth/centienc/main/installers/universal/install.sh | sudo bash
```

After install, open `http://<your-ip>:9099` to run the setup wizard.

### Manual Install

```bash
git clone https://github.com/JoshuaMGoth/centienc.git
cd centienc
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[tray]"

# Run as tray app (opens browser)
centient --tray --open

# Or run as headless service
centient --service
```

### Proxmox LXC Container

Run directly on your Proxmox VE host:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/JoshuaMGoth/centienc/main/installers/proxmox/create-centienc-lxc.sh) \
    --name centienc --ip 10.10.10.50/24 --gw 10.10.10.1 --cores 1 --memory 512
```

### macOS & Windows

- **macOS**: Download and double-click [`centienc-installer.command`](https://raw.githubusercontent.com/JoshuaMGoth/centienc/main/installers/macos/centienc-installer.command)
- **Windows**: Download and run [`centienc-installer.bat`](https://raw.githubusercontent.com/JoshuaMGoth/centienc/main/installers/windows/centienc-installer.bat)

---

## Platform Installers

| Platform | Script | Method |
|----------|--------|--------|
| **Debian / Ubuntu** | [`installers/debian/install.sh`](installers/debian/install.sh) | apt + systemd |
| **Arch Linux** | [`installers/arch/install.sh`](installers/arch/install.sh) | pacman + systemd |
| **Fedora / RHEL** | [`installers/fedora/install.sh`](installers/fedora/install.sh) | dnf/yum + systemd |
| **Universal Linux** | [`installers/universal/install.sh`](installers/universal/install.sh) | Auto-detects distro |
| **macOS** | [`installers/macos/centienc-installer.command`](installers/macos/centienc-installer.command) | launchd agent |
| **Windows** | [`installers/windows/centienc-installer.bat`](installers/windows/centienc-installer.bat) | Service or tray |
| **Proxmox LXC** | [`installers/proxmox/create-centienc-lxc.sh`](installers/proxmox/create-centienc-lxc.sh) | LXC container |

All installers are also available at **[joshuagoth.com/downloads/centienc](https://joshuagoth.com/downloads/centienc/)**.

---

## Configuration

CentienC stores its data in `~/.centient` (user mode) or `/var/lib/centient` (system install). The database and all settings are managed through the web UI.

### CLI Options

```
centient [OPTIONS]

Modes (mutually exclusive):
  --tray       Run with system tray icon (desktop, binds 127.0.0.1)
  --service    Run headless as a background service (binds 0.0.0.0)
  (default)    Auto-detect: tray if display available, service otherwise

Options:
  --host       Bind address        (auto per mode)
  --port       Listen port         (default: 9099)
  --data-dir   Data directory      (default: ~/.centient)
  --open       Open browser on start (tray mode)
  --version    Show version
```

### Environment Variables

| Variable | Description |
|----------|-------------|
| `CENTIENT_DATA_DIR` | Override the data directory path |
| `CENTIENT_JWT_SECRET` | Override the JWT signing secret |

### Default Credentials

On first install, a default admin account is created:

| Field | Value |
|-------|-------|
| **Username** | `admin` |
| **Password** | `changeme` |

Change the password from the admin panel after login.

---

## API Reference

All API routes return JSON. When auth is enabled, include a `Cookie: centient_token=<jwt>` or `Authorization: Bearer <jwt>` header.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/health` | Health check (no auth) |
| `GET` | `/api/overview` | Dashboard overview with all stats |
| `GET` | `/api/update-check` | Check for new versions |
| `POST` | `/api/setup` | Complete the setup wizard |
| `POST` | `/api/auth/login` | Login, returns JWT in cookie |
| `POST` | `/api/auth/logout` | Clear auth cookie |
| `GET` | `/api/servers` | List servers |
| `POST` | `/api/servers` | Add a server |
| `PUT` | `/api/servers/:id` | Update |
| `DELETE` | `/api/servers/:id` | Delete |
| `GET` | `/api/websites` | List websites |
| `POST` | `/api/websites` | Add a website |
| `PUT` | `/api/websites/:id` | Update |
| `DELETE` | `/api/websites/:id` | Delete |
| `GET` | `/api/services` | List services |
| `POST` | `/api/services` | Add a service |
| `GET` | `/api/settings` | Get all settings |
| `PUT` | `/api/settings` | Update settings |
| `GET` | `/api/users` | List admin users |
| `POST` | `/api/users` | Create user |
| `GET` | `/api/notifications` | List notification channels |
| `POST` | `/api/notifications` | Add channel |
| `GET` | `/api/incidents` | List recent incidents |

---

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
├── tray.py              # System tray icon (optional)
├── templates/
│   ├── dashboard.html   # Main monitoring dashboard
│   ├── admin.html       # Settings & administration panel
│   ├── setup.html       # First-run setup wizard
│   └── login.html       # Login page
└── static/              # Icons, logos, and static assets
```

---

## Reporting Issues

Found a bug or have a feature request? We use **[GitHub Issues](https://github.com/JoshuaMGoth/centienc/issues)** for all bug reports and feature requests.

### How to Report a Bug

1. Go to [Issues → New Issue](https://github.com/JoshuaMGoth/centienc/issues/new/choose)
2. Select the **Bug Report** template
3. Include your OS, Python version, and CentienC version (`centient --version`)
4. Attach relevant logs: `journalctl -u centient -n 50` (Linux) or check the admin panel
5. Describe the expected vs. actual behavior

### How to Request a Feature

1. Go to [Issues → New Issue](https://github.com/JoshuaMGoth/centienc/issues/new/choose)
2. Select the **Feature Request** template
3. Describe the feature and the use case

### Security Vulnerabilities

Please **do not** open a public issue for security vulnerabilities. See [SECURITY.md](SECURITY.md) for responsible disclosure instructions.

---

## Contributing

Contributions are welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) before submitting a pull request.

---

## Updates

CentienC checks for updates automatically by comparing your installed version against the latest GitHub release. When a new version is available, a notification banner appears on your dashboard with a link to the release notes and upgrade instructions.

### Manual Update

**Systemd installs (Linux):**
```bash
cd /opt/centient && source venv/bin/activate
pip install --upgrade centient
systemctl restart centient
```

**Development installs:**
```bash
cd centienc && git pull
pip install -e ".[tray]"
```

---

## License

CentienC is free software distributed under the **[GNU General Public License v3.0](https://www.gnu.org/licenses/gpl-3.0.html)**.

```
Copyright (C) 2026 Joshua Goth

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.
```

See the [LICENSE](LICENSE) file for the full license text.

---

<p align="center">
  <strong>A <a href="https://joshuagoth.com">JoshuaGoth</a> Software</strong><br>
  <a href="https://github.com/JoshuaMGoth/centienc">GitHub</a> · <a href="https://joshuagoth.com">JoshuaGoth.com</a> · <a href="https://joshuagoth.com/downloads/centienc/">Downloads</a>
</p>
