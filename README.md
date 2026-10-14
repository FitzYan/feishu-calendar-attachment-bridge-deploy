# Feishu Calendar Attachment Bridge

Authenticated Bitable webhook → download PDF/Word → upload fresh calendar-scoped media → PATCH only merged attachments → verify event and optional record writeback. Encrypted durable SQLite queue and token mapping, single worker. Keep credentials, tenant configuration, records and files out of Git.

## Deploy

Use Python 3.12 and a persistent filesystem. `docker compose up -d --build` starts the service from the example compose file. Configure `.env.example`, a private persistent volume, a trusted HTTPS reverse proxy and the organizer user's OAuth redirect URI. Register that exact URI in Feishu, then run `python service.py oauth-url` on the server. Existing Aliyun/Docker hosting can be reused; Render is optional and its ephemeral free filesystem is unsuitable for this SQLite service.

POST `/webhooks/attachments` with `Authorization: Bearer <WEBHOOK_SECRET>` and a JSON body containing mode=attach, record_id, calendar_id, event_id. Configure Content-Type=application/json; authenticated raw JSON is also parsed when workflow sender headers differ. Authentication, strict schema and 32 KiB limit remain required. HTTP 202 means queued; GET `/jobs/<job_id>` with the same key until succeeded. The worker optionally writes and reads back success fields; it does not automatically write failure state. Preserve the native create-event node and save its IDs before the webhook.

## Validation

`python -m unittest discover -s tests -v`: 37 tests. The actual native button, synthetic Bitable PDF/Word download, fresh calendar upload, event attachments and record writeback were verified on existing hosting; restart replay reused the job without new uploads. Old attachments and other event properties were checked separately. Separate interviewer access, expiry-driven live OAuth refresh and platform failure/maximum-size scenarios remain unverified. API-create mode is optional and disabled in that deployment.

See `.env.example`, `API-VERIFICATION.md`, generic nginx/systemd examples, and `tools.py`. Never commit app secrets, user tokens, databases or resumes. A user token may include historical grants for the same app; application-side target allowlists do not reduce the token's granted scope.

## Multiple interview stages

Keep the original request without stage for its existing binding/writeback fields and stable job IDs. To add a final interview, configure STAGE_FIELDS_JSON with six server-controlled fields (status_field, job_field, event_field, calendar_field, bound_event_field, bound_calendar_field) under final, then send stage=final in attach requests. Each stage must use four distinct writeback fields; its binding fields must equal its event/calendar output fields. Mapping overlap is rejected. Stage selection uses a per-job configuration copy; the same worker, encrypted database and OAuth token provider are retained. Do not duplicate single-use refresh tokens across independent services. The final button, original duration, PDF/Word attachments, independent writeback and replay dedupe were verified with synthetic files.

Native workflow exports may omit formula inputs even when other params are present. Preserve and edit those nodes in the Feishu UI; round-tripping incomplete exports can fail validation or lose formula settings.
