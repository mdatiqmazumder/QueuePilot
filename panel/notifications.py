"""Encrypted SMTP configuration, durable outbox, bounded retries and alert cooldown."""
import asyncio
import json
import logging
import os
import re
import smtplib
import ssl
from datetime import timedelta, timezone
from email.message import EmailMessage

from cryptography.fernet import Fernet
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select

from .database import DATA_DIR, MailDelivery, Session, Setting, utcnow

log = logging.getLogger(__name__)
EMAIL = re.compile(r'^[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+$')


class SMTPInput(BaseModel):
    enabled: bool = False
    host: str = Field(default='', max_length=255)
    port: int = Field(default=587, ge=1, le=65535)
    security: str = 'starttls'
    username: str = Field(default='', max_length=255)
    password: str | None = Field(default=None, max_length=4096)
    clear_password: bool = False
    sender: str = Field(default='', max_length=255)
    recipients: list[str] = Field(default_factory=list, max_length=50)
    notify_failures: bool = True
    notify_stops: bool = True
    notify_health: bool = True
    cooldown_seconds: int = Field(default=900, ge=60, le=86400)

    @field_validator('host', 'username', 'sender')
    @classmethod
    def no_headers(cls, value):
        if '\r' in value or '\n' in value or '\x00' in value:
            raise ValueError('Invalid configuration characters')
        return value.strip()

    @field_validator('sender')
    @classmethod
    def valid_sender(cls, value):
        if value and not EMAIL.fullmatch(value):
            raise ValueError('Enter a valid sender email address')
        return value

    @field_validator('recipients')
    @classmethod
    def valid_recipients(cls, values):
        if any(not EMAIL.fullmatch(value) or len(value)>255 for value in values):
            raise ValueError('Enter valid recipient email addresses')
        return list(dict.fromkeys(values))

    @model_validator(mode='after')
    def complete(self):
        if self.security not in ('starttls', 'ssl', 'none'):
            raise ValueError('Choose STARTTLS, SSL/TLS or no encryption')
        if self.enabled and (not self.host or not self.sender or not self.recipients):
            raise ValueError('SMTP host, sender and recipients are required when enabled')
        return self


def get_value(db, key, default=None):
    row = db.get(Setting, key)
    return json.loads(row.value) if row else default


def set_value(db, key, value):
    row = db.get(Setting, key)
    if row:
        row.value = json.dumps(value)
    else:
        db.add(Setting(key=key, value=json.dumps(value)))


def settings(db):
    return get_value(db, 'smtp', SMTPInput().model_dump(exclude={'password', 'clear_password'}))


def public_settings(db):
    value = settings(db).copy()
    value['password_set'] = bool(value.pop('password_encrypted', None))
    return value


def cipher(create=False):
    path = DATA_DIR / 'smtp.key'
    if create and not path.exists():
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, 'wb') as file:
                file.write(Fernet.generate_key())
    return Fernet(path.read_bytes())


def save_settings(db, data):
    previous = settings(db)
    values = data.model_dump(exclude={'password', 'clear_password'})
    secret = previous.get('password_encrypted')
    if data.clear_password:
        secret = None
    if data.password:
        secret = cipher(create=True).encrypt(data.password.encode()).decode()
    if data.enabled and data.username and not secret:
        raise ValueError('SMTP authentication password is required for this username')
    if secret:
        values['password_encrypted'] = secret
    set_value(db, 'smtp', values)
    # Do not deliver old alerts to recipients after a deliberate configuration change.
    for mail in db.scalars(select(MailDelivery).where(MailDelivery.status == 'pending')):
        mail.status, mail.last_error = 'cancelled', 'SMTP configuration changed'
    db.flush()
    return public_settings(db)


