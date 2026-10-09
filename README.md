# Feishu Calendar Attachment Bridge

Authenticated Bitable webhook → download PDF/Word → upload fresh calendar-scoped media → PATCH only merged attachments → verify event and optional record writeback. Encrypted durable SQLite queue and token mapping, single worker. Keep credentials, tenant configuration, records and files out of Git.

## Deploy

Use Python 3.12 and a persistent filesystem. `docker compose up -d --build` starts the service from the example compose file. Configure `.env.example`, a private persistent volume, a trusted HTTPS reverse proxy and the organizer user's OAuth redirect URI. Register that exact URI in Feishu, then run `python service.py oauth-url` on the server. Existing Aliyun/Docker hosting can be reused; Render is optional and its ephemeral free filesystem is unsuitable for this SQLite service.

POST `/webhooks/attachments` with `Authorization: Bearer <WEBHOOK_SECRET>` and a JSON body containing mode=attach, record_id, calendar_id, event_id. Configure Content-Type=application/json; authenticated raw JSON is also parsed when workflow sender headers differ. Authentication, strict schema and 32 KiB limit remain required. HTTP 202 means queued; GET `/jobs/<job_id>` with the same key until succeeded. The worker optionally writes and reads back success fields; it does not automatically write failure state. Preserve the native create-event node and save its IDs before the webhook.

## Validation

`python -m unittest discover -s tests -v`: 33 tests. The actual native button, synthetic Bitable PDF/Word download, fresh calendar upload, event attachments and record writeback were verified on existing hosting; restart replay reused the job without new uploads. Old attachments and other event properties were checked separately. Separate interviewer access, expiry-driven live OAuth refresh and platform failure/maximum-size scenarios remain unverified. API-create mode is optional and disabled in that deployment.

See `.env.example`, `API-VERIFICATION.md`, generic nginx/systemd examples, and `tools.py`. Never commit app secrets, user tokens, databases or resumes. A user token may include historical grants for the same app; application-side target allowlists do not reduce the token's granted scope.
