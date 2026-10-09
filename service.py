"""Single-instance durable Feishu attachment bridge. No resume bytes are stored on disk."""
import argparse
import fcntl
import hashlib
import hmac
import io
import json
import logging
import os
import random
import re
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlencode

import requests
from cryptography.fernet import Fernet
from dotenv import load_dotenv
from flask import Flask, jsonify, request

API = 'https://open.feishu.cn/open-apis'
UPLOAD_LIMIT = 20 * 1024 * 1024
EVENT_LIMIT = 25 * 1024 * 1024
LOG = logging.getLogger('bridge')


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def segment(value):
    return quote(str(value), safe='')


class Failure(Exception):
    """Safe code only: never include URLs, API bodies, filenames or credentials."""
    def __init__(self, code, retry=False):
        super().__init__(code)
        self.code, self.retry = code, retry


class Config:
    def __init__(self):
        load_dotenv()
        self.app_id = os.getenv('FEISHU_APP_ID', '')
        self.secret = os.getenv('FEISHU_APP_SECRET', '')
        self.base = os.getenv('BITABLE_APP_TOKEN', '')
        self.table = os.getenv('BITABLE_TABLE_ID', '')
        self.calendars = json.loads(os.getenv('ALLOWED_CALENDAR_IDS', '[]'))
        self.fields = json.loads(os.getenv('ATTACHMENT_FIELDS', '["候选人简历"]'))
        self.auth_mode = os.getenv('AUTH_MODE', 'tenant')
        self.parent_type = os.getenv('CALENDAR_PARENT_TYPE', 'calendar')
        self.webhook_secret = os.getenv('WEBHOOK_SECRET', '')
        self.admin_secret = os.getenv('ADMIN_SECRET', '')
        self.key = os.getenv('DATA_ENCRYPTION_KEY', '')
        self.data_dir = Path(os.getenv('DATA_DIR', './data'))
        self.allow_create = os.getenv('ALLOW_API_CREATE', 'false').lower() == 'true'
        self.status_field = os.getenv('STATUS_FIELD', '')
        self.job_field = os.getenv('JOB_ID_FIELD', '')
        self.event_field = os.getenv('EVENT_ID_FIELD', '')
        self.calendar_field = os.getenv('CALENDAR_ID_FIELD', '')
        self.bound_event_field = os.getenv('BOUND_EVENT_ID_FIELD', '')
        self.bound_calendar_field = os.getenv('BOUND_CALENDAR_ID_FIELD', '')
        self.redirect_uri = os.getenv('OAUTH_REDIRECT_URI', '')
        self.scopes = os.getenv('OAUTH_SCOPES', 'bitable:app:readonly docs:document.media:download docs:document.media:upload calendar:calendar:read calendar:calendar.event:read calendar:calendar.event:update offline_access')
        self.max_attempts = 6
        self.max_queue = 1000
        self.retention_days = int(os.getenv('JOB_RETENTION_DAYS', '30'))

    def validate(self):
        if not all([self.app_id, self.secret, self.base, self.table, self.key]):
            raise Failure('configuration_missing')
        if len(self.webhook_secret) < 32 or len(self.admin_secret) < 32 or self.webhook_secret == self.admin_secret:
            raise Failure('secrets_must_be_distinct_and_at_least_32_chars')
        if self.auth_mode not in ('tenant', 'user') or self.parent_type not in ('calendar', 'calender'):
            raise Failure('configuration_invalid')
        if not isinstance(self.calendars, list) or not self.calendars or not all(isinstance(x, str) and x != 'primary' and x for x in self.calendars):
            raise Failure('explicit_calendar_allowlist_required')
        if not isinstance(self.fields, list) or not self.fields or not all(isinstance(x, str) and x for x in self.fields):
            raise Failure('attachment_fields_invalid')
        write_fields = [x for x in (self.status_field,self.job_field,self.event_field,self.calendar_field) if x]
        if len(set(write_fields)) != len(write_fields) or set(write_fields) & set(self.fields):
            raise Failure('writeback_field_mapping_conflict')
        if self.retention_days < 1:
            raise Failure('retention_invalid')
        if self.auth_mode == 'user' and not self.redirect_uri.startswith('https://'):
            raise Failure('oauth_https_redirect_required')
        Fernet(self.key.encode())


