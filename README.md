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

## Resource Monitoring Architecture (Stage 3D)

```text
Validated managed process → supervisor TelemetryCollector → bounded TelemetryStore
                          → authenticated supervisor API → bot.view-scoped FastAPI API → dashboard
```

The durable supervisor owns one central sampling loop for every managed bot, independent of the number of dashboard viewers. Before every sample it reuses Stage 3A's complete process identity validation: PID, OS creation time, resolved executable, working directory, and entry-point command must still match the current durable instance. PID alone is never sufficient. FastAPI and browser requests only read already-collected samples and never call `psutil`.

### CPU, memory, and uptime

CPU uses `psutil.Process.cpu_percent(None)`. A new process handle is primed on its first collection pass and is not published until a later interval produces a meaningful delta. Values use psutil's process convention: **100% represents one fully utilised logical CPU core**, so a multi-threaded process can exceed 100%. CPU is not normalized across total host capacity.

Memory is the main managed process's resident set size (`memory_info().rss`) stored as raw bytes. Child-process tree memory and host-wide memory are deliberately excluded. Uptime is calculated from the validated OS process creation timestamp, so refreshing or restarting FastAPI does not reset it; a new Stage 3A instance/process does.

### Sampling, history, freshness, and permissions

`TELEMETRY_INTERVAL_SECONDS` defaults to five seconds. `TELEMETRY_HISTORY_MINUTES` defaults to 60 minutes, giving 721 lightweight samples per bot at the default interval; bounded deques evict the oldest samples and SQLite receives no per-sample writes. Samples retain bot and instance IDs, old-instance history can remain within the short window, and only the current instance can be returned as live telemetry. `TELEMETRY_STALE_AFTER_SECONDS` defaults to 15 seconds. Missing, invalid, exited, or stale processes return unavailable metrics rather than old CPU/RSS values.

Both current and history APIs require the existing per-bot `bot.view` permission. Owner access, assignments, disabled users/assignments, explicit denies, and hidden-resource 404 behavior remain unchanged. The dashboard polls cached current data every five seconds and redraws lightweight Canvas CPU/RSS history every ten seconds.

Telemetry failures are isolated from process state, Discord readiness, and console capture. A supervisor restart safely adopts and primes telemetry for identity-validated processes; its in-memory short-term history resets, but process uptime remains based on the original OS creation time. This stage does not include host temperature/load, disk/network metrics, process-tree RSS, alerting, or persistent long-term analytics.

## Backup Architecture (Stage 4)

Authenticated FastAPI routes reuse bot assignments, the canonical permission resolver, CSRF sessions, operations, audit logs, and structured events. `BackupService` is the only filesystem boundary. Every bot has one relative data root, include/exclude patterns, an optional source version, and a conservative restore policy. Browser requests contain bot and backup IDs only, never filesystem paths.

### Creating a Backup

`BACKUP_ROOT` is central storage outside live bot data. Snapshots live at `<BACKUP_ROOT>/<bot-id>/<BKP-id>/` as a finalized `data.tar.gz` and `manifest.json`. Creation streams regular files into an unlisted temporary directory, verifies it, then atomically renames it into place. `BKP-…` identifies the snapshot and a separate `ACT-…` operation tracks the action. Incomplete or failed rows are not restorable.

The configured data root must resolve inside the registered bot folder. Walks never follow symlinks. Bot include patterns narrow the scope; excludes add to safe defaults for `.env*`, tokens, secrets, credentials, virtual environments, caches, Git data, logs, and backups. The store is rejected when it sits in selected live data. `BACKUP_MAX_SIZE_MB` and a reserved `BACKUP_MIN_FREE_MB` are checked before archive or staging work.

### Backup Verification

Manifests expose only relative POSIX paths, sizes, and SHA-256 checksums. Verification rejects absolute or traversing archive paths, links, unexpected/missing files, size or checksum differences, invalid included JSON, and failed SQLite integrity checks. Failed verification blocks normal restore. Preview compares current and archived checksums and reports added, removed, changed, or unchanged metadata; binary contents are never diffed or displayed.

### Restore Process and Pre-Restore Backup

