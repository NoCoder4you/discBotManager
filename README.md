# Discord Bot Management Platform

A deny-by-default, multi-user foundation for managing heterogeneous Discord bots. Stage 1 provides FastAPI/Jinja UI, Discord OAuth2, database-backed sessions and registry, relational authorization, audit/events, operations, adapters, and safe service boundaries. It deliberately does **not** start bot processes or edit bot data.

## Requirements

- Python 3.11 or newer
- Discord application for remote login
- SQLite (included with Python); PostgreSQL can later replace it through SQLAlchemy

## Raspberry Pi / Linux installation

```bash
sudo apt update
sudo apt install python3 python3-pip python3-venv
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## Windows installation

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

## Environment configuration

Generate `APP_SECRET` with `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Never commit `.env`.

| Variable | Purpose |
|---|---|
| `APP_SECRET` | Required, random value of 32+ characters |
| `DATABASE_URL` | SQLAlchemy URL; initially `sqlite:///./platform.db` |
| `DISCORD_CLIENT_ID` | Discord application's client ID |
| `DISCORD_CLIENT_SECRET` | Discord OAuth client secret |
| `DISCORD_REDIRECT_URI` | Exact registered callback, ending in `/auth/callback` |
| `PLATFORM_OWNER_DISCORD_ID` | Permanent Discord ID bootstrapped as immutable owner at login |
| `ENVIRONMENT` | `development`, `test`, or `production`; production enables Secure cookies and requires OAuth/owner configuration |
| `HOST`, `PORT` | Uvicorn bind address and port |

## Discord OAuth setup

1. Create an application in the Discord Developer Portal.
2. Copy its Application/Client ID and reset/copy a Client Secret into `.env`.
3. Under OAuth2, add the **exact** `DISCORD_REDIRECT_URI` (for example `http://127.0.0.1:8000/auth/callback`).
4. No privileged intents or bot installation are needed. The platform requests only the `identify` scope.
5. Enable Developer Mode in Discord, copy your user ID, and set `PLATFORM_OWNER_DISCORD_ID`.

OAuth access tokens are used only server-side for the profile request and are never persisted.

## Database migrations

```bash
alembic upgrade head
# after an intentional model change:
alembic revision --autogenerate -m "describe change"
```

The first owner record is securely created/promoted when the configured Discord account completes OAuth. Bots and assignments should currently be created through a controlled seed/admin script or database migration; a registration UI is a Stage 2 feature.

## Run the development server

