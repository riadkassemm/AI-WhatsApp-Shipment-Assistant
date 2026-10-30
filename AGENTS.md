# Repository Guidelines

## Project Structure & Module Organization

- `app/`: FastAPI application, chatbot logic, authentication, support services, and database repositories. Keep HTTP handling, business rules, and persistence in their existing layers.
- `app/templates/support.html` and `app/static/support/`: support dashboard markup, CSS, and JavaScript. `app/support_ui.py` serves versioned assets; preserve script ordering and routes.
- `whatsapp_bridge/src/`: Node.js linked-device transport; bridge tests live under `whatsapp_bridge/`.
- `tests/`: Python regressions; `tests/integration/` exercises real MariaDB and Redis.
- `migrations/`: ordered SQL migrations. `scripts/`, `deployment/`, and `docs/` contain maintenance commands, service configuration, and operating guides.

## Build, Test, and Development Commands

```bash
python -m venv venv
venv/bin/python -m pip install -r requirements-dev.txt
npm ci --prefix whatsapp_bridge
venv/bin/uvicorn app.main:app --reload --port 8000
npm start --prefix whatsapp_bridge
```

These install dependencies and start the API and linked-device bridge. Configure private environment variables first; see `.env.example` and `docs/support-platform.md`.

```bash
venv/bin/python -m pytest --ignore=tests/integration
npm test --prefix whatsapp_bridge
venv/bin/python scripts/check_support_assets.py
venv/bin/python -m compileall -q app scripts tests
git diff --check
```

These run unit/bridge regressions, JavaScript syntax checks, Python compilation, and whitespace checks. There is no dedicated formatter/linter configuration.

## Coding Style & Naming Conventions

Use four-space Python indentation, type hints, `snake_case` functions/modules, and `PascalCase` classes. Dashboard JavaScript uses two-space indentation and `camelCase`; follow surrounding bridge conventions. Name migrations `YYYYMMDD_description.sql`; preserve existing migrations and make additions safe to apply to existing databases.

## Testing Guidelines

Use pytest/pytest-asyncio with `test_*.py` files and Node's built-in test runner. Cover changed behavior and authorization boundaries; no numeric coverage threshold is configured. Use deterministic synchronization for concurrency tests.

Run integration tests separately from unit tests: historical unit modules install dependency doubles. Start disposable services with `docker compose -f deployment/test-services.compose.yml up -d --wait`, then follow the environment and pytest commands in `docs/support-platform.md`. Mock WhatsApp services; never send customer messages. Set `SUPPORT_TEST_BROWSER` to explicitly select Chrome/Chromium for browser regressions.

## Commit & Pull Request Guidelines

Use short imperative subjects, matching history: “Fix …”, “Add …”, or “Extract …”. Keep behavioral fixes and refactors separate. Describe behavior, verification results, migrations, and deployment impact; link relevant issues and include screenshots for visible UI changes.

Workflow:

- Developer PC: source editing and Git only. The build, test, and run commands above are executed on the DEV VPS, not on the PC.
- DEV VPS: builds, tests, runtime services, and deployment, with disposable or mocked services for tests.
- Committing, pushing, opening PRs, and deploying to the DEV VPS require authorization for the specific task; finishing a change is not that authorization.
- Never merge PRs automatically or deploy to production.
- Read-only tasks stay read-only unless explicitly expanded.

## Security & Configuration

Never commit secrets, session/pairing state, or customer media. Preserve conversation-access rules and delivery claims; uncertain provider outcomes must not be blindly resent.
