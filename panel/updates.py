"""Check a fixed GitHub repository and request a fixed systemd updater."""
import asyncio
import json
import re
import subprocess
import time
from pathlib import Path
from urllib.request import Request, urlopen

INSTALL = Path(__file__).resolve().parent.parent
REQUEST = Path('/var/lib/queuepilot-updates/request')
STATE = Path('/var/lib/queuepilot-update-state.json')
cached = {'checked': 0, 'latest': None, 'error': None}
lock = asyncio.Lock()


def latest_revision():
    request = Request('https://api.github.com/repos/mdatiqmazumder/QueuePilot/commits/main', headers={'User-Agent':'QueuePilot', 'Accept':'application/vnd.github+json'})
    with urlopen(request, timeout=10) as response:
        revision = json.load(response)['sha']
    if not re.fullmatch('[a-f0-9]{40}', revision):
        raise ValueError('Invalid revision')
    return revision


def service_active(unit):
    try:
        result = subprocess.run(['/usr/bin/systemctl', 'is-active', unit], timeout=3, capture_output=True, text=True)
        return result.stdout.strip() in ('active', 'activating', 'reloading')
    except (OSError, subprocess.TimeoutExpired):
        return False


async def status(force=False):
    async with lock:
        if force or time.monotonic() - cached['checked'] > 300 or not cached['checked']:
            try:
                cached['latest'] = await asyncio.to_thread(latest_revision)
                cached['error'] = None
            except Exception:
                cached['error'] = 'Could not check GitHub. Try again later (network or API limit).'
            cached['checked'] = time.monotonic()
    try:
        installed = (INSTALL / '.installed-revision').read_text().strip()
    except OSError:
        installed = None
    try:
        result = json.loads(STATE.read_text()).get('status', 'idle')
    except (OSError, ValueError):
        result = 'idle'
    running, ready = await asyncio.gather(asyncio.to_thread(service_active, 'queuepilot-update.service'), asyncio.to_thread(service_active, 'queuepilot-update.path'))
    return {'installed':installed, 'latest':cached['latest'], 'available':bool(cached['latest'] and cached['latest'] != installed),
            'error':cached['error'], 'ready':ready, 'running':running or REQUEST.exists(), 'result':result}


async def request_update():
    if not await asyncio.to_thread(service_active, 'queuepilot-update.path'):
        raise ValueError('Run queuepilot update once in the terminal to enable UI updates.')
    if await asyncio.to_thread(service_active, 'queuepilot-update.service'):
        raise ValueError('An update is already running.')
    try:
        with REQUEST.open('x') as file:
            file.write('update\n')
    except FileExistsError as exc:
        raise ValueError('An update is already queued.') from exc
    except OSError as exc:
        raise ValueError('Update helper is unavailable. Run queuepilot setup-ui-updates as root.') from exc
