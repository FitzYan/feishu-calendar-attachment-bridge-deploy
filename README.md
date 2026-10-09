# Feishu Calendar Attachment Bridge

Receives authenticated Bitable workflow HTTP requests, downloads PDF/Word attachments, uploads fresh calendar-scoped media, and updates only event attachments. Durable encrypted queue and token mappings use a single SQLite worker. Never put app secrets, user tokens, candidate records or resume files in this repository.

## Render deployment

- Runtime: Python 3, Python 3.12.
- Build command: `pip install -r requirements.lock.txt`.
- Start command: `python service.py serve`.
- Health check: `/healthz` (process/worker health only).
- One service instance, persistent disk mount `/var/data`, `DATA_DIR=/var/data/bridge`, `HOST=0.0.0.0`.
- Configure secrets and the target Base/table/calendar using `.env.example`, within server environment settings. User OAuth is required to edit a native workflow event on its organizer's calendar unless the bot has adequate actual access.
- Register `https://<actual-service>.onrender.com/oauth/callback` as the exact Feishu redirect URI. Generate authorization URL with `python service.py oauth-url` in the service shell, and authorize the organizer user. Tokens are encrypted in the persistent disk.
- POST `/webhooks/attachments`, header `Authorization: Bearer <WEBHOOK_SECRET>`, JSON `{ "mode": "attach", "record_id": "rec...", "calendar_id": "feishu.cn_...@group.calendar.feishu.cn", "event_id": "..._0" }`.
- Poll authenticated GET `/jobs/<job_id>` until `succeeded`, `verified=true`. HTTP 202 is queue acceptance only.

Render free instances lose local SQLite data on restart and spin-down. Production requires a paid instance with a persistent disk or a deliberate migration to a durable external datastore. Do not deploy the current SQLite version on an ephemeral filesystem.

## Local checks

`python -m unittest discover -s tests -v`

See `API-VERIFICATION.md` for API limits, partial real testing and unverified business-chain acceptance. Synthetic API tests do not establish resume-download permissions or interviewer access.
