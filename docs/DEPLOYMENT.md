# Forcelet production deployment

This guide covers running Forcelet for real use: a production WSGI server,
TLS termination, backups, and the background scheduler. The Flask dev server
(`python run.py`) is for local development only.

## Quick start (Docker)

```bash
docker build -t forcelet .
mkdir -p /srv/forcelet/data
docker run -d --name forcelet --restart unless-stopped \
  -p 127.0.0.1:8000:8000 \
  -v /srv/forcelet/data:/data \
  -e FORCELET_ENC_KEY="$(openssl rand -hex 32)" \
  forcelet
```

On first boot the server prints each seeded user's **initial password once**
to the container log — capture it, sign in, and set a real password (the
app forces this before anything else works):

```bash
docker logs forcelet | grep 'initial password'
```

## TLS termination (Caddy)

Put Caddy (or nginx) in front for HTTPS. Caddy handles certificates
automatically:

```
crm.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

The app sends `Strict-Transport-Security` and trusts `X-Forwarded-For`
only when `FORCELET_BEHIND_PROXY=1` (set in the image by default).

## Bare-metal (systemd + gunicorn)

```bash
pip install . gunicorn
export FORCELET_DB=/srv/forcelet/forcelet.db
export FORCELET_BACKUP_DIR=/srv/forcelet/backups
export FORCELET_ENC_KEY="$(openssl rand -hex 32)"
export FORCELET_BEHIND_PROXY=1
gunicorn -c gunicorn.conf.py wsgi:app
```

Example unit file (`/etc/systemd/system/forcelet.service`):

```ini
[Unit]
Description=Forcelet CRM
After=network.target

[Service]
User=forcelet
WorkingDirectory=/opt/forcelet
Environment=FORCELET_DB=/srv/forcelet/forcelet.db
Environment=FORCELET_BACKUP_DIR=/srv/forcelet/backups
Environment=FORCELET_BEHIND_PROXY=1
EnvironmentFile=/etc/forcelet/env   # holds FORCELET_ENC_KEY
ExecStart=/opt/forcelet/.venv/bin/gunicorn -c gunicorn.conf.py wsgi:app
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

## Background scheduler

Scheduled jobs (flows on a timer, report subscriptions, SLA escalations) run
in a background thread. The default gunicorn config (`workers = 1`) runs it
in-process safely — a DB heartbeat lock guarantees only one runner even if
you start more workers. To run it as a dedicated process instead:

```bash
# web workers: no in-process scheduler
FORCELET_SCHEDULER=0 gunicorn -c gunicorn.conf.py wsgi:app
# scheduler process (loop it with systemd or a process supervisor)
python -m forcelet.scheduler_run
```

## Backups

Online backups (SQLite `VACUUM INTO`, no downtime) via the API or on a timer:

```bash
# manual, as an admin (Authorization: Bearer <token>)
curl -X POST https://crm.example.com/api/admin/backups -H "Authorization: Bearer $TOKEN"

# nightly via cron
0 2 * * * curl -sf -X POST http://127.0.0.1:8000/api/admin/backups \
  -H "Authorization: Bearer $FORCELET_ADMIN_TOKEN" >/dev/null
```

The newest 14 backups are kept (`FORCELET_BACKUP_KEEP` overrides). Copy the
backup directory off-host — a backup on the same disk is not a backup.

## Database upgrades

Schema changes ship as migrations (`forcelet/migrations.py`) and apply
automatically at startup; the applied version is stored in `mf_meta`. Always
take a backup before upgrading, then deploy the new code and restart.

## Environment reference

| Variable | Default | Purpose |
|---|---|---|
| `FORCELET_DB` | `./forcelet.db` | SQLite database file |
| `FORCELET_ENC_KEY` | generated `.forcelet.key` | AES key for encrypted fields — back this up, losing it loses the data |
| `FORCELET_BEHIND_PROXY` | `0` | `1` trusts `X-Forwarded-For` for client IPs |
| `FORCELET_SCHEDULER` | `1` | `0` disables the in-process scheduler thread |
| `FORCELET_BACKUP_DIR` | `./backups` | Online backup destination |
| `FORCELET_BACKUP_KEEP` | `14` | How many backups to retain |
| `FORCELET_LOG_LEVEL` | `INFO` | Python log level |

## Security notes

- Sessions are unguessable random tokens, stored hashed, expiring after
  12 hours idle (7 days absolute). Changing a password kills all other sessions.
- Logins are rate-limited and locked out after 5 failures per 15 minutes.
- Seeded/admin-created accounts must set a personal password at first login.
- API keys (`mf_live_…`) are for integrations; rotate them from Setup.
- `/api/health` is an unauthenticated liveness probe for your load balancer.