Restore requires `backups.restore`, an eligible verified snapshot belonging to the same bot, CSRF, and typing the upper-case internal bot ID. That permission authorizes the predefined stop/restore/start workflow only; it does not grant arbitrary process controls. Every restore re-verifies the source and creates a verified, protected `PRE_RESTORE` safety snapshot. Failure to create it stops the restore.

A reusable per-bot lock prevents colliding restores and is intended for future editors. Safe extraction writes a sibling staging directory and validates it. Same-filesystem atomic renames move live data to rollback storage and staging to live; final checksums are validated before rollback storage is removed. Failure atomically returns the original directory where possible and records whether rollback succeeded. Rollback failure remains protected and is a critical manual-intervention condition.

### Bot Stop/Restart Restore Policies

`REQUIRES_STOP` is the default. A running bot is controlled only through `BotProcessManager` and the supervisor, restarted after data validation, then observed for up to 60 seconds using the existing Discord Ready state. A Ready timeout is reported separately from successful data restoration. `SUPPORTS_LIVE` is available only when an owner knows atomic live replacement is safe.

### Backup Retention and Pinned Backups

The model supports hourly, daily, weekly, monthly, manual, pre-edit, pre-restore, automatic, and system categories. Count limits use `BACKUP_RETENTION_HOURLY`, `BACKUP_RETENTION_DAILY`, `BACKUP_RETENTION_WEEKLY`, `BACKUP_RETENTION_MONTHLY`, and `BACKUP_RETENTION_MANUAL`; zero disables count cleanup for that category. Cleanup preserves the newest configured count and always skips pinned or protected snapshots. Pre-restore snapshots remain protected while rollback needs them. Pin/unpin is initially owner-only and audited. Routine cleanup publishes one `SYSTEM` event without impersonating a human or creating per-file audit spam.

### Permissions and Security

* `backups.view` allows bot-scoped lists, safe details, and previews.
* `backups.create` allows manual snapshots with a validated optional 200-character plain-text reason.
* `backups.restore` is the critical-risk permission for the controlled recovery workflow.

Unknown, unassigned, disabled, explicitly denied, wrong-bot, and guessed backup resources retain the existing non-enumerating 404 boundary. Cross-bot ownership is checked server-side for every detail, preview, pin, and restore. Human create, pin, restore request/result, and failure actions are audited without file contents; verification, restore, and retention transitions publish structured events. Download and manual deletion remain absent because the canonical catalog has no specific permissions for those sensitive actions.

### Known limitations

Filesystem work runs away from FastAPI's async event loop, but the operation worker is currently in-process rather than a durable external queue. Atomic directory exchange protects the replacement boundary and incomplete backup directories are never valid; automatic scheduling and restart-resumption of in-progress operations are deferred. One configured data root per bot is supported. Full text/JSON diff presentation, file editing, SQLite browsing, deployment history, and backup downloads are outside Stage 4.

## Stage 5 data management architecture

Stage 5 implements a deliberately bot-scoped data path: **Bot Data Root → Safe Path Resolver → Adapter Data Source Registry → JSON/Pydantic or custom validation → verified PRE_EDIT backup → atomic write → version history**. It is not a host file manager. Each bot must have exactly one relative `data_roots` entry beneath its registered folder; requests contain only relative paths, and resolution rejects absolute Unix/Windows paths, traversal, backslashes, symlinks, and targets outside that root.

### File and configuration permissions

`files.view` permits browsing safe metadata and viewing allowlisted UTF-8 `.json`, `.txt`, `.md`, `.yaml`, `.yml`, `.toml`, and `.ini` files. `files.edit` permits mutation only of JSON sources explicitly registered by the bot adapter as editable; generic text and Python source editing are not available. `config.view` and `config.edit` independently govern adapter-registered typed configuration. Every request rechecks the enabled user, enabled assignment, bot visibility, and current bot permission, with explicit denial precedence and identical not-found responses for hidden resources.

### JSON editing, history, and concurrency

