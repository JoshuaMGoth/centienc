"""¢entien¢ — Notification dispatcher (email, webhook, Discord)."""

from __future__ import annotations

import json
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

import httpx

from .database import Database

logger = logging.getLogger("centient.notifications")


async def send_notification(
    db: Database,
    target_type: str,
    target_name: str,
    status: str,
    details: str | None = None,
) -> None:
    """Send alert to all enabled notification channels."""
    enabled = await db.get_setting("notifications_enabled", "false")
    if enabled != "true":
        return

    channels = await db.list_channels()
    for ch in channels:
        if not ch.get("enabled"):
            continue
        try:
            if ch["type"] == "email":
                await _send_email(ch, target_type, target_name, status, details)
            elif ch["type"] == "webhook":
                await _send_webhook(ch, target_type, target_name, status, details)
            elif ch["type"] == "discord":
                await _send_discord(ch, target_type, target_name, status, details)
        except Exception as e:
            logger.error("Notification failed (%s/%s): %s", ch["type"], ch["name"], e)


async def _send_email(
    channel: dict[str, Any],
    target_type: str,
    target_name: str,
    status: str,
    details: str | None,
) -> None:
    config = channel.get("config", {})
    if isinstance(config, str):
        config = json.loads(config)

    smtp_host = config.get("smtp_host", "localhost")
    smtp_port = int(config.get("smtp_port", 25))
    from_addr = config.get("from_address", "centient@localhost")
    to_addr = config.get("to_address")
    use_tls = config.get("use_tls", False)
    username = config.get("username")
    password = config.get("password")

    if not to_addr:
        return

    emoji = {"up": "✅", "down": "🔴", "warning": "⚠️"}.get(status, "ℹ️")
    subject = f"[¢entien¢] {emoji} {target_type.title()} {status.upper()}: {target_name}"

    body = (
        f"Target: {target_name}\n"
        f"Type:   {target_type}\n"
        f"Status: {status.upper()}\n"
    )
    if details:
        body += f"Detail: {details}\n"

    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.attach(MIMEText(body, "plain"))

    try:
        if use_tls:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=10) as s:
                if username and password:
                    s.login(username, password)
                s.sendmail(from_addr, [to_addr], msg.as_string())
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as s:
                s.ehlo()
                if username and password:
                    s.starttls()
                    s.login(username, password)
                s.sendmail(from_addr, [to_addr], msg.as_string())
        logger.info("Email notification sent to %s", to_addr)
    except Exception as e:
        logger.error("Email send failed: %s", e)
        raise


async def _send_webhook(
    channel: dict[str, Any],
    target_type: str,
    target_name: str,
    status: str,
    details: str | None,
) -> None:
    config = channel.get("config", {})
    if isinstance(config, str):
        config = json.loads(config)

    url = config.get("url")
    if not url:
        return

    payload = {
        "target_type": target_type,
        "target_name": target_name,
        "status": status,
        "details": details,
        "source": "¢entien¢",
    }

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
    logger.info("Webhook notification sent to %s", url)


async def _send_discord(
    channel: dict[str, Any],
    target_type: str,
    target_name: str,
    status: str,
    details: str | None,
) -> None:
    config = channel.get("config", {})
    if isinstance(config, str):
        config = json.loads(config)

    webhook_url = config.get("webhook_url")
    if not webhook_url:
        return

    color = {"up": 0x3FB950, "down": 0xF85149, "warning": 0xD29922}.get(status, 0x9DA7B3)
    emoji = {"up": "✅", "down": "🔴", "warning": "⚠️"}.get(status, "ℹ️")

    payload = {
        "embeds": [{
            "title": f"{emoji} {target_type.title()} {status.upper()}",
            "description": f"**{target_name}**\n{details or 'No additional details.'}",
            "color": color,
            "footer": {"text": "¢entien¢"},
        }],
    }

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(webhook_url, json=payload)
        resp.raise_for_status()
    logger.info("Discord notification sent")


async def test_channel(channel: dict[str, Any]) -> dict[str, Any]:
    """Send a test notification through a channel."""
    try:
        ch_type = channel["type"]
        if ch_type == "email":
            await _send_email(channel, "test", "Test Target", "up", "This is a test notification from ¢entien¢.")
        elif ch_type == "webhook":
            await _send_webhook(channel, "test", "Test Target", "up", "This is a test notification from ¢entien¢.")
        elif ch_type == "discord":
            await _send_discord(channel, "test", "Test Target", "up", "This is a test notification from ¢entien¢.")
        return {"ok": True, "message": "Test notification sent successfully"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
