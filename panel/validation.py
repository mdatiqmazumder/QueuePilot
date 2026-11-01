import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from pydantic import BaseModel, Field, field_validator


class JobInput(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    kind: str = 'script'
    mode: str = 'scheduled'
    restart_delay: int = Field(default=5, ge=1, le=3600)
    stop_grace: int = Field(default=90, ge=1, le=3600)
    path: str = Field(min_length=1, max_length=4096)
    argv: list[str] = Field(min_length=1, max_length=100)
    schedule: str = Field(default='* * * * *', max_length=120)
    timezone: str = Field(default='Asia/Dhaka', max_length=80)
    timeout: int = Field(default=300, ge=1, le=604800)
    enabled: bool = True

    @field_validator('name')
    @classmethod
    def name_not_blank(cls, value):
        if not value.strip():
            raise ValueError('Name cannot be blank')
        return value.strip()

    @field_validator('argv')
    @classmethod
    def valid_argv(cls, value):
        if any('\x00' in arg or len(arg) > 8192 for arg in value) or not value[0].strip():
            raise ValueError('Invalid command arguments')
        return value


def next_time(expression, tz, base=None):
    if len(expression.split()) != 5 or not croniter.is_valid(expression):
        raise ValueError('Use a valid five-field cron: minute hour day month weekday')
    zone = ZoneInfo(tz)
    base = base or datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return croniter(expression, base.astimezone(zone)).get_next(datetime).astimezone(timezone.utc).replace(tzinfo=None)


def validate_job(data: JobInput):
    if data.kind not in ('script', 'laravel'):
        raise ValueError('Unknown executor type')
    if data.mode not in ('scheduled', 'continuous'):
        raise ValueError('Unknown execution mode')
    path = Path(data.path).expanduser()
    if not path.is_absolute() or not path.is_dir():
        raise ValueError('Working directory must be an existing absolute directory')
    path = path.resolve()
    if not os.access(path, os.R_OK | os.X_OK):
        raise ValueError('Working directory is not accessible to the panel service user')
    executable = data.argv[0]
    resolved = shutil.which(executable) if not Path(executable).is_absolute() else executable
    if not resolved or not Path(resolved).is_file() or not os.access(resolved, os.X_OK):
        raise ValueError('Executable was not found or is not executable by the service user')
    if data.kind == 'laravel':
        if not (path / 'artisan').is_file() or not os.access(path / 'artisan', os.R_OK):
            raise ValueError('Laravel artisan file is missing or unreadable in this directory')
        if Path(resolved).stem.lower() not in ('php', 'php8.1', 'php8.2', 'php8.3', 'php8.4', 'php8.5'):
            raise ValueError('Laravel executors must use a PHP executable')
        if len(data.argv) < 3 or data.argv[1] != 'artisan':
            raise ValueError('Laravel command must use: php artisan <command>')
    try:
        upcoming = datetime.now(timezone.utc).replace(tzinfo=None) if data.mode == 'continuous' else next_time(data.schedule, data.timezone)
    except (ValueError, KeyError, ZoneInfoNotFoundError) as exc:
        raise ValueError(f'Invalid schedule or timezone: {exc}') from exc
    return dict(name=data.name, kind=data.kind, mode=data.mode, restart_delay=data.restart_delay, stop_grace=data.stop_grace,
                path=str(path), command=json.dumps([str(resolved), *data.argv[1:]]), schedule=data.schedule,
                timezone=data.timezone, timeout=data.timeout, enabled=data.enabled, next_run=upcoming if data.enabled else None)