JSON is parsed server-side, depth/size limited, and passed through an optional Pydantic model or custom source validator before any backup or write. The browser keeps JSON as text, so string Discord snowflakes and object keys are never passed through JavaScript numeric conversion. A save must present the SHA-256 hash issued when the file was opened. A stale hash returns a conflict without changing the live file.

Under the shared Stage 4 per-bot data lock, the service creates and verifies a protected `PRE_EDIT` backup; failure blocks the edit. It writes a same-directory temporary file, flushes and fsyncs it, preserves mode and ownership where permitted, validates the temporary content, atomically installs it with `os.replace`, fsyncs the directory, and verifies final bytes. Successful history rows reference the Stage 4 backup rather than duplicating file content and link actor, time, operation, and before/after hashes. History diffs read the protected backup artifact and redact registered sensitive top-level JSON fields. Historical whole-backup restoration remains exclusively in Stage 4 and requires `backups.restore`; Stage 5 does not add a lower-privilege restore shortcut.

### Typed configuration and secrets

Adapters expose `DataSource` and `ConfigField` definitions rather than central hard-coded bot settings. Supported fields are boolean switches, strings, constrained integers/floats, fixed choices, colours, Discord channel/role/user IDs (validated and retained as strings), and durations. Fields can be required, read-only, sensitive, range constrained, and marked as requiring restart. Configuration is validated again server-side and uses the same backup, lock, hash, atomic-write, operation, audit, and event pipeline. A restart-required result is displayed but never triggers an implicit restart.

Sensitive values are returned only as a `configured` marker. Existing secrets are never placed in JSON API responses or HTML/page source; an omitted secret remains unchanged and a supplied replacement is write-only. Mutation audit/event payloads contain source identity and hashes, never file contents or secret values.

### Security restrictions

The central policy blocks `.env`, keys/certificates, credentials, tokens/secrets, virtual environments, caches, Git data, logs, backup storage, supervisor state, the platform database, binary/non-UTF-8 data, and symlink traversal. Stage 5 provides **no arbitrary filesystem access, source-code editing, `.env` viewing, supervisor-state access, platform-database access, SQL, scheduler, shell, eval, deployment, or terminal capability**. View/edit byte limits and JSON nesting limits are configurable with `FILE_VIEW_MAX_BYTES`, `FILE_EDIT_MAX_BYTES`, and `JSON_MAX_DEPTH`.

## Stage 6: Safe SQLite database architecture

Stage 6 adds a deliberately constrained database path:

**Registered SQLite Source → Stage 5 Safe Resolver → `SQLiteDataService` → parameterised structured query builder → typed validation → verified `PRE_EDIT` backup → SQLite transaction.**

Adapters register `DatabaseSource`, `DatabaseTable`, and `DatabaseColumn` metadata. A source ID is resolved only inside an already-authorised bot; its relative path must remain under that bot's single data root and may not traverse, be absolute, use a symlink, or identify the configured platform database. The service never scans the host for databases. A source without table metadata is optionally available for generic **read-only** table/schema inspection; mutation is possible only when the source, table, and individual column are all explicitly editable.

### Database permissions and non-enumeration

`database.view` permits bot-assigned users to list registered sources and inspect visible tables, schemas, and bounded rows. `database.edit` permits only the predefined safe mutation workflow; it does not override source/table/column read-only policy. Existing assignment, account-enabled, explicit-deny, and owner rules are evaluated by the canonical `PermissionService` on every request. Database IDs are resolved within the authorised bot route, and missing, hidden, cross-bot, unsafe, and unregistered resources all return the same unavailable response.

### Read-only and sensitive safety

Table registration controls visible, hidden, editable, insertable, and deletable states. Column registration controls typed editing, choices, ranges, nullability, custom validators, and hidden/sensitive status. Primary keys and BLOBs are read-only by default. Sensitive columns are excluded from grids, details, search, filtering, concurrency tokens, diffs, events, and audits. BLOB reads expose only type and size, large text is truncated, `NULL` remains distinct, and Discord snowflakes remain strings end to end.

### Browsing, filtering, and sorting

