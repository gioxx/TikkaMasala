# Tikka Masala

Tikka Masala is a small FastAPI web app for backing up and restoring Cloudflare Tunnel configurations. It gives you a simple UI to verify a token, list available tunnels, save JSON snapshots locally, and restore a saved configuration later.

## Features

- Verify a Cloudflare API token before using it
- Load the tunnels visible to a Cloudflare account
- Create JSON backups of a tunnel configuration
- Download saved snapshots
- Restore a snapshot to the original tunnel or to another tunnel
- Keep local metadata in SQLite
- Track restore history per backup
- Prefill `Account ID` and `API token` from environment, browser, or database
- Clear saved authentication data without deleting backups
- Switch between system, dark, and light theme
- Schedule automatic backups for all tunnels or a selected subset of tunnels in the account
- Set per-tunnel backup frequency (every run, once per week, once per month)
- Send notifications through a generic webhook and/or Telegram
- Customize the notification message for each event with plain text and dynamic placeholders
- Run in demo mode without persisting authentication data, scheduler state, or notifications

## Requirements

- Docker and Docker Compose, or
- Python 3.12 if you want to run the app directly
- A Cloudflare API token with permissions suitable for Cloudflare Tunnel configuration changes

Recommended token permissions:

- `Account: Cloudflare Tunnel -> Edit`
- `Zone: DNS -> Edit`

For backup-only usage, you can later reduce the token to read-only permissions if your workflow allows it.

## Environment Variables

You can configure the app through environment variables, whether you use Docker Compose, `docker run`, Portainer, or a direct shell session.

| Variable | Required | Description |
| --- | --- | --- |
| `CLOUDFLARE_ACCOUNT_ID` | No | Prefills the Cloudflare account ID in the UI. |
| `CLOUDFLARE_API_TOKEN` | No | Prefills the API token in the UI. |
| `TOKEN_ENCRYPTION_KEY` | Recommended | Fernet key used to encrypt the API token before saving it in SQLite. |
| `AUTO_BACKUP_TIMEZONE` | No | Force automatic backups to use a specific IANA timezone such as `Europe/Rome`. |
| `BACKUP_RETENTION_DAYS` | No | Automatically delete backups older than this many days after new backups are created. |
| `NOTIFICATION_WEBHOOK_URL` | No | Send JSON notifications to a generic webhook endpoint. |
| `NOTIFICATION_WEBHOOK_EVENTS` | No | Comma-separated list of notification events to emit to the webhook. |
| `TELEGRAM_BOT_TOKEN` | No | Telegram bot token used to send notifications through the Bot API. |
| `TELEGRAM_CHAT_ID` | No | Telegram chat, group, or channel ID where notifications should be sent. |
| `TELEGRAM_NOTIFICATION_EVENTS` | No | Comma-separated list of notification events to emit to Telegram. |
| `DATA_DIR` | No | Storage path for the SQLite database and JSON backups. Default: `/data`. |
| `REQUEST_TIMEOUT` | No | Outbound Cloudflare API timeout in seconds. Default: `20`. |
| `LOG_LEVEL` | No | Application log level written to container stdout. Default: `INFO`. |
| `CLOUDFLARE_API_BASE` | No | Override for the Cloudflare API base URL. Default: `https://api.cloudflare.com/client/v4`. |
| `DEMO` | No | Enable demo mode. Defaults to `false`. Demo mode disables persistence, automatic backups, notifications, and the backup archive. |

Start from [`.env.sample`](.env.sample):

```bash
cp .env.sample .env
```

Generate a Fernet key for `TOKEN_ENCRYPTION_KEY` with:

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Run with Docker Compose

The included [`docker-compose.yml`](docker-compose.yml) mounts `./data` into the container so backups and settings survive restarts.

```bash
docker compose up -d --build
```

Then open:

```text
http://localhost:8080
```

Example `.env` for Compose:

```env
CLOUDFLARE_ACCOUNT_ID=your-32-char-account-id
CLOUDFLARE_API_TOKEN=your-cloudflare-api-token
TOKEN_ENCRYPTION_KEY=your-generated-fernet-key
AUTO_BACKUP_TIMEZONE=Europe/Rome
BACKUP_RETENTION_DAYS=90
NOTIFICATION_WEBHOOK_URL=
NOTIFICATION_WEBHOOK_EVENTS=auto_backup_success,auto_backup_partial,auto_backup_failed,restore_failed,retention_cleanup
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_NOTIFICATION_EVENTS=auto_backup_success,auto_backup_partial,auto_backup_failed,restore_failed,retention_cleanup
LOG_LEVEL=INFO
DEMO=false
```

