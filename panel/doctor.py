import os
import shutil
from sqlalchemy import text
from .database import DATA_DIR, Session


def main():
    print('QueuePilot diagnostics')
    try:
        with Session() as db:
            db.execute(text('SELECT 1'))
        print('Database: connected')
    except Exception:
        raise SystemExit('Database: connection failed (check .env and database grants)') from None
    print(f'Panel bind: {os.getenv("PANEL_HOST", "127.0.0.1")}:{os.getenv("PANEL_PORT", "8090")}')
    print(f'Execution slots: {os.getenv("MAX_CONCURRENT_RUNS", "8")}')
    print(f'Data directory: {DATA_DIR}')
    print(f'PHP: {shutil.which("php") or "not installed — required only for Laravel"}')


if __name__ == '__main__':
    main()
