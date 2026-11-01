import asyncio
import json
import logging
import os
import signal
from datetime import timedelta

from sqlalchemy import select

from .database import Job, Run, Session, utcnow
from .validation import JobInput, next_time, validate_job
from .notifications import executor_event

log = logging.getLogger(__name__)
OUTPUT_LIMIT = 256 * 1024
ACTIVE = ['queued', 'running', 'stopping']


def snapshot(job):
    return json.dumps(dict(name=job.name, kind=job.kind, path=job.path, argv=json.loads(job.command),
                           schedule=job.schedule, timezone=job.timezone, timeout=job.timeout, enabled=job.enabled,
                           mode=job.mode, restart_delay=job.restart_delay, stop_grace=job.stop_grace,
                           system_key=job.system_key, options=json.loads(job.options)))


def enqueue(db, job, trigger):
    active = db.scalar(select(Run.id).where(Run.job_id == job.id, Run.status.in_(ACTIVE)))
    if active:
        raise ValueError('This executor already has a queued or running execution')
    run = Run(job_id=job.id, job_name=job.name, trigger=trigger, status='queued', snapshot=snapshot(job))
    db.add(run)
    db.flush()
    return run


def request_stop(db, run):
    if run.status == 'queued':
        run.status, run.finished_at = 'cancelled', utcnow()
        job = db.get(Job, run.job_id)
        if job:
            executor_event(db, job, 'stop', 'Queued execution cancelled', run.id)
    elif run.status == 'running':
        run.status = 'stopping'
    event = runner.stop_events.get(run.id)
    if event:
        event.set()


def stop_job(db, job):
    was_enabled = job.enabled
    job.enabled, job.next_run = False, None
    for run in db.scalars(select(Run).where(Run.job_id == job.id, Run.status.in_(ACTIVE))):
        request_stop(db, run)
    if was_enabled:
        executor_event(db, job, 'stop', 'Stop requested by administrator; automatic restart disabled')


