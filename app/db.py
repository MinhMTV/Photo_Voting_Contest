from datetime import datetime
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

def migrate_votes_table_rebuild() -> None:
    """
    Rebuilds votes table to enforce:
      - UNIQUE(image_id, voter_session_id, contest_year)
      - generic vote columns exist in schema
    Safe to run multiple times.
    """
    db = get_db()

    cols = db.execute("PRAGMA table_info(votes)").fetchall()
    if not cols:
        return  # votes table doesn't exist yet

    col_names = {c[1] for c in cols}

    # If the desired UNIQUE already exists, we can skip rebuild (simple heuristic):
    # SQLite doesn't expose constraint columns easily; we rebuild only if contest_year unique isn't enforced.
    # We'll rebuild if table was created without vote_option_key column OR if contest_year column missing.
    needs_rebuild = ('vote_option_key' not in col_names) or ('contest_year' not in col_names)

    if not needs_rebuild:
        # still ensure columns exist (no harm)
        _ensure_column(db, 'votes', 'vote_option_key', 'vote_option_key TEXT')
        _ensure_column(db, 'votes', 'vote_value', 'vote_value INTEGER DEFAULT 1')
        _ensure_column(db, 'votes', 'vote_label', 'vote_label TEXT')
        return
    db.commit()
    db.execute("BEGIN")

    # 1) Create new table
    db.execute("""
        CREATE TABLE IF NOT EXISTS votes_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            image_id INTEGER,
            voter_session_id TEXT,
            contest_year INTEGER DEFAULT 2025,

            vote_option_key TEXT,
            vote_value INTEGER DEFAULT 1,
            vote_label TEXT,

            chip_label TEXT,
            chip_value INTEGER DEFAULT 1,

            UNIQUE(image_id, voter_session_id, contest_year)
        )
    """)

    # 2) Copy data from old votes into votes_new (only columns that exist)
    # We try to map best-effort:
    # - contest_year defaults to 2025 if missing
    # - vote_* default NULL/1
    select_parts = []
    insert_cols = []

    # id is kept if present
    if 'id' in col_names:
        insert_cols.append('id')
        select_parts.append('id')

    for c in ['image_id', 'voter_session_id']:
        if c in col_names:
            insert_cols.append(c)
            select_parts.append(c)
        else:
            insert_cols.append(c)
            select_parts.append('NULL')

    # contest_year
    insert_cols.append('contest_year')
    select_parts.append('contest_year' if 'contest_year' in col_names else '2025')

    # vote_* (might not exist yet)
    for c, fallback in [('vote_option_key', 'NULL'), ('vote_value', '1'), ('vote_label', 'NULL')]:
        insert_cols.append(c)
        select_parts.append(c if c in col_names else fallback)

    # chip_* legacy (might exist)
    for c, fallback in [('chip_label', 'NULL'), ('chip_value', '1')]:
        insert_cols.append(c)
        select_parts.append(c if c in col_names else fallback)

    db.execute(f"""
        INSERT OR IGNORE INTO votes_new ({", ".join(insert_cols)})
        SELECT {", ".join(select_parts)} FROM votes
    """)

    # 3) Drop old table and rename new
    db.execute("DROP TABLE votes")
    db.execute("ALTER TABLE votes_new RENAME TO votes")

    db.execute("COMMIT")


