"""Run as root inside WSL against the test installation. Restarts the panel service."""
import json
import subprocess
import time
from pathlib import Path

import httpx

access=json.loads(Path('.local-access.json').read_text())
client=httpx.Client(base_url='http://127.0.0.1:8090',timeout=15,trust_env=False)
for _ in range(40):
    try:
        response=client.post('/api/login',json={k:access[k] for k in ('username','password')})
        response.raise_for_status()
        client.headers['X-CSRF-Token']=response.json()['csrf']
        break
    except httpx.TransportError:
        time.sleep(.2)
else:
    raise AssertionError('Panel not reachable')


def call(method,url,**kwargs):
    response=client.request(method,url,**kwargs)
    response.raise_for_status()
    return response.json()


def running(job_id,after=0):
    for _ in range(100):
        try:
            for run in call('GET',f'/api/runs?job_id={job_id}')['items']:
                if run['id']>after and run['status']=='running':return run
        except httpx.TransportError:
            pass
        time.sleep(.2)
    raise AssertionError('Worker did not start')


data=dict(name='Smoke · continuous worker',kind='script',mode='continuous',path='/tmp',
          argv=['python3','-u','-c',"import time; print('CONTINUOUS_WORKER_READY'); time.sleep(120)"],
          schedule='',timezone='Asia/Dhaka',timeout=1,enabled=True,restart_delay=1,stop_grace=2)
job_id=call('POST','/api/jobs',json=data)['id']
try:
    first=running(job_id)
    time.sleep(1.5)
    assert call('GET',f'/api/runs/{first["id"]}')['status']=='running'
    print('PASS continuous worker remains running beyond scheduled process timeout')
    subprocess.run(['systemctl','restart','queuepilot'],check=True)
    second=running(job_id,after=first['id'])
    assert call('GET',f'/api/runs/{first["id"]}')['status']=='interrupted'
    assert 'CONTINUOUS_WORKER_READY' in call('GET',f'/api/runs/{second["id"]}')['output']
    print('PASS enabled worker automatically resumes after service restart')
    call('POST',f'/api/jobs/{job_id}/stop')
    for _ in range(80):
        if call('GET',f'/api/runs/{second["id"]}')['status']=='cancelled':break
        time.sleep(.1)
    else:raise AssertionError('Worker did not stop')
    count=call('GET',f'/api/runs?job_id={job_id}')['total']
    time.sleep(2)
    assert call('GET',f'/api/runs?job_id={job_id}')['total']==count
    assert next(j for j in call('GET','/api/jobs')['items'] if j['id']==job_id)['enabled'] is False
    print('PASS Stop disables automatic restart; no duplicate worker')
finally:
    call('POST',f'/api/jobs/{job_id}/stop')
    for _ in range(40):
        response=client.delete(f'/api/jobs/{job_id}')
        if response.status_code==200:break
        time.sleep(.1)
    call('POST','/api/logout')
    client.close()
