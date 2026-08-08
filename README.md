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

> Stage 2 originally used a memory-local registry. Stage 3A replaces it with the durable supervisor described below. CPU/memory sampling and Discord-ready telemetry remain deferred.

## Stage 3A durable process supervisor

### Architecture and development startup

FastAPI remains the authenticated management interface, while the independently runnable supervisor owns bot processes. `BotProcessManager` sends only a registered bot ID over a strict loopback HTTP API; the supervisor resolves the executable, entry file, and working directory from the shared trusted registry. It never accepts a command line or uses a shell. Stopping FastAPI therefore closes only the management connection and does not signal bot processes.

Generate a separate credential (`python -c "import secrets; print(secrets.token_urlsafe(48))"`), set the same `SUPERVISOR_SECRET` for both services, migrate, and start the services in separate terminals:

```bash
alembic upgrade head
python -m app.supervisor
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

| Variable | Purpose / default |
|---|---|
| `SUPERVISOR_SECRET` | Dedicated internal credential; 32+ characters required in production |
| `SUPERVISOR_URL` | FastAPI client URL (`http://127.0.0.1:8765`) |
| `SUPERVISOR_HOST`, `SUPERVISOR_PORT` | Supervisor bind address and port (`127.0.0.1`, `8765`) |
| `SUPERVISOR_TIMEOUT` | Dashboard-to-supervisor request timeout (3 seconds) |
| `SUPERVISOR_STOP_TIMEOUT` | Graceful termination timeout before forceful termination (10 seconds) |

### Identity, generations, and reconciliation

Every launch creates a UUID-backed `INST-…` generation in `bot_instances`. The durable record contains PID, OS creation time, resolved Python executable, resolved entry file, working directory, timestamps, expected-running state, last state, and exit data. Adoption requires all available identity attributes to match; **PID alone is never trusted**. An unverifiable process is reported unknown/crashed as appropriate and is never killed automatically.

On supervisor startup or any status/reconcile request, persisted records are compared with OS processes. A strong match is adopted with the same PID, instance ID, start time, and uptime. A missing process expected to run becomes crashed; an intentionally stopped process remains offline. Duplicate starts are rejected under a non-blocking per-bot operation lock. Restart ends the old generation before creating a new one. Automatic crash restarting and host-boot restoration are intentionally not enabled in Stage 3A.

Supervisor unavailability is reported as `unknown`, not offline. FastAPI reconnects naturally on its next timeout-bounded request; no FastAPI restart is needed. Process-running status explicitly says Discord connectivity is unconfirmed until Stage 3B.

### Linux / Raspberry Pi production

Install the example [`deploy/discbot-supervisor.service`](deploy/discbot-supervisor.service), adjusting its user and paths, then run `sudo systemctl daemon-reload && sudo systemctl enable --now discbot-supervisor`. Run FastAPI in a **separate** service ordered after (but not lifecycle-coupled to) the supervisor. Both services need the registry database and the same internal credential. Keep port 8765 loopback-only and protect the environment file and database with restrictive permissions.

The detached child/session behavior is supported on Linux and Windows. OS permissions must allow the supervisor account to inspect and signal its children. A host reboot does not start every bot: manual, start-with-supervisor, and restore-previous-state policies are reserved for a later stage.

### Security

The internal API has no public frontend route and authenticates every request with a constant-time checked supervisor-only secret. Browser users can reach only assignment- and permission-scoped FastAPI endpoints, which retain CSRF protection and non-enumerating 404 behavior. Bot environments use a small host-variable allowlist plus `BOT_INSTANCE_ID`; dashboard/OAuth/supervisor secrets are not inherited. Raw command lines and host paths are not returned to users.

### Administration routes

- `/admin/users` and `/admin/users/{id}` — paginated/searchable known-user provisioning.
- `/admin/bots` — paginated/searchable bot registration.
- `/admin/audit` — paginated audit viewer with validated user/operation, action, result, bot, and date filters.
- `/api/bots/{bot_id}/status` and `/api/bots/{bot_id}/process/{start|stop|restart}` — non-enumerating, permission-scoped process API.

### Security notes and Stage 3 boundary

Visibility always comes from enabled database assignments; every action is checked on the backend and browser mutations require CSRF. Hidden and unknown bot detail/action requests share the same not-found behavior. A newly registered bot is visible only to the owner. Assignment administrators are not owners.

Stage 2 deliberately does not implement file/database editors, backups/restore, scheduler editing, full console streaming, metrics, maintenance, deployment history, custom modules, or Discord server tooling. Stage 3 should first add a durable external process supervisor/control channel and Discord-ready heartbeat, then console streaming and resource telemetry—without weakening the Stage 2 authorization boundary.

## Discord heartbeat architecture (Stage 3B)

A managed Discord bot uses the dependency-light `BotManagementAgent`, which sends a small heartbeat to the independently running supervisor's local-only HTTP listener (`127.0.0.1` by default). The supervisor authenticates and validates the message, stores only current state on the active `BotInstance`, and returns that state to FastAPI through the existing authenticated supervisor channel. FastAPI never needs to own the Discord bot lifecycle.

**Connected** means Discord's WebSocket is established. **Ready** means Discord has completed READY processing and the client can operate. A disconnect clears both flags; a reconnect first becomes connected/not-ready, and every `on_ready` revalidates readiness. `integrate_discord_client()` installs reconnect-safe `discord.py` listeners and obtains latency from `Client.latency` (converted from seconds to milliseconds) and guild count from `Client.guilds`.

### Agent authentication and provisioning

