from sqlalchemy import inspect, text

from .database import Base, engine


def migrate():
    """Additive migration for existing v1 installations; existing jobs stay scheduled."""
    Base.metadata.create_all(engine)
    columns = {column['name'] for column in inspect(engine).get_columns('jobs')}
    additions = {
        'mode': "VARCHAR(20) NOT NULL DEFAULT 'scheduled'",
        'restart_delay': 'INTEGER NOT NULL DEFAULT 5',
        'stop_grace': 'INTEGER NOT NULL DEFAULT 90',
        'system_key': 'VARCHAR(40) NULL',
        'options': "VARCHAR(12000) NOT NULL DEFAULT '{}'",
    }
    with engine.begin() as connection:
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(text(f'ALTER TABLE jobs ADD COLUMN {name} {definition}'))
    indexes = inspect(engine).get_indexes('jobs') + inspect(engine).get_unique_constraints('jobs')
    if not any(index.get('unique', True) and index.get('column_names') == ['system_key'] for index in indexes):
        with engine.begin() as connection:
            connection.execute(text('CREATE UNIQUE INDEX ux_jobs_system_key ON jobs (system_key)'))
