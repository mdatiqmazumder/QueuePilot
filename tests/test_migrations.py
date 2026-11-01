def test_existing_scheduled_jobs_survive_migration(monkeypatch):
    from sqlalchemy import create_engine, inspect, text
    from panel import migrations
    engine=create_engine('sqlite://')
    with engine.begin() as db:
        db.execute(text('CREATE TABLE jobs (id INTEGER PRIMARY KEY, name VARCHAR(160))'))
        db.execute(text("INSERT INTO jobs (id,name) VALUES (1,'Existing job')"))
    monkeypatch.setattr(migrations,'engine',engine)
    migrations.migrate()
    migrations.migrate()
    with engine.connect() as db:
        row=db.execute(text('SELECT name,mode,restart_delay,stop_grace FROM jobs')).one()
        assert tuple(row)==('Existing job','scheduled',5,90)
    engine.dispose()
