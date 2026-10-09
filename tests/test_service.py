import copy
import io
import json
import logging
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
import service as s


class FakeFeishu:
    def __init__(self):
        self.event_data={'event_id':'event_0','organizer_calendar_id':'cal@example.com',
                         'summary':'原主题','description':'原描述','start_time':{'timestamp':'1'},
                         'attachments':[{'file_token':'old','file_size':'123','name':'已有资料.pdf'}]}
        self.values={'候选人简历':[{'file_token':'src','name':'隐私姓名.pdf','size':8}]}
        self.role='owner';self.uploads=0;self.patches=[];self.created=0;self.invites=0;self.ids=[]
        self.patch_timeout=False;self.invite_timeout=False;self.forbidden=False;self.writeback=False
        self.last_extra=None

    def api(self,method,path,**kwargs):
        if path.endswith('/records/batch_get'):
            return {'records':[],'forbidden_record_ids':['recABC']} if self.forbidden else {'records':[{'record_id':'recABC','fields':copy.deepcopy(self.values)}]}
        if path.endswith('/fields'):
            return {'items':[{'field_name':'候选人简历','field_id':'fldABC','type':17}],'has_more':False}
        if path.endswith('/attendees'):
            if method=='GET':
                return {'items':[{'type':'user','user_id':x} for x in self.ids], 'has_more':False}
            self.ids.extend(x['user_id'] for x in kwargs['json']['attendees']);self.invites+=1
            if self.invite_timeout:
                self.invite_timeout=False
                raise s.Failure('network_or_timeout',True)
            return {'attendees':[]}
        if path.endswith('/events') and method=='POST':
            self.created+=1
            self.create_params=kwargs['params'];self.create_body=kwargs['json']
            self.event_data['attachments']=[]
            return {'event':copy.deepcopy(self.event_data)}
        if path.endswith('/records/recABC') and method=='PUT':
            self.writeback=True;self.values.update(kwargs['json']['fields']);return {'record':{'fields':self.values}}
        if '/events/' in path:
            if method=='GET':return {'event':copy.deepcopy(self.event_data)}
            self.patches.append(copy.deepcopy(kwargs['json']))
            old={x['file_token']:x for x in self.event_data['attachments']}
            self.event_data['attachments']=[old.get(x['file_token'],{'file_token':x['file_token'],'file_size':'8'}) for x in kwargs['json']['attachments']]
            if self.patch_timeout:
                self.patch_timeout=False
                raise s.Failure('network_or_timeout',True)
            return {'event':copy.deepcopy(self.event_data)}
        return {'calendar':{'role':self.role,'type':'shared'}}

    def download(self,a,field_id,record_id):
        self.last_extra=(field_id,record_id,a['file_token']);return b'%PDF-x\n\n'

    def upload_calendar(self,a,data,cal):
        self.uploads+=1;return {'file_token':'new'+str(self.uploads),'size':len(data)}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.cfg=types.SimpleNamespace(data_dir=Path(self.tmp.name),key=Fernet.generate_key().decode(),
            app_id='cli_test',auth_mode='tenant',base='baseTest',table='tblABC',fields=['候选人简历'],
            calendars=['cal@example.com'],allow_create=True,status_field='',job_field='',event_field='',calendar_field='',bound_event_field='',bound_calendar_field='',
            webhook_secret='w'*40,admin_secret='a'*40,max_queue=10,max_attempts=3,retention_days=30,
            secret='privateSecret',parent_type='calendar',redirect_uri='https://example.com/oauth/callback')
        self.store=s.Store(self.cfg);self.api=FakeFeishu();self.sync=s.Sync(self.cfg,self.store,self.api)
        self.body={'mode':'attach','record_id':'recABC','calendar_id':'cal@example.com','event_id':'event_0','request_id':'v1'}

    def tearDown(self):self.tmp.cleanup()

    def run_sync(self):return self.sync.run(s.digest('test-job'),self.body)

    def test_append_preserves_other_properties_and_attachment(self):
        old=copy.deepcopy(self.api.event_data)
        result=self.run_sync()
        self.assertTrue(result['verified']);self.assertEqual(result['attachment_count'],2)
        self.assertEqual(set(self.api.patches[0]),{'attachments'})
        self.assertEqual(self.api.event_data['summary'],old['summary'])
        self.assertEqual(self.api.event_data['start_time'],old['start_time'])
        self.assertIn('old',{x['file_token'] for x in self.api.event_data['attachments']})
        self.assertEqual(self.api.last_extra,('fldABC','recABC','src'))

    def test_repeat_no_upload_or_patch(self):
        self.run_sync();self.run_sync()
        self.assertEqual(self.api.uploads,1);self.assertEqual(len(self.api.patches),1)

    def test_timeout_after_patch_is_reconciled_after_restart(self):
        self.api.patch_timeout=True
        with self.assertRaises(s.Failure):self.run_sync()
        fresh=s.Store(self.cfg);sync=s.Sync(self.cfg,fresh,self.api)
        self.assertTrue(sync.run(s.digest('test-job'),self.body)['verified'])
        self.assertEqual(self.api.uploads,1);self.assertEqual(len(self.api.patches),1)

    def test_foreign_calendar_copy_denied_before_upload(self):
        self.api.event_data['organizer_calendar_id']='someone_else'
        with self.assertRaisesRegex(s.Failure,'organizer_calendar'):self.run_sync()
        self.assertEqual(self.api.uploads,0)

    def test_record_event_binding_enforced(self):
        self.cfg.bound_event_field='初试日程 ID'
        self.api.values['初试日程 ID']='other_0'
        with self.assertRaisesRegex(s.Failure,'binding_mismatch'):self.run_sync()
        self.assertEqual(self.api.uploads,0)
        self.api.values['初试日程 ID']='event_0'
        self.assertTrue(self.run_sync()['verified'])

    def test_reader_denied(self):
        self.api.role='reader'
        with self.assertRaisesRegex(s.Failure,'calendar_not_editable'):self.run_sync()
        self.assertEqual(self.api.uploads,0)

    def test_forbidden_record_denied(self):
        self.api.forbidden=True
        with self.assertRaisesRegex(s.Failure,'record_missing_or_forbidden'):self.run_sync()
        self.assertEqual(self.api.uploads,0)

    def test_large_file_denied(self):
        self.api.values['候选人简历'][0]['size']=s.UPLOAD_LIMIT+1
        with self.assertRaisesRegex(s.Failure,'20mb'):self.run_sync()
        self.assertEqual(self.api.uploads,0)

    def test_total_cap_includes_existing(self):
        self.api.event_data['attachments'][0]['file_size']=str(s.EVENT_LIMIT)
        with self.assertRaisesRegex(s.Failure,'25mb'):self.run_sync()
        self.assertEqual(self.api.uploads,0)

    def test_unknown_existing_size_fails_closed(self):
        del self.api.event_data['attachments'][0]['file_size']
        with self.assertRaisesRegex(s.Failure,'size_unknown'):self.run_sync()

    def test_recurring_or_cancelled_denied(self):
        self.api.event_data['recurrence']='FREQ=DAILY'
        with self.assertRaisesRegex(s.Failure,'recurring'):self.run_sync()
        self.api.event_data.pop('recurrence');self.api.event_data['status']='cancelled'
        with self.assertRaisesRegex(s.Failure,'cancelled'):self.run_sync()

    def test_existing_token_loss_stops(self):
        self.api.patch_timeout=True
        with self.assertRaises(s.Failure):self.run_sync()
        self.api.event_data['attachments']=[x for x in self.api.event_data['attachments'] if x['file_token']!='old']
        with self.assertRaisesRegex(s.Failure,'existing_attachments_changed'):self.run_sync()

    def test_duplicate_source_token_across_fields(self):
        self.api.values['候选人简历']*=2
        self.run_sync();self.assertEqual(self.api.uploads,1)

    def test_writeback_only_success_and_readback(self):
        self.cfg.status_field='同步状态';self.cfg.event_field='日程 ID'
        self.run_sync();self.assertEqual(self.api.values['同步状态'],'同步成功')
        self.assertEqual(self.api.values['日程 ID'],'event_0')

    def test_create_idempotency_and_invitation_dedup(self):
        self.body.pop('event_id');self.body['mode']='create'
        self.body['event']={'summary':'测试','start_time':{'timestamp':'1791784800','timezone':'Asia/Shanghai'},'end_time':{'timestamp':'1791788400','timezone':'Asia/Shanghai'}}
        self.body['attendee_open_ids']=['ou_ABC']
        self.run_sync();self.run_sync()
        self.assertEqual(self.api.created,1);self.assertEqual(self.api.invites,1)
        self.assertEqual(len(self.api.create_params['idempotency_key']),36)
        self.assertNotIn('attendees',self.api.create_body)

    def test_uncertain_invite_not_blindly_retried(self):
        self.body.pop('event_id');self.body['mode']='create';self.body['event']={};self.body['attendee_open_ids']=['ou_ABC']
        self.api.invite_timeout=True
        with self.assertRaisesRegex(s.Failure,'attendee_result_unknown') as ctx:self.run_sync()
        self.assertFalse(ctx.exception.retry)
        self.run_sync();self.assertEqual(self.api.invites,1)

    def test_sqlite_encryption_and_idempotency_conflict(self):
        body={**self.body,'request_id':'privateCandidateName'}
        job,new=self.store.enqueue(body,10)
        self.assertTrue(new);self.assertEqual(self.store.enqueue(body,10),(job,False))
        # logical request same but different payload cannot silently overwrite.
        changed={**body,'attendee_open_ids':[]}
        with self.assertRaisesRegex(s.Failure,'conflict'):self.store.enqueue(changed,10)
        self.assertNotIn(b'privateCandidateName',self.store.path.read_bytes())

    def test_http_auth_and_quick_enqueue(self):
        worker=types.SimpleNamespace()
        app=s.create_app(self.cfg,self.store,self.api,worker);app.testing=True
        client=app.test_client()
        self.assertEqual(client.post('/webhooks/attachments',json=self.body).status_code,401)
        headers={'Authorization':'Bearer '+self.cfg.webhook_secret}
        r=client.post('/webhooks/attachments',json=self.body,headers=headers)
        self.assertEqual(r.status_code,202);self.assertEqual(self.api.uploads,0)
        job=r.json['job_id']
        self.assertEqual(client.get('/jobs/'+job).status_code,401)
        self.assertEqual(client.get('/jobs/'+job,headers=headers).json['state'],'queued')
        self.assertEqual(client.post('/webhooks/attachments',json={**self.body,'calendar_id':'evil'},headers=headers).status_code,400)
        self.assertEqual(client.post('/webhooks/attachments',json={**self.body,'download_url':'https://evil'},headers=headers).status_code,400)
        self.assertEqual(client.post('/webhooks/attachments',data='x'*40000,headers={**headers,'Content-Type':'application/json'}).status_code,413)

    def test_durable_queue_claim(self):
        job,_=self.store.enqueue(self.body,10)
        fresh=s.Store(self.cfg)
        self.assertEqual(fresh.claim()['id'],job);self.assertIsNone(fresh.claim())
        self.assertEqual(fresh.result(job)['attempts'],1)

    def test_queue_bound(self):
        self.store.enqueue(self.body,1)
        with self.assertRaisesRegex(s.Failure,'queue_full'):self.store.enqueue({**self.body,'request_id':'v2'},1)

    def test_signature_and_stream_size_check(self):
        api=s.Feishu(self.cfg,self.store)
        r=types.SimpleNamespace(iter_content=lambda n:iter([b'<html>']),close=lambda:None)
        with patch.object(api,'token',return_value='secret'),patch.object(api,'raw',return_value=r):
            with self.assertRaisesRegex(s.Failure,'signature'):api.download({'file_token':'src','size':6,'name':'a.pdf'},'fldABC','recABC')
        r.iter_content=lambda n:iter([b'%PDF-x\n\n'])
        with patch.object(api,'token',return_value='secret'),patch.object(api,'raw',return_value=r) as raw:
            data=api.download({'file_token':'src','size':8,'name':'a.pdf'},'fldABC','recABC')
            self.assertEqual(len(data),8)
            extra=json.loads(raw.call_args.kwargs['params']['extra'])
            self.assertEqual(extra['bitablePerm']['attachments'],{'fldABC':{'recABC':['src']}})

    def test_oauth_state_one_time(self):
        with self.store.db() as db:db.execute('INSERT INTO oauth_states VALUES (?,?)',(s.digest('state'),9999999999))
        self.assertTrue(self.store.consume_state('state'));self.assertFalse(self.store.consume_state('state'))

    def test_oauth_unknown_blocks_before_refresh(self):
        self.cfg.auth_mode='user';self.store.put('oauth_unknown',{'blocked':True})
        api=s.Feishu(self.cfg,self.store)
        with patch.object(api,'oauth_exchange') as exchange:
            with self.assertRaisesRegex(s.Failure,'unknown'):api.token()
            exchange.assert_not_called()

    def test_retry_is_bounded_and_log_is_redacted(self):
        self.store.enqueue(self.body,10)
        worker=s.Worker(self.cfg,self.store,self.sync)
        log=io.StringIO();handler=logging.StreamHandler(log);s.LOG.addHandler(handler)
        def fail(*args):
            worker.stop.set();raise s.Failure('network_or_timeout',True)
        with patch.object(self.sync,'run',side_effect=fail):worker.loop()
        s.LOG.removeHandler(handler)
        row=self.store.result(s.digest(s.canonical(['attach','recABC','cal@example.com','event_0','v1'])))
        self.assertEqual(row['state'],'retry');self.assertEqual(row['attempts'],1)
        self.assertNotIn('recABC',log.getvalue());self.assertNotIn('隐私姓名',log.getvalue())

    def test_invitation_reconciliation_does_not_invite(self):
        job,_=self.store.enqueue(self.body,10)
        with self.store.db() as db:
            db.execute("UPDATE jobs SET state='failed',error='attendee_result_unknown_check_calendar' WHERE id=?",(job,))
        app=s.create_app(self.cfg,self.store,self.api,types.SimpleNamespace());app.testing=True
        client=app.test_client();headers={'Authorization':'Bearer '+self.cfg.admin_secret}
        self.assertEqual(client.post('/admin/jobs/'+job+'/retry',headers=headers).status_code,409)
        self.assertEqual(client.post('/admin/jobs/'+job+'/reconcile-invites',headers=headers).status_code,202)
        self.assertTrue(self.store.get('invite_check_only:'+job))
        self.body.pop('event_id');self.body['mode']='create';self.body['event']={};self.body['attendee_open_ids']=['ou_ABC']
        with self.assertRaisesRegex(s.Failure,'attendees_still_missing'):self.sync.run(job,self.body)
        self.assertEqual(self.api.invites,0)
        self.api.ids=['ou_ABC']
        self.assertTrue(self.sync.run(job,self.body)['verified']);self.assertEqual(self.api.invites,0)

    def test_unknown_error_payload_is_not_logged(self):
        self.store.enqueue(self.body,10)
        worker=s.Worker(self.cfg,self.store,self.sync)
        log=io.StringIO();handler=logging.StreamHandler(log);s.LOG.addHandler(handler)
        def fail(*args):
            worker.stop.set();raise RuntimeError('secret candidate name token')
        with patch.object(self.sync,'run',side_effect=fail):worker.loop()
        s.LOG.removeHandler(handler)
        self.assertIn('internal_error',log.getvalue());self.assertNotIn('candidate',log.getvalue())

    def test_attendee_readback_paginates_with_documented_limit(self):
        calls=[]
        def pages(method,path,**kwargs):
            params=kwargs['params'];calls.append(params)
            self.assertLessEqual(params['page_size'],100)
            return {'items':[{'type':'user','user_id':'ou_SECOND'}],'has_more':False} if params.get('page_token') else {
                'items':[{'type':'user','user_id':'ou_FIRST'},{'type':'user','user_id':'ou_REMOVED','rsvp_status':'removed'}],
                'has_more':True,'page_token':'next'}
        with patch.object(self.api,'api',side_effect=pages):
            self.assertEqual(self.sync.attendee_ids('/event'),{'ou_FIRST','ou_SECOND'})
        self.assertEqual(len(calls),2)

    def test_disabled_create_and_millisecond_time(self):
        self.cfg.allow_create=False
        with self.assertRaises(s.Failure):s.validate_payload({**self.body,'mode':'create'},self.cfg)
        self.cfg.allow_create=True
        b={'mode':'create','record_id':'recABC','calendar_id':'cal@example.com','request_id':'v1','event':{
            'summary':'test','start_time':{'timestamp':'1791784800000','timezone':'Asia/Shanghai'},'end_time':{'timestamp':'1791788400000','timezone':'Asia/Shanghai'}}}
        with self.assertRaisesRegex(s.Failure,'unix_seconds'):s.validate_payload(b,self.cfg)


if __name__=='__main__':unittest.main()