class Store:
    def __init__(self, cfg):
        cfg.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(cfg.data_dir, 0o700)
        self.path = cfg.data_dir / 'bridge.sqlite3'
        self.cipher = Fernet(cfg.key.encode())
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, payload BLOB NOT NULL,
              state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
              next_at REAL NOT NULL DEFAULT 0, error TEXT, result BLOB, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS uploads (
              source_key TEXT PRIMARY KEY, value BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS oauth_states (hash TEXT PRIMARY KEY, expires REAL NOT NULL);
            ''')
        os.chmod(self.path, 0o600)

    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def seal(self, data):
        return self.cipher.encrypt(canonical(data).encode())

    def unseal(self, data):
        return json.loads(self.cipher.decrypt(data))

    def get(self, key):
        with self.db() as db:
            row = db.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
        return self.unseal(row['value']) if row else None

    def put(self, key, value):
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO kv VALUES (?,?)', (key, self.seal(value)))

    def enqueue(self, body, max_queue):
        # Existing mode has a stable event target; create mode requires a persisted request_id.
        logical = [body['mode'], body['record_id'], body['calendar_id'], body.get('event_id'), body.get('request_id', '')]
        job_id, fingerprint = digest(canonical(logical)), digest(canonical(body))
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if old:
                if old['fingerprint'] != fingerprint:
                    raise Failure('idempotency_payload_conflict')
                return job_id, False
            count = db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','retry','running')").fetchone()[0]
            if count >= max_queue:
                raise Failure('queue_full', True)
            db.execute('INSERT INTO jobs(id,fingerprint,payload,state,created) VALUES (?,?,?,?,?)',
                       (job_id, fingerprint, self.seal(body), 'queued', time.time()))
        return job_id, True

    def claim(self):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM jobs WHERE state IN ('queued','retry') AND next_at<=? ORDER BY created LIMIT 1", (time.time(),)).fetchone()
            if row:
                db.execute("UPDATE jobs SET state='running',attempts=attempts+1 WHERE id=?", (row['id'],))
                return dict(row)
        return None

    def result(self, job_id):
        with self.db() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            return None
        result = self.unseal(row['result']) if row['result'] else None
        if result is None:
            body = self.unseal(row['payload'])
            created = self.get('created:' + job_id) or {}
            event_id = body.get('event_id') or created.get('event_id')
            if event_id:
                result = {'calendar_id':body['calendar_id'], 'event_id':event_id, 'verified':False}
        return {'job_id': job_id, 'state': row['state'], 'attempts': row['attempts'],
                'error': row['error'], 'result': result}

    def upload(self, key, value=None):
        with self.db() as db:
            if value is not None:
                db.execute('INSERT OR REPLACE INTO uploads VALUES (?,?)', (key, self.seal(value)))
                return value
            row = db.execute('SELECT value FROM uploads WHERE source_key=?', (key,)).fetchone()
        return self.unseal(row['value']) if row else None

    def consume_state(self, state):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT expires FROM oauth_states WHERE hash=?', (digest(state),)).fetchone()
            db.execute('DELETE FROM oauth_states WHERE hash=?', (digest(state),))
        return bool(row and row['expires'] > time.time())


class Feishu:
    def __init__(self, cfg, store):
        self.cfg, self.store = cfg, store
        self.token_lock = threading.Lock()
        self.tenant = None
        self.http = requests.Session()
        # Disable ambient proxies/.netrc so credentials do not leak to a misconfigured host.
        self.http.trust_env = False
        self.next_media = 0

    def raw(self, method, path, **kwargs):
        try:
            r = self.http.request(method, API + path, timeout=(10, 60), allow_redirects=False, **kwargs)
        except requests.RequestException:
            raise Failure('network_or_timeout', True) from None
        if r.status_code == 429 or r.status_code >= 500:
            retry_after = r.headers.get('Retry-After', '')
            r.close()
            # Job-level backoff avoids blind POST retries. Provider Retry-After is bounded.
            if retry_after.isdigit():
                self.store.put('retry_after', {'until': time.time() + min(int(retry_after), 3600)})
            raise Failure('upstream_transient', True)
        if not 200 <= r.status_code < 300:
            try:
                code = r.json().get('code', 'http_' + str(r.status_code))
            except ValueError:
                code = 'http_' + str(r.status_code)
            r.close()
            raise Failure('feishu_' + str(code), code in (1061045,99991400,99991401))
        return r

    @staticmethod
    def parsed(r):
        try:
            data = r.json()
        except ValueError:
            raise Failure('upstream_invalid_json', True) from None
        finally:
            r.close()
        if data.get('code', 0) != 0:
            code = data['code']
            raise Failure('feishu_' + str(code), code in (1061045, 99991400, 99991401))
        return data

    def oauth_exchange(self, grant):
        data = self.parsed(self.raw('POST', '/authen/v2/oauth/token', json={
            'client_id': self.cfg.app_id, 'client_secret': self.cfg.secret, **grant}))
        if not data.get('access_token'):
            raise Failure('oauth_token_missing')
        data['expires_at'] = time.time() + int(data['expires_in'])
        # Refresh-token rotation is persisted before another API call.
        self.store.put('user_token', data)
        return data

    def token(self):
        with self.token_lock:
            if self.cfg.auth_mode == 'tenant':
                if not self.tenant or self.tenant['expires_at'] <= time.time() + 120:
                    d = self.parsed(self.raw('POST', '/auth/v3/tenant_access_token/internal',
                                            json={'app_id': self.cfg.app_id, 'app_secret': self.cfg.secret}))
                    self.tenant = {'token': d['tenant_access_token'], 'expires_at': time.time() + d['expire']}
                return self.tenant['token']
            if self.store.get('oauth_unknown'):
                raise Failure('oauth_refresh_result_unknown_reauthorize')
            d = self.store.get('user_token')
            if not d:
                raise Failure('oauth_login_required')
            if d['expires_at'] <= time.time() + 120:
                if not d.get('refresh_token'):
                    raise Failure('oauth_reauthorization_required')
                self.store.put('oauth_unknown', {'blocked': True})
                try:
                    d = self.oauth_exchange({'grant_type': 'refresh_token', 'refresh_token': d['refresh_token']})
                    with self.store.db() as db:
                        db.execute("DELETE FROM kv WHERE key='oauth_unknown'")
                except Failure as e:
                    # Refresh grant is single-use. An unknown result must not auto-repeat it.
                    if e.retry:
                        self.store.put('oauth_unknown', {'blocked': True})
                        raise Failure('oauth_refresh_result_unknown_reauthorize') from None
                    raise
            if self.store.get('oauth_unknown'):
                raise Failure('oauth_refresh_result_unknown_reauthorize')
            return d['access_token']

    def api(self, method, path, **kwargs):
        d = self.parsed(self.raw(method, path, headers={'Authorization': 'Bearer ' + self.token()}, **kwargs))
        return d.get('data', d)

    def media_throttle(self):
        delay = self.next_media - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self.next_media = time.monotonic() + 0.5  # <=2 calls/sec, below documented 5 QPS

    def download(self, attachment, field_id, record_id):
        self.media_throttle()
        extra = {'bitablePerm': {'tableId': self.cfg.table, 'attachments': {field_id: {record_id: [attachment['file_token']]}}}}
        r = self.raw('GET', '/drive/v1/medias/' + segment(attachment['file_token']) + '/download',
                     headers={'Authorization': 'Bearer ' + self.token()}, params={'extra': canonical(extra)}, stream=True)
        content = io.BytesIO()
        try:
            for chunk in r.iter_content(65536):
                content.write(chunk)
                if content.tell() > UPLOAD_LIMIT:
                    raise Failure('single_file_exceeds_20mb')
        except requests.RequestException:
            raise Failure('download_interrupted', True) from None
        finally:
            r.close()
        data = content.getvalue()
        if len(data) != int(attachment['size']):
            raise Failure('source_size_changed_or_invalid', True)
        ext = Path(attachment['name']).suffix.lower()
        valid = (ext == '.pdf' and data[:1024].find(b'%PDF-') >= 0) or (ext == '.docx' and data.startswith(b'PK\x03\x04')) or (ext == '.doc' and data.startswith(bytes.fromhex('D0CF11E0A1B11AE1')))
        if not valid:
            raise Failure('file_signature_mismatch')
        return data

    def upload_calendar(self, attachment, data, calendar_id):
        self.media_throttle()
        result = self.api('POST', '/drive/v1/medias/upload_all',
                          data={'file_name': attachment['name'], 'parent_type': self.cfg.parent_type,
                                'parent_node': calendar_id, 'size': str(len(data))},
                          files={'file': (attachment['name'], data, 'application/octet-stream')})
        if not result.get('file_token'):
            raise Failure('upload_token_missing')
        return {'file_token': result['file_token'], 'size': len(data)}


def validate_payload(body, cfg):
    if not isinstance(body, dict) or set(body) - {'mode','record_id','calendar_id','event_id','request_id','event','attendee_open_ids'}:
        raise Failure('payload_invalid')
    b = dict(body)
    b.setdefault('mode', 'attach')
    for name in ('record_id', 'calendar_id'):
        if not isinstance(b.get(name), str) or not b[name] or len(b[name]) > 256:
            raise Failure('payload_' + name + '_invalid')
    if not re.fullmatch(r'rec[A-Za-z0-9]+', b['record_id']) or b['calendar_id'] not in cfg.calendars:
        raise Failure('target_not_allowed')
    if 'request_id' in b and (not isinstance(b['request_id'], str) or not 1 <= len(b['request_id']) <= 128):
        raise Failure('request_id_invalid')
    if b['mode'] == 'attach':
        if not isinstance(b.get('event_id'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', b['event_id']):
            raise Failure('event_id_invalid')
        if 'event' in b or 'attendee_open_ids' in b:
            raise Failure('attach_payload_has_create_fields')
    elif b['mode'] == 'create':
        if not cfg.allow_create or not b.get('request_id') or b.get('event_id'):
            raise Failure('api_create_disabled_or_invalid')
        event = b.get('event')
        if not isinstance(event, dict) or set(event) - {'summary','description','start_time','end_time','location','vchat'}:
            raise Failure('create_event_invalid')
        if not isinstance(event.get('summary'), str) or not 1 <= len(event['summary']) <= 1000:
            raise Failure('create_summary_invalid')
        for name in ('start_time', 'end_time'):
            t = event.get(name)
            if not isinstance(t, dict) or set(t) - {'timestamp','timezone'} or not re.fullmatch(r'\d{10,11}', str(t.get('timestamp', ''))):
                raise Failure('create_time_invalid_use_unix_seconds')
            if t.get('timezone') != 'Asia/Shanghai':
                raise Failure('create_timezone_must_be_Asia_Shanghai')
            t['timestamp'] = str(t['timestamp'])
        if int(event['end_time']['timestamp']) <= int(event['start_time']['timestamp']):
            raise Failure('create_end_before_start')
        if 'description' in event and (not isinstance(event['description'], str) or len(event['description']) > 4096):
            raise Failure('create_description_invalid')
        if 'location' in event and (not isinstance(event['location'], dict) or set(event['location']) - {'name','address'} or not all(isinstance(x,str) and len(x)<=2000 for x in event['location'].values())):
            raise Failure('create_location_invalid')
        if 'vchat' in event and event['vchat'] not in ({'vc_type':'vc'}, {'vc_type':'no_meeting'}):
            raise Failure('create_vchat_invalid')
        ids = b.get('attendee_open_ids', [])
        if not isinstance(ids, list) or len(ids) > 100 or not all(isinstance(x,str) and re.fullmatch(r'ou_[A-Za-z0-9]+',x) for x in ids):
            raise Failure('attendee_open_ids_invalid')
        b['attendee_open_ids'] = sorted(set(ids))
    else:
        raise Failure('mode_invalid')
    return b


class Sync:
    def __init__(self, cfg, store, api):
        self.cfg, self.store, self.api = cfg, store, api
        self.root = '/bitable/v1/apps/' + segment(cfg.base) + '/tables/' + segment(cfg.table)

    def record(self, record_id):
        d = self.api.api('POST', self.root + '/records/batch_get', json={'record_ids':[record_id]})
        records = d.get('records', [])
        if d.get('forbidden_record_ids') or len(records) != 1 or records[0].get('record_id') != record_id:
            raise Failure('record_missing_or_forbidden')
        return records[0]['fields']

    def attachments(self, record_id):
        field_defs, cursor = {}, None
        while True:
            d = self.api.api('GET', self.root + '/fields', params={'page_size':100, **({'page_token':cursor} if cursor else {})})
            for f in d.get('items', []):
                field_defs[f['field_name']] = f
            if not d.get('has_more'):
                break
            next_cursor = d.get('page_token')
            if not next_cursor or next_cursor == cursor:
                raise Failure('fields_pagination_invalid')
            cursor = next_cursor
        values = self.record(record_id)
        result, seen = [], set()
        for name in self.cfg.fields:
            if name not in field_defs or field_defs[name]['type'] != 17:
                raise Failure('configured_field_is_not_attachment')
            items = values.get(name, [])
            if not isinstance(items, list):
                raise Failure('source_attachment_invalid')
            for a in items:
                if not isinstance(a, dict) or not all(k in a for k in ('file_token','name','size')):
                    raise Failure('source_attachment_invalid')
                if not isinstance(a['file_token'], str) or not re.fullmatch(r'[A-Za-z0-9_-]+', a['file_token']):
                    raise Failure('source_token_invalid')
                if not isinstance(a['name'],str) or len(a['name']) > 250 or any(ord(x)<32 for x in a['name']):
                    raise Failure('source_filename_invalid')
                # Reject rather than silently skipping an unsupported configured attachment.
                if Path(a['name']).suffix.lower() not in ('.pdf','.doc','.docx'):
                    raise Failure('unsupported_file_use_pdf_doc_docx')
                try:
                    size = int(a['size'])
                except (TypeError, ValueError):
                    raise Failure('source_size_invalid') from None
                if size < 1 or size > UPLOAD_LIMIT:
                    raise Failure('single_file_exceeds_20mb_or_empty')
                if a['file_token'] not in seen:
                    result.append((a, field_defs[name]['field_id']))
                    seen.add(a['file_token'])
        if not result:
            raise Failure('no_source_attachments')
        if len(result) > 20 or sum(int(a['size']) for a,_ in result) > EVENT_LIMIT:
            raise Failure('attachment_count_or_total_exceeds_limit')
        return result

    def calendar_check(self, calendar_id):
        d = self.api.api('GET', '/calendar/v4/calendars/' + segment(calendar_id))
        c = d.get('calendar', d)
        if c.get('role') not in ('writer','owner') or c.get('type') not in ('primary','shared'):
            raise Failure('calendar_not_editable')

    @staticmethod
    def live(event):
        if event.get('status') == 'cancelled':
            raise Failure('event_cancelled')
        if event.get('recurrence') or event.get('is_exception'):
            raise Failure('recurring_event_not_supported')
        attachments = event.get('attachments', [])
        if not isinstance(attachments, list):
            raise Failure('event_attachments_invalid')
        return {x['file_token']:x for x in attachments if not x.get('is_deleted',False)}

    def event(self, path, calendar_id):
        e = self.api.api('GET', path)['event']
        if e.get('organizer_calendar_id') != calendar_id:
            raise Failure('use_organizer_calendar_not_attendee_copy')
        self.live(e)
        return e

    def run(self, job_id, body):
        cal = body['calendar_id']
        self.calendar_check(cal)
        collection = '/calendar/v4/calendars/' + segment(cal) + '/events'
        if body['mode'] == 'attach' and (self.cfg.bound_event_field or self.cfg.bound_calendar_field):
            saved_fields = self.record(body['record_id'])
            for field,value in ((self.cfg.bound_event_field,body['event_id']), (self.cfg.bound_calendar_field,cal)):
                if field and saved_fields.get(field) != value:
                    raise Failure('record_event_binding_mismatch')
        source = self.attachments(body['record_id'])  # validate everything before create/upload
        if body['mode'] == 'create':
            key = 'created:' + job_id
            created = self.store.get(key)
            if not created:
                payload = {'visibility':'default', 'attendee_ability':'none', **body['event']}
                created = self.api.api('POST', collection, params={'idempotency_key': str(uuid.UUID(job_id[:32]))}, json=payload)['event']
                self.store.put(key, {'event_id':created['event_id']})
            event_id = created['event_id']
        else:
            event_id = body['event_id']
        path = collection + '/' + segment(event_id)
        current = self.live(self.event(path, cal))
        protected = self.store.get('protected:' + job_id) or []
        if not set(protected).issubset(current):
            raise Failure('existing_attachments_changed_manual_check')
        self.store.put('protected:' + job_id, sorted(set(protected) | set(current)))
        uploaded = []
        for a, field_id in source:
            key = digest(canonical([self.cfg.app_id,self.cfg.auth_mode,self.cfg.base,self.cfg.table,cal,event_id,a['file_token']]))
            saved = self.store.upload(key)
            if not saved:
                # Preflight avoids orphan uploads when existing + pending attachments already exceed capacity.
                total = self.total_size(current, uploaded) + int(a['size'])
                if total > EVENT_LIMIT:
                    raise Failure('event_total_exceeds_25mb')
                data = self.api.download(a, field_id, body['record_id'])
                saved = self.api.upload_calendar(a, data, cal)
                del data
                self.store.upload(key, saved)  # persist before PATCH for timeout/restart recovery
            uploaded.append(saved)
        # Re-read immediately before PATCH, preserve every live token and send only attachments.
        before_event = self.event(path, cal)
        before = self.live(before_event)
        if not set(current).issubset(before):
            raise Failure('existing_attachments_changed_manual_check')
        self.store.put('protected:' + job_id, sorted(set(protected) | set(before)))
        if self.total_size(before, uploaded) > EVENT_LIMIT:
            raise Failure('event_total_exceeds_25mb')
        desired = set(before) | {x['file_token'] for x in uploaded}
        if desired - set(before):
            self.api.api('PATCH', path, json={'attachments':[{'file_token':t,'is_deleted':False} for t in sorted(desired)]})
        after = self.live(self.event(path, cal))
        if not set(before).issubset(after):
            raise Failure('existing_attachments_changed_manual_check')
        if not desired.issubset(after):
            raise Failure('attachment_readback_mismatch', True)
        if body['mode'] == 'create' and body.get('attendee_open_ids'):
            existing = self.attendee_ids(path)
            missing = set(body['attendee_open_ids']) - existing
            if missing and self.store.get('invite_check_only:' + job_id):
                raise Failure('attendees_still_missing_manual_check')
            if missing:
                # No blind retry of an ambiguous invitation POST: require manual reconciliation.
                try:
                    self.api.api('POST', path + '/attendees', params={'user_id_type':'open_id'},
                                 json={'attendees':[{'type':'user','user_id':x} for x in sorted(missing)],'need_notification':True})
                except Failure as e:
                    if e.retry:
                        raise Failure('attendee_result_unknown_check_calendar') from None
                    raise
                got = self.attendee_ids(path)
                if not set(body['attendee_open_ids']).issubset(got):
                    raise Failure('attendee_readback_mismatch')
        result = {'calendar_id':cal,'event_id':event_id,'source_count':len(source),'attachment_count':len(after),'verified':True}
        fields = {}
        for name,value in ((self.cfg.status_field,'同步成功'),(self.cfg.job_field,job_id),(self.cfg.calendar_field,cal),(self.cfg.event_field,event_id)):
            if name:
                fields[name]=value
        if fields:
            # PUT updates only supplied fields, never the attachment field or the entire source record.
            self.api.api('PUT', self.root + '/records/' + segment(body['record_id']), json={'fields':fields})
            readback = self.record(body['record_id'])
            if any(readback.get(k) != v for k,v in fields.items()):
                raise Failure('writeback_readback_mismatch', True)
        return result

    def attendee_ids(self, path):
        existing, cursor, seen = set(), None, set()
        while True:
            d = self.api.api('GET', path + '/attendees', params={
                'user_id_type':'open_id','page_size':100,
                **({'page_token':cursor} if cursor else {})})
            existing.update(x.get('user_id') for x in d.get('items', [])
                            if x.get('type')=='user' and x.get('rsvp_status')!='removed')
            if not d.get('has_more'):
                return existing
            cursor=d.get('page_token')
            if not cursor or cursor in seen:
                raise Failure('attendees_pagination_invalid')
            seen.add(cursor)

    @staticmethod
    def total_size(current, uploaded):
        sizes={x['file_token']:int(x['size']) for x in uploaded}
        for token,x in current.items():
            if token not in sizes:
                try:
                    sizes[token]=int(x['file_size'])
                except (KeyError,ValueError,TypeError):
                    raise Failure('existing_attachment_size_unknown') from None
        return sum(sizes.values())


class Worker:
    def __init__(self,cfg,store,sync):
        self.cfg,self.store,self.sync=cfg,store,sync
        self.stop=threading.Event()
        self.last_tick=0

    def start(self):
        # One worker process across HTTP + restart. Never run multiple replicas against this state.
        self.lockfile=open(self.cfg.data_dir/'worker.lock','a')
        try:
            fcntl.flock(self.lockfile,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise Failure('another_worker_is_running') from None
        with self.store.db() as db:
            db.execute("UPDATE jobs SET state='retry',next_at=0 WHERE state='running'")
        self.thread=threading.Thread(target=self.loop,daemon=True,name='durable-worker')
        self.thread.start()

    def loop(self):
        while not self.stop.is_set():
            self.last_tick=time.time()
            # Remove expired job request/result data; retain upload mappings for dedupe.
            with self.store.db() as db:
                expired = db.execute("SELECT id FROM jobs WHERE state IN ('succeeded','failed') AND created<?", (time.time()-self.cfg.retention_days*86400,)).fetchall()
                for expired_job in expired:
                    db.execute('DELETE FROM kv WHERE key IN (?,?)', ('protected:'+expired_job['id'],'invite_check_only:'+expired_job['id']))
                    db.execute('DELETE FROM jobs WHERE id=?', (expired_job['id'],))
                db.execute('DELETE FROM oauth_states WHERE expires<?',(time.time(),))
            if self.cfg.auth_mode == 'user' and not self.store.get('oauth_unknown'):
                user_token = self.store.get('user_token')
                if user_token and user_token['expires_at'] <= time.time() + 120:
                    try:
                        self.sync.api.token()
                    except Failure as exc:
                        LOG.warning(canonical({'event':'oauth_attention_required','error':exc.code}))
                        # Avoid a tight loop when reauthorization is required.
                        self.store.put('oauth_unknown', {'blocked':True})
            job=self.store.claim()
            if not job:
                self.stop.wait(1)
                continue
            job_id=job['id']; attempts=job['attempts']+1
            try:
                result=self.sync.run(job_id,self.store.unseal(job['payload']))
                with self.store.db() as db:
                    db.execute("UPDATE jobs SET state='succeeded',error=NULL,result=? WHERE id=?",(self.store.seal(result),job_id))
                LOG.info(canonical({'event':'job_succeeded','job_id':job_id,'attempt':attempts,'source_count':result['source_count']}))
            except Exception as exc:
                failure=exc if isinstance(exc,Failure) else Failure('internal_error')
                retry=failure.retry and attempts<self.cfg.max_attempts
                delay=min(300,5*2**(attempts-1))+random.uniform(0,3)
                backoff=self.store.get('retry_after') or {}
                next_at=max(time.time()+delay,backoff.get('until',0))
                with self.store.db() as db:
                    db.execute('UPDATE jobs SET state=?,error=?,next_at=? WHERE id=?',('retry' if retry else 'failed',failure.code,next_at,job_id))
                LOG.warning(canonical({'event':'job_retry' if retry else 'job_failed','job_id':job_id,'attempt':attempts,'error':failure.code}))


def authorized(secret):
    header=request.headers.get('Authorization','')
    return hmac.compare_digest(header,'Bearer '+secret)


def create_app(cfg,store,api,worker):
    app=Flask(__name__)
    app.config['MAX_CONTENT_LENGTH']=32768

    @app.after_request
    def privacy_headers(response):
        response.headers['Cache-Control']='no-store'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['X-Content-Type-Options']='nosniff'
        return response

    @app.errorhandler(Failure)
    def safe_error(exc):
        return jsonify(error=exc.code), (503 if exc.retry else 400)

    @app.get('/healthz')
    def health():
        alive=worker.thread.is_alive() if hasattr(worker,'thread') else False
        return jsonify(status='ok' if alive else 'worker_down'),200 if alive else 503

    @app.post('/webhooks/attachments')
    def webhook():
        if not authorized(cfg.webhook_secret):
            return jsonify(error='unauthorized'),401
        if not request.is_json:
            return jsonify(error='json_required'),415
        body=validate_payload(request.get_json(),cfg)
        job_id,new=store.enqueue(body,cfg.max_queue)
        state=store.result(job_id)
        return jsonify(job_id=job_id,state=state['state'],accepted=new),202

    @app.get('/jobs/<job_id>')
    def job_status(job_id):
        if not authorized(cfg.webhook_secret):
            return jsonify(error='unauthorized'),401
        row=store.result(job_id)
        return (jsonify(row),200) if row else (jsonify(error='not_found'),404)

    @app.post('/admin/jobs/<job_id>/retry')
    def retry(job_id):
        if not authorized(cfg.admin_secret):
            return jsonify(error='unauthorized'),401
        with store.db() as db:
            row=db.execute('SELECT state,error FROM jobs WHERE id=?',(job_id,)).fetchone()
            if not row or row['state']!='failed':
                return jsonify(error='failed_job_required'),409
            if row['error'] in ('attendee_result_unknown_check_calendar','attendee_readback_mismatch'):
                return jsonify(error='manual_attendee_reconciliation_required'),409
            db.execute("UPDATE jobs SET state='queued',attempts=0,error=NULL,next_at=0 WHERE id=?",(job_id,))
        return jsonify(state='queued'),202

    @app.post('/admin/jobs/<job_id>/reconcile-invites')
    def reconcile_invites(job_id):
        if not authorized(cfg.admin_secret):
            return jsonify(error='unauthorized'),401
        with store.db() as db:
            row=db.execute('SELECT state,error FROM jobs WHERE id=?',(job_id,)).fetchone()
            if not row or row['state']!='failed' or row['error'] not in (
                'attendee_result_unknown_check_calendar','attendee_readback_mismatch','attendees_still_missing_manual_check'):
                return jsonify(error='uncertain_invitation_job_required'),409
            db.execute('INSERT OR REPLACE INTO kv VALUES (?,?)',('invite_check_only:'+job_id,store.seal({'enabled':True})))
            db.execute("UPDATE jobs SET state='queued',attempts=0,error=NULL,next_at=0 WHERE id=?",(job_id,))
        return jsonify(state='queued',invitation_mode='verify_only'),202

    @app.get('/oauth/callback')
    def callback():
        if cfg.auth_mode!='user' or not store.consume_state(request.args.get('state','')):
            return '授权链接无效或已过期，请重新生成。',400
        if not request.args.get('code') or request.args.get('error'):
            return '未完成授权，请重新生成授权链接。',400
        with api.token_lock:
            api.oauth_exchange({'grant_type':'authorization_code','code':request.args['code'],'redirect_uri':cfg.redirect_uri})
            with store.db() as db:
                db.execute("DELETE FROM kv WHERE key='oauth_unknown'")
        return '授权已保存。可以关闭此页面，继续使用多维表格。'

    return app


def main():
    p=argparse.ArgumentParser()
    p.add_argument('command',choices=['serve','oauth-url','check-config','prune-mappings'])
    p.add_argument('--confirm',action='store_true',help='prune-mappings requires confirmation; clears dedupe history')
    args=p.parse_args()
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO,format='%(message)s')
    cfg=Config();cfg.validate();store=Store(cfg)
    if args.command=='check-config':
        print('配置格式检查通过；尚未验证飞书身份、权限、上传类型和真实日程。')
        return
    if args.command=='prune-mappings':
        if not args.confirm:
            raise Failure('confirmation_required_clears_dedupe_history')
        # Only run after stopping service; prevents concurrent worker mutation.
        lock=open(cfg.data_dir/'worker.lock','a')
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise Failure('stop_service_before_pruning') from None
        with store.db() as db:
            db.execute('DELETE FROM uploads')
            db.execute("DELETE FROM kv WHERE key LIKE 'created:%'")
            db.execute('DELETE FROM jobs')
            db.commit()
            db.execute('VACUUM')
        print('已清理同步映射及任务；旧请求不得重放，可能导致重复。')
        return
    if args.command=='oauth-url':
        if cfg.auth_mode!='user':
            raise Failure('AUTH_MODE_must_be_user')
        state=secrets.token_urlsafe(32)
        with store.db() as db:
            db.execute('INSERT INTO oauth_states VALUES (?,?)',(digest(state),time.time()+600))
        print('https://accounts.feishu.cn/open-apis/authen/v1/authorize?'+urlencode({
            'client_id':cfg.app_id,'response_type':'code','redirect_uri':cfg.redirect_uri,
            'scope':cfg.scopes,'state':state,'prompt':'consent'}))
        return
    api=Feishu(cfg,store); worker=Worker(cfg,store,Sync(cfg,store,api));worker.start()
    app=create_app(cfg,store,api,worker)
    from waitress import serve
    # Waitress access logs disabled; reverse proxy must redact OAuth query and auth headers.
    serve(app,host=os.getenv('HOST','127.0.0.1'),port=int(os.getenv('PORT','8080')),threads=4)


if __name__=='__main__':
    try:
        main()
    except Failure as exc:
        raise SystemExit(exc.code) from None
