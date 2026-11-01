import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

TEMP = tempfile.TemporaryDirectory()
os.environ['DATABASE_URL'] = 'sqlite:///' + str(Path(TEMP.name) / 'test.db')
os.environ['DATA_DIR'] = str(Path(TEMP.name) / 'data')
os.environ['MAX_CONCURRENT_RUNS'] = '2'

from fastapi.testclient import TestClient
from sqlalchemy import select
from panel.app import app, hasher
from panel.database import Base, Job, Run, Session, User, engine, utcnow
from panel.runner import enqueue, runner


@pytest.fixture(scope='module')
def client():
    Base.metadata.create_all(engine)
    with Session.begin() as db:
        db.add(User(username='admin', password_hash=hasher.hash('test-password-123')))
    with TestClient(app) as client:
        yield client
    engine.dispose()
    TEMP.cleanup()


def sign_in(client):
    response = client.post('/api/login', json={'username':'admin','password':'test-password-123'})
    assert response.status_code == 200
    return {'X-CSRF-Token':response.json()['csrf']}


def payload(code="print('hello from executor')", **kwargs):
    return dict(name='Test executor', kind='script', path=TEMP.name, argv=[sys.executable,'-c',code],
                schedule='0 0 1 1 *', timezone='Asia/Dhaka', timeout=10, enabled=False, **kwargs)


def create(client, headers, data=None):
    response = client.post('/api/jobs', json=data or payload(), headers=headers)
    assert response.status_code == 201, response.text
    return response.json()['id']


def wait_run(client, run_id):
    for _ in range(120):
        run = client.get(f'/api/runs/{run_id}').json()
        if run['status'] not in ('queued','running','stopping'):
            return run
        time.sleep(.1)
    pytest.fail('Run never completed')


def test_auth_and_csrf(client):
    assert client.get('/api/jobs').status_code == 401
    assert client.post('/api/login', json={'username':'admin','password':'wrong'}).status_code == 401
    headers=sign_in(client)
    assert client.post('/api/jobs',json=payload()).status_code == 403
    assert 'httponly' in client.cookies.__str__().lower() or client.cookies.get('queuepilot_session')
    assert client.get('/api/me').json()['username']=='admin'


def test_validation_rejects_missing_artisan_and_bad_cron(client):
    headers=sign_in(client)
    data=payload();data['kind']='laravel'
    r=client.post('/api/validate',json=data,headers=headers)
    assert r.status_code==422 and 'artisan' in r.text
    data=payload();data['schedule']='not a cron'
    assert client.post('/api/jobs',json=data,headers=headers).status_code==422
    data=payload();data['timezone']='No/SuchTimezone'
    assert client.post('/api/jobs',json=data,headers=headers).status_code==422
    data=payload();data['path']='relative/path'
    assert client.post('/api/jobs',json=data,headers=headers).status_code==422


def test_success_output_history_and_delete(client):
    headers=sign_in(client);job_id=create(client,headers)
    run_id=client.post(f'/api/jobs/{job_id}/run',headers=headers).json()['id']
    result=wait_run(client,run_id)
    assert result['status']=='success' and result['exit_code']==0
    assert 'hello from executor' in result['output']
    assert client.get(f'/api/runs?job_id={job_id}').json()['total']==1
    assert client.delete(f'/api/jobs/{job_id}',headers=headers).status_code==200
    assert client.get(f'/api/runs/{run_id}').status_code==200


def test_failure_and_output_cap(client):
    headers=sign_in(client)
    job_id=create(client,headers,payload("import sys; print('x'*400000); print('END'); sys.exit(7)"))
    run_id=client.post(f'/api/jobs/{job_id}/run',headers=headers).json()['id']
    result=wait_run(client,run_id)
    assert result['status']=='failed' and result['exit_code']==7
    assert 'END' in result['output'] and 'truncated' in result['output']
    assert len(result['output'])<263000


def test_timeout_overlap_and_cancel(client):
    headers=sign_in(client);data=payload('import time; time.sleep(10)');data['timeout']=1
    job_id=create(client,headers,data)
    run_id=client.post(f'/api/jobs/{job_id}/run',headers=headers).json()['id']
    assert client.post(f'/api/jobs/{job_id}/run',headers=headers).status_code==409
    assert client.delete(f'/api/jobs/{job_id}',headers=headers).status_code==409
    assert wait_run(client,run_id)['status']=='timeout'
    run_id=client.post(f'/api/jobs/{job_id}/run',headers=headers).json()['id']
    assert client.post(f'/api/runs/{run_id}/cancel',headers=headers).status_code==200
    assert wait_run(client,run_id)['status']=='cancelled'


