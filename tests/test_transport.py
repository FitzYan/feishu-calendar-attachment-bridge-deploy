"""Actual localhost HTTP transport with simulated Feishu responses; no external requests."""
import json
import tempfile
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch
from cryptography.fernet import Fernet
import service as s


class TransportTests(unittest.TestCase):
    def test_binary_download_multipart_upload_and_json_api(self):
        calls=[];payload=b'%PDF-test'
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                calls.append(('GET',self.path,self.headers.get('Authorization'),None))
                self.send_response(200);self.send_header('Content-Type','application/octet-stream');self.end_headers();self.wfile.write(payload)
            def do_POST(self):
                body=self.rfile.read(int(self.headers.get('Content-Length','0')))
                calls.append(('POST',self.path,self.headers.get('Authorization'),body))
                if self.path.endswith('/tenant_access_token/internal'):
                    d={'code':0,'tenant_access_token':'TEST_TOKEN','expire':7200}
                else:
                    d={'code':0,'data':{'file_token':'NEW_CAL_TOKEN'}}
                self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps(d).encode())
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                cfg=types.SimpleNamespace(data_dir=Path(tmp),key=Fernet.generate_key().decode(),auth_mode='tenant',app_id='test',secret='test-secret',table='tblABC',parent_type='calendar')
                api=s.Feishu(cfg,s.Store(cfg))
                with patch.object(s,'API','http://127.0.0.1:'+str(server.server_port)+'/open-apis'):
                    data=api.download({'file_token':'SOURCE','name':'test.pdf','size':len(payload)},'fldABC','recABC')
                    result=api.upload_calendar({'name':'test.pdf'},data,'cal@example.com')
                    self.assertEqual(result['file_token'],'NEW_CAL_TOKEN')
                download=calls[1];params=parse_qs(urlparse(download[1]).query)
                self.assertEqual(json.loads(params['extra'][0])['bitablePerm']['attachments'],{'fldABC':{'recABC':['SOURCE']}})
                self.assertEqual(download[2],'Bearer TEST_TOKEN')
                upload=calls[2][3]
                self.assertIn(b'name="parent_type"\r\n\r\ncalendar',upload)
                self.assertIn(b'cal@example.com',upload)
                self.assertIn(payload,upload)
                self.assertNotIn(b'SOURCE',upload)
                self.assertEqual(sum('tenant_access_token' in x[1] for x in calls),1)
        finally:
            server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main()
