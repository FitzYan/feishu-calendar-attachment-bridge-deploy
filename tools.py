"""Local operator helper: credentials are read from .env, never in argv or output."""
import argparse
import json
import os
from pathlib import Path
import requests
from dotenv import load_dotenv


def main():
    load_dotenv()
    p=argparse.ArgumentParser()
    p.add_argument('command',choices=['submit','status','retry','reconcile-invites'])
    p.add_argument('argument',help='JSON file for submit; job_id for status/retry')
    p.add_argument('--url',default='http://127.0.0.1:8080',help='service origin; HTTPS required except localhost')
    args=p.parse_args()
    from urllib.parse import urlparse
    u=urlparse(args.url)
    if u.scheme!='https' and not(u.scheme=='http' and u.hostname in ('127.0.0.1','localhost')):
        raise SystemExit('服务地址须使用 HTTPS；只有本机可用 HTTP。')
    if u.query or u.fragment or u.username or u.password:
        raise SystemExit('服务地址格式不合法。')
    secret=os.environ.get('ADMIN_SECRET' if args.command in ('retry','reconcile-invites') else 'WEBHOOK_SECRET','')
    if not secret:
        raise SystemExit('请先在 .env 填写对应服务密钥。')
    headers={'Authorization':'Bearer '+secret}
    sess=requests.Session();sess.trust_env=False
    origin=args.url.rstrip('/')
    try:
        if args.command=='submit':
            body=json.loads(Path(args.argument).read_text())
            r=sess.post(origin+'/webhooks/attachments',json=body,headers=headers,timeout=20,allow_redirects=False)
        else:
            import re
            if not re.fullmatch(r'[0-9a-f]{64}',args.argument):
                raise SystemExit('job_id 必须为 64 位十六进制哈希。')
            path=('/admin/jobs/'+args.argument+'/'+args.command) if args.command in ('retry','reconcile-invites') else '/jobs/'+args.argument
            r=sess.request('POST' if args.command in ('retry','reconcile-invites') else 'GET',origin+path,headers=headers,timeout=20,allow_redirects=False)
        print('HTTP',r.status_code)
        try:
            print(json.dumps(r.json(),ensure_ascii=False,indent=2))
        except ValueError:
            print('响应不是 JSON；请核对代理配置。')
    except requests.RequestException:
        raise SystemExit('网络请求失败；请核对服务地址、网络和证书。') from None
    finally:
        sess.close()


if __name__=='__main__':main()
