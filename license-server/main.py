"""CentienC License Server
========================
Handles license key generation, validation, and Stripe webhook processing.

Deploy on any $5 VPS or Render/Railway free tier.

Environment variables required:
  CENTIENT_LICENSE_SECRET  – must match the one in your centienc app
  STRIPE_SECRET_KEY        – from Stripe dashboard
  STRIPE_WEBHOOK_SECRET    – from Stripe webhook settings
  SMTP_HOST                – SMTP server for sending license emails
  SMTP_PORT                – usually 587
  SMTP_USER                – SMTP username
  SMTP_PASS                – SMTP password
  SMTP_FROM                – From address (e.g. licenses@centienc.joshuagoth.com)
  ADMIN_TOKEN              – A secret token for the /generate endpoint

Optional:
  LICENSE_DB               – Path to SQLite database (default: licenses.db)
  PORT                     – Port to listen on (default: 8000)
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import smtplib
import sqlite3
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import stripe
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("license-server")

app = FastAPI(title="CentienC License Server", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://centienc.joshuagoth.com"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# ─── Config ────────────────────────────────────────────────────────────────────
LICENSE_SECRET = os.environ.get("CENTIENT_LICENSE_SECRET", "")
STRIPE_SECRET = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "licenses@centienc.joshuagoth.com")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
DB_PATH = os.environ.get("LICENSE_DB", "licenses.db")
PRO_WHEEL_PATH = os.environ.get("PRO_WHEEL_PATH", "")
PRO_DOWNLOAD_BASE = os.environ.get("PRO_DOWNLOAD_BASE", "https://licenses.centienc.joshuagoth.com")
STRIPE_SUCCESS_URL = os.environ.get("STRIPE_SUCCESS_URL", "https://centienc.joshuagoth.com/#pricing")
STRIPE_CANCEL_URL = os.environ.get("STRIPE_CANCEL_URL", "https://centienc.joshuagoth.com/#pricing")

if STRIPE_SECRET:
    stripe.api_key = STRIPE_SECRET

# ─── Database ──────────────────────────────────────────────────────────────────

def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS licenses (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            key_text    TEXT    NOT NULL UNIQUE,
            email       TEXT    NOT NULL,
            tier        TEXT    NOT NULL DEFAULT 'pro',
            domain      TEXT,
            expires     TEXT,
            stripe_id   TEXT,
            created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_licenses_email ON licenses(email)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_licenses_stripe ON licenses(stripe_id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS download_tokens (
            token TEXT PRIMARY KEY,
            stripe_id TEXT,
            key_text TEXT,
            file_path TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            expires_at TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tokens_stripe ON download_tokens(stripe_id)")
    conn.commit()
    return conn


# ─── Key Generation ────────────────────────────────────────────────────────────

def _generate_key(
    tier: str = "pro",
    domain: str | None = None,
    expires: str | None = None,
) -> str:
    """Generate a CENT-{payload_b64}-{hmac16} license key.

    Format is intentionally identical to what centient/app.py's
    _validate_license_key() expects.
    """
    if not LICENSE_SECRET:
        raise RuntimeError("CENTIENT_LICENSE_SECRET is not set")

    payload: dict = {"tier": tier}
    if domain:
        payload["domain"] = domain
    if expires:
        payload["expires"] = expires

    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    payload_b64 = base64.urlsafe_b64encode(payload_json.encode()).rstrip(b"=").decode()

    sig = hmac.new(LICENSE_SECRET.encode(), payload_b64.encode(), "sha256").hexdigest()[:16].upper()
    return f"CENT-{payload_b64}-{sig}"


def _validate_key(key: str) -> dict:
    """Validate a license key (mirrors centient/app.py logic)."""
    if not LICENSE_SECRET or not key:
        return {"valid": False, "message": "Missing secret or key"}
    try:
        parts = key.split("-", 2)
        if len(parts) != 3 or parts[0] != "CENT":
            return {"valid": False, "message": "Invalid key format"}
        _, payload_b64, sig = parts
        expected = hmac.new(LICENSE_SECRET.encode(), payload_b64.encode(), "sha256").hexdigest()[:16].upper()
        if not hmac.compare_digest(sig, expected):
            return {"valid": False, "message": "Invalid key signature"}
        pad = 4 - len(payload_b64) % 4
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=" * pad).decode())
        expires = payload.get("expires")
        if expires and date.fromisoformat(expires) < date.today():
            return {"valid": False, "tier": payload.get("tier"), "expires": expires,
                    "message": f"License expired {expires}"}
        return {"valid": True, "tier": payload.get("tier", "pro"),
                "domain": payload.get("domain"), "expires": expires,
                "message": "License valid"}
    except Exception as exc:
        return {"valid": False, "message": f"Validation error: {exc}"}


# ─── Email ─────────────────────────────────────────────────────────────────────

def _send_license_email(to_email: str, key: str, tier: str, expires: str | None, download_token: str | None = None) -> None:
    if not SMTP_HOST or not SMTP_USER:
        logger.warning("SMTP not configured — skipping email to %s", to_email)
        return

    expires_line = f"Expires: {expires}" if expires else "Expires: Never (lifetime license)"

    download_html = ""
    download_text = ""
    if download_token and PRO_DOWNLOAD_BASE:
        dl = f"{PRO_DOWNLOAD_BASE.rstrip('/')}/download-pro/{download_token}"
        download_html = f"<p style=\"color: #8899B4; font-size: 0.9rem; margin: 0 0 28px;\">Download CentienC Pro: <a href=\"{dl}\">{dl}</a></p>"
        download_text = f"\nDownload CentienC Pro: {dl}\n"

    body_html = f"""
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="font-family: Inter, system-ui, sans-serif; background: #0A1628; color: #F0F4FA; margin: 0; padding: 0;">
  <div style="max-width: 560px; margin: 40px auto; background: #152038; border: 1px solid #1E3055; border-radius: 16px; padding: 40px;">
    <img src="https://centienc.joshuagoth.com/assets/centienc-logo.png" width="48" height="48" style="border-radius: 10px; margin-bottom: 20px;" alt="CentienC">
    <h1 style="margin: 0 0 8px; font-size: 1.4rem; color: #F0F4FA;">Your CentienC Pro License</h1>
    <p style="color: #8899B4; margin: 0 0 28px;">Thank you for purchasing CentienC Pro. Your license key is below.</p>

    <div style="background: #0A1628; border: 1px solid #1E3055; border-radius: 12px; padding: 20px; font-family: monospace; font-size: 1rem; letter-spacing: 0.04em; color: #2D7FF9; word-break: break-all; margin-bottom: 28px;">
      {key}
    </div>
        {download_html}

    <p style="color: #8899B4; font-size: 0.9rem; margin: 0 0 8px;">Tier: <strong style="color: #F0F4FA;">{tier.upper()}</strong></p>
    <p style="color: #8899B4; font-size: 0.9rem; margin: 0 0 28px;">{expires_line}</p>

    <h2 style="font-size: 1rem; margin: 0 0 12px;">How to activate</h2>
    <ol style="color: #8899B4; font-size: 0.9rem; padding-left: 20px; margin: 0 0 28px;">
      <li>Open your CentienC dashboard</li>
      <li>Go to <strong style="color: #F0F4FA;">Admin → Settings → License</strong></li>
      <li>Paste the key above and click <strong style="color: #F0F4FA;">Activate</strong></li>
    </ol>

    <p style="color: #8899B4; font-size: 0.85rem; margin: 0 0 4px;">Questions? Reply to this email or open an issue on GitHub.</p>
    <p style="color: #5A6B85; font-size: 0.8rem; margin: 0;">
      <a href="https://centienc.joshuagoth.com" style="color: #2D7FF9;">centienc.joshuagoth.com</a> ·
      <a href="https://github.com/JoshuaMGoth/centienc" style="color: #2D7FF9;">github.com/JoshuaMGoth/centienc</a>
    </p>
  </div>
