# Security Policy

## Supported Versions

| Version | Supported          |
|---------|--------------------|
| 1.0.x   | :white_check_mark: |

## Reporting a Vulnerability

If you discover a security vulnerability in CentienC, **please do not open a public issue**.

### How to Report

1. **Email**: Send a detailed report to the maintainer via [joshuagoth.com/contact](https://joshuagoth.com/contact)
2. **GitHub Private Advisory**: Use [GitHub's private security advisory feature](https://github.com/JoshuaMGoth/centienc/security/advisories/new)

### What to Include

- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

### Response Timeline

- **Acknowledgment**: Within 48 hours
- **Initial Assessment**: Within 7 days
- **Fix & Disclosure**: Coordinated with reporter; typically within 30 days

### What to Expect

- You will receive an acknowledgment within 48 hours
- We will work with you to understand and validate the issue
- A fix will be developed and tested privately
- A security advisory will be published with the fix release
- Credit will be given to the reporter (unless anonymity is requested)

## Security Best Practices for Deployment

### Authentication
- Change the default `admin` / `changeme` credentials immediately after installation
- Use the "secured" mode in the setup wizard for production deployments
- Use strong, unique passwords

### Network
- Place CentienC behind a reverse proxy (nginx, Caddy) with HTTPS
- Restrict access to the dashboard port (9099) via firewall rules
- Use SSH key-based authentication for monitored servers — avoid storing passwords

### SSH Keys
- The generated SSH monitoring key has limited scope — it only needs read access
- Regularly rotate SSH keys used for monitoring
- Use a dedicated non-root user on monitored servers

## Scope

This security policy applies to the CentienC application code in this repository. Third-party dependencies are managed via `pip` and should be kept up to date.

---

Thank you for helping keep CentienC and its users safe.
