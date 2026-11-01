"""Explicit smoke test for the locally installed WSL instance; creates test executions."""
import json
import time
from pathlib import Path

import httpx

access=json.loads(Path('.local-access.json').read_text())
client=httpx.Client(base_url=access['url'],timeout=20)
login=client.post('/api/login',json={'username':access['username'],'password':access['password']})
login.raise_for_status()
client.headers['X-CSRF-Token']=login.json()['csrf']


def call(method,url,**kwargs):
    response=client.request(method,url,**kwargs)
    response.raise_for_status()
    return response.json()


def wait(run_id):
    for _ in range(200):
        result=call('GET',f'/api/runs/{run_id}')
        if result['status'] not in ('queued','running'):
            return result
        time.sleep(.1)
    raise AssertionError('Execution did not finish')


def job(name,argv,timeout=10,path='/tmp',kind='script'):
    return call('POST','/api/jobs',json=dict(name=name,kind=kind,path=path,argv=argv,
        schedule='*/5 * * * *',timezone='Asia/Dhaka',timeout=timeout,enabled=False))['id']


ids=[]
try:
    healthy=call('GET','/api/health')
    assert healthy['database']=='connected' and healthy['scheduler']=='healthy'
    print('PASS real MySQL connection and scheduler heartbeat')
    data=dict(name='Missing artisan test',kind='laravel',path='/tmp',argv=['php','artisan','about'],
              schedule='* * * * *',timezone='Asia/Dhaka',timeout=10,enabled=False)
    response=client.post('/api/validate',json=data)
    assert response.status_code==422 and 'artisan' in response.text
    print('PASS missing Laravel artisan rejected')
    first=job('Smoke · Python success',['python3','-c',"import time; print('WSL + MySQL execution OK'); time.sleep(2)"])
    ids.append(first)
    second=job('Smoke · large output',['python3','-c',"print('x'*400000); print('LOG_TAIL_OK')"])
    ids.append(second)
    r1=call('POST',f'/api/jobs/{first}/run')['id']
    r2=call('POST',f'/api/jobs/{second}/run')['id']
    assert client.post(f'/api/jobs/{first}/run').status_code==409
    a,b=wait(r1),wait(r2)
    assert a['status']==b['status']=='success' and 'WSL + MySQL' in a['output']
    assert 'LOG_TAIL_OK' in b['output'] and 'truncated' in b['output']
    assert b['started_at']<a['finished_at']
    print('PASS parallel execution, overlap protection, MySQL 256 KiB logs and exit codes')
    third=job('Smoke · timeout',['python3','-c','import time; time.sleep(20)'],timeout=1)
    ids.append(third)
    r3=call('POST',f'/api/jobs/{third}/run')['id']
    assert wait(r3)['status']=='timeout'
    print('PASS real Linux execution timeout')
    fourth=job('Smoke · cancellation',['python3','-c','import time; time.sleep(20)'])
    ids.append(fourth)
    r4=call('POST',f'/api/jobs/{fourth}/run')['id']
    for _ in range(40):
        if call('GET',f'/api/runs/{r4}')['status']=='running':break
        time.sleep(.1)
    call('POST',f'/api/runs/{r4}/cancel')
    assert wait(r4)['status']=='cancelled'
    print('PASS active Linux run cancellation')
finally:
    for job_id in ids:
        response=client.delete(f'/api/jobs/{job_id}')
        if response.status_code!=200:
            print(f'Cleanup pending for executor {job_id}: {response.status_code}')
    call('POST','/api/logout')
    client.close()
