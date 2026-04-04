#!/usr/bin/env python3
"""CentienC License Server.

A minimal FastAPI service that:
  1. Validates Stripe webhook events (checkout.session.completed)
  2. Generates a signed CentienC Pro license key
  3. Sends the key to the customer via email (SMTP)
  4. Provides a /api/validate endpoint for key verification

Deploy behind nginx at centienc.joshuagoth.com/license/

Environment variables:
  CENTIENT_LICENSE_SECRET  — HMAC secret shared with CentienC installs
  STRIPE_SECRET_KEY        — Stripe API secret key
  STRIPE_WEBHOOK_SECRET    — Stripe webhook signing secret
  SMTP_HOST                — SMTP server (default: smtp.gmail.com)
  SMTP_PORT                — SMTP port (default: 587)
  SMTP_USER                — SMTP auth username
  SMTP_PASS                — SMTP auth password
  LICENSE_FROM_EMAIL        — From address for license emails
"""
import hashlib
import hmac as _hmac
import json
import logging
import os
import smtplib
import sqlite3
from contextlib import asynccontextmanager
from datetime import date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from generate_key import generate_license_key

logger = logging.getLogger("license-server")
logging.basicConfig(level=logging.INFO)

# ── Config ────────────────────────────────────────────────────────
LICENSE_SECRET = os.environ.get("CENTIENT_LICENSE_SECRET", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
LICENSE_FROM_EMAIL = os.environ.get("LICENSE_FROM_EMAIL", "licenses@centienc.com")

DB_PATH = Path(__file__).parent / "licenses.db"

# ── Tier mapping from Stripe price IDs ───────────────────────────
# Map your Stripe Price IDs here after creating them in the dashboard.
PRICE_TIER_MAP: dict[str, dict] = {
    # "price_xxx": {"tier": "starter", "expires_months": 12},
    # "price_yyy": {"tier": "pro", "expires_months": 12},
}

# ── Database ─────────────────────────────────────────────────────
def _init_db() -> None:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS licenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            tier TEXT NOT NULL DEFAULT 'pro',
            domain TEXT,
            license_key TEXT NOT NULL,
            stripe_session_id TEXT,
            expires TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lic_email ON licenses(email)")
    conn.commit()
    conn.close()

def _store_license(email: str, tier: str, domain: str | None,
                   key: str, session_id: str | None, expires: str | None) -> None:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        "INSERT INTO licenses (email, tier, domain, license_key, stripe_session_id, expires) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (email, tier, domain, key, session_id, expires),
    )
    conn.commit()
    conn.close()


# ── Email ────────────────────────────────────────────────────────
def _send_license_email(to_email: str, key: str, tier: str) -> None:
    if not SMTP_USER or not SMTP_PASS:
        logger.warning("SMTP not configured — skipping email to %s", to_email)
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Your CentienC {tier.title()} License Key"
    msg["From"] = LICENSE_FROM_EMAIL
    msg["To"] = to_email

    text = (
        f"Thank you for purchasing CentienC {tier.title()}!\n\n"
        f"Your license key:\n{key}\n\n"
        f"To activate:\n"
        f"1. Open your CentienC dashboard\n"
        f"2. Go to Admin → License\n"
        f"3. Paste your key and click Activate\n\n"
        f"All Pro features will unlock immediately.\n\n"
        f"Need help? Reply to this email or visit https://centienc.joshuagoth.com\n"
    )

    html = f"""\
    <div style="font-family:Inter,sans-serif;max-width:560px;margin:0 auto;padding:32px;background:#0D1B2A;color:#F0F4FA;border-radius:16px">
      <h1 style="color:#2D7FF9;margin:0 0 20px">CentienC {tier.title()}</h1>
      <p>Thank you for your purchase! Here is your license key:</p>
      <div style="background:#152038;border:1px solid #1E3055;border-radius:10px;padding:18px 22px;margin:20px 0;font-family:monospace;font-size:14px;letter-spacing:.03em;word-break:break-all">{key}</div>
      <h3 style="color:#2D7FF9;margin:24px 0 12px">How to activate</h3>
      <ol style="color:#8899B4;line-height:2">
        <li>Open your CentienC dashboard</li>
        <li>Go to <strong style="color:#F0F4FA">Admin → License</strong></li>
        <li>Paste your key and click <strong style="color:#F0F4FA">Activate</strong></li>
      </ol>
      <p style="color:#8899B4">All Pro features unlock immediately. Need help? Reply to this email.</p>
      <hr style="border:none;border-top:1px solid #1E3055;margin:28px 0">
      <p style="color:#5A6B85;font-size:12px">CentienC · <a href="https://centienc.joshuagoth.com" style="color:#2D7FF9">centienc.joshuagoth.com</a></p>
    </div>
    """

    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)

    logger.info("License email sent to %s", to_email)


