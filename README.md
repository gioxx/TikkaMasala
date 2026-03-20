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
| `DATA_DIR` | No | Storage path for the SQLite database and JSON backups. Default: `/data`. |
| `REQUEST_TIMEOUT` | No | Outbound Cloudflare API timeout in seconds. Default: `20`. |
| `CLOUDFLARE_API_BASE` | No | Override for the Cloudflare API base URL. Default: `https://api.cloudflare.com/client/v4`. |

Start from [`.env.sample`](/Users/gioxx/Documents/GitHub/TikkaMasala/.env.sample):

```bash
cp .env.sample .env
```

Generate a Fernet key for `TOKEN_ENCRYPTION_KEY` with:

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Run with Docker Compose

The included [docker-compose.yml](/Users/gioxx/Documents/GitHub/TikkaMasala/docker-compose.yml) mounts `./data` into the container so backups and settings survive restarts.

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

## Security Notes

- The API token is never persisted in plaintext in the database by current versions of the app.
- If `TOKEN_ENCRYPTION_KEY` is missing, the token is not written to SQLite.
- The API token can still be temporarily reused through the browser cookie or environment prefill.
- Older plaintext tokens already present in the database are upgraded to encrypted form the first time they are read successfully with a valid `TOKEN_ENCRYPTION_KEY`.

## Project Notes

- Main application entrypoint: [app/main.py](/Users/gioxx/Documents/GitHub/TikkaMasala/app/main.py)
- Main UI template: [app/templates/index.html](/Users/gioxx/Documents/GitHub/TikkaMasala/app/templates/index.html)
- Backup detail template: [app/templates/backup.html](/Users/gioxx/Documents/GitHub/TikkaMasala/app/templates/backup.html)

Repository:

- <https://github.com/gioxx/TikkaMasala>
