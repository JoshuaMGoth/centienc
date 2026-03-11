# Contributing to ¢entien¢

Thank you for your interest in contributing to ¢entien¢! This document provides guidelines and instructions for contributing.

## Code of Conduct

By participating in this project, you agree to maintain a respectful and constructive environment. Be kind, be helpful, and focus on building great software together.

## How to Contribute

### Reporting Bugs

1. Check [existing issues](https://github.com/JoshuaMGoth/centienc/issues) to avoid duplicates
2. Open a new issue using the **Bug Report** template
3. Include:
   - ¢entien¢ version (`centient --version`)
   - Operating system and version
   - Python version (`python3 --version`)
   - Steps to reproduce the bug
   - Expected vs. actual behavior
   - Relevant logs (`journalctl -u centient -n 50`)

### Suggesting Features

1. Open a new issue using the **Feature Request** template
2. Describe the feature, its use case, and how it benefits users
3. If possible, include mockups or examples

### Submitting Code

#### Setting Up Your Development Environment

```bash
# Fork and clone the repository
git clone https://github.com/<your-username>/centienc.git
cd centienc

# Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install in development mode with all extras
pip install -e ".[tray,dev]"

# Run locally
centient --port 9199
```

#### Making Changes

1. Create a feature branch from `main`:
   ```bash
   git checkout -b feature/your-feature-name
   ```
2. Make your changes, following the coding standards below
3. Test your changes thoroughly
4. Commit with clear, descriptive messages:
   ```bash
   git commit -m "Add: description of what was added"
   git commit -m "Fix: description of what was fixed"
   git commit -m "Change: description of what was changed"
   ```

#### Pull Request Process

1. Push your branch and open a pull request against `main`
2. Fill out the PR template with a description of your changes
3. Link any related issues
4. Wait for review — maintainers will review and provide feedback
5. Address any requested changes
6. Once approved, your PR will be merged

## Coding Standards

### Python
- Follow PEP 8 style guidelines
- Use type hints for function signatures
- Use `async`/`await` for all I/O-bound operations
- Keep functions focused and under 50 lines where practical
- Add docstrings to public functions and classes

### JavaScript (Dashboard HTML)
- All dashboard code is inline in HTML templates (no build step)
- Use vanilla JavaScript — no frameworks or bundlers
- Minimize external dependencies

### General
- Don't introduce new runtime dependencies without discussion
- Keep the RAM footprint low — ¢entien¢ targets ~30 MB
- All features must work in both tray and service modes
- Test on at least one Linux distribution before submitting

## Project Structure

```
centient/
├── app.py           # FastAPI routes and API endpoints
├── auth.py          # Authentication (bcrypt + JWT)
├── database.py      # SQLite database layer
├── monitors.py      # Background monitoring workers
├── notifications.py # Alert dispatchers (email, webhook, Discord)
├── tray.py          # System tray icon
├── templates/       # HTML dashboard templates
└── static/          # Static assets (icons, logos)
```

## Release Process

Releases are tagged on `main` and published as GitHub Releases. The version is defined in:
- `centient/__init__.py` (`__version__`)
- `pyproject.toml` (`version`)

Both must be updated together for a release.

## License

By contributing to ¢entien¢, you agree that your contributions will be licensed under the [GNU General Public License v3.0](LICENSE).

---

Questions? Open a [discussion](https://github.com/JoshuaMGoth/centienc/issues) or reach out via [joshuagoth.com](https://joshuagoth.com).
