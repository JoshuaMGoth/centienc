# CentienC License Server

Minimal license key generation and Stripe webhook handler for CentienC Pro.

## Setup

```bash
cd license-server
pip install fastapi uvicorn
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `CENTIENT_LICENSE_SECRET` | Yes | Same secret used in CentienC installs |
| `STRIPE_WEBHOOK_SECRET` | Yes | From Stripe Dashboard → Webhooks |
| `SMTP_HOST` | No | SMTP server (default: smtp.gmail.com) |
| `SMTP_PORT` | No | SMTP port (default: 587) |
| `SMTP_USER` | For email | SMTP login username |
| `SMTP_PASS` | For email | SMTP login password |
| `LICENSE_FROM_EMAIL` | No | From address (default: licenses@centienc.com) |

## Run

```bash
uvicorn server:app --host 0.0.0.0 --port 8100
```

## Generate Keys Manually

```bash
export CENTIENT_LICENSE_SECRET="your-secret"
python generate_key.py --tier pro --domain example.com --expires 2027-01-01
```

## Stripe Integration

1. Create products in Stripe Dashboard (Starter $9/mo, Pro $29/mo)
2. Add `metadata.tier` to each price (e.g. `starter` or `pro`)
3. Create a webhook endpoint pointing to `https://centienc.joshuagoth.com/license/api/stripe-webhook`
4. Select the `checkout.session.completed` event
5. Copy the webhook signing secret to `STRIPE_WEBHOOK_SECRET`

## Nginx Config

```nginx
location /license/ {
    proxy_pass http://127.0.0.1:8100/;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```
