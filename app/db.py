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

def init_db():
    db = get_db()
    db.executescript('''
        CREATE TABLE IF NOT EXISTS images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            description TEXT,
            uploader TEXT,
            uploaded_at TEXT,
            visible INTEGER DEFAULT 0 
        );

        CREATE TABLE IF NOT EXISTS votes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            image_id INTEGER,
            voter_session_id TEXT,  -- Neue Spalte für die Session-ID
            UNIQUE(image_id, voter_session_id)
        );
    ''')

@click.command('init-db')
@with_appcontext
def init_db_command():
    init_db()
    click.echo('✔ Datenbank initialisiert.')

def init_app(app):
    app.teardown_appcontext(close_db)
    app.cli.add_command(init_db_command)