class Runner:
    def __init__(self):
        self.capacity = max(1, int(os.getenv('MAX_CONCURRENT_RUNS', '8')))
        self.tasks = {}
        self.stop_events = {}
        self.processes = {}
        self.system_tasks = set()
        self.last_tick = None
        self.error = None
        self.stopping = False

    def recover(self):
        with Session.begin() as db:
            for run in db.scalars(select(Run).where(Run.status.in_(['running', 'stopping']))):
                run.status = 'interrupted'
                run.finished_at = utcnow()
                run.output += '\nPanel restarted; execution was interrupted. Check external side effects before retrying.'
                job = db.get(Job, run.job_id)
                if job:
                    executor_event(db, job, 'stop', 'Execution interrupted by panel restart', run.id)
            for job in db.scalars(select(Job).where(Job.mode == 'continuous', Job.enabled.is_(True))):
                job.next_run = utcnow()

    async def loop(self):
        while True:
            try:
                now = utcnow()
                with Session.begin() as db:
                    jobs = db.scalars(select(Job).where(Job.enabled.is_(True), Job.next_run <= now)).all()
                    for job in jobs:
                        try:
                            enqueue(db, job, 'worker' if job.mode == 'continuous' else 'schedule')
                        except ValueError:
                            pass  # Skip overlapping slots; never build a catch-up backlog.
                        job.next_run = None if job.mode == 'continuous' else next_time(job.schedule, job.timezone, now)
                    free = max(0, self.capacity - (len(self.tasks) - len(self.system_tasks)))
                    ids = []
                    if free:
                        ids = list(db.scalars(select(Run.id).join(Job, Job.id == Run.job_id).where(Run.status == 'queued', Job.system_key.is_(None)).order_by(Run.id).limit(free)))
                    if not self.system_tasks:
                        internal = list(db.scalars(select(Run.id).join(Job, Job.id == Run.job_id).where(Run.status == 'queued', Job.system_key.is_not(None)).order_by(Run.id).limit(1)))
                        self.system_tasks.update(internal)
                        ids.extend(internal)
                    for run_id in ids:
                        if run_id not in self.tasks:
                            self.stop_events[run_id] = asyncio.Event()
                            task = asyncio.create_task(self.execute(run_id))
                            self.tasks[run_id] = task
                            task.add_done_callback(lambda t, rid=run_id: self.completed(rid, t))
                self.last_tick = utcnow()
                self.error = None
            except Exception:
                self.error = 'Scheduler could not complete its last tick; inspect service logs.'
                log.exception('Scheduler tick failed')
            await asyncio.sleep(1)

    def completed(self, run_id, task):
        self.tasks.pop(run_id, None)
        self.stop_events.pop(run_id, None)
        self.processes.pop(run_id, None)
        self.system_tasks.discard(run_id)
        if not task.cancelled() and task.exception():
            log.error('Execution %s failed: %s', run_id, task.exception())

    async def shutdown(self):
        self.stopping = True
        tasks = list(self.tasks.values())
        for event in self.stop_events.values():
            event.set()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def execute(self, run_id):
        proc = None
        output = bytearray()
        total = 0
        status, code = 'failed', None
        drain = None
        waiter = None
        stopper = None
        should_finalize = False

        async def read_output():
            nonlocal total
            last_saved = 0.0
            while chunk := await proc.stdout.read(8192):
                total += len(chunk)
                output.extend(chunk)
                if len(output) > OUTPUT_LIMIT:
                    del output[:-OUTPUT_LIMIT]
                if asyncio.get_running_loop().time() - last_saved > 1:
                    with Session.begin() as db:
                        db.get(Run, run_id).output = output.decode('utf-8', 'replace')
                    last_saved = asyncio.get_running_loop().time()

        async def terminate(grace=0):
            if proc:
                if os.name == 'posix':
                    try:
                        os.killpg(proc.pid, signal.SIGTERM if grace else signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                elif proc.returncode is None:
                    proc.kill()
                if grace:
                    try:
                        await asyncio.wait_for(asyncio.shield(proc.wait()), grace)
                    except asyncio.TimeoutError:
                        pass
                    # Also clean up descendants whose parent exited before them.
                    if os.name == 'posix':
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    elif proc.returncode is None:
                        proc.kill()
                await proc.wait()

        try:
            with Session.begin() as db:
                run = db.get(Run, run_id)
                if run.status != 'queued' or self.stopping:
                    return
                should_finalize = True
                payload = json.loads(run.snapshot)
                if not payload.get('system_key'):
                    data = JobInput(**payload)
                    checked = validate_job(data)  # Recheck files and permissions immediately before execution.
                run.status, run.started_at = 'running', utcnow()
            if payload.get('system_key'):
                from .system_jobs import execute_system
                result = await asyncio.wait_for(execute_system(run_id, payload), timeout=60)
                output.extend(result.encode('utf-8', 'replace'))
                status, code = 'success', 0
                return
            proc = await asyncio.create_subprocess_exec(*json.loads(checked['command']), cwd=checked['path'],
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                start_new_session=(os.name == 'posix'),
                env={k: v for k, v in os.environ.items() if k not in ('DATABASE_URL', 'ADMIN_PASSWORD')})
            self.processes[run_id] = proc
            drain = asyncio.create_task(read_output())
            waiter = asyncio.gather(proc.wait(), drain)
            stopper = asyncio.create_task(self.stop_events[run_id].wait())
            try:
                done, _ = await asyncio.wait([waiter, stopper], timeout=None if data.mode == 'continuous' else data.timeout,
                                             return_when=asyncio.FIRST_COMPLETED)
                if stopper in done:
                    status = 'interrupted' if self.stopping else 'cancelled'
                    with Session.begin() as db:
                        db.get(Run, run_id).status = 'stopping'
                    await terminate(data.stop_grace)
                    code = proc.returncode
                elif waiter in done:
                    await waiter
                    code = proc.returncode
                    status = 'success' if code == 0 else 'failed'
                else:
                    status = 'timeout'
                    output.extend(b'\nExecution exceeded its timeout.\n')
            finally:
                # Kill descendants even if the direct command exited and left background processes.
                await terminate()
        except asyncio.CancelledError:
            status = 'interrupted' if self.stopping else 'cancelled'
            await terminate()
        except Exception as exc:
            output.extend(('\n' + str(exc)).encode('utf-8', 'replace'))
            await terminate()
        finally:
            if stopper:
                stopper.cancel()
                await asyncio.gather(stopper, return_exceptions=True)
            if drain and not drain.done():
                # Processes have been terminated; finish reading their final output.
                try:
                    await asyncio.wait_for(asyncio.shield(drain), 5)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    drain.cancel()
                await asyncio.gather(drain, return_exceptions=True)
            if waiter:
                await asyncio.gather(waiter, return_exceptions=True)
            if total > OUTPUT_LIMIT:
                output = bytearray(b'[Output truncated; showing last 256 KiB]\n') + output[-OUTPUT_LIMIT:]
            with Session.begin() as db:
                run = db.get(Run, run_id)
                if should_finalize and run and run.status in ACTIVE:
                    run.status, run.exit_code, run.finished_at = status, code, utcnow()
                    run.output = output[-(OUTPUT_LIMIT + 100):].decode('utf-8', 'replace')
                    job = db.get(Job, run.job_id)
                    if job and job.mode == 'continuous' and job.enabled:
                        job.next_run = utcnow() + timedelta(seconds=job.restart_delay)
                    if job:
                        if status in ('failed', 'timeout'):
                            executor_event(db, job, 'failure', f'Execution {status}', run.id, code)
                        elif status in ('cancelled', 'interrupted'):
                            executor_event(db, job, 'stop', f'Execution {status}', run.id, code)
                        elif job.mode == 'continuous':
                            executor_event(db, job, 'stop', 'Continuous process exited; restart scheduled', run.id, code)


runner = Runner()
