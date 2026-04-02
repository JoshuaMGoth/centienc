centienc-pro (private)
=======================

Skeleton private plugin for CentienC Pro features. Build a wheel and upload it
to the license server, then set `PRO_WHEEL_PATH` on the license-server to
point at the wheel file so buyers can download it after purchase.

Build wheel:

```bash
python -m pip install --upgrade build
python -m build --wheel
# result in dist/centienc_pro-0.1.0-py3-none-any.whl
```

Upload/placement options:
- Place the wheel file on the license-server host (e.g. /opt/centienc-pro/dist/centienc_pro-0.1.0.whl)
- Set `PRO_WHEEL_PATH` to that absolute path and `PRO_DOWNLOAD_BASE` to the public base URL for the license server.

Installation on customer server:

```bash
pip install /path/to/centienc_pro-0.1.0-py3-none-any.whl
# or with private index: pip install --index-url https://... centienc-pro
```

After installation, restart CentienC service; the core will dynamically import
`centienc_pro` when a valid Pro license is active and call `register_pro(app, db, engine)`.
