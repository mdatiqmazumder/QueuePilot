import asyncio
import hashlib
import json
import os
import secrets
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

import psutil
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from filelock import FileLock, Timeout
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, text

from .database import Base, DATA_DIR, Job, LoginSession, MailDelivery, Run, Session, User, engine, utcnow
from .runner import ACTIVE, enqueue, request_stop, runner, stop_job
from .migrations import migrate
from .validation import JobInput, next_time, validate_job
from .notifications import SMTPInput, executor_event, get_value, mailer, public_settings, queue_mail, save_settings
from .system_jobs import AutomationInput, seed_automations
from . import telegram
from .database import TelegramDelivery

hasher = PasswordHasher()
attempts = OrderedDict()
started = time.monotonic()
secure_cookie = os.getenv('COOKIE_SECURE', 'false').lower() == 'true'


@asynccontextmanager
async def lifespan(app):
    lock = FileLock(str(DATA_DIR / 'panel.lock'))
    try:
        lock.acquire(timeout=0)
    except Timeout as exc:
        raise RuntimeError('Only one panel process may use this data directory. Use one uvicorn worker.') from exc
    try:
        migrate()
        seed_automations()
        mailer.recover()
        telegram.bot.recover()
        runner.stopping = False
        runner.recover()
        scheduler = asyncio.create_task(runner.loop())
        email_task = asyncio.create_task(mailer.loop())
        telegram_task = asyncio.create_task(telegram.bot.loop())
        try:
            yield
        finally:
            scheduler.cancel()
            await asyncio.gather(scheduler, return_exceptions=True)
            await runner.shutdown()
            email_task.cancel()
            await asyncio.gather(email_task, return_exceptions=True)
            telegram_task.cancel()
            await asyncio.gather(telegram_task, return_exceptions=True)
    finally:
        lock.release()


