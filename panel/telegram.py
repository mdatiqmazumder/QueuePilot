"""Telegram notifications and commands restricted to an explicit chat and user."""
import asyncio
import json
import re
from datetime import timedelta, timezone
from urllib.request import Request, urlopen

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select

from .database import Job, Run, Session, TelegramDelivery, utcnow
from .notifications import cipher, get_value, set_value


class TelegramInput(BaseModel):
    enabled: bool = False
    token: str | None = Field(default=None, max_length=200)
    clear_token: bool = False
    chat_id: str = Field(default='', max_length=40)
    commands_enabled: bool = False
    allowed_user_id: str = Field(default='', max_length=40)
    notify_failures: bool = True
    notify_stops: bool = True
    notify_health: bool = True
    notify_reports: bool = True
    cooldown_seconds: int = Field(default=900, ge=60, le=86400)

    @model_validator(mode='after')
    def check(self):
        self.chat_id = self.chat_id.strip()
        self.allowed_user_id = self.allowed_user_id.strip()
        if self.token and not re.fullmatch(r'\d+:[A-Za-z0-9_-]+', self.token):
            raise ValueError('Invalid bot token format')
        if self.chat_id and not re.fullmatch(r'-?\d+', self.chat_id):
            raise ValueError('Chat ID must be numeric')
        if self.allowed_user_id and not re.fullmatch(r'\d+', self.allowed_user_id):
            raise ValueError('User ID must be numeric')
        if self.enabled and not self.chat_id:
            raise ValueError('Chat ID is required')
        if self.commands_enabled and not self.allowed_user_id:
            raise ValueError('An allowed Telegram user ID is required for commands')
        return self


def settings(db):
    return get_value(db, 'telegram', TelegramInput().model_dump(exclude={'token', 'clear_token'}))


def public_settings(db):
    config = settings(db).copy()
    config['token_set'] = bool(config.pop('token_encrypted', None))
    return config


def save_settings(db, data):
    old = settings(db)
    config = data.model_dump(exclude={'token', 'clear_token'})
    secret = None if data.clear_token else old.get('token_encrypted')
    if data.token:
        secret = cipher(create=True).encrypt(data.token.encode()).decode()
    if config['enabled'] and not secret:
        raise ValueError('Bot token is required')
    if secret:
        config['token_encrypted'] = secret
    set_value(db, 'telegram', config)
    # Discard previously unseen commands whenever destination or authorization changes.
    if any(old.get(k) != config.get(k) for k in ('chat_id', 'allowed_user_id', 'commands_enabled', 'enabled', 'token_encrypted')):
        set_value(db, 'telegram_offset', None)
    for row in db.scalars(select(TelegramDelivery).where(TelegramDelivery.status == 'pending')):
        row.status = 'cancelled'
    return public_settings(db)


def queue_message(db, category, subject, body, event_key=None, cooldown=None):
    config = settings(db)
    flag = {'failure':'notify_failures', 'stop':'notify_stops', 'health':'notify_health', 'report':'notify_reports'}.get(category)
    if not config.get('enabled') or (flag and not config.get(flag, True)):
        return None
    if event_key:
        key = 'tg:' + event_key
        stamp = utcnow().replace(tzinfo=timezone.utc).timestamp()
        last = get_value(db, key)
        if last and stamp - last < (cooldown or config['cooldown_seconds']):
            return None
        set_value(db, key, stamp)
    row = TelegramDelivery(category=category, subject=subject[:255], body=body)
    db.add(row)
    db.flush()
    return row.id


def api(config, method, payload):
    token = cipher().decrypt(config['token_encrypted'].encode()).decode()
    request = Request(f'https://api.telegram.org/bot{token}/{method}',
                      data=json.dumps(payload).encode(), headers={'Content-Type':'application/json'})
    with urlopen(request, timeout=15) as response:
        result = json.load(response)
    if not result.get('ok'):
        raise RuntimeError('Telegram request rejected')
    return result['result']


def send(config, text):
    markup = text.get('reply_markup') if isinstance(text, dict) else None
    text = text['text'] if isinstance(text, dict) else text
    # Keep a single bounded message; full output remains available in the panel.
    text = text if len(text) <= 3800 else text[:3700] + '\n… Open QueuePilot for the full report.'
    api(config, 'sendMessage', {'chat_id':config['chat_id'], 'text':text,
        'reply_markup': markup or {'keyboard': [[{'text':'Status'}, {'text':'Executors'}], [{'text':'Help'}]], 'resize_keyboard':True, 'is_persistent':True}})


def command_buttons(db, message):
    command = message.get('text', '').split('@')[0].strip().lower()
    buttons = []
    if command in ('executors', '/jobs'):
        for job in db.scalars(select(Job).where(Job.system_key.is_(None)).order_by(Job.id).limit(20)):
            row = [{'text':f'▶ #{job.id} {job.name}'[:55], 'callback_data':f'/run {job.id}'}]
            if job.mode == 'continuous':
                row.append({'text':f'■ Stop #{job.id}', 'callback_data':f'/stop {job.id}'})
            buttons.append(row)
    buttons.append([{'text':'Status', 'callback_data':'/status'}, {'text':'Executors', 'callback_data':'/jobs'}, {'text':'Help', 'callback_data':'/help'}])
    return {'inline_keyboard':buttons}


HELP = 'QueuePilot commands\n/status — server summary\n/jobs — executor IDs and states\n/run ID — start a worker or run an executor\n/stop ID — stop a continuous worker\n/help — show commands'