def queue_mail(db, category, subject, body, event_key=None, cooldown=None):
    config = settings(db)
    if not config.get('enabled'):
        return None
    flag = {'failure':'notify_failures', 'stop':'notify_stops', 'health':'notify_health'}.get(category)
    if flag and not config.get(flag, True):
        return None
    if event_key:
        key = 'mail:' + event_key
        last = get_value(db, key)
        now = utcnow()
        stamp = now.replace(tzinfo=timezone.utc).timestamp()
        if last and stamp - last < (cooldown or config['cooldown_seconds']):
            return None
        set_value(db, key, stamp)
    mail = MailDelivery(category=category, subject=subject.replace('\r',' ').replace('\n',' ')[:255], body=body)
    db.add(mail)
    db.flush()
    return mail.id


def executor_event(db, job, category, reason, run_id=None, exit_code=None):
    return queue_mail(db, category, f'[QueuePilot] {category.upper()}: {job.name}',
        f'Executor: {job.name}\nExecutor ID: {job.id}\nMode: {job.mode}\nEvent: {reason}\n'
        f'Run ID: {run_id or "—"}\nExit code: {exit_code if exit_code is not None else "—"}\n'
        f'Time (UTC): {utcnow().isoformat()}\n'
        f'Auto-restart: {"enabled" if job.mode == "continuous" and job.enabled else "disabled"}\n'
        'Open the panel for execution details and logs.\n', event_key=f'executor:{job.id}:{category}')


def send_smtp(config, subject, body, message_id):
    message = EmailMessage()
    message['Subject'], message['From'] = subject, config['sender']
    message['To'] = ', '.join(config['recipients'])
    message['Message-ID'] = f'<queuepilot-{message_id}@{config["sender"].split("@")[-1]}>'
    message.set_content(body)
    context = ssl.create_default_context()
    client_type = smtplib.SMTP_SSL if config['security'] == 'ssl' else smtplib.SMTP
    kwargs = {'context':context} if config['security'] == 'ssl' else {}
    with client_type(config['host'], config['port'], timeout=10, **kwargs) as smtp:
        if config['security'] == 'starttls':
            smtp.starttls(context=context)
        if config.get('username'):
            password = cipher().decrypt(config['password_encrypted'].encode()).decode()
            smtp.login(config['username'], password)
        refused = smtp.send_message(message)
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)


class Mailer:
    def recover(self):
        with Session.begin() as db:
            for mail in db.scalars(select(MailDelivery).where(MailDelivery.status == 'sending')):
                mail.status, mail.available_at = 'pending', utcnow()

    async def deliver_one(self):
        with Session.begin() as db:
            mail = db.scalar(select(MailDelivery).where(MailDelivery.status == 'pending', MailDelivery.available_at <= utcnow()).order_by(MailDelivery.id).limit(1))
            if not mail:
                return False
            config = settings(db)
            if not config.get('enabled'):
                mail.status, mail.last_error = 'cancelled', 'SMTP disabled'
                return True
            mail.status, mail.attempts = 'sending', mail.attempts + 1
            mail_id, subject, body = mail.id, mail.subject, mail.body
        try:
            await asyncio.to_thread(send_smtp, config, subject, body, mail_id)
        except Exception as exc:
            # SMTP response bodies can contain credentials or other private provider data.
            error = type(exc).__name__ + (f' (SMTP {exc.smtp_code})' if isinstance(exc, smtplib.SMTPResponseException) else '')
            with Session.begin() as db:
                mail = db.get(MailDelivery, mail_id)
                mail.status = 'failed' if mail.attempts >= 3 else 'pending'
                mail.available_at = utcnow() + timedelta(seconds=60 * (5 ** (mail.attempts - 1)))
                mail.last_error = error[:255]
        else:
            with Session.begin() as db:
                mail = db.get(MailDelivery, mail_id)
                mail.status, mail.sent_at, mail.last_error = 'sent', utcnow(), None
        return True

    async def loop(self):
        while True:
            try:
                await self.deliver_one()
            except Exception:
                log.exception('Email outbox could not be processed')
            await asyncio.sleep(1)


mailer = Mailer()