app = FastAPI(title='QueuePilot', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
STATIC = Path(__file__).parent / 'static'
app.mount('/static', StaticFiles(directory=STATIC), name='static')


@app.middleware('http')
async def headers(request, call_next):
    if request.headers.get('content-length', '').isdigit() and int(request.headers['content-length']) > 65536:
        return JSONResponse({'detail': 'Request too large'}, status_code=413)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'same-origin'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    response.headers['Cache-Control'] = 'no-store'
    return response


def auth(request: Request):
    token = request.cookies.get('queuepilot_session', '')
    with Session() as db:
        session = db.get(LoginSession, hashlib.sha256(token.encode()).hexdigest()) if token else None
        if not session or session.expires <= utcnow():
            raise HTTPException(401, 'Please sign in')
        if request.method not in ('GET', 'HEAD', 'OPTIONS') and not secrets.compare_digest(request.headers.get('x-csrf-token', ''), session.csrf):
            raise HTTPException(403, 'Invalid CSRF token; reload the panel')
        user = db.get(User, session.user_id)
        if not user:
            raise HTTPException(401, 'User no longer exists')
        return {'username': user.username, 'csrf': session.csrf}


class LoginInput(BaseModel):
    username: str = Field(max_length=100)
    password: str = Field(max_length=1024)


@app.post('/api/login')
async def login(data: LoginInput, request: Request, response: Response):
    if request.headers.get('sec-fetch-site') == 'cross-site':
        raise HTTPException(403, 'Cross-site login blocked')
    key = request.client.host
    now = time.monotonic()
    recent = [t for t in attempts.get(key, []) if now - t < 300]
    if len(recent) >= 10:
        raise HTTPException(429, 'Too many attempts. Try again in five minutes.')
    attempts[key] = recent + [now]
    attempts.move_to_end(key)
    if len(attempts) > 10000:
        attempts.popitem(last=False)
    with Session.begin() as db:
        user = db.scalar(select(User).where(User.username == data.username))
        try:
            valid = user and await asyncio.to_thread(hasher.verify, user.password_hash, data.password)
        except (VerificationError, InvalidHashError):
            valid = False
        if not valid:
            raise HTTPException(401, 'Invalid username or password')
        db.execute(delete(LoginSession).where(LoginSession.expires <= utcnow()))
        old = request.cookies.get('queuepilot_session')
        if old:
            db.execute(delete(LoginSession).where(LoginSession.token_hash == hashlib.sha256(old.encode()).hexdigest()))
        token, csrf = secrets.token_urlsafe(48), secrets.token_hex(32)
        db.add(LoginSession(token_hash=hashlib.sha256(token.encode()).hexdigest(), user_id=user.id, csrf=csrf, expires=utcnow() + timedelta(hours=12)))
    attempts.pop(key, None)
    response.set_cookie('queuepilot_session', token, httponly=True, secure=secure_cookie, samesite='strict', max_age=43200)
    return {'username': user.username, 'csrf': csrf}


@app.get('/api/me')
def me(user=Depends(auth)):
    return user


@app.get('/api/directories')
def directories(path: str = '/', user=Depends(auth)):
    directory = Path(path)
    if not directory.is_absolute() or len(path) > 4096:
        raise HTTPException(422, 'Enter an absolute directory path.')
    try:
        directory = directory.resolve(strict=True)
        if not directory.is_dir():
            raise HTTPException(422, 'Path is not a directory.')
        with os.scandir(directory) as entries:
            names = sorted((entry.name for entry in entries if entry.is_dir()), key=str.casefold)
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(400, 'Cannot access this directory.') from exc
    return {'path': str(directory), 'parent': str(directory.parent), 'directories': names}


@app.post('/api/logout')
def logout(request: Request, response: Response, user=Depends(auth)):
    with Session.begin() as db:
        db.execute(delete(LoginSession).where(LoginSession.token_hash == hashlib.sha256(request.cookies['queuepilot_session'].encode()).hexdigest()))
    response.delete_cookie('queuepilot_session')
    return {'ok': True}


def job_dict(job, active=None):
    return dict(id=job.id, name=job.name, kind=job.kind, path=job.path, argv=json.loads(job.command), schedule=job.schedule,
                timezone=job.timezone, timeout=job.timeout, enabled=job.enabled, next_run=job.next_run,
                mode=job.mode, restart_delay=job.restart_delay, stop_grace=job.stop_grace,
                system_key=job.system_key, options=json.loads(job.options),
                active_status=active.status if active else None)


def checked(data):
    try:
        return validate_job(data)
    except (ValueError, OSError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post('/api/validate')
def validate(data: JobInput, user=Depends(auth)):
    result = checked(data)
    return {'valid': True, 'next_run': result['next_run'], 'path': result['path'], 'argv': json.loads(result['command'])}


@app.get('/api/jobs')
def jobs(page: int = 1, user=Depends(auth)):
    with Session() as db:
        items = db.scalars(select(Job).order_by(Job.id.desc()).offset(max(0, page-1)*50).limit(50)).all()
        active = {r.job_id: r for r in db.scalars(select(Run).where(Run.job_id.in_([j.id for j in items]), Run.status.in_(ACTIVE)))}
        return {'items': [job_dict(j, active.get(j.id)) for j in items],
                'total': db.scalar(select(func.count()).select_from(Job))}


@app.post('/api/jobs', status_code=201)
def create_job(data: JobInput, user=Depends(auth)):
    values = checked(data)
    with Session.begin() as db:
        job = Job(**values)
        db.add(job)
        db.flush()
        return job_dict(job)


@app.put('/api/jobs/{job_id}')
async def update_job(job_id: int, data: JobInput, user=Depends(auth)):
    values = checked(data)
    with Session.begin() as db:
        job = db.get(Job, job_id)
        if not job:
            raise HTTPException(404, 'Executor not found')
        if job.system_key:
            raise HTTPException(409, 'Edit this built-in automation using its automation settings')
        if (job.mode == 'continuous' or data.mode == 'continuous') and db.scalar(select(Run.id).where(Run.job_id == job_id, Run.status.in_(ACTIVE))):
            raise HTTPException(409, 'Stop the worker and wait for it to finish before editing')
        for key, value in values.items():
            setattr(job, key, value)
        return job_dict(job)


@app.post('/api/jobs/{job_id}/toggle')
async def toggle(job_id: int, user=Depends(auth)):
    with Session.begin() as db:
        job = db.get(Job, job_id)
        if not job:
            raise HTTPException(404, 'Executor not found')
        if job.mode == 'continuous' and job.enabled:
            stop_job(db, job)
        else:
            job.enabled = not job.enabled
            job.next_run = (utcnow() if job.mode == 'continuous' else next_time(job.schedule, job.timezone)) if job.enabled else None
            if not job.enabled:
                executor_event(db, job, 'stop', 'Scheduled executor paused by administrator')
        return job_dict(job)


@app.delete('/api/jobs/{job_id}')
async def delete_job(job_id: int, user=Depends(auth)):
    with Session.begin() as db:
        job = db.get(Job, job_id)
        if not job:
            raise HTTPException(404, 'Executor not found')
        if job.system_key:
            raise HTTPException(409, 'Pause a built-in automation instead of deleting it')
        if db.scalar(select(Run.id).where(Run.job_id == job_id, Run.status.in_(ACTIVE))):
            raise HTTPException(409, 'Cancel active executions before deleting')
        db.delete(job)
    return {'ok': True}


@app.put('/api/automations/{job_id}')
async def update_automation(job_id: int, data: AutomationInput, user=Depends(auth)):
    try:
        upcoming = next_time(data.schedule, data.timezone)
    except (ValueError, KeyError) as exc:
        raise HTTPException(422, 'Invalid schedule or timezone') from exc
    if not data.name.strip():
        raise HTTPException(422, 'Name cannot be blank')
    with Session.begin() as db:
        job = db.get(Job, job_id)
        if not job or not job.system_key:
            raise HTTPException(404, 'Built-in automation not found')
        if job.enabled and not data.enabled:
            executor_event(db, job, 'stop', 'System automation paused by administrator')
        job.name, job.schedule, job.timezone = data.name.strip(), data.schedule, data.timezone
        job.enabled, job.next_run = data.enabled, upcoming if data.enabled else None
        job.options = json.dumps(data.options.model_dump())
        return job_dict(job)


@app.get('/api/smtp')
async def smtp_settings(user=Depends(auth)):
    with Session() as db:
        return public_settings(db)


@app.get('/api/telegram')
def telegram_settings(user=Depends(auth)):
    with Session() as db:
        return telegram.public_settings(db)


@app.put('/api/telegram')
def telegram_save(data: telegram.TelegramInput, user=Depends(auth)):
    with Session.begin() as db:
        try:
            return telegram.save_settings(db, data)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc


@app.post('/api/telegram/test')
def telegram_test(user=Depends(auth)):
    with Session.begin() as db:
        row_id = telegram.queue_message(db, 'test', '[QueuePilot] Telegram test', 'Your QueuePilot Telegram bot is connected.')
        if not row_id:
            raise HTTPException(409, 'Save and enable Telegram first')
        return {'id': row_id}


@app.get('/api/telegram/history')
def telegram_history(user=Depends(auth)):
    with Session() as db:
        rows = db.scalars(select(TelegramDelivery).order_by(TelegramDelivery.id.desc()).limit(50))
        return {'items': [{key:getattr(row, key) for key in ('id', 'subject', 'status', 'attempts', 'created_at', 'last_error')} for row in rows]}


@app.put('/api/smtp')
async def smtp_save(data: SMTPInput, user=Depends(auth)):
    with Session.begin() as db:
        try:
            return save_settings(db, data)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc


@app.post('/api/smtp/test')
async def smtp_test(user=Depends(auth)):
    with Session.begin() as db:
        mail_id = queue_mail(db, 'test', '[QueuePilot] SMTP test', 'Your QueuePilot SMTP settings are working.\nTime (UTC): '+utcnow().isoformat())
        if not mail_id:
            raise HTTPException(409, 'Save and enable SMTP before sending a test email')
        return {'id':mail_id, 'status':'pending'}


@app.get('/api/mail-history')
async def mail_history(page: int = 1, user=Depends(auth)):
    with Session() as db:
        rows = db.scalars(select(MailDelivery).order_by(MailDelivery.id.desc()).offset(max(0,page-1)*50).limit(50))
        return {'items':[{key:getattr(mail,key) for key in ('id','category','subject','status','attempts','created_at','sent_at','last_error')} for mail in rows],
                'total':db.scalar(select(func.count()).select_from(MailDelivery))}


@app.post('/api/jobs/{job_id}/run')
async def run_now(job_id: int, user=Depends(auth)):
    with Session.begin() as db:
        job = db.get(Job, job_id)
        if not job:
            raise HTTPException(404, 'Executor not found')
        try:
            run = enqueue(db, job, 'manual')
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        if job.mode == 'continuous':
            job.enabled, job.next_run = True, None
        return {'id': run.id, 'status': run.status}


@app.post('/api/jobs/{job_id}/stop')
async def stop_worker(job_id: int, user=Depends(auth)):
    with Session.begin() as db:
        job = db.get(Job, job_id)
        if not job:
            raise HTTPException(404, 'Executor not found')
        if job.mode != 'continuous':
            raise HTTPException(409, 'Use cancel on a scheduled execution')
        stop_job(db, job)
    return {'ok': True}


def run_dict(run, detail=False):
    fields = ('id', 'job_id', 'job_name', 'status', 'trigger', 'queued_at', 'started_at', 'finished_at', 'exit_code')
    result = {field: getattr(run, field) for field in fields}
    if detail:
        result['output'] = run.output
    return result


@app.get('/api/runs')
def runs(page: int = 1, job_id: int | None = None, user=Depends(auth)):
    filters = [Run.job_id == job_id] if job_id is not None else []
    with Session() as db:
        return {'items': [run_dict(r) for r in db.scalars(select(Run).where(*filters).order_by(Run.id.desc()).offset(max(0, page-1)*50).limit(50))],
                'total': db.scalar(select(func.count()).select_from(Run).where(*filters))}


@app.get('/api/runs/{run_id}')
def run_detail(run_id: int, user=Depends(auth)):
    with Session() as db:
        run = db.get(Run, run_id)
        if not run:
            raise HTTPException(404, 'Execution not found')
        return run_dict(run, True)


@app.post('/api/runs/{run_id}/cancel')
async def cancel_run(run_id: int, user=Depends(auth)):
    with Session.begin() as db:
        run = db.get(Run, run_id)
        if not run:
            raise HTTPException(404, 'Execution not found')
        if run.status in ACTIVE:
            job = db.get(Job, run.job_id)
            if job and job.mode == 'continuous':
                stop_job(db, job)
            else:
                request_stop(db, run)
    return {'ok': True}


@app.get('/api/health')
def health(user=Depends(auth)):
    with Session() as db:
        db.execute(text('SELECT 1'))
        counts = dict(db.execute(select(Run.status, func.count()).group_by(Run.status)).all())
        total_jobs = db.scalar(select(func.count()).select_from(Job))
        enabled = db.scalar(select(func.count()).select_from(Job).where(Job.enabled.is_(True)))
        audit = get_value(db, 'health_snapshot')
        smtp = public_settings(db)
        mail_counts = dict(db.execute(select(MailDelivery.status, func.count()).group_by(MailDelivery.status)).all())
    mem, disk = psutil.virtual_memory(), psutil.disk_usage(str(DATA_DIR))
    alive = runner.last_tick is not None and (utcnow()-runner.last_tick).total_seconds() < 10 and not runner.error
    return dict(database='connected', scheduler='healthy' if alive else 'degraded', scheduler_error=runner.error,
                last_tick=runner.last_tick, uptime_seconds=int(time.monotonic()-started), cpu_percent=psutil.cpu_percent(),
                memory_percent=mem.percent, disk_percent=disk.percent, max_concurrent=runner.capacity,
                counts=counts, total_jobs=total_jobs, enabled_jobs=enabled, audit=audit,
                smtp_enabled=smtp['enabled'], mail_counts=mail_counts)


@app.get('/')
def index():
    return FileResponse(STATIC / 'index.html')