On every managed launch, the supervisor generates a dedicated 48-byte URL-safe instance credential with Python's `secrets` API. Only its SHA-256 verifier is stored server-side in the `bots.management_secret_hash` column and automatically supplied only to the managed process environment as `BOT_MANAGEMENT_SECRET`; it is never included in dashboard/API responses or audit data. This is distinct from the Discord token, OAuth secrets, `APP_SECRET`, and `SUPERVISOR_SECRET`. Rotation can replace this nullable per-bot credential before a later launch; existing credentials are never displayed.

Each message must authenticate, name the current `INST-…` generation, have a UTC timestamp within the clock-skew window, and be newer than the last accepted agent timestamp. Ended, stale-generation, disabled, non-running, too-fast, and oversized reports are rejected. Routine heartbeats update one current instance row and create no operations or audit records; activity events are emitted only for meaningful Discord transitions.

### Settings

* `BOT_HEARTBEAT_INTERVAL_SECONDS` (default `10`) controls normal agent reporting.
* `BOT_HEARTBEAT_TIMEOUT_SECONDS` (default `30`) controls freshness.
* `BOT_READY_TIMEOUT_SECONDS` (default `60`) is the startup grace period.
* `BOT_HEARTBEAT_CLOCK_SKEW_SECONDS` (default `60`) bounds timestamp replay acceptance.
* `BOT_HEARTBEAT_MIN_INTERVAL_SECONDS` (default `0.5`) limits abusive send rates.
* `SUPERVISOR_HOST` defaults to `127.0.0.1`; Stage 3B is intentionally same-host only.

### Operational state model

The central `BotStateResolver` applies this precedence: disabled registration → restart operation → stop operation → crash loop → unknown supervisor/process identity → crashed or offline process → fresh connected/ready heartbeat (maintenance, otherwise online) → startup grace (starting) → disconnected. Thus a running process alone is never online, and an expired heartbeat never means the process crashed.

To integrate another `discord.py` bot, construct `BotManagementAgent.from_environment()`, call `integrate_discord_client(client, agent)`, start the agent from the bot's async startup hook, and stop it during clean shutdown. `on_connect`, `on_disconnect`, and every `on_ready` update the reusable agent; reporting failures are isolated from those callbacks.

If FastAPI is unavailable, heartbeats continue to the supervisor and the Discord bot continues running. If the supervisor or heartbeat path is unavailable, the agent retries with capped exponential backoff while Discord operation continues. After infrastructure returns, a fresh current-instance heartbeat restores online state without restarting the bot. Full sharding aggregation, multi-host agents, console streaming, telemetry, and long-term uptime analytics are deliberately deferred.

## Live Console Architecture (Stage 3C)

```text
Bot stdout/stderr → durable supervisor → SecretRedactor → bounded ConsoleBroker
                    ├→ rotating per-bot logs
                    └→ authenticated supervisor API → permission-scoped FastAPI WebSocket
```

The durable supervisor opens and continuously drains both subprocess pipes as soon as it launches a bot; console viewing does not create pipe readers and is never required to prevent a child from blocking. Every structured record contains a monotonic sequence, UTC timestamp, validated bot ID, durable instance ID, `stdout`/`stderr`/`system` stream, and plain-text message. The in-memory history defaults to 5,000 records and each decoded UTF-8 line (invalid bytes use replacement characters) is limited to 16,384 characters. Instance start/end markers cannot be confused with bot output because their stream is `system`.

### Redaction and log rotation

`SecretRedactor` strips ANSI/control sequences and replaces known exact secrets and common token/key assignments with `[REDACTED]` before a record enters memory, disk, or fan-out. Exact values include the application secret, OAuth client secret, supervisor secret, database URL/credentials, secret-like supervisor environment values, and the generated bot-agent credential. Discord-token-shaped strings and labelled tokens, API keys, passwords, and secrets are also redacted. Operators should still register uncommon application secrets through protected supervisor environment variables whose names contain `TOKEN`, `SECRET`, `PASSWORD`, or `API_KEY`.

Logs use the validated internal bot ID beneath `CONSOLE_LOG_ROOT` (default `logs/bots`) and never a display name. `console.log` rotates at 10 MiB with five retained backups by default. Persistence errors mark console persistence unavailable but never propagate into, stop, or change Discord/process health.

### WebSocket permissions and safety

`/ws/bots/{bot_id}/console` authenticates the existing `dbm_session`, checks session expiry, enabled-user state, bot assignment, and the existing per-bot `console.view` permission **before accepting**. Unknown and unauthorised bots share a non-enumerating denial. Explicit denies continue to override roles/grants. Active subscriptions are marked for immediate revalidation by assignment, override, account-disable, and logout mutations and are periodically checked every five seconds for missed or external changes. Each user defaults to three connections, each bot to 25, and every subscriber queue is bounded at 1,000 records; lagging clients are disconnected without affecting other viewers.

The browser receives structured JSON and constructs DOM nodes with `textContent`, so output such as `<script>` and `<img onerror>` remains literal text. Pause, stream filters, auto-scroll, and Clear Display are browser-only controls. Disconnects retry at 1, 2, 5, 10, then 30 seconds; permission denial does not retry.

FastAPI can restart without controlling the bot or the supervisor's capture threads. Its replacement WebSocket retrieves the supervisor's bounded history and resumes live updates. A supervisor restart cannot reattach to stdout/stderr pipes belonging to an already adopted Stage 3A process; the bot remains running and Discord health remains independent, but console capture is unavailable until the next managed process generation. Stage 3C deliberately provides no console input, remote shell, Python execution, telemetry, file/database tooling, or long-term console analytics.
