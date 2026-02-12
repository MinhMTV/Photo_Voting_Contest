import sqlite3
import click
from flask import current_app, g
from flask.cli import with_appcontext

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(current_app.config['DATABASE'])
        g.db.row_factory = sqlite3.Row
    return g.db

def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()

def _ensure_column(db, table_name: str, column_name: str, ddl: str):
    cols = db.execute(f"PRAGMA table_info({table_name})").fetchall()
    col_names = {c[1] for c in cols}
    if column_name not in col_names:
        db.execute(f"ALTER TABLE {table_name} ADD COLUMN {ddl}")

def init_db():
    db = get_db()
    db.executescript('''
        CREATE TABLE IF NOT EXISTS images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            description TEXT,
            uploader TEXT,
            uploaded_at TEXT,
            visible INTEGER DEFAULT 0,
            contest_year INTEGER DEFAULT 2025
        );

        CREATE TABLE IF NOT EXISTS votes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            image_id INTEGER,
            voter_session_id TEXT,
            contest_year INTEGER DEFAULT 2025,
            UNIQUE(image_id, voter_session_id)
        );
    ''')

    # Lightweight migration for existing DBs
    _ensure_column(db, 'images', 'contest_year', 'contest_year INTEGER DEFAULT 2025')
    _ensure_column(db, 'votes', 'contest_year', 'contest_year INTEGER DEFAULT 2025')

    db.commit()

@click.command('init-db')
@with_appcontext
def init_db_command():
    init_db()
    click.echo('✔ Datenbank initialisiert.')

def init_app(app):
    app.teardown_appcontext(close_db)
    app.cli.add_command(init_db_command)