## Run with Docker Only

```bash
docker build -t tikkamasala .
docker run -d \
  --name tikkamasala \
  --restart unless-stopped \
  --env-file .env \
  -p 8080:8080 \
  -v "$(pwd)/data:/data" \
  tikkamasala
```

## Run Without Docker

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
set -a
source .env
set +a
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

## How Prefill and Persistence Work

The UI can prefill authentication fields from three sources:

1. Environment variables
2. Saved values in the local database
3. Browser cookie for the API token

Effective priority:

- `Account ID`: database, then environment
- `API token`: browser cookie, then database, then environment

Automatic backups use only server-side credentials:

- `Account ID`: database or environment
- `API token`: encrypted database value or environment

Browser cookies are never used by the scheduler.

After a successful token verification or API action:

- the `Account ID` is saved in `./data/app.db`
- the `API token` is stored in a browser cookie
- the `API token` is also saved in `./data/app.db` only if `TOKEN_ENCRYPTION_KEY` is configured

Important:

- `Clear saved auth data` removes only the saved `Account ID` and API token from the database and clears the browser cookie
- if `CLOUDFLARE_ACCOUNT_ID` or `CLOUDFLARE_API_TOKEN` are still present in the environment, the UI will continue to show those values as prefilled

## Storage Layout

Everything is stored under `DATA_DIR`, which defaults to `/data` in the container and typically maps to `./data` on the host.

- `app.db`: SQLite database for backup metadata, restore history, and saved auth settings
- `backups/*.json`: exported tunnel configuration snapshots

## What Gets Backed Up

Tikka Masala saves the tunnel configuration returned by the Cloudflare Tunnel configuration endpoints, along with backup metadata such as:

- export timestamp
- account ID
- tunnel ID
- tunnel name
- notes

It does not back up arbitrary account-wide Cloudflare settings.

## Restore Behavior

Restoring a backup replaces the current remote configuration of the selected tunnel with the configuration stored in the snapshot.

The backup detail page keeps a restore history so you can see:

- when a backup was restored
- which account it was restored to
- which tunnel it was restored to
- whether the restore targeted the original tunnel, a different tunnel, or a different account

## Automatic Backups

Tikka Masala can schedule recurring backups for tunnels visible in the configured account.

The scheduler is built into the app and stores its configuration in the local SQLite database. From the home page you can:

- enable or disable automatic backups
- set a cron expression
- run the job immediately with `Run now`
- see the last run, next run, and recent execution history
- open the **Advanced schedule** page to control which tunnels are backed up and how often

By default, automatic backups use the timezone detected from the browser when you save the schedule. If you want a fixed server-side timezone for all users, set `AUTO_BACKUP_TIMEZONE`.

### Advanced schedule

The **Advanced schedule** page (`/auto-backup/tunnel-filters`) lets you control backup scope and per-tunnel frequency:

**Backup mode:**

- **All tunnels** (default): every tunnel in the account is backed up on each run. You can still throttle individual tunnels with a frequency override.
- **Selected tunnels only**: only the tunnels you explicitly check are included in each run.

**Frequency overrides (per tunnel):**

- **Every run**: backed up on every scheduled or manual run (default).
- **Once per week**: skipped if a backup for that tunnel already exists within the last 7 days.
- **Once per month**: skipped if a backup for that tunnel already exists within the last 30 days.

This lets you, for example, run the scheduler daily but back up low-priority tunnels only once a week.

Important notes:

- automatic backups require server-side credentials available from the database or environment
- browser-only token prefill is not enough for scheduled jobs
- each run creates normal backups, so automatic and manual backups share the same archive
- if `BACKUP_RETENTION_DAYS` is set, old backup files and related database records are deleted automatically after new backups are created
- retention cleanup removes both the JSON snapshot and its related restore history
- container logs include both Uvicorn access logs and application logs for startup, backup creation, restore, scheduler activity, cleanup, and most UI-triggered operations

## Notifications

Tikka Masala can send server-side notifications through:

- a generic webhook endpoint
- Telegram

You can use either channel on its own or enable both at the same time.

Set:

