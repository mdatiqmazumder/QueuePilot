"""Interactive configuration; database credentials never enter shell arguments."""
import getpass
import os
import re
from pathlib import Path

from argon2 import PasswordHasher
from sqlalchemy import create_engine, select
from sqlalchemy.engine import URL


def ask(label, default=None):
    value = input(f'{label}' + (f' [{default}]' if default is not None else '') + ': ').strip()
    return value or default or ''


def port(label, default):
    value = int(ask(label, str(default)))
    if not 1 <= value <= 65535:
        raise ValueError('Port must be between 1 and 65535')
    return value


def main():
    print('\nQueuePilot configuration — existing MySQL database required.\n')
    if Path('.env').exists():
        raise SystemExit('Configuration already exists. Edit .env to change connection settings; use panel.admin to reset passwords.')
    public_host = ask('Server IP / hostname', '127.0.0.1')
    if not re.fullmatch(r'[A-Za-z0-9.:-]+', public_host):
        raise ValueError('Invalid server hostname')
    host = ask('Panel listen address', '0.0.0.0')
    panel_port = port('Panel port', 8090)
    db_host = ask('MySQL server IP / hostname', '127.0.0.1')
    db_port = port('MySQL port', 3306)
    db_user = ask('MySQL username', 'queuepilot')
    db_pass = getpass.getpass('MySQL password: ')
    db_name = ask('MySQL database name', 'queuepilot')
    admin_name = ask('Panel admin username', 'admin')
    if not admin_name or len(admin_name) > 100:
        raise ValueError('Admin username must be 1–100 characters')
    admin_pass = getpass.getpass('Panel admin password (minimum 12 characters): ')
    if len(admin_pass) < 12 or admin_pass != getpass.getpass('Confirm admin password: '):
        raise ValueError('Passwords must match and contain at least 12 characters')
    concurrent = int(ask('Maximum simultaneous executions', '8'))
    if concurrent < 1:
        raise ValueError('Concurrency must be positive')
    secure = ask('Use HTTPS-only cookies? Set yes behind an HTTPS reverse proxy', 'no').lower() in ('yes', 'y')
    url = URL.create('mysql+pymysql', username=db_user, password=db_pass, host=db_host, port=db_port, database=db_name, query={'charset': 'utf8mb4'})
    rendered = url.render_as_string(hide_password=False)
    connection = create_engine(url, pool_pre_ping=True)
    with connection.connect() as conn:
        conn.exec_driver_sql('SELECT 1')
    connection.dispose()
    # Set environment before importing models, which intentionally require configuration.
    os.environ['DATABASE_URL'] = rendered
    from .database import Base, Session, User, engine
    Base.metadata.create_all(engine)
    with Session.begin() as db:
        if db.scalar(select(User.id)):
            raise ValueError('This database already contains users. Use a dedicated empty database for a new installation.')
        db.add(User(username=admin_name, password_hash=PasswordHasher().hash(admin_pass)))
    values = dict(DATABASE_URL=rendered, PANEL_HOST=host, PANEL_PORT=str(panel_port), COOKIE_SECURE=str(secure).lower(),
                  MAX_CONCURRENT_RUNS=str(concurrent), DATA_DIR=str(Path('data').resolve()))
    def quote(value):
        if '\n' in value or '\r' in value:
            raise ValueError('Configuration values cannot contain newlines')
        return "'" + value.replace('\\', '\\\\').replace("'", "\\'") + "'"
    with open('.env', 'x', encoding='utf-8') as file:
        file.write(''.join(f'{key}={quote(value)}\n' for key, value in values.items()))
    os.chmod('.env', 0o640)
    print(f'\nConfigured. Panel address: {"https" if secure else "http"}://{public_host}:{panel_port}\n')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Driver exceptions may contain connection parameters; do not print credentials.
        if isinstance(exc, ValueError):
            raise SystemExit(str(exc))
        raise SystemExit('Setup failed. Check the database connection, privileges, and writable installation directory.') from None