Browsing uses server-side pages of exactly 25, 50, or 100 rows and never returns an unbounded table. Sorting identifiers must match the server-derived visible-column allowlist. Search covers at most five visible text columns unless the adapter provides a narrower list. Filters are structured column/operator/value objects and support equals, not-equals, contains, starts-with, greater/less-than, before/after, and null predicates. Values are always SQLite bound parameters; identifiers are selected from registered or introspected allowlists and safely quoted. There is no JOIN builder or user-controlled SQL/SQL function facility. Counts are exact in the current release and may be relatively expensive on exceptionally large unindexed tables.

### Editing, backups, concurrency, and integrity

Typed Pydantic request envelopes carry record keys, field-value maps, and concurrency tokens. The service revalidates SQLite affinity, required/null constraints, configured choices/ranges, custom validators, and edit policy. Before every insert, update, or delete it acquires the shared Stage 4/5 per-bot data lock, runs `PRAGMA quick_check`, and creates and verifies a protected Stage 4 `PRE_EDIT` backup. Stage 4 detects SQLite files and uses Python's SQLite online backup API, producing a transaction-consistent snapshot that works with WAL databases rather than copying live `.db`, `-wal`, and `-shm` files blindly. Backup failure blocks the live mutation.

Each write uses a short `BEGIN IMMEDIATE` transaction with `PRAGMA foreign_keys=ON` and a two-second busy timeout. Constraint failures roll back and database contention returns a safe retry message. A deterministic SHA-256 concurrency token covers the current non-sensitive row state; a stale token is rejected before backup/write, preventing lost updates without disclosing secrets. A `quick_check` runs inside the transaction and again after mutation. Successful mutations reuse operations, append-only audits, and structured `DATABASE_ROW_CREATED`, `DATABASE_ROW_UPDATED`, `DATABASE_ROW_DELETED`, and `BOT_DATABASE_CHANGED` events. Audit changes contain only explicitly changed, non-sensitive values.

Inserts require source edit, table edit, and `allow_insert`; generated integer primary keys remain SQLite-generated. Deletes require source edit, table edit, `allow_delete`, `database.edit`, CSRF, a current concurrency token, and the exact typed confirmation `DELETE <table>`. Foreign-key constraints are never disabled. Databases registered with the offline edit policy remain browsable and use the supervisor-controlled process-aware workflow described below; the database service never performs direct process lifecycle calls.

### Database security boundaries

Stage 6 provides **no raw SQL console, no arbitrary SQL, no platform database access, no schema modification, no arbitrary database paths, no `ATTACH DATABASE`, no PRAGMA editor, no extension loading, no database download/upload/replacement, and no host-wide database browser**. Application database filenames and the configured SQLite application database path are hard-blocked even if an adapter attempts to register them. Recovery continues to use the Stage 4 restore workflow.

## Process-Aware Database Editing (Stage 6)

A registered `DatabaseSource` explicitly selects `LIVE_EDIT_SUPPORTED` or
`EDIT_REQUIRES_BOT_STOP`; policy is registration metadata and is never inferred
from a filename. Live editing retains the structured, parameterised Stage 6
transaction without unnecessary lifecycle actions. Both policies require
`database.edit`, optimistic concurrency, a Stage 4 `PRE_EDIT` snapshot made with
SQLite's online backup API, archive/checksum/integrity verification, a short
transaction, SQLite `quick_check`, an optional registered domain validator, and
safe audit/events.

### Offline Mutation Workflow

Offline sources run as one correlated database operation: **Backup → Stop →
Transaction → Integrity Validation → Restart → Ready**. The per-bot data lock is
held across backup, mutation, validation, and recovery, while the process
workflow lock rejects colliding manual start/stop/restart actions. Stop and start
are requested only through `BotProcessManager` and the supervisor. A write starts
only after the current instance is confirmed `OFFLINE`; startup success requires
both a surviving new instance and a fresh current-instance heartbeat reporting
Discord connected and Ready.

### Failure Recovery

Backup creation or verification failure leaves the bot running and data
untouched. Stop failure blocks the transaction. A pre-commit error rolls back the
short SQLite transaction and restarts a bot that the workflow stopped. A
post-commit integrity or domain failure invokes Stage 4 automatic recovery from
the protected `PRE_EDIT` archive, validates the restored tree/database, and only
then restarts. If recovery cannot be validated, the bot stays stopped. Restart
failure and startup crash are reported independently from a committed database
change. A Ready timeout does not roll valid data back: the response records a
successful database change and process restart with `discord_ready=timeout`.