```bash
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Visit <http://127.0.0.1:8000>. Run tests with `pytest`.

> **Production warning:** Never expose Uvicorn's development/reload server directly to the internet. Use HTTPS with a maintained reverse proxy or secure tunnel, trusted proxy configuration, process supervision, restrictive filesystem permissions, and backups. Set `ENVIRONMENT=production` so cookies require HTTPS.

## Architecture and security

- **Permission order:** platform owner → explicit denial → explicit grant → per-bot role → future Discord-role mapping → default deny. Unknown and unauthorized bot IDs both produce the same not-found response.
- Platform roles are Owner, Administrator, Operator, and Viewer, but non-owner roles grant nothing implicitly. Per-resource permissions and assignments remain mandatory.
- Owner is derived from the configured permanent Discord ID at every login. There is no UI endpoint that can demote the owner.
- Sessions and OAuth state live server-side. Cookies contain only an opaque ID and are HttpOnly/SameSite=Lax (also Secure in production).
- Templates receive already-filtered resources; authorization occurs in reusable backend dependencies and services.
- Audit records have an append-only service surface. UTC timestamps and structured activity events support later consumers.
- Adapter health separates process-running, Discord-connected, and Discord-ready state. Quick actions are typed and cannot contain shell strings.
- File utilities reject absolute/traversal/symlink escapes and support flushed atomic replacement. No arbitrary file browser, SQL console, or command executor exists.

## Stage 1 boundaries and next stage

Working now: OAuth login/profile persistence, owner bootstrap, opaque server-side sessions, restricted dashboards, bot non-enumeration, permission resolution, schema/migrations, typed adapter/module/action/status contracts, audit/event/operation foundations, and responsive UI shell.

Intentionally deferred: user/bot/assignment administration UI, process execution, Discord bot control channel, console streaming, files/config/database editors, backups, schedulers, notifications, role mapping, and real adapter implementations.

**Recommended Stage 2:** build owner-only user/bot/role administration workflows (including CSRF-protected mutations, validation, audit emission, seed catalog, and integration tests), then implement one authenticated bot-agent/process adapter behind the existing service boundary. Do not add operational modules before assignments can be safely managed.

## Stage 2 administration

### Permission model and standard roles

Bot authorization is resolved in this exact order: **Platform Owner bypass → explicit denial → explicit grant → assigned bot role → default deny**. Disabled users and disabled assignments are denied before role access is considered. Bot Administrator is a bot-scoped role and never grants platform ownership, user administration, OAuth configuration, other bots, or global audit access.

The canonical catalog lives in `app/services/catalog.py`. **Viewer** is read-only (`bot.view`, console/command/cog/error/server/activity views). **Operator** adds routine start/restart, command sync, cog reload, backup creation, and scheduler execution. **Administrator** receives the full canonical per-bot catalog. Permissions for later-stage modules are seeded for stability but do not make those modules available.

Seed or safely reconcile the catalog at any time (the command is idempotent):

```bash
python -m app.seed
```

### User provisioning

1. A user authenticates with Discord and becomes a known user.
2. With no assignment they see only the safe “No bots assigned” state.
3. The owner opens **Users**, selects the user, assigns one enabled bot and chooses Viewer, Operator, or Administrator.
4. The owner may add an explicit Allow or Deny per permission. Removing it returns to inherited role behavior.
5. Disabling either the account or assignment revokes access immediately while preserving history.

All owner mutations require the server-side owner dependency, a session CSRF token, typed Pydantic validation, a transaction-scoped operation, append-only audit record, and structured activity event. New bots have no assignments and therefore remain owner-only.

### Registering and controlling a bot

Set `BOT_ROOT` to the directory under which managed bot folders reside. Registration requires a safe lowercase ID, display metadata, an existing folder under `BOT_ROOT`, an entry file that resolves inside that folder, an existing Python executable path, colour, enabled state, owner, and adapter name. Absolute/traversal/symlink escapes are rejected. Registration never executes a bot and assignments never change ownership.

The first integration is the generic Python Discord-bot adapter: it is the safest initial choice because it executes a registered entry file without requiring changes to heterogeneous bot source. `BotProcessManager` exclusively owns subprocess calls and per-bot async locks. It supports start, stop, restart, PID/uptime detail, exit status, duplicate/conflicting-action rejection, and honest `Discord state unknown` health. Process endpoints enforce `bot.view`, `bot.start`, `bot.stop`, or `bot.restart`, CSRF, operations, audit, and events. Automated tests use harmless local Python processes and never connect to Discord.

> Current process registry limitation: it is memory-local. Run a single application worker for Stage 2; restarts lose process tracking, and production-grade adoption/supervision belongs in Stage 3. CPU/memory sampling and Discord-ready telemetry are also deferred.

### Administration routes

- `/admin/users` and `/admin/users/{id}` — paginated/searchable known-user provisioning.
- `/admin/bots` — paginated/searchable bot registration.
- `/admin/audit` — paginated audit viewer with validated user/operation, action, result, bot, and date filters.
- `/api/bots/{bot_id}/status` and `/api/bots/{bot_id}/process/{start|stop|restart}` — non-enumerating, permission-scoped process API.

### Security notes and Stage 3 boundary

Visibility always comes from enabled database assignments; every action is checked on the backend and browser mutations require CSRF. Hidden and unknown bot detail/action requests share the same not-found behavior. A newly registered bot is visible only to the owner. Assignment administrators are not owners.

Stage 2 deliberately does not implement file/database editors, backups/restore, scheduler editing, full console streaming, metrics, maintenance, deployment history, custom modules, or Discord server tooling. Stage 3 should first add a durable external process supervisor/control channel and Discord-ready heartbeat, then console streaming and resource telemetry—without weakening the Stage 2 authorization boundary.
