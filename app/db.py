import os
import shutil
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

        CREATE TABLE IF NOT EXISTS stickers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contest_year INTEGER NOT NULL,
            filename TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1,
            created_at TEXT,
            UNIQUE(contest_year, filename)
        );

        CREATE TABLE IF NOT EXISTS reactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            image_id INTEGER NOT NULL,
            voter_session_id TEXT NOT NULL,
            reaction_type TEXT NOT NULL,
            contest_year INTEGER DEFAULT 2025,
            created_at TEXT,
            UNIQUE(image_id, voter_session_id, reaction_type, contest_year)
        );

        CREATE TABLE IF NOT EXISTS duel_votes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            image_id INTEGER NOT NULL,
            voter_session_id TEXT NOT NULL,
            contest_year INTEGER DEFAULT 2025,
            created_at TEXT
        );
    ''')

    # Lightweight migration for existing DBs
    _ensure_column(db, 'images', 'contest_year', 'contest_year INTEGER DEFAULT 2025')
    _ensure_column(db, 'votes', 'contest_year', 'contest_year INTEGER DEFAULT 2025')
    _ensure_column(db, 'votes', 'chip_value', 'chip_value INTEGER DEFAULT 1')
    _ensure_column(db, 'votes', 'chip_label', 'chip_label TEXT DEFAULT "vote"')

    # Backfill legacy rows (existing previous contest is treated as 2025)
    db.execute('UPDATE images SET contest_year = 2025 WHERE contest_year IS NULL OR contest_year = 0')
    db.execute('UPDATE votes SET contest_year = 2025 WHERE contest_year IS NULL OR contest_year = 0')

    db.commit()


def migrate_uploads_to_year_dirs(default_legacy_year: int = 2025):
    """Move legacy files from static/uploads to static/uploads_<year> if needed."""
    db = get_db()
    root = current_app.root_path
    legacy_uploads = os.path.join(root, 'static', 'uploads')
    if not os.path.isdir(legacy_uploads):
        return

    rows = db.execute('SELECT filename, COALESCE(contest_year, ?) as contest_year FROM images', (default_legacy_year,)).fetchall()
    for row in rows:
        filename = row['filename']
        year = int(row['contest_year'] or default_legacy_year)
        target_dir = os.path.join(root, f'static/uploads_{year}')
        os.makedirs(target_dir, exist_ok=True)
        src = os.path.join(legacy_uploads, filename)
        dst = os.path.join(target_dir, filename)
        if os.path.exists(src) and not os.path.exists(dst):
            shutil.move(src, dst)

def migrate_null_years(default_legacy_year: int = 2025) -> None:
    db = get_db()

    # Nur laufen lassen, wenn überhaupt NULLs existieren
    null_images = db.execute('SELECT COUNT(*) FROM images WHERE contest_year IS NULL').fetchone()[0]
    null_votes = db.execute('SELECT COUNT(*) FROM votes WHERE contest_year IS NULL').fetchone()[0]
    null_reactions = db.execute('SELECT COUNT(*) FROM reactions WHERE contest_year IS NULL').fetchone()[0]
    null_duel = db.execute('SELECT COUNT(*) FROM duel_votes WHERE contest_year IS NULL').fetchone()[0]

    if (null_images + null_votes + null_reactions + null_duel) == 0:
        return

    db.execute('UPDATE images SET contest_year = ? WHERE contest_year IS NULL', (default_legacy_year,))
    db.execute('UPDATE votes SET contest_year = ? WHERE contest_year IS NULL', (default_legacy_year,))
    db.execute('UPDATE reactions SET contest_year = ? WHERE contest_year IS NULL', (default_legacy_year,))
    db.execute('UPDATE duel_votes SET contest_year = ? WHERE contest_year IS NULL', (default_legacy_year,))
    db.commit()


@click.command('init-db')
@with_appcontext
def init_db_command():
    init_db()
    click.echo('✔ Datenbank initialisiert.')

def init_app(app):
    app.teardown_appcontext(close_db)
    app.cli.add_command(init_db_command)