### Permission Semantics

`database.edit` authorises this predefined safety workflow, including its
internal supervisor stop/start steps, but does **not** grant the actor standalone
`bot.stop`, `bot.start`, or `bot.restart` permissions. Authorization and bot
visibility remain server-side requirements before the durable critical workflow
begins; recovery does not depend on the browser connection remaining open.
# Stage 7 — Registered Scheduler and Task Management

## Task registration

The scheduler executes **only trusted code-registered tasks**. A bot adapter exposes
`RegisteredTask` objects from `BaseBotAdapter.get_tasks()`, and `TaskRegistry`
resolves them by the registered bot adapter and a strict lowercase task ID. Task
definitions contain the handler plus safe capabilities: manual-run and schedule
editing controls, danger level, timeout, supported schedule types, process/Discord
requirements, concurrency policy, and misfire policy. There is one persisted
schedule per bot/task in this initial implementation. Removing a definition never
causes dynamic import: its old configuration becomes an orphaned, non-executable
record for reconciliation diagnostics.

## Scheduler permissions and scope

The canonical bot-scoped permissions are:

* `scheduler.view` — task metadata, schedules, status, and paginated safe history.
* `scheduler.run` — manually queue a task that permits manual execution.
* `scheduler.edit` — configure, enable, or disable a task schedule when the task
  permits that change.

Every web request rechecks the enabled user, enabled bot assignment, explicit
deny/role resolution, and required permission. Hidden bot, task, history, and run
resources use the existing non-enumerating `404 Resource not found` model.

## Structured schedules and time

Supported validated schedule types are interval (minimum **5 minutes**, maximum
365 days), daily, weekly, monthly, and one-time. Monthly days are deliberately
limited to 1–28 so the rule has an occurrence in every month; ambiguous shorter-
month behavior is not invented. One-time instants must be future, timezone-aware
values and are disabled after success. All stored run instants are UTC; recurring
rules retain an explicit IANA timezone and APScheduler's timezone-aware triggers
apply that zone at DST boundaries. APScheduler follows the timezone database for
missing/ambiguous local wall times and coalesces to at most one recovery execution.

**No raw cron input is accepted.** Typed Pydantic discriminated schemas accept
only known structured fields, then trusted server code converts them to internal
APScheduler triggers. The browser never supplies cron text, handler names, module
paths, or scheduler internals.

## Execution, durability, and reconciliation

APScheduler lives in the independently deployed Stage 3 supervisor, not FastAPI.
FastAPI authenticates and authorizes mutations, creates/correlates operations, and
sends only bot/task IDs plus validated schedule data across the authenticated
supervisor boundary. The supervisor resolves the trusted handler again, persists
run state, and executes in a detached worker, so closing a browser or restarting
FastAPI does not cancel work. It never falls back to execution inside FastAPI when
the supervisor is unavailable.

On supervisor startup and explicit reconciliation, persisted definitions are
compared with current adapter tasks and deterministic APScheduler job IDs. Active
jobs are replaced rather than duplicated, disabled/stale jobs are removed, and
next-run timestamps are recalculated without executing discovered jobs. Orphaned
or invalid configurations are marked reconciliation-required and never run.
Disabled bot registrations do not receive automatic task jobs by default.

The default misfire policy is conservative `SKIP`; trusted tasks may opt into one
coalesced `RUN_ONCE` recovery. A supervisor restart cannot preserve an in-process
Python coroutine; runs left running can be diagnosed as interrupted during
reconciliation, and are never silently reported successful. Disabling a schedule
prevents future automatic starts but does not kill a run already in progress.
Manual execution does not change the schedule or its next-run calculation.

## Concurrency, failure handling, and history

The default `FORBID_OVERLAP` lock is scoped to `bot_id + task_id`, so a second
manual/scheduled attempt cannot duplicate a running task while unrelated tasks
remain independent. Trusted definitions can opt into overlap explicitly. Required
process and authoritative Stage 3 Discord Ready state are checked before execution;
failed requirements produce a safe `SKIPPED` result rather than an exception.