def test_timeout_with_continuous_output(client):
    headers=sign_in(client)
    data=payload("import sys;\nwhile True: sys.stdout.write('x'*8192); sys.stdout.flush()")
    data['timeout']=1
    job_id=create(client,headers,data)
    run_id=client.post(f'/api/jobs/{job_id}/run',headers=headers).json()['id']
    result=wait_run(client,run_id)
    assert result['status']=='timeout'
    assert len(result['output'])<263000


def test_schedule_tick_and_health(client):
    headers=sign_in(client);job_id=create(client,headers)
    with Session.begin() as db:
        job=db.get(Job,job_id);job.enabled=True;job.next_run=utcnow()
    for _ in range(40):
        runs=client.get(f'/api/runs?job_id={job_id}').json()['items']
        if runs:break
        time.sleep(.1)
    assert runs and runs[0]['trigger']=='schedule'
    assert wait_run(client,runs[0]['id'])['status']=='success'
    h=client.get('/api/health').json()
    assert h['database']=='connected' and h['scheduler']=='healthy'
    assert h['max_concurrent']==2
    assert client.post(f'/api/jobs/{job_id}/toggle',headers=headers).json()['enabled'] is False


def test_execution_revalidates_directory(client):
    headers=sign_in(client)
    directory=Path(TEMP.name)/'vanishing';directory.mkdir()
    data=payload();data['path']=str(directory)
    job_id=create(client,headers,data);directory.rmdir()
    run_id=client.post(f'/api/jobs/{job_id}/run',headers=headers).json()['id']
    r=wait_run(client,run_id)
    assert r['status']=='failed' and 'existing absolute directory' in r['output']


def test_logout_revokes_session(client):
    headers=sign_in(client)
    assert client.post('/api/logout',headers=headers).status_code==200
    assert client.get('/api/health').status_code==401


def running_worker(client, job_id, after=0):
    for _ in range(80):
        runs=client.get(f'/api/runs?job_id={job_id}').json()['items']
        for run in runs:
            if run['id']>after and run['status']=='running':
                return run
        time.sleep(.1)
    pytest.fail('Worker did not start')


def test_continuous_lifetime_stop_start_and_cancel(client):
    headers=sign_in(client)
    data=payload('import time; print("WORKER_READY",flush=True); time.sleep(60)')
    data.update(mode='continuous', enabled=True, timeout=1, restart_delay=1, stop_grace=2, schedule='')
    job_id=create(client,headers,data)
    try:
        run=running_worker(client,job_id)
        time.sleep(1.3)
        assert client.get(f'/api/runs/{run["id"]}').json()['status']=='running'
        assert client.post(f'/api/jobs/{job_id}/run',headers=headers).status_code==409
        assert client.put(f'/api/jobs/{job_id}',headers=headers,json=data).status_code==409
        client.post(f'/api/jobs/{job_id}/stop',headers=headers).raise_for_status()
        assert wait_run(client,run['id'])['status']=='cancelled'
        count=client.get(f'/api/runs?job_id={job_id}').json()['total']
        time.sleep(2)
        assert client.get(f'/api/runs?job_id={job_id}').json()['total']==count
        restarted=client.post(f'/api/jobs/{job_id}/run',headers=headers).json()['id']
        running_worker(client,job_id,after=run['id'])
        client.post(f'/api/runs/{restarted}/cancel',headers=headers).raise_for_status()
        assert wait_run(client,restarted)['status']=='cancelled'
        with Session() as db:
            assert db.get(Job,job_id).enabled is False
    finally:
        client.post(f'/api/jobs/{job_id}/stop',headers=headers)


@pytest.mark.parametrize('exit_code',[0,7])
def test_continuous_exit_automatically_restarts(client,exit_code):
    headers=sign_in(client)
    data=payload(f'import sys; print("EXITING"); sys.exit({exit_code})')
    data.update(mode='continuous', enabled=True, restart_delay=1, stop_grace=2)
    job_id=create(client,headers,data)
    try:
        for _ in range(80):
            runs=client.get(f'/api/runs?job_id={job_id}').json()['items']
            if len(runs)>=2:break
            time.sleep(.1)
        assert len(runs)>=2
        first=client.get(f'/api/runs/{runs[-1]["id"]}').json()
        assert first['exit_code']==exit_code
        assert first['status']==('success' if exit_code==0 else 'failed')
        from datetime import datetime
        assert (datetime.fromisoformat(runs[0]['queued_at'])-datetime.fromisoformat(first['finished_at'])).total_seconds()>=1
    finally:
        client.post(f'/api/jobs/{job_id}/stop',headers=headers)


