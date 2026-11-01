import json
from datetime import timedelta

import psutil
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from .database import DATA_DIR, Job, Run, Session, utcnow
from .notifications import get_value, queue_mail, set_value
from .validation import JobInput, next_time, validate_job

SYSTEM_JOBS = {'health_audit', 'daily_report'}


class HealthOptions(BaseModel):
    cpu_percent: int = Field(default=95, ge=1, le=100)
    memory_percent: int = Field(default=90, ge=1, le=100)
    disk_percent: int = Field(default=90, ge=1, le=100)
    queue_wait_seconds: int = Field(default=300, ge=10, le=86400)
    worker_start_grace: int = Field(default=60, ge=5, le=3600)


class AutomationInput(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    schedule: str = Field(max_length=120)
    timezone: str = Field(default='Asia/Dhaka', max_length=80)
    enabled: bool = True
    options: HealthOptions = Field(default_factory=HealthOptions)


def seed_automations():
    with Session.begin() as db:
        for key, name, cron in [('health_audit', 'System · Health audit', '* * * * *'),
                                 ('daily_report', 'System · Daily email report', '0 9 * * *')]:
            if db.scalar(select(Job.id).where(Job.system_key == key)):
                continue
            db.add(Job(name=name, kind='system', mode='scheduled', system_key=key, options=json.dumps(HealthOptions().model_dump()),
                path='', command='[]', schedule=cron, timezone='Asia/Dhaka', timeout=60, stop_grace=1,
                enabled=True, next_run=next_time(cron, 'Asia/Dhaka')))


def audit_health(db):
    from .runner import ACTIVE, runner, snapshot
    audit = db.scalar(select(Job).where(Job.system_key == 'health_audit'))
    options = HealthOptions(**json.loads(audit.options)) if audit else HealthOptions()
    now, issues = utcnow(), []
    def issue(key, message):
        issues.append({'key':key, 'message':message})
    if runner.last_tick is None or (now - runner.last_tick).total_seconds() > 10 or runner.error:
        issue('scheduler', 'Scheduler heartbeat is missing or degraded')
    resources = dict(cpu_percent=psutil.cpu_percent(), memory_percent=psutil.virtual_memory().percent,
                     disk_percent=psutil.disk_usage(str(DATA_DIR)).percent)
    for metric, value in resources.items():
        if value >= getattr(options, metric):
            issue(metric, f'{metric.replace("_", " ")}: {value}% (threshold {getattr(options, metric)}%)')
    jobs = db.scalars(select(Job).where(Job.system_key.is_(None), Job.enabled.is_(True))).all()
    active = {run.job_id:run for run in db.scalars(select(Run).where(Run.status.in_(ACTIVE)))}
    monitored = []
    for job in jobs:
        run = active.get(job.id)
        state = run.status if run else ('waiting' if job.mode == 'continuous' else 'scheduled')
        monitored.append({'id':job.id, 'name':job.name, 'mode':job.mode, 'state':state})
        try:
            validate_job(JobInput(**json.loads(snapshot(job))))
        except (ValueError, OSError) as exc:
            issue(f'executor:{job.id}:path', f'{job.name}: {exc}')
        if run and run.status == 'queued' and (now-run.queued_at).total_seconds()>options.queue_wait_seconds:
            issue(f'executor:{job.id}:queued', f'{job.name}: waiting for an execution slot for more than {options.queue_wait_seconds}s')
        elif run and run.status in ('running', 'stopping'):
            process = runner.processes.get(run.id)
            if run.id not in runner.tasks or (process and process.returncode is not None):
                issue(f'executor:{job.id}:process', f'{job.name}: recorded process is no longer running')
        elif job.mode == 'continuous' and not run:
            due = job.next_run or job.created_at
            if (now-due).total_seconds()>options.worker_start_grace:
                issue(f'executor:{job.id}:missing', f'{job.name}: enabled continuous worker has not started')
        elif job.mode == 'scheduled' and job.next_run and (now-job.next_run).total_seconds()>options.worker_start_grace:
            issue(f'executor:{job.id}:overdue', f'{job.name}: scheduled run is overdue')
    result = dict(checked_at=now.isoformat(), status='healthy' if not issues else 'degraded',
                  issues=issues, resources=resources, monitored=monitored)
    set_value(db, 'health_snapshot', result)
    for item in issues:
        queue_mail(db, 'health', '[QueuePilot] Health alert', item['message']+'\nTime (UTC): '+now.isoformat(), event_key='health:'+item['key'])
    return result


def daily_report(db, run_id):
    now = utcnow()
    cutoff = now-timedelta(hours=24)
    counts = dict(db.execute(select(Run.status, func.count()).where(Run.queued_at >= cutoff).group_by(Run.status)).all())
    jobs = db.scalars(select(Job).where(Job.system_key.is_(None))).all()
    active = {run.job_id:run.status for run in db.scalars(select(Run).where(Run.status.in_(['queued','running','stopping'])))}
    latest_ids = select(func.max(Run.id)).group_by(Run.job_id)
    latest = {run.job_id:run for run in db.scalars(select(Run).where(Run.id.in_(latest_ids)))}
    health = get_value(db, 'health_snapshot', {})
    lines = ['QueuePilot daily report', f'Period (UTC): {cutoff.isoformat()} — {now.isoformat()}',
             f'Executors: {len(jobs)} · Enabled: {sum(j.enabled for j in jobs)}',
             'Run totals (executors and system automations, last 24 hours): '+json.dumps(counts),
             'Last health audit: '+health.get('status', 'not checked'),
             'Health audit time (UTC): '+health.get('checked_at', 'not checked'),
             'Latest resource sample: '+json.dumps(health.get('resources', {})), '', 'Executor status:']
    for job in jobs:
        state = active.get(job.id, 'waiting' if job.enabled and job.mode=='continuous' else 'scheduled' if job.enabled else 'stopped/paused')
        last = latest.get(job.id)
        detail = f'; latest run #{last.id}: {last.status}, exit={last.exit_code}, queued={last.queued_at.isoformat()} UTC' if last else '; no runs recorded'
        lines.append(f'- #{job.id} {job.name}: {state} ({job.mode}){detail}')
    for issue in health.get('issues', []):
        lines.append('Health issue: '+issue['message'])
    report = '\n'.join(lines)
    delivery_id = queue_mail(db, 'report', '[QueuePilot] Daily server report', report)
    return f'Daily report queued as email #{delivery_id}.\n{report}' if delivery_id else f'SMTP disabled; daily email skipped.\n{report}'


async def execute_system(run_id, payload):
    with Session.begin() as db:
        key = payload.get('system_key')
        if key == 'health_audit':
            return json.dumps(audit_health(db), ensure_ascii=False, indent=2)
        if key == 'daily_report':
            return daily_report(db, run_id)
        raise ValueError('Unknown built-in automation')