</body>
</html>
"""
    body_text = f"""Your CentienC Pro License Key
==============================
{key}

Tier: {tier.upper()}
{expires_line}

How to activate:
1. Open your CentienC dashboard
2. Go to Admin → Settings → License
3. Paste the key above and click Activate

Questions? Reply to this email.
https://centienc.joshuagoth.com
"""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Your CentienC Pro License Key"
    msg["From"] = SMTP_FROM
    msg["To"] = to_email
    msg.attach(MIMEText(body_text, "plain"))
    msg.attach(MIMEText(body_html, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_FROM, [to_email], msg.as_string())
        logger.info("License email sent to %s", to_email)
    except Exception as exc:
        logger.error("Failed to send license email to %s: %s", to_email, exc)


# ─── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"ok": True, "service": "centienc-license-server"}


class GenerateRequest(BaseModel):
    email: str
    tier: str = "pro"
    domain: str | None = None
    expires_days: int | None = 365  # None = lifetime


@app.post("/generate")
async def generate_license(
    body: GenerateRequest,
    x_admin_token: str = Header(default=""),
):
    """Admin endpoint — generate a license key manually.

    Requires X-Admin-Token header matching ADMIN_TOKEN env var.
    """
    if not ADMIN_TOKEN or not hmac.compare_digest(x_admin_token, ADMIN_TOKEN):
        raise HTTPException(403, "Invalid admin token")

    expires: str | None = None
    if body.expires_days is not None:
        expires = (date.today() + timedelta(days=body.expires_days)).isoformat()

    key = _generate_key(tier=body.tier, domain=body.domain, expires=expires)

    with _db() as conn:
        try:
            conn.execute(
                "INSERT INTO licenses (key_text, email, tier, domain, expires) VALUES (?,?,?,?,?)",
                (key, body.email, body.tier, body.domain, expires),
            )
        except sqlite3.IntegrityError:
            pass  # key already exists (collision extremely unlikely)

    _send_license_email(body.email, key, body.tier, expires)
    logger.info("Manual license generated for %s tier=%s expires=%s", body.email, body.tier, expires)
    return {"ok": True, "key": key, "expires": expires}


@app.get("/validate/{key}")
async def validate_license(key: str):
    """Public endpoint — validate a license key."""
    result = _validate_key(key)
    return {"ok": True, **result}


@app.post("/stripe-webhook")
async def stripe_webhook(request: Request):
    """Stripe webhook — called after a successful purchase.

    Configure in Stripe dashboard:
      Endpoint URL: https://licenses.centienc.joshuagoth.com/stripe-webhook
      Events: checkout.session.completed, customer.subscription.created
    """
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(500, "Stripe webhook secret not configured")

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except stripe.error.SignatureVerificationError:
        raise HTTPException(400, "Invalid Stripe signature")
    except Exception as exc:
        raise HTTPException(400, f"Webhook error: {exc}")

    if event["type"] in ("checkout.session.completed", "customer.subscription.created"):
        session = event["data"]["object"]
        email = (
            session.get("customer_details", {}).get("email")
            or session.get("customer_email")
            or ""
        )
        if not email:
            logger.warning("Stripe event %s has no email", event["id"])
            return JSONResponse({"ok": True})

        # Determine license tier and duration from Stripe metadata
        metadata = session.get("metadata", {})
        tier = metadata.get("tier", "pro")
        expires_days_str = metadata.get("expires_days", "365")
        domain = metadata.get("domain") or None

        try:
            expires_days = int(expires_days_str) if expires_days_str else 365
        except ValueError:
            expires_days = 365

        expires: str | None = None
        if expires_days > 0:
            expires = (date.today() + timedelta(days=expires_days)).isoformat()

        stripe_id = session.get("id", "")
        key = _generate_key(tier=tier, domain=domain, expires=expires)

        with _db() as conn:
            try:
                conn.execute(
                    "INSERT INTO licenses (key_text, email, tier, domain, expires, stripe_id) VALUES (?,?,?,?,?,?)",
                    (key, email, tier, domain, expires, stripe_id),
                )
            except sqlite3.IntegrityError:
                # Re-fetch existing key for this stripe session and re-send
                row = conn.execute(
                    "SELECT key_text, tier, expires FROM licenses WHERE stripe_id=?",
                    (stripe_id,),
                ).fetchone()
                if row:
                    _send_license_email(email, row["key_text"], row["tier"], row["expires"], None)
                return JSONResponse({"ok": True, "duplicate": True})

        # Optionally create a one-time download token for proprietary package delivery
        download_token = None
        if PRO_WHEEL_PATH:
            import secrets
            token = secrets.token_urlsafe(32)
            # expires in 7 days
            expires_at = (date.today() + timedelta(days=7)).isoformat()
            with _db() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO download_tokens (token, stripe_id, key_text, file_path, expires_at) VALUES (?,?,?,?,?)",
                    (token, stripe_id, key, PRO_WHEEL_PATH, expires_at),
                )
                download_token = token

        # send email with token link if created
        if download_token:
            _send_license_email(email, key, tier, expires, download_token)
            logger.info("Pro download link: %s/download-pro/%s", PRO_DOWNLOAD_BASE.rstrip('/'), download_token)
        else:
            _send_license_email(email, key, tier, expires, None)

        logger.info("License issued via Stripe to %s (event=%s)", email, event["id"])

    return JSONResponse({"ok": True})


@app.get("/download-pro/{token}")
def download_pro(token: str):
    """Serve a one-time/short-lived download for the proprietary wheel mapped to a token."""
    if not PRO_WHEEL_PATH:
        raise HTTPException(404, "Pro package not configured")
    with _db() as conn:
        row = conn.execute("SELECT token, file_path, expires_at FROM download_tokens WHERE token=?", (token,)).fetchone()
        if not row:
            raise HTTPException(404, "Download token not found")
        # check expiry
        expires_at = row["expires_at"]
        if expires_at and date.fromisoformat(expires_at) < date.today():
            raise HTTPException(410, "Download token expired")
        file_path = row["file_path"]
        if not Path(file_path).exists():
            raise HTTPException(404, "Package file missing")
        # Optionally enforce single-use by deleting the token now
        conn.execute("DELETE FROM download_tokens WHERE token=?", (token,))
    from fastapi.responses import FileResponse
    return FileResponse(file_path, media_type="application/octet-stream", filename=Path(file_path).name)


@app.post("/create-checkout")
async def create_checkout(payload: dict):
    """Create a Stripe Checkout Session for a Pro license.

    Expects JSON: {"price_id": "price_...", "email": "buyer@example.com", "domain": "optional"}
    Returns: {url: <checkout_url>, id: <session_id>}
    """
    if not STRIPE_SECRET:
        raise HTTPException(500, "Stripe secret not configured")

    price_id = payload.get("price_id")
    email = payload.get("email")
    domain = payload.get("domain")
    if not price_id:
        raise HTTPException(400, "price_id is required")

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="payment",
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=STRIPE_SUCCESS_URL + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=STRIPE_CANCEL_URL,
            customer_email=email or None,
            metadata={"tier": "pro", "expires_days": "365", **({"domain": domain} if domain else {})},
        )
        return JSONResponse({"ok": True, "url": session.url, "id": session.id})
    except Exception as exc:
        logger.exception("Failed to create checkout session")
        raise HTTPException(500, f"Stripe error: {exc}")


# ─── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), reload=False)