- `NOTIFICATION_WEBHOOK_URL` to the target endpoint
- `NOTIFICATION_WEBHOOK_EVENTS` to a comma-separated list of events
- `TELEGRAM_BOT_TOKEN` to your Telegram bot token
- `TELEGRAM_CHAT_ID` to the destination chat ID
- `TELEGRAM_NOTIFICATION_EVENTS` to a comma-separated list of events

Supported events:

- `notification_test`
- `manual_backup_success`
- `manual_backup_failed`
- `auto_backup_success`
- `auto_backup_partial`
- `auto_backup_failed`
- `restore_success`
- `restore_failed`
- `retention_cleanup`

If `NOTIFICATION_WEBHOOK_EVENTS` or `TELEGRAM_NOTIFICATION_EVENTS` is left empty, Tikka Masala enables the default event set for that channel:

- `auto_backup_success`
- `auto_backup_partial`
- `auto_backup_failed`
- `restore_failed`
- `retention_cleanup`

Webhook notifications send a JSON payload with:

- app name and version
- event name
- message
- timestamp
- event-specific details

Telegram notifications send the same information as a formatted plain-text message through the Telegram Bot API.

When notifications are configured:

- container logs show the active channels and event counts at startup
- the home page shows a dedicated `Notifications` box with channel and event counts
- you can use `Send test notification` from the UI to verify webhook and/or Telegram delivery immediately
- the test notification uses the `notification_test` event
- notification results are shown directly inside the section that triggered them instead of at the top of the page

### Customizing notification messages

Each event has a built-in default message. You can override it from the **Customize notification messages** page (`/notifications/messages`).

Messages support `{placeholder}` tokens that are replaced at runtime with event-specific values. Two tokens are always available regardless of the event:

- `{version}` — current app version
- `{timestamp}` — ISO 8601 UTC timestamp of the notification

Available tokens per event:

| Event | Available tokens |
|---|---|
| `notification_test` | _(none specific)_ |
| `manual_backup_success` | `{backup_id}`, `{account_id}`, `{tunnel_id}`, `{tunnel_name}`, `{route_count}` |
| `manual_backup_failed` | `{account_id}`, `{tunnel_id}`, `{error}` |
| `auto_backup_success` | `{trigger}`, `{account_id}`, `{tunnel_count}`, `{backup_count}`, `{error_count}` |
| `auto_backup_partial` | `{trigger}`, `{account_id}`, `{tunnel_count}`, `{backup_count}`, `{error_count}` |
| `auto_backup_failed` | `{trigger}`, `{account_id}`, `{tunnel_count}`, `{backup_count}`, `{error_count}` |
| `restore_success` | `{backup_id}`, `{account_id}`, `{tunnel_id}` |
| `restore_failed` | `{backup_id}`, `{account_id}`, `{tunnel_id}`, `{error}` |
| `retention_cleanup` | `{deleted_count}`, `{retention_days}`, `{cutoff}` |

Leaving a field empty restores the built-in default for that event. Unknown tokens are left as-is in the output.

## Demo Mode

Set `DEMO=true` when you want to exercise the UI without persisting auth data or enabling server-side automation.

In demo mode:

- authentication data is not stored in SQLite or the browser cookie
- automatic backups are disabled
- notifications are disabled
- the backup archive is disabled

Manual tunnel listing and manual backup downloads still work.

## Security Notes

- The API token is never persisted in plaintext in the database by current versions of the app.
- If `TOKEN_ENCRYPTION_KEY` is missing, the token is not written to SQLite.
- The API token can still be temporarily reused through the browser cookie or environment prefill.
- Older plaintext tokens already present in the database are upgraded to encrypted form the first time they are read successfully with a valid `TOKEN_ENCRYPTION_KEY`.

## Project Notes

- Main application entrypoint: [`app/main.py`](app/main.py)
- Main UI template: [`app/templates/index.html`](app/templates/index.html)
- Backup detail template: [`app/templates/backup.html`](app/templates/backup.html)
- Automatic backup archive template: [`app/templates/scheduled_runs.html`](app/templates/scheduled_runs.html)
- Advanced schedule template: [`app/templates/tunnel_filters.html`](app/templates/tunnel_filters.html)
- Notification messages template: [`app/templates/notification_messages.html`](app/templates/notification_messages.html)

## Credits

- Icons: [Tabler Icons](https://tabler.io/icons)
