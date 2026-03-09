# Centienc Installer Release Workflow

This workflow keeps installer files in one source repo and syncs them to your Hugo site downloads section.

## Source of truth

- Installer source repo: `monitoring-system/installers/`
- Hugo downloads mirror: `JoshuaGothBlog/static/downloads/centient/installers/`
- Hugo zip bundles: `JoshuaGothBlog/static/downloads/centient/packages/`
- Checksum files: `SHA256SUMS.txt` and `*.zip.sha256` in the same packages folder
- Updater manifest: `JoshuaGothBlog/static/downloads/centient/latest.json`
- Demo page mirror: `JoshuaGothBlog/static/downloads/centient/demo/index.html`

## Update steps (local)

1. Edit installers in `monitoring-system/installers/...`
2. Verify shell syntax:
   - `bash -n installers/arch/install.sh`
   - `bash -n installers/debian/install.sh`
   - `bash -n installers/universal/install.sh`
   - `bash -n installers/macos/install.sh`
   - `bash -n installers/proxmox/create-centient-lxc.sh`
3. Copy files into Hugo static downloads paths.
4. Build zip bundles (one per OS) in `static/downloads/centient/packages/`.
5. Generate checksums in that folder:
   - `sha256sum *.zip > SHA256SUMS.txt`
   - `for f in *.zip; do sha256sum "$f" > "$f.sha256"; done`
6. Update `latest.json` so it points to current package URLs/checksums.
7. Build Hugo locally:
   - `cd /home/jmgoth/Desktop/web-apps/JoshuaGothBlog`
   - `hugo --minify`
8. Verify generated files exist in:
   - `public/downloads/centient/installers/...`
   - `public/downloads/centient/packages/...`
   - `public/downloads/centient/packages/SHA256SUMS.txt`
   - `public/downloads/centient/packages/*.zip.sha256`
   - `public/downloads/centient/latest.json`
   - `public/downloads/centient/demo/index.html`
   - `public/tools/centient/index.html`
   - `public/downloads/centient/index.html`

## Why permanent downloads paths matter

Installers called from website links must live at stable URLs. Keeping files in `static/downloads/centient/` gives each installer a permanent path that users can bookmark and download at any time.

## Proxmox guidance

For personal desktop/tray use, Proxmox is optional and usually overkill. Keep it as an advanced deployment path for homelab/server users who want isolation and easy VM/LXC management.