def migrate_vote_generic_columns(default_legacy_year: int = 2025) -> None:
    """
    Adds generic vote columns:
      - votes.vote_option_key
      - votes.vote_value
      - votes.vote_label
    Backfills them from existing chip_* columns and opt_key.
    Safe to run multiple times.
    """
    db = get_db()

    # Add new generic columns if missing
    _ensure_column(db, 'votes', 'vote_option_key', 'vote_option_key TEXT')
    _ensure_column(db, 'votes', 'vote_value', 'vote_value INTEGER DEFAULT 1')
    _ensure_column(db, 'votes', 'vote_label', 'vote_label TEXT')

    # legacy chip columns (damit SQL nicht crasht)
    _ensure_column(db, 'votes', 'chip_label', 'chip_label TEXT')

    db.execute("""
        UPDATE votes
        SET vote_option_key =
        CASE
            WHEN vote_option_key IS NOT NULL AND TRIM(vote_option_key) != '' THEN vote_option_key

            WHEN LOWER(COALESCE(chip_label, '')) IN ('all-in','allin','all_in') THEN 'all_in'
            WHEN TRIM(COALESCE(chip_label, '')) = '5'   THEN 'chip_5'
            WHEN TRIM(COALESCE(chip_label, '')) = '25'  THEN 'chip_25'
            WHEN TRIM(COALESCE(chip_label, '')) = '50'  THEN 'chip_50'
            WHEN TRIM(COALESCE(chip_label, '')) = '100' THEN 'chip_100'

            WHEN LOWER(COALESCE(chip_label, '')) IN ('vote','heart') THEN 'heart'
            WHEN chip_label IS NULL OR TRIM(chip_label) = '' THEN 'heart'
            ELSE 'heart'
        END
        WHERE vote_option_key IS NULL OR TRIM(vote_option_key) = ''
    """)


    # 2) vote_label: use chip_label if present, else derive from vote_option_key
    db.execute("""
        UPDATE votes
        SET vote_label =
          CASE
            WHEN vote_label IS NOT NULL AND TRIM(vote_label) != '' THEN vote_label
            WHEN chip_label IS NOT NULL AND TRIM(chip_label) != '' THEN chip_label
            WHEN vote_option_key = 'all_in' THEN 'All-in'
            WHEN vote_option_key = 'heart' THEN 'Vote'
            ELSE vote_option_key
          END
        WHERE vote_label IS NULL OR TRIM(vote_label) = ''
    """)

    # 3) vote_value: prefer existing chip_value, else fallback 1
    _ensure_column(db, 'votes', 'chip_value', 'chip_value INTEGER DEFAULT 1')
    db.execute("""
        UPDATE votes
        SET vote_value =
          CASE
            WHEN vote_value IS NOT NULL AND vote_value > 0 THEN vote_value
            WHEN chip_value IS NOT NULL AND chip_value > 0 THEN chip_value
            ELSE 1
          END
        WHERE vote_value IS NULL OR vote_value = 0
    """)

    db.commit()




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

            vote_option_key TEXT,
            vote_value INTEGER DEFAULT 1,
            vote_label TEXT,

            UNIQUE(image_id, voter_session_id, contest_year)
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
        CREATE TABLE IF NOT EXISTS contest_year_settings (
            contest_year INTEGER PRIMARY KEY,
            vote_mode TEXT DEFAULT 'toggle',          -- 'toggle' (ein Button), 'unique_options' (Chips), später erweiterbar
            max_actions INTEGER DEFAULT 3,            -- wie viele Aktionen/Chips pro User/Jahr
            unit_name TEXT DEFAULT 'Stimme',          -- z.B. Stimme, Chip, Diamant
            unit_icon TEXT DEFAULT '❤️',              -- Emoji oder leer
            theme_id TEXT DEFAULT 'default',          -- später für Designs
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS vote_options (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contest_year INTEGER NOT NULL,
            opt_key TEXT NOT NULL,                    -- z.B. 'heart', 'chip_5', 'all_in'
            label TEXT NOT NULL,                      -- z.B. '5', 'All-in'
            icon TEXT,                                -- Emoji oder Dateiname
            value INTEGER DEFAULT 1,                  -- Punktewert
            unique_per_user INTEGER DEFAULT 0,         -- 1 = pro User/Jahr nur einmal nutzbar (wie Chip 25)
            exclusive_group TEXT,                      -- z.B. 'allin' => blockiert andere Gruppenregeln (Logik kommt später)
            is_special INTEGER DEFAULT 0,              -- nur fürs UI
            active INTEGER DEFAULT 1,
            sort_order INTEGER DEFAULT 0,
            created_at TEXT,
            UNIQUE(contest_year, opt_key)
        );
           

        CREATE TABLE IF NOT EXISTS duel_votes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            image_id INTEGER NOT NULL,
            voter_session_id TEXT NOT NULL,
            contest_year INTEGER DEFAULT 2025,
            created_at TEXT
        );
    ''')

    # ---- Seed defaults (safe upserts) ----
    now = datetime.now().isoformat()

    # Settings per year (adjust years as you like)
    db.execute('''
        INSERT INTO contest_year_settings (contest_year, vote_mode, max_actions, unit_name, unit_icon, theme_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(contest_year) DO NOTHING
    ''', (2025, 'toggle', 3, 'Stimme', '❤️', 'default', now))

    db.execute('''
        INSERT INTO contest_year_settings (contest_year, vote_mode, max_actions, unit_name, unit_icon, theme_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(contest_year) DO NOTHING
    ''', (2026, 'unique_options', 4, 'Chip', '🪙', 'casino', now))

    # Vote options 2025 (classic heart vote)
    db.execute('''
        INSERT INTO vote_options (contest_year, opt_key, label, icon, value, unique_per_user, exclusive_group, is_special, active, sort_order, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(contest_year, opt_key) DO NOTHING
    ''', (2025, 'heart', 'Vote', '❤️', 1, 0, None, 0, 1, 10, now))

    # Vote options 2026 (chips)
    defaults_2026 = [
        ('chip_5',   '5',    '🪙', 5,   1, None, 0, 10),
        ('chip_25',  '25',   '🪙', 25,  1, None, 0, 20),
        ('chip_50',  '50',   '🪙', 50,  1, None, 0, 30),
        ('chip_100', '100',  '🪙', 100, 1, None, 0, 40),
        ('all_in',   'All-in','🎰', 180, 1, 'allin', 1, 99),
    ]
    for opt_key, label, icon, value, unique_per_user, exclusive_group, is_special, sort_order in defaults_2026:
        db.execute('''
            INSERT INTO vote_options (contest_year, opt_key, label, icon, value, unique_per_user, exclusive_group, is_special, active, sort_order, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(contest_year, opt_key) DO NOTHING
        ''', (2026, opt_key, label, icon, value, unique_per_user, exclusive_group, is_special, 1, sort_order, now))


    # Lightweight migration for existing DBs
    _ensure_column(db, 'images', 'contest_year', 'contest_year INTEGER DEFAULT 2025')
    _ensure_column(db, 'votes', 'contest_year', 'contest_year INTEGER DEFAULT 2025')

    # Backfill legacy rows (existing previous contest is treated as 2025)
    db.execute('UPDATE images SET contest_year = 2025 WHERE contest_year IS NULL OR contest_year = 0')
    db.execute('UPDATE votes SET contest_year = 2025 WHERE contest_year IS NULL OR contest_year = 0')

    # IMPORTANT: rebuild schema/unique first, then backfill vote fields
    migrate_votes_table_rebuild()
    migrate_vote_generic_columns(default_legacy_year=2025)

    db.commit()





@click.command('init-db')
@with_appcontext
def init_db_command():
    init_db()
    click.echo('✔ Datenbank initialisiert.')

def init_app(app):
    app.teardown_appcontext(close_db)
    app.cli.add_command(init_db_command)