@pytest.mark.skipif(os.name!='posix',reason='POSIX graceful signal behavior')
def test_continuous_graceful_stop_finishes_current_job(client):
    headers=sign_in(client)
    code="import signal,time,sys; signal.signal(signal.SIGTERM,lambda *args: (print('FINISHED_CURRENT_JOB',flush=True),sys.exit(0))); print('READY',flush=True); time.sleep(60)"
    data=payload(code);data.update(mode='continuous',enabled=True,stop_grace=2,restart_delay=1)
    job_id=create(client,headers,data)
    try:
        run=running_worker(client,job_id)
        for _ in range(30):
            detail=client.get(f'/api/runs/{run["id"]}').json()
            if 'READY' in detail['output']:break
            time.sleep(.1)
        client.post(f'/api/jobs/{job_id}/stop',headers=headers).raise_for_status()
        result=wait_run(client,run['id'])
        assert result['status']=='cancelled' and result['exit_code']==0
        assert 'FINISHED_CURRENT_JOB' in result['output']
    finally:
        client.post(f'/api/jobs/{job_id}/stop',headers=headers)


@pytest.mark.skipif(os.name!='posix',reason='POSIX process groups')
def test_continuous_force_kill_after_grace(client):
    headers=sign_in(client)
    data=payload("import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('READY',flush=True); time.sleep(60)")
    data.update(mode='continuous',enabled=True,stop_grace=1)
    job_id=create(client,headers,data)
    try:
        run=running_worker(client,job_id)
        for _ in range(30):
            if 'READY' in client.get(f'/api/runs/{run["id"]}').json()['output']:break
            time.sleep(.1)
        started=time.monotonic()
        client.post(f'/api/jobs/{job_id}/stop',headers=headers).raise_for_status()
        result=wait_run(client,run['id'])
        assert result['status']=='cancelled' and result['exit_code']==-9
        assert 0.8<time.monotonic()-started<5
    finally:
        client.post(f'/api/jobs/{job_id}/stop',headers=headers)


def smtp_payload(**changes):
    result=dict(enabled=True,host='127.0.0.1',port=2525,security='none',username='',
                sender='panel@example.test',recipients=['owner@example.test'],cooldown_seconds=60)
    result.update(changes)
    return result


def test_smtp_validation_encryption_and_redaction(client):
    from panel.database import Setting
    from panel.notifications import cipher
    headers=sign_in(client)
    assert client.put('/api/smtp',json=smtp_payload()).status_code==403
    assert client.put('/api/smtp',json=smtp_payload(recipients=['bad\nBcc: x@example.test']),headers=headers).status_code==422
    secret='smtp-private-password'
    response=client.put('/api/smtp',json=smtp_payload(enabled=False,username='smtp-user',password=secret),headers=headers)
    assert response.status_code==200
    assert response.json()['password_set'] is True and secret not in response.text
    with Session() as db:
        stored=json.loads(db.get(Setting,'smtp').value)
        assert secret not in json.dumps(stored)
        assert cipher().decrypt(stored['password_encrypted'].encode()).decode()==secret
    response=client.put('/api/smtp',json=smtp_payload(enabled=False,username='smtp-user'),headers=headers)
    assert response.json()['password_set'] is True
    response=client.put('/api/smtp',json=smtp_payload(enabled=False,clear_password=True),headers=headers)
    assert response.json()['password_set'] is False


@pytest.mark.parametrize('security',['starttls','ssl','none'])
def test_smtp_transport_security_and_authentication(client,monkeypatch,security):
    from panel import notifications as n
    calls=[]
    class SMTP:
        def __init__(self,*args,**kwargs):calls.append(('connect',args,kwargs))
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def starttls(self,**kwargs):calls.append(('starttls',kwargs))
        def login(self,user,password):calls.append(('login',user,password))
        def send_message(self,message):calls.append(('send',message));return {}
    monkeypatch.setattr(n.smtplib,'SMTP',SMTP)
    monkeypatch.setattr(n.smtplib,'SMTP_SSL',SMTP)
    config=smtp_payload(security=security,username='user')
    config['password_encrypted']=n.cipher(create=True).encrypt(b'private').decode()
    n.send_smtp(config,'Subject','Body',123)
    assert ('login','user','private') in calls
    assert any(c[0]=='starttls' for c in calls)==(security=='starttls')
    assert ('context' in calls[0][2])==(security=='ssl')
    assert calls[-1][1]['To']=='owner@example.test'


