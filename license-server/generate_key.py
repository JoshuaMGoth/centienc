#!/usr/bin/env python3
"""CentienC License Key Generator.

Generates HMAC-signed license keys compatible with the CentienC Pro
validation system.  Keys follow the format:

    CENT-{base64url_payload}-{hmac16upper}

Usage (CLI):
    export CENTIENT_LICENSE_SECRET="your-secret"
    python generate_key.py --tier pro --domain example.com --expires 2027-01-01

Usage (as module):
    from generate_key import generate_license_key
    key = generate_license_key(secret, tier="pro", domain="example.com",
                               expires="2027-01-01")
"""
import argparse
import base64
import hashlib
import hmac as _hmac
import json
import os
import sys
from datetime import date


def generate_license_key(
    secret: str,
    tier: str = "pro",
    domain: str | None = None,
    expires: str | None = None,
) -> str:
    """Generate a signed CentienC license key.

    Args:
        secret:  The CENTIENT_LICENSE_SECRET used by the target install.
        tier:    License tier (e.g. "starter", "pro").
        domain:  Optional domain restriction.
        expires: Optional ISO-8601 date string (YYYY-MM-DD).

    Returns:
        A ``CENT-…-…`` license key string.
    """
    if not secret:
        raise ValueError("License secret must not be empty")

    payload: dict = {"tier": tier}
    if domain:
        payload["domain"] = domain
    if expires:
        # Validate the date format
        date.fromisoformat(expires)
        payload["expires"] = expires

    payload_json = json.dumps(payload, separators=(",", ":"))
    payload_b64 = base64.urlsafe_b64encode(payload_json.encode()).decode().rstrip("=")

    sig = _hmac.new(
        secret.encode(), payload_b64.encode(), hashlib.sha256
    ).hexdigest()[:16].upper()

    return f"CENT-{payload_b64}-{sig}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a CentienC Pro license key")
    parser.add_argument("--secret", default=os.getenv("CENTIENT_LICENSE_SECRET", ""),
                        help="License secret (or set CENTIENT_LICENSE_SECRET env var)")
    parser.add_argument("--tier", default="pro", choices=["starter", "pro"],
                        help="License tier (default: pro)")
    parser.add_argument("--domain", default=None,
                        help="Optional domain restriction (e.g. example.com)")
    parser.add_argument("--expires", default=None,
                        help="Expiration date in YYYY-MM-DD format")
    args = parser.parse_args()

    secret = args.secret
    if not secret:
        print("Error: provide --secret or set CENTIENT_LICENSE_SECRET", file=sys.stderr)
        sys.exit(1)

    key = generate_license_key(secret, tier=args.tier, domain=args.domain, expires=args.expires)
    print(key)


if __name__ == "__main__":
    main()