Every run has a UUID-backed `RUN-…` ID and records trigger (`MANUAL`, `SCHEDULED`,
or `RECOVERY`), actor (`SYSTEM` for automatic work), operation correlation, queued/
started/finished times, duration, status, a 500-character safe summary, and at most
8 KiB of JSON metadata. History APIs are paginated (maximum page size 100) and
retention is bounded to the newest 500 runs per bot/task. Task exceptions are
isolated and redacted to a generic failure; timeouts become `TIMED_OUT`; neither
condition changes bot health or crashes the supervisor. There are no automatic
retries in Stage 7.

Meaningful run and schedule changes publish transaction-scoped activity events and
append audit entries with bot/task/run/operation correlation. Scheduler ticks and
countdowns are not audited.

## Scheduler security boundaries

The scheduler provides:

* **No arbitrary shell commands** and no `shell=True` execution.
* **No arbitrary Python**, `eval`, `exec`, user-selected imports, or script runner.
* **No arbitrary SQL** or user-defined database mutations.
* **No user-defined executable jobs** or executable browser-supplied fields.
* CSRF protection for Run Now, enable/disable, and schedule updates.

Trusted tasks that mutate files or databases remain responsible for using the
existing backup, data, database, and process-aware lock services from Stages 4–6.

## Stage 8 — Incident architecture

Stage 8 is a durable, read-only projection of trusted platform events:

```text
Existing Events → IncidentService → Incident / Error Group → Timeline → Permission-Scoped UI
```

`EventBus` persists each structured activity event and synchronously passes its durable ID to `IncidentService`. The detector accepts only its fixed event allow-list; browser users cannot submit incident payloads or rules. Detection begins with events emitted after this migration rather than reconstructing all historical logs. Existing incidents survive application restarts.

### Incident types and severity

The initial canonical bot incident types are `BOT_CRASH` (HIGH), `CRASH_LOOP` (CRITICAL), `DISCORD_DISCONNECT` (MEDIUM), `READY_TIMEOUT` (HIGH), `TASK_FAILURE` (MEDIUM), `TASK_TIMEOUT` (HIGH), `DATABASE_EDIT_FAILURE` (HIGH), `DATABASE_RECOVERY_FAILURE` (CRITICAL), `BACKUP_FAILURE` (HIGH), and `RESTORE_FAILURE` (CRITICAL). `SUPERVISOR_UNAVAILABLE` is a HIGH platform incident. Console and telemetry availability remain represented by their existing live status because the current event bus does not emit durable server-side threshold transitions for them; browser WebSocket failures never create incidents.

Availability incidents remain `OPEN` until an objective trusted recovery event resolves them automatically. Completed failure facts (task, database, and backup failures) are stored as `RESOLVED` with resolution `OCCURRED`; this records the failure without inventing a manual ticket workflow. A repeated failure after recovery creates a new incident. Three crashes in five minutes promote the newest incident to `CRASH_LOOP`. Incident triage acknowledgement and notes are deliberately deferred: the existing permission catalog contains `errors.acknowledge`, but no pre-existing safe triage workflow exists.

### Correlation, timelines, and error grouping

Correlation uses `bot_id` plus strong metadata already supplied by events: instance, task, run, operation, and backup IDs. Recovery targets the newest matching open incident for the same bot. Each timeline row references its source activity-event ID and has a unique `(source event key, event code)` constraint, making duplicate delivery idempotent. UTC timestamp plus row ID provides deterministic display order.

Error fingerprints hash the bot, canonical incident type, normalised redacted signature, and the strongest available task/instance/operation key. Timestamps, UUIDs, addresses, and numeric values are normalised. Exception type, safe final message, and the top relevant Python frame are used when present. Equal fingerprints update one durable error group (`first_seen`, `last_seen`, and `occurrence_count`) while individual incidents remain available.