def handle_command(db, config, message):
    if not config.get('commands_enabled') or str(message.get('chat', {}).get('id')) != config['chat_id'] or str(message.get('from', {}).get('id')) != config['allowed_user_id'] or message.get('from', {}).get('is_bot'):
        return None
    text = message.get('text', '').strip().split()
    if not text:
        return None
    command = text[0].split('@')[0].lower()
    command = {'status':'/status', 'executors':'/jobs', 'help':'/help'}.get(command, command)
    if command in ('/start', '/help'):
        return HELP
    if command == '/status':
        jobs = db.scalars(select(Job).where(Job.system_key.is_(None))).all()
        active = db.scalars(select(Run).where(Run.status.in_(['queued', 'running', 'stopping']))).all()
        return f'QueuePilot is online\nExecutors: {len(jobs)}\nEnabled: {sum(j.enabled for j in jobs)}\nActive executions: {len(active)}'
    if command == '/jobs':
        jobs = db.scalars(select(Job).where(Job.system_key.is_(None)).order_by(Job.id).limit(50)).all()
        return '\n'.join(f'#{j.id} · {j.name} · {j.mode} · {"enabled" if j.enabled else "paused"}' for j in jobs) or 'No executors configured.'
    if command not in ('/run', '/stop'):
        return HELP
    if len(text) != 2 or not text[1].isdigit():
        return f'Usage: {command} EXECUTOR_ID'
    job = db.get(Job, int(text[1]))
    if not job or job.system_key:
        return 'Executor not found. Use /jobs for IDs.'
    from .runner import enqueue, stop_job
    if command == '/stop':
        if job.mode != 'continuous':
            return 'Only continuous workers can be stopped with /stop. Manage scheduled executions in the panel.'
        stop_job(db, job)
        return f'Stop requested: #{job.id} {job.name}. Automatic restart disabled.'
    try:
        run = enqueue(db, job, 'telegram')
    except ValueError:
        return 'This executor already has an active execution or cannot be queued. Check the panel.'
    if job.mode == 'continuous':
        job.enabled, job.next_run = True, None
    return f'Queued #{job.id} {job.name} — run #{run.id}'


class TelegramBot:
    def recover(self):
        with Session.begin() as db:
            for row in db.scalars(select(TelegramDelivery).where(TelegramDelivery.status == 'sending')):
                row.status, row.available_at = 'pending', utcnow()

    async def deliver_one(self):
        with Session.begin() as db:
            row = db.scalar(select(TelegramDelivery).where(TelegramDelivery.status == 'pending', TelegramDelivery.available_at <= utcnow()).order_by(TelegramDelivery.id).limit(1))
            if not row:
                return
            config = settings(db)
            if not config.get('enabled'):
                row.status = 'cancelled'
                return
            row.status, row.attempts = 'sending', row.attempts + 1
            row_id, text = row.id, row.subject + '\n\n' + row.body
            if row.category == 'command':
                try:
                    text = json.loads(row.body)
                except ValueError:
                    text = row.body
        try:
            await asyncio.to_thread(send, config, text)
        except Exception:
            with Session.begin() as db:
                row = db.get(TelegramDelivery, row_id)
                row.status = 'failed' if row.attempts >= 3 else 'pending'
                row.last_error = 'Telegram delivery failed. Check token, chat ID and bot permissions.'
                row.available_at = utcnow() + timedelta(seconds=60 * 5 ** (row.attempts - 1))
        else:
            with Session.begin() as db:
                row = db.get(TelegramDelivery, row_id)
                row.status, row.sent_at, row.last_error = 'sent', utcnow(), None

    async def poll(self):
        with Session() as db:
            config, offset = settings(db), get_value(db, 'telegram_offset')
        if not config.get('enabled') or not config.get('commands_enabled'):
            return
        updates = await asyncio.to_thread(api, config, 'getUpdates', {'offset':offset if offset is not None else -1, 'timeout':0, 'limit':20, 'allowed_updates':['message', 'callback_query']})
        callbacks = []
        with Session.begin() as db:
            if settings(db) != config:
                return
            for update in updates:
                # First poll establishes a baseline; never replay old start/stop commands.
                if offset is not None:
                    message = update.get('message', {})
                    callback = update.get('callback_query')
                    if callback:
                        callbacks.append(callback['id'])
                        message = {'chat':callback.get('message', {}).get('chat', {}), 'from':callback.get('from', {}), 'text':callback.get('data', ''), 'date':utcnow().replace(tzinfo=timezone.utc).timestamp()}
                    age = utcnow().replace(tzinfo=timezone.utc).timestamp() - message.get('date', 0)
                    reply = handle_command(db, config, message) if 0 <= age < 120 else None
                    if reply:
                        queue_message(db, 'command', '[QueuePilot] Bot response', json.dumps({'text':reply, 'reply_markup':command_buttons(db, message)}))
                offset = update['update_id'] + 1
            set_value(db, 'telegram_offset', offset if offset is not None else 0)
        for callback_id in callbacks:
            try:
                await asyncio.to_thread(api, config, 'answerCallbackQuery', {'callback_query_id':callback_id})
            except Exception:
                pass

    async def loop(self):
        while True:
            try:
                await self.deliver_one()
                await self.poll()
            except Exception:
                # Never log HTTP exceptions containing the bot token in their URL.
                pass
            await asyncio.sleep(3)


bot = TelegramBot()