def test_local_smtp_delivery_and_history(client):
    import socketserver
    import threading
    messages=[]
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.wfile.write(b'220 local-test ESMTP\r\n')
            while line:=self.rfile.readline():
                command=line.upper()
                if command.startswith(b'EHLO'):
                    self.wfile.write(b'250-local-test\r\n250 SIZE 1000000\r\n')
                elif command.startswith(b'DATA'):
                    self.wfile.write(b'354 End with dot\r\n');data=[]
                    while (part:=self.rfile.readline())!=b'.\r\n':
                        if not part:break
                        data.append(part)
                    messages.append(b''.join(data));self.wfile.write(b'250 accepted\r\n')
                elif command.startswith(b'QUIT'):
                    self.wfile.write(b'221 goodbye\r\n');break
                else:self.wfile.write(b'250 ok\r\n')
    server=socketserver.ThreadingTCPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    headers=sign_in(client)
    try:
        client.put('/api/smtp',json=smtp_payload(port=server.server_address[1],clear_password=True),headers=headers).raise_for_status()
        response=client.post('/api/smtp/test',headers=headers)
        assert response.status_code==200
        mail_id=response.json()['id']
        for _ in range(50):
            item=next(m for m in client.get('/api/mail-history').json()['items'] if m['id']==mail_id)
            if item['status']=='sent':break
            time.sleep(.1)
        assert item['status']=='sent' and item['attempts']==1
        assert any(b'Your QueuePilot SMTP settings are working' in m for m in messages)
    finally:
        client.put('/api/smtp',json=smtp_payload(enabled=False),headers=headers)
        server.shutdown();server.server_close();thread.join(timeout=2)


def test_alert_cooldown_disable_and_retry(client,monkeypatch):
    from panel import notifications as n
    from panel.database import MailDelivery
    headers=sign_in(client)
    # Control the outbox directly; defer rows so the background loop cannot race this test.
    client.put('/api/smtp',json=smtp_payload(),headers=headers).raise_for_status()
    try:
        with Session.begin() as db:
            first=n.queue_mail(db,'failure','Failed','Details',event_key='test:cooldown')
            assert first is not None
            assert n.queue_mail(db,'failure','Duplicate','Details',event_key='test:cooldown') is None
            db.get(MailDelivery,first).available_at=utcnow()+__import__('datetime').timedelta(hours=1)
        def fail(*args):raise n.smtplib.SMTPAuthenticationError(535,b'private-provider-response')
        monkeypatch.setattr(n,'send_smtp',fail)
        for attempt in range(3):
            with Session.begin() as db:
                db.get(MailDelivery,first).available_at=utcnow()
            client.portal.call(n.mailer.deliver_one)
        with Session() as db:
            mail=db.get(MailDelivery,first)
            assert mail.status=='failed' and mail.attempts==3
            assert mail.last_error=='SMTPAuthenticationError (SMTP 535)'
            assert 'private-provider-response' not in mail.last_error
    finally:
        client.put('/api/smtp',json=smtp_payload(enabled=False),headers=headers)
    with Session.begin() as db:
        assert n.queue_mail(db,'failure','Disabled','No send') is None


def test_system_automations_seed_edit_and_reserved_slot(client):
    from panel.system_jobs import seed_automations
    headers=sign_in(client)
    seed_automations();seed_automations()
    with Session() as db:
        system=db.scalars(select(Job).where(Job.system_key.is_not(None))).all()
        assert len(system)==2
        audit=next(j for j in system if j.system_key=='health_audit')
    response=client.put(f'/api/automations/{audit.id}',headers=headers,json=dict(name='Custom health check',schedule='*/3 * * * *',timezone='Asia/Dhaka',enabled=False,options={'disk_percent':98}))
    assert response.status_code==200 and response.json()['options']['disk_percent']==98
    seed_automations()
    with Session() as db:
        assert db.get(Job,audit.id).schedule=='*/3 * * * *' and db.get(Job,audit.id).enabled is False
    assert client.delete(f'/api/jobs/{audit.id}',headers=headers).status_code==409
    workers=[]
    try:
        for _ in range(runner.capacity):
            data=payload('import time; time.sleep(60)');data.update(mode='continuous',enabled=True,stop_grace=1)
            job_id=create(client,headers,data);workers.append(job_id);running_worker(client,job_id)
        run_id=client.post(f'/api/jobs/{audit.id}/run',headers=headers).json()['id']
        result=wait_run(client,run_id)
        assert result['status']=='success'
        h=client.get('/api/health').json()
        assert h['audit']['monitored'] and h['audit']['checked_at']
    finally:
        for job_id in workers:client.post(f'/api/jobs/{job_id}/stop',headers=headers)