# ── Stripe webhook verification ──────────────────────────────────
def _verify_stripe_signature(payload: bytes, sig_header: str) -> dict:
    """Verify a Stripe webhook signature and return the parsed event."""
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(500, "Stripe webhook secret not configured")

    parts = dict(item.split("=", 1) for item in sig_header.split(",") if "=" in item)
    timestamp = parts.get("t", "")
    expected_sig = parts.get("v1", "")

    signed_payload = f"{timestamp}.{payload.decode()}".encode()
    computed = _hmac.new(
        STRIPE_WEBHOOK_SECRET.encode(), signed_payload, hashlib.sha256
    ).hexdigest()

    if not _hmac.compare_digest(computed, expected_sig):
        raise HTTPException(400, "Invalid Stripe signature")

    return json.loads(payload)


# ── App ──────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    _init_db()
    yield

app = FastAPI(title="CentienC License Server", lifespan=lifespan)


@app.post("/api/stripe-webhook")
async def stripe_webhook(request: Request):
    """Handle Stripe checkout.session.completed events."""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    event = _verify_stripe_signature(payload, sig_header)

    if event.get("type") != "checkout.session.completed":
        return {"ok": True, "action": "ignored"}

    session = event["data"]["object"]
    customer_email = session.get("customer_email") or session.get("customer_details", {}).get("email")

    if not customer_email:
        logger.error("No email in Stripe session %s", session.get("id"))
        return {"ok": False, "error": "No customer email"}

    # Determine tier from line items / metadata
    tier = session.get("metadata", {}).get("tier", "pro")
    domain = session.get("metadata", {}).get("domain")

    # Calculate expiration (1 year from purchase)
    expires = (date.today() + timedelta(days=365)).isoformat()

    # Generate key
    key = generate_license_key(LICENSE_SECRET, tier=tier, domain=domain, expires=expires)

    # Store and email
    _store_license(customer_email, tier, domain, key, session.get("id"), expires)
    _send_license_email(customer_email, key, tier)

    logger.info("License generated for %s: tier=%s expires=%s", customer_email, tier, expires)
    return {"ok": True, "email": customer_email, "tier": tier}


@app.post("/api/validate")
async def validate_key(request: Request):
    """Validate a license key (public endpoint for CentienC installs)."""
    body = await request.json()
    key = str(body.get("key", "")).strip()
    if not key:
        raise HTTPException(400, "No key provided")

    # Use the same validation logic as the main app
    from generate_key import generate_license_key as _  # noqa: F401
    import base64

    try:
        parts = key.strip().split("-")
        if len(parts) < 3 or parts[0].upper() != "CENT":
            return {"valid": False, "message": "Invalid key format"}

        payload_b64 = parts[1]
        sig = parts[2].upper()

        expected = _hmac.new(
            LICENSE_SECRET.encode(), payload_b64.encode(), hashlib.sha256
        ).hexdigest()[:16].upper()

        if not _hmac.compare_digest(sig, expected):
            return {"valid": False, "message": "Invalid signature"}

        pad = (4 - len(payload_b64) % 4) % 4
        payload = json.loads(base64.b64decode(payload_b64 + "=" * pad).decode())
        expires = payload.get("expires")

        if expires and date.fromisoformat(expires) < date.today():
            return {"valid": False, "message": f"License expired {expires}"}

        return {"valid": True, "tier": payload.get("tier", "pro"), "expires": expires}
    except Exception:
        return {"valid": False, "message": "Validation error"}


@app.get("/api/health")
async def health():
    return {"ok": True, "service": "centienc-license-server"}