Context is an allow-listed, bounded snapshot rather than a copy of raw event payloads. It can include process instance/PID/exit/uptime and heartbeat fields, a recent CPU/RSS sample, task/run/trigger/duration/operation identifiers, safe database/backup identifiers, and a bounded console excerpt when an existing producer supplies them. All text uses the console secret redactor before persistence; tracebacks are capped and absolute Python paths are shortened. Templates render text through Jinja autoescaping, never as HTML.

### Incident permissions and read-only principle

Every bot incident read requires a current assignment with `errors.view`; Owner can view all bot and platform incidents. Search, filters, pagination totals, dashboard counts, incident IDs, and error groups are scoped in the database query before results are returned. Hidden resources return the standard 404 response.

Incident access does **not** grant adjacent permissions. Raw console excerpts require `console.view`; task/run detail requires `scheduler.view`; backup identifiers require `backups.view`; and database identifiers require `database.view`. Safe high-level failure wording remains visible. No incident route can restart a bot, rerun a task, restore a backup, edit data, execute code, notify users, or alter the authoritative bot state. Incident detection performs no automatic remediation; existing supervisor behaviour remains completely separate.

Incident records are durably retained and paginated. No deletion or aggressive critical-incident cleanup is implemented in Stage 8.

## Discord Diagnostics Architecture (Stage 9)

Stage 9 uses a strictly read-only data path: **Discord → managed bot (`discord.py`) → authenticated management agent → validated guild snapshot → cached permission diagnostics → permission-scoped dashboard**. FastAPI never logs managed bots into Discord and the browser never receives bot tokens, agent credentials, OAuth credentials, webhook data, or a direct Discord API connection.

### Permissions and bot scope

Every Servers page and API uses the existing `servers.view` permission after enabled-account, enabled-assignment, and bot visibility checks. Explicit denial retains precedence. Guild IDs are resolved beneath an already-authorised bot route, so two bots in one guild remain separate bot-guild integrations and guessed guild IDs return the same non-enumerating not-found response.

### Guild snapshots and caching

The dependency-light management agent derives bounded metadata from the running client's cached `Guild`, bot `Member`, roles, channels, `Member.guild_permissions`, and `channel.permissions_for(bot_member)`. It sends snapshots separately from heartbeats on Ready, relevant guild/channel/role/bot-member changes, periodic safety refreshes, and rate-limited dashboard requests. The authenticated supervisor endpoint validates the agent credential, current `INST-…` generation, timestamp, process state, payload bytes, role count, channel count, canonical string IDs, and channel types before atomically replacing the current cache. Only current snapshots are retained; no messages or member directory are collected.

Snapshots retain generated/received timestamps and their instance ID. A Ready matching instance is `CURRENT`; a recent snapshot while disconnected or from a replaced instance is `CACHED`; data older than `DISCORD_SNAPSHOT_STALE_SECONDS` is `STALE`, and certainty-dependent diagnostic results become `UNKNOWN`. No snapshot is `UNAVAILABLE` unless no cache exists. A dashboard restart reloads the compact cache from the database.

### Role hierarchy and channel permissions

Roles are displayed in Discord position order with managed and bot-held roles marked. Diagnostics select the highest bot-held, non-managed role by Discord position (with snowflake ordering as the equal-position tie-break) and require a strictly greater position for role management. Managed targets always fail role-management checks. Administrator satisfies permission-bit checks but never bypasses hierarchy or managed-role restrictions.

The agent relies on discord.py's authoritative `guild.me.guild_permissions` and `channel.permissions_for(guild.me)`. Consequently `@everyone`, combined role overwrites, member overwrites, category synchronisation, and Administrator semantics are resolved by discord.py rather than approximated by FastAPI. The UI maps canonical snake-case permission names to readable labels and safely tolerates names added by future discord.py releases.

### Registered capabilities and transitions

Adapters declare trusted `DiscordCapability` records through `get_discord_capabilities()`: required guild/channel permissions, a configured target channel and allowed types, or a target role and role-management requirement. The central `GuildSnapshotService` reports `PASS`, `WARNING`, `FAIL`, or (for stale data) `UNKNOWN`, with `INFO` through `CRITICAL` severity. Missing declared permissions, missing/wrong channel targets, missing/managed role targets, and equal/higher role conflicts are checked independently; undeclared permissions are not labelled failures.

