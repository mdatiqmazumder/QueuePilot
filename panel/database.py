import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.dialects.mysql import MEDIUMTEXT

load_dotenv()
DATA_DIR = Path(os.getenv('DATA_DIR', './data')).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
url = os.getenv('DATABASE_URL')
if not url:
    raise RuntimeError('DATABASE_URL missing. Run the installer first.')
engine = create_engine(url, pool_pre_ping=True, **({'connect_args': {'check_same_thread': False, 'timeout': 30}} if url.startswith('sqlite') else {}))
if engine.dialect.name == 'sqlite':
    @event.listens_for(engine, 'connect')
    def sqlite_options(connection, record):
        cursor = connection.cursor()
        cursor.execute('PRAGMA busy_timeout=30000')
        cursor.execute('PRAGMA journal_mode=WAL')
        cursor.close()
Session = sessionmaker(engine, expire_on_commit=False)


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = 'users'
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))


class LoginSession(Base):
    __tablename__ = 'sessions'
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    csrf: Mapped[str] = mapped_column(String(64))
    expires: Mapped[datetime] = mapped_column(DateTime)


class Job(Base):
    __tablename__ = 'jobs'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(30))
    mode: Mapped[str] = mapped_column(String(20), default='scheduled', server_default='scheduled')
    restart_delay: Mapped[int] = mapped_column(Integer, default=5, server_default='5')
    stop_grace: Mapped[int] = mapped_column(Integer, default=90, server_default='90')
    system_key: Mapped[str | None] = mapped_column(String(40), nullable=True, unique=True)
    options: Mapped[str] = mapped_column(String(12000), default='{}', server_default='{}')
    path: Mapped[str] = mapped_column(Text)
    command: Mapped[str] = mapped_column(Text)
    schedule: Mapped[str] = mapped_column(String(120))
    timezone: Mapped[str] = mapped_column(String(80))
    timeout: Mapped[int] = mapped_column(Integer, default=300)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    next_run: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Run(Base):
    __tablename__ = 'runs'
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(Integer, index=True)
    job_name: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(30), index=True)
    trigger: Mapped[str] = mapped_column(String(20))
    snapshot: Mapped[str] = mapped_column(Text().with_variant(MEDIUMTEXT(), 'mysql'))
    queued_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output: Mapped[str] = mapped_column(Text().with_variant(MEDIUMTEXT(), 'mysql'), default='')


class Setting(Base):
    __tablename__ = 'panel_settings'
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text().with_variant(MEDIUMTEXT(), 'mysql'))


class MailDelivery(Base):
    __tablename__ = 'mail_deliveries'
    id: Mapped[int] = mapped_column(primary_key=True)
    category: Mapped[str] = mapped_column(String(30))
    subject: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text().with_variant(MEDIUMTEXT(), 'mysql'))
    status: Mapped[str] = mapped_column(String(20), default='pending', index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(255), nullable=True)


class TelegramDelivery(Base):
    __tablename__ = 'telegram_deliveries'
    id: Mapped[int] = mapped_column(primary_key=True)
    category: Mapped[str] = mapped_column(String(30))
    subject: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text().with_variant(MEDIUMTEXT(), 'mysql'))
    status: Mapped[str] = mapped_column(String(20), default='pending', index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(255), nullable=True)
