import os
from pathlib import Path
import subprocess
import sys


def test_default_sqlite_setup_login_and_execution(tmp_path):
    script = '''
from pathlib import Path
import os, sys, time
from panel import setup
questions=[]
def ask(label, default=None):
    questions.append(label)
    return default or ''
setup.ask=ask
setup.getpass.getpass=lambda prompt: 'setup-test-password'
setup.main()
assert not any('MySQL' in label for label in questions)
assert Path('data/queuepilot.db').is_file()
assert 'sqlite:///' in Path('.env').read_text()
if os.name=='posix':
    assert Path('data/queuepilot.db').stat().st_mode & 0o777 == 0o600
from fastapi.testclient import TestClient
from panel.app import app
with TestClient(app) as client:
    login=client.post('/api/login',json={'username':'admin','password':'setup-test-password'})
    assert login.status_code==200,login.text
    headers={'X-CSRF-Token':login.json()['csrf']}
    job=client.post('/api/jobs',headers=headers,json={'name':'SQLite execution','kind':'script','mode':'scheduled','path':str(Path.cwd()),'argv':[sys.executable,'-c','print("sqlite works")'],'schedule':'0 0 1 1 *','timezone':'Asia/Dhaka','timeout':10,'enabled':False})
    assert job.status_code==201,job.text
    run=client.post('/api/jobs/'+str(job.json()['id'])+'/run',headers=headers).json()
    for _ in range(80):
        result=client.get('/api/runs/'+str(run['id'])).json()
        if result['status'] not in ('queued','running'):break
        time.sleep(.1)
    assert result['status']=='success',result
    assert 'sqlite works' in result['output']
print('SQLite default setup, migrations, admin login and executor run: OK')
'''
    env=os.environ.copy()
    for key in ('DATABASE_URL','DATA_DIR'):
        env.pop(key,None)
    env['PYTHONPATH']=str(Path(__file__).resolve().parents[1])
    result=subprocess.run([sys.executable,'-c',script],cwd=tmp_path,env=env,capture_output=True,text=True,timeout=45)
    assert result.returncode==0,result.stdout+result.stderr