def test_daily_report_failure_and_stop_notifications(client,monkeypatch):
    from panel import notifications as n
    sent=[]
    monkeypatch.setattr(n,'send_smtp',lambda config,subject,body,message_id:sent.append((subject,body)))
    headers=sign_in(client)
    client.put('/api/smtp',json=smtp_payload(),headers=headers).raise_for_status()
    try:
        failed=create(client,headers,payload('import sys; sys.exit(3)'))
        failure_id=client.post(f'/api/jobs/{failed}/run',headers=headers).json()['id']
        assert wait_run(client,failure_id)['status']=='failed'
        data=payload('import time; time.sleep(60)');data.update(mode='continuous',enabled=True,stop_grace=1)
        worker=create(client,headers,data);run=running_worker(client,worker)
        client.post(f'/api/jobs/{worker}/stop',headers=headers).raise_for_status()
        wait_run(client,run['id'])
        report=next(j for j in client.get('/api/jobs').json()['items'] if j['system_key']=='daily_report')
        report_id=client.post(f'/api/jobs/{report["id"]}/run',headers=headers).json()['id']
        assert wait_run(client,report_id)['status']=='success'
        for _ in range(80):
            if any('Daily server report' in s for s,b in sent) and any('FAILURE' in s for s,b in sent) and any('STOP' in s for s,b in sent):break
            time.sleep(.1)
        assert any('Daily server report' in s and 'Run totals' in b for s,b in sent)
        assert any('FAILURE' in s and str(failed) in b for s,b in sent)
        assert any('STOP' in s and str(worker) in b for s,b in sent)
    finally:
        client.put('/api/smtp',json=smtp_payload(enabled=False),headers=headers)


def test_notification_preferences_and_disable_cancels_pending(client):
    from datetime import timedelta
    from panel import notifications as n
    from panel.database import MailDelivery
    headers=sign_in(client)
    client.put('/api/smtp',json=smtp_payload(notify_failures=False,notify_stops=False,notify_health=False),headers=headers).raise_for_status()
    try:
        with Session.begin() as db:
            for category in ('failure','stop','health'):
                assert n.queue_mail(db,category,'Suppressed','Preferences disabled') is None
            mail_id=n.queue_mail(db,'report','Pending report','Body')
            db.get(MailDelivery,mail_id).available_at=utcnow()+timedelta(hours=1)
        client.put('/api/smtp',json=smtp_payload(enabled=False),headers=headers).raise_for_status()
        with Session() as db:
            assert db.get(MailDelivery,mail_id).status=='cancelled'
    finally:
        client.put('/api/smtp',json=smtp_payload(enabled=False),headers=headers)


def test_health_audit_detects_missing_path_and_sends_alert(client,monkeypatch):
    from panel import notifications as n
    from panel.database import MailDelivery
    sent=[]
    monkeypatch.setattr(n,'send_smtp',lambda config,subject,body,message_id:sent.append((subject,body)))
    headers=sign_in(client)
    client.put('/api/smtp',json=smtp_payload(),headers=headers).raise_for_status()
    directory=Path(TEMP.name)/'health-missing';directory.mkdir()
    data=payload();data.update(name='Missing path health target',path=str(directory),enabled=True)
    job_id=create(client,headers,data);directory.rmdir()
    try:
        audit=next(j for j in client.get('/api/jobs').json()['items'] if j['system_key']=='health_audit')
        run_id=client.post(f'/api/jobs/{audit["id"]}/run',headers=headers).json()['id']
        result=wait_run(client,run_id)
        assert result['status']=='success'
        report=json.loads(result['output'])
        assert report['status']=='degraded' and any(i['key']==f'executor:{job_id}:path' for i in report['issues'])
        for _ in range(50):
            if any('Missing path health target' in body for subject,body in sent):break
            time.sleep(.1)
        assert any('Missing path health target' in body for subject,body in sent)
    finally:
        client.put('/api/smtp',json=smtp_payload(enabled=False),headers=headers)
        client.delete(f'/api/jobs/{job_id}',headers=headers)