Stable bot/guild/capability/target fingerprints persist only current status. A prior `PASS` changing to `FAIL` emits one `DISCORD_CAPABILITY_FAILED` activity event; recovery emits one `DISCORD_CAPABILITY_RECOVERED`; unchanged refreshes emit nothing. Guild joins/removals similarly produce bounded operational activity. UI routes never create incidents.

### Stage 9 security boundary

Stage 9 provides **no Discord server mutation**: no automatic fixes, role creation/deletion/reordering/assignment, channel creation/deletion/overwrite changes, message or webhook sending, invites, kicks, bans, timeouts, nickname changes, member directory, arbitrary REST requests, or arbitrary Discord API calls. All effective permission changes must be performed externally in Discord and then observed through a fresh snapshot.

## Maintenance Architecture (Stage 10)

Maintenance follows the existing trusted control path: **Dashboard → `MaintenanceService` → durable database/supervisor heartbeat → `BotManagementAgent` → `MaintenanceGate` → Discord dispatch**. It is a per-bot administrative state, not a process-health state: the process, Discord connection and Ready state, heartbeat, console, telemetry, and diagnostics remain available. A healthy Ready bot resolves to `MAINTENANCE`, while disabled, restarting, stopping, crash, unknown-supervisor, startup, and disconnected conditions retain their higher safety precedence.

### State, permissions, and failure behaviour

`bot_maintenance` stores desired enablement, separate internal reason and public plain-text message, actor/timestamps, optional informational planned end, trusted per-bot bypass IDs, and the last instance-applied state. `ACTIVE`, `PENDING_SYNC`, and `DEGRADED` distinguish desired from applied control; a passed planned end never expires maintenance. Enable and disable require the canonical `bot.maintenance.enable` and `bot.maintenance.disable` permissions, CSRF, bot assignment/visibility, and the existing per-bot operation lock. Explicit permission denial and non-enumerating 404 behaviour remain authoritative. Transitions (not repeated identical requests) publish/audit `BOT_MAINTENANCE_ENABLED` and `BOT_MAINTENANCE_DISABLED`; agent convergence publishes `BOT_MAINTENANCE_SYNCED`. Maintenance itself does not create an incident, while crashes and other failures still do.

### Command blocking, allowlist, and bypass

The dependency-light agent keeps an immutable local `MaintenancePolicyState`, so the hot path performs no database, HTTP, or supervisor call. `integrate_discord_client` installs a global prefix check and a command-tree check covering slash and context-menu commands. `protect_interactive_item` rechecks Views (buttons/selects, including persistent Views) and Modals at interaction/submission time. Interactions receive an ephemeral safe public message; prefix commands receive the same plain-text response. DMs are blocked like guild commands. Running commands are not cancelled; maintenance blocks new dispatches before business logic.

Adapters and bot code may explicitly mark trusted actions with `maintenance_safe`, `MaintenanceGate.register_safe`, or `CommandCapability(maintenance_allowed=True)`. There is no dashboard command-name editor. Trusted Discord user IDs bypass per bot. Role bypass records contain both guild and role string IDs, so they apply only to a member in the configured guild; dashboard platform identity is never inferred to be Discord command identity.

### Scheduler and restart behaviour

Registered tasks default to `BLOCK_DURING_MAINTENANCE`; both scheduled and manual triggers finish as `SKIPPED` before their handler runs. A trusted internal task may opt into `RUN_DURING_MAINTENANCE`, while all its other Stage 7 preconditions still apply.

The supervisor injects the durable desired policy into the child environment before process launch, and the agent constructs its gate from it before Discord integration. This prevents a Ready command-open window for offline enable, normal restart, or supervisor recovery. Every authenticated heartbeat carries the locally applied flag and returns current desired policy; therefore FastAPI outages cannot clear an already-active local gate, and reconnect convergence is automatic and idempotent. If a live agent has not acknowledged a transition, the dashboard truthfully shows `PENDING_SYNC`; disabling maintenance never restarts or repairs the bot and never makes a disconnected bot appear Online.
