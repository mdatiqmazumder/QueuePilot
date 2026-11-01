"""WSL/MySQL smoke test. Sends only to a temporary loopback SMTP capture server."""
import json
import os
import socketserver
import tempfile
import threading
import time
from pathlib import Path

import httpx

messages=[]


class SMTPHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b'220 local-capture ESMTP\r\n')
        while line:=self.rfile.readline():
            command=line.upper()
            if command.startswith(b'EHLO'):
                self.wfile.write(b'250-local-capture\r\n250 SIZE 2000000\r\n')
            elif command.startswith(b'DATA'):
                self.wfile.write(b'354 End with dot\r\n');data=[]
                while (part:=self.rfile.readline())!=b'.\r\n':
                    if not part:break
                    data.append(part)
                messages.append(b''.join(data));self.wfile.write(b'250 accepted\r\n')
            elif command.startswith(b'QUIT'):
                self.wfile.write(b'221 goodbye\r\n');break
            else:
                self.wfile.write(b'250 ok\r\n')


def main():
    access=json.loads(Path('.local-access.json').read_text())
    client=httpx.Client(base_url='http://127.0.0.1:8090',timeout=20,trust_env=False)
    login=client.post('/api/login',json={k:access[k] for k in ('username','password')})
    login.raise_for_status();client.headers['X-CSRF-Token']=login.json()['csrf']
    def call(method,url,**kwargs):
        response=client.request(method,url,**kwargs);response.raise_for_status();return response.json()
    def wait(run_id):
        for _ in range(150):
            result=call('GET',f'/api/runs/{run_id}')
            if result['status'] not in ('queued','running','stopping'):return result
            time.sleep(.1)
        raise AssertionError('Execution did not finish')
    previous=call('GET','/api/smtp')
    baseline=max((mail['id'] for mail in call('GET','/api/mail-history')['items']),default=0)
    assert previous['enabled'] is False, 'Do not replace an enabled SMTP configuration in a smoke test'
    jobs=call('GET','/api/jobs')['items']
    audit=next(j for j in jobs if j['system_key']=='health_audit')
    report=next(j for j in jobs if j['system_key']=='daily_report')
    print('PASS existing MySQL installation migrated; both system automations seeded')
    server=socketserver.ThreadingTCPServer(('127.0.0.1',0),SMTPHandler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    created=[]
    try:
        call('PUT','/api/smtp',json=dict(enabled=True,host='127.0.0.1',port=server.server_address[1],security='none',
            username='',sender='panel@example.test',recipients=['owner@example.test'],cooldown_seconds=60))
        call('POST','/api/smtp/test')
        def job(name,code,mode='scheduled',path='/tmp',enabled=False):
            job_id=call('POST','/api/jobs',json=dict(name=name,kind='script',mode=mode,path=path,
                argv=['python3','-u','-c',code],schedule='0 0 1 1 *',timezone='Asia/Dhaka',timeout=10,
                enabled=enabled,restart_delay=1,stop_grace=1))['id'];created.append(job_id);return job_id
        failed=job('SMTP smoke · failure','import sys; sys.exit(7)')
        assert wait(call('POST',f'/api/jobs/{failed}/run')['id'])['status']=='failed'
        worker=job('SMTP smoke · stop','import time; print("READY"); time.sleep(60)','continuous')
        worker_run=call('POST',f'/api/jobs/{worker}/run')['id']
        for _ in range(40):
            if call('GET',f'/api/runs/{worker_run}')['status']=='running':break
            time.sleep(.1)
        call('POST',f'/api/jobs/{worker}/stop');assert wait(worker_run)['status']=='cancelled'
        assert wait(call('POST',f'/api/jobs/{report["id"]}/run')['id'])['status']=='success'
        # A unique missing-path issue avoids reusing a resource-alert cooldown from an earlier smoke run.
        health_path=Path(tempfile.mkdtemp(prefix='queuepilot-health-smoke-'))
        os.chmod(health_path,0o755)
        job('SMTP smoke · health path',"print('health fixture')",path=str(health_path),enabled=True)
        health_path.rmdir()
        changed={k:audit[k] for k in ('name','schedule','timezone','enabled','options')}
        changed['options']={**audit['options'],'memory_percent':1}
        call('PUT',f'/api/automations/{audit["id"]}',json=changed)
        assert wait(call('POST',f'/api/jobs/{audit["id"]}/run')['id'])['status']=='success'
        health=call('GET','/api/health')
        assert health['audit']['status']=='degraded' and any(i['key']=='memory_percent' for i in health['audit']['issues'])
        for _ in range(150):
            history=call('GET','/api/mail-history')['items']
            delivered={m['category'] for m in history if m['id']>baseline and m['status']=='sent'}
            if {'test','failure','stop','report','health'}<=delivered:break
            time.sleep(.1)
        assert {'test','failure','stop','report','health'}<=delivered
        assert len(messages)>=5
        print('PASS actual loopback SMTP: test, failure, stop, daily report and health alert accepted')
        print('PASS email delivery history persisted in MySQL')
    finally:
        call('PUT','/api/smtp',json=previous)
        call('PUT',f'/api/automations/{audit["id"]}',json={k:audit[k] for k in ('name','schedule','timezone','enabled','options')})
        # Refresh health after restoring thresholds so the panel shows real current health.
        wait(call('POST',f'/api/jobs/{audit["id"]}/run')['id'])
        for job_id in created:
            for _ in range(30):
                response=client.delete(f'/api/jobs/{job_id}')
                if response.status_code==200:break
                client.post(f'/api/jobs/{job_id}/stop');time.sleep(.1)
        call('POST','/api/logout');client.close()
        server.shutdown();server.server_close();thread.join(timeout=2)
    print('PASS SMTP restored to disabled; automation schedule/options preserved')


if __name__=='__main__':
    main()
