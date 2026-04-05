from datetime import datetime
import os
import re
import shutil
import sqlite3
import click
from flask import current_app, g
from flask.cli import with_appcontext


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
    return g.db


def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _table_exists(db, table_name: str) -> bool:
    row = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return bool(row)


def _ensure_column(db, table_name: str, column_name: str, ddl: str):
    if not _table_exists(db, table_name):
        return
    cols = db.execute(f"PRAGMA table_info({table_name})").fetchall()
    col_names = {c[1] for c in cols}
    if column_name not in col_names:
        db.execute(f"ALTER TABLE {table_name} ADD COLUMN {ddl}")


def _slugify_contest_text(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")


def _build_contest_slug(title: str, start_at: str, contest_id: int) -> str:
    raw_title = (title or "").strip()
    raw_start = (start_at or "").strip()
    date_part = ""
    match = re.match(r"^\s*(\d{4}-\d{2}-\d{2})", raw_start)
    if match:
        date_part = match.group(1)
    elif raw_start:
        year_match = re.match(r"^\s*(\d{4})", raw_start)
        if year_match:
            date_part = year_match.group(1)
    base = _slugify_contest_text("-".join(part for part in (date_part, raw_title) if part))
    return base or f"contest-{contest_id}"


def migrate_contest_slugs(db) -> None:
    if not _table_exists(db, "contests"):
        return

    rows = db.execute("SELECT id, slug, title, start_at FROM contests ORDER BY id ASC").fetchall()
    if not rows:
        return

    used_slugs = set()
    changed = False
    for row in rows:
        contest_id = int(row["id"])
        current_slug = str(row["slug"] or "").strip()
        needs_migration = (not current_slug) or bool(re.fullmatch(r"contest-\d+", current_slug))
        target_slug = current_slug if not needs_migration else _build_contest_slug(str(row["title"] or ""), str(row["start_at"] or ""), contest_id)
        if not target_slug:
            target_slug = f"contest-{contest_id}"

        unique_slug = target_slug
        suffix = 2
        while unique_slug in used_slugs:
            unique_slug = f"{target_slug}-{suffix}"
            suffix += 1
        used_slugs.add(unique_slug)

        if unique_slug != current_slug:
            db.execute("UPDATE contests SET slug = ? WHERE id = ?", (unique_slug, contest_id))
            changed = True

    if changed:
        db.commit()


def migrate_contest_ids_to_internal_sequence(db) -> None:
    if not _table_exists(db, "contests"):
        return

    rows = db.execute("SELECT id FROM contests ORDER BY id ASC").fetchall()
    if not rows:
        return

    old_ids = [int(row["id"]) for row in rows]
    target_ids = list(range(1, len(old_ids) + 1))
    if old_ids == target_ids:
        return

    temp_map = {old_id: 100000 + index for index, old_id in enumerate(old_ids, start=1)}
    final_map = dict(zip(old_ids, target_ids))

    def _update_all_ids(id_map: dict[int, int]) -> None:
        for old_id, new_id in id_map.items():
            db.execute("UPDATE contests SET id = ? WHERE id = ?", (new_id, old_id))
            if _table_exists(db, "contest_year_settings"):
                db.execute("UPDATE contest_year_settings SET contest_year = ? WHERE contest_year = ?", (new_id, old_id))
            for table in ("images", "votes", "reactions", "duel_votes", "stickers", "vote_options"):
                if _table_exists(db, table):
                    db.execute(f"UPDATE {table} SET contest_id = ? WHERE contest_id = ?", (new_id, old_id))
                    db.execute(f"UPDATE {table} SET contest_year = ? WHERE contest_year = ?", (new_id, old_id))
            if _table_exists(db, "contest_reaction_options"):
                db.execute("UPDATE contest_reaction_options SET contest_id = ? WHERE contest_id = ?", (new_id, old_id))
            if _table_exists(db, "scoring_rules"):
                db.execute("UPDATE scoring_rules SET contest_id = ? WHERE contest_id = ?", (new_id, old_id))

    _update_all_ids(temp_map)
    _update_all_ids({temp_map[old_id]: final_map[old_id] for old_id in old_ids})
    db.commit()


def cleanup_synthetic_contests(db) -> None:
    if not _table_exists(db, "contests"):
        return

    rows = db.execute("SELECT id, slug, title, start_at, storage_key FROM contests").fetchall()
    synthetic_ids = []
    for row in rows:
        contest_id = int(row["id"])
        slug = str(row["slug"] or "").strip()
        title = str(row["title"] or "").strip()
        start_at = str(row["start_at"] or "").strip()
        storage_key = str(row["storage_key"] or "").strip()
        if (
            slug == f"contest-{contest_id}"
            and title == f"Contest {contest_id}"
            and storage_key == str(contest_id)
            and start_at.startswith(f"{contest_id}-")
        ):
            synthetic_ids.append(contest_id)

    for contest_id in synthetic_ids:
        for table in ("images", "votes", "reactions", "duel_votes", "stickers", "vote_options", "contest_reaction_options", "scoring_rules"):
            if _table_exists(db, table):
                db.execute(f"DELETE FROM {table} WHERE contest_id = ?", (contest_id,))
        if _table_exists(db, "contest_year_settings"):
            db.execute("DELETE FROM contest_year_settings WHERE contest_year = ?", (contest_id,))
        db.execute("DELETE FROM contests WHERE id = ?", (contest_id,))

    if synthetic_ids:
        remaining = db.execute("SELECT id FROM contests ORDER BY COALESCE(start_at, '') DESC, id DESC LIMIT 1").fetchone()
        if remaining:
            db.execute("UPDATE contests SET is_active = CASE WHEN id = ? THEN 1 ELSE 0 END", (int(remaining["id"]),))
        db.commit()


def migrate_uploads_to_year_dirs(default_legacy_year: int = 2025):
    """
    Legacy helper kept for backwards compatibility.
    Moves files from static/uploads to static/uploads_<year>.
    """
    db = get_db()
    root = current_app.root_path
    legacy_uploads = os.path.join(root, "static", "uploads")
    if not os.path.isdir(legacy_uploads):
        return

    rows = db.execute(
        "SELECT filename, COALESCE(contest_year, ?) as contest_year FROM images",
        (default_legacy_year,),
    ).fetchall()
    for row in rows:
        filename = row["filename"]
        year = int(row["contest_year"] or default_legacy_year)
        target_dir = os.path.join(root, f"static/uploads_{year}")
        os.makedirs(target_dir, exist_ok=True)
        src = os.path.join(legacy_uploads, filename)
        dst = os.path.join(target_dir, filename)
        if os.path.exists(src) and not os.path.exists(dst):
            shutil.move(src, dst)


def migrate_null_years(default_legacy_year: int = 2025) -> None:
    db = get_db()
    for table in ("images", "votes", "reactions", "duel_votes", "stickers"):
        if _table_exists(db, table):
            db.execute(
                f"UPDATE {table} SET contest_year = ? "
                f"WHERE contest_year IS NULL OR contest_year = 0",
                (default_legacy_year,),
            )
    db.commit()


def migrate_votes_table_rebuild() -> None:
    """
    Keep legacy migration behavior, but ensure we can also scope by contest_id.
    """
    db = get_db()
    cols = db.execute("PRAGMA table_info(votes)").fetchall()
    if not cols:
        return
    col_names = {c[1] for c in cols}

    needs_rebuild = (
        "vote_option_key" not in col_names
        or "contest_year" not in col_names
        or "contest_id" not in col_names
    )
    if not needs_rebuild:
        _ensure_column(db, "votes", "vote_option_key", "vote_option_key TEXT")
        _ensure_column(db, "votes", "vote_value", "vote_value INTEGER DEFAULT 1")
        _ensure_column(db, "votes", "vote_label", "vote_label TEXT")
        return

    db.commit()
    db.execute("BEGIN")
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS votes_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            image_id INTEGER,
            voter_session_id TEXT,
            contest_year INTEGER DEFAULT 2025,
            contest_id INTEGER,
            vote_option_key TEXT,
            vote_value INTEGER DEFAULT 1,
            vote_label TEXT,
            chip_label TEXT,
            chip_value INTEGER DEFAULT 1,
            UNIQUE(image_id, voter_session_id, contest_id)
        )
        """
    )

    select_parts = []
    insert_cols = []

    if "id" in col_names:
        insert_cols.append("id")
        select_parts.append("id")

    for c in ("image_id", "voter_session_id"):
        insert_cols.append(c)
        select_parts.append(c if c in col_names else "NULL")

    insert_cols.append("contest_year")
    select_parts.append("contest_year" if "contest_year" in col_names else "2025")

    insert_cols.append("contest_id")
    if "contest_id" in col_names:
        select_parts.append("contest_id")
    elif "contest_year" in col_names:
        select_parts.append("contest_year")
    else:
        select_parts.append("2025")

    for c, fallback in (
        ("vote_option_key", "NULL"),
        ("vote_value", "1"),
        ("vote_label", "NULL"),
        ("chip_label", "NULL"),
        ("chip_value", "1"),
    ):
        insert_cols.append(c)
        select_parts.append(c if c in col_names else fallback)

    db.execute(
        f"""
        INSERT OR IGNORE INTO votes_new ({", ".join(insert_cols)})
        SELECT {", ".join(select_parts)} FROM votes
        """
    )

    db.execute("DROP TABLE votes")
    db.execute("ALTER TABLE votes_new RENAME TO votes")
    db.execute("COMMIT")


def migrate_vote_generic_columns(default_legacy_year: int = 2025) -> None:
    db = get_db()
    _ensure_column(db, "votes", "vote_option_key", "vote_option_key TEXT")
    _ensure_column(db, "votes", "vote_value", "vote_value INTEGER DEFAULT 1")
    _ensure_column(db, "votes", "vote_label", "vote_label TEXT")
    _ensure_column(db, "votes", "chip_label", "chip_label TEXT")
    _ensure_column(db, "votes", "chip_value", "chip_value INTEGER DEFAULT 1")

    db.execute(
        """
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
            ELSE 'heart'
        END
        WHERE vote_option_key IS NULL OR TRIM(vote_option_key) = ''
        """
    )

    db.execute(
        """
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
        """
    )

    db.execute(
        """
        UPDATE votes
        SET vote_value =
          CASE
            WHEN vote_value IS NOT NULL AND vote_value > 0 THEN vote_value
            WHEN chip_value IS NOT NULL AND chip_value > 0 THEN chip_value
            ELSE 1
          END
        WHERE vote_value IS NULL OR vote_value = 0
        """
    )
    db.commit()


def _ensure_default_reaction_data(db, contest_id: int):
    return None


def _ensure_vote_scoring_defaults(db, contest_id: int):
    return None


def migrate_reaction_points_from_scoring(db) -> None:
    if not _table_exists(db, "contest_reaction_options") or not _table_exists(db, "scoring_rules"):
        return
    db.execute(
        """
        UPDATE contest_reaction_options
        SET points = COALESCE(
            (
                SELECT sr.points
                FROM scoring_rules sr
                WHERE sr.contest_id = contest_reaction_options.contest_id
                  AND sr.source_type = 'reaction'
                  AND sr.source_key = contest_reaction_options.reaction_key
            ),
            points,
            0
        )
        WHERE points IS NULL OR points = 0
        """
    )
    db.commit()


def _migrate_legacy_contests(db):
    now = datetime.now().isoformat()

    # Build candidate legacy years from existing data.
    years = set()
    for table in ("images", "votes", "reactions", "duel_votes", "stickers", "contest_year_settings", "vote_options"):
        if not _table_exists(db, table):
            continue
        if table == "vote_options":
            rows = db.execute("SELECT DISTINCT contest_year FROM vote_options WHERE contest_year IS NOT NULL").fetchall()
        else:
            rows = db.execute(f"SELECT DISTINCT contest_year FROM {table} WHERE contest_year IS NOT NULL").fetchall()
        for row in rows:
            y = int(row["contest_year"] or 0)
            if y > 0:
                years.add(y)

    if not years:
        years = {datetime.now().year}

    for y in sorted(years):
        settings = (
            db.execute(
                """
                SELECT vote_mode, max_actions, unit_name, unit_icon, theme_id
                FROM contest_year_settings
                WHERE contest_year = ?
                """,
                (y,),
            ).fetchone()
            if _table_exists(db, "contest_year_settings")
            else None
        )
        vote_mode = (settings["vote_mode"] if settings else None) or "single_vote"
        max_actions = int((settings["max_actions"] if settings else 3) or 3)
        unit_name = (settings["unit_name"] if settings else None) or "Stimme"
        unit_icon = (settings["unit_icon"] if settings else None) or "â¤ï¸"
        theme_id = (settings["theme_id"] if settings else None) or "default"

        db.execute(
            """
            INSERT INTO contests (
                id, slug, title, start_at, end_at, vote_mode, max_actions, unit_name, unit_icon,
                theme_id, waiting_text, published, is_active, storage_key, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?)
            ON CONFLICT(id) DO NOTHING
            """,
            (
                y,
                f"contest-{y}",
                f"Contest {y}",
                f"{y}-01-01T00:00:00",
                f"{y}-12-31T23:59:59",
                vote_mode,
                max_actions,
                unit_name,
                unit_icon,
                theme_id,
                "Die Abstimmung lÃ¤uft noch. Ergebnisse werden nach Freigabe verÃ¶ffentlicht.",
                str(y),
                now,
            ),
        )

    # Ensure exactly one active contest. Prefer the latest by start date, then id.
    active_rows = db.execute("SELECT id FROM contests WHERE is_active = 1 ORDER BY COALESCE(start_at, '') DESC, id DESC").fetchall()
    if active_rows:
        keep_id = int(active_rows[0]["id"])
        db.execute("UPDATE contests SET is_active = CASE WHEN id = ? THEN 1 ELSE 0 END", (keep_id,))
    else:
        top = db.execute("SELECT id FROM contests ORDER BY COALESCE(start_at, '') DESC, id DESC LIMIT 1").fetchone()
        if top:
            db.execute("UPDATE contests SET is_active = CASE WHEN id = ? THEN 1 ELSE 0 END", (int(top["id"]),))

    # Map old contest_year data to contest_id.
    for table in ("images", "votes", "reactions", "duel_votes", "stickers", "vote_options"):
        if not _table_exists(db, table):
            continue
        if table == "vote_options":
            db.execute(
                """
                UPDATE vote_options
                SET contest_id = contest_year
                WHERE contest_id IS NULL AND contest_year IS NOT NULL
                """
            )
        else:
            db.execute(
                f"""
                UPDATE {table}
                SET contest_id = contest_year
                WHERE contest_id IS NULL AND contest_year IS NOT NULL
                """
            )

    # Bring legacy vote options into contest scope.
    if _table_exists(db, "vote_options"):
        _ensure_column(db, "vote_options", "display_type", "display_type TEXT DEFAULT 'icon'")
        _ensure_column(db, "vote_options", "image_filename", "image_filename TEXT")
        db.execute(
            """
            UPDATE vote_options
            SET display_type = CASE
                WHEN image_filename IS NOT NULL AND TRIM(image_filename) != '' THEN 'image'
                ELSE 'icon'
            END
            WHERE display_type IS NULL OR TRIM(display_type) = ''
            """
        )

        # Ensure minimal option exists per contest.
        contest_rows = db.execute("SELECT id FROM contests").fetchall()
        for contest in contest_rows:
            cid = int(contest["id"])
            count = db.execute(
                "SELECT COUNT(*) FROM vote_options WHERE contest_id = ?",
                (cid,),
            ).fetchone()[0]
            if count == 0:
                db.execute(
                    """
                    INSERT INTO vote_options
                    (contest_id, contest_year, opt_key, label, icon, display_type, image_filename, value, unique_per_user, exclusive_group, is_special, active, sort_order, created_at)
                    VALUES (?, ?, 'heart', 'Vote', 'â¤ï¸', 'icon', NULL, 1, 0, NULL, 0, 1, 10, ?)
                    """,
                    (cid, cid, now),
                )

    # Ensure reaction option + scoring defaults.
    contest_rows = db.execute("SELECT id FROM contests").fetchall()
    for contest in contest_rows:
        cid = int(contest["id"])
        _ensure_default_reaction_data(db, cid)
        _ensure_vote_scoring_defaults(db, cid)


def _ensure_known_contest_defaults(db):
    """
    Ensure known historical contests exist with expected date windows/theme defaults.
    This removes manual post-migration fixups for legacy 2025/2026 events.
    """
    now = datetime.now().isoformat()

    # Rename legacy/old theme ids to the new naming.
    db.execute(
        """
        UPDATE contests
        SET theme_id = CASE
            WHEN theme_id = 'legacy_2025' THEN 'rose_theme'
            WHEN theme_id = 'legacy_2026' THEN 'casino'
            WHEN theme_id = 'dark' THEN 'dark_theme'
            WHEN theme_id = 'casino_old' THEN 'dark_theme'
            ELSE theme_id
        END
        """
    )

    known_contests = [
        {
            "storage_key": "2025",
            "title": "Contest 2025",
            "slug": "2025-03-27-contest-2025",
            "start_at": "2025-03-27T00:00:00",
            "end_at": "2025-03-28T00:00:00",
            "vote_mode": "single_vote",
            "max_actions": 3,
            "unit_name": "Stimme",
            "unit_icon": "❤",
            "theme_id": "rose_theme",
            "waiting_text": "Die Abstimmung läuft noch. Ergebnisse werden nach Freigabe veröffentlicht.",
            "is_active": 0,
        },
        {
            "storage_key": "2026",
            "title": "Contest 2026",
            "slug": "2026-04-11-contest-2026",
            "start_at": "2026-04-11T00:00:00",
            "end_at": "2026-04-12T00:00:00",
            "vote_mode": "unique_options",
            "max_actions": 4,
            "unit_name": "Stimme",
            "unit_icon": "*",
            "theme_id": "casino",
            "waiting_text": "Die Abstimmung läuft noch. Ergebnisse werden nach Freigabe veröffentlicht.",
            "is_active": 0,
        },
    ]

    for item in known_contests:
        rows = db.execute(
            "SELECT id FROM contests WHERE storage_key = ? ORDER BY id ASC",
            (item["storage_key"],),
        ).fetchall()
        if rows:
            keep_id = int(rows[0]["id"])
            existing = db.execute("SELECT * FROM contests WHERE id = ?", (keep_id,)).fetchone()
            if existing:
                existing = dict(existing)
                db.execute(
                    """
                    UPDATE contests
                    SET title = ?, slug = ?, start_at = ?, end_at = ?, vote_mode = ?, max_actions = ?,
                        unit_name = ?, unit_icon = ?, theme_id = ?, waiting_text = ?, is_active = ?, storage_key = ?
                    WHERE id = ?
                    """,
                    (
                        (existing.get("title") or "").strip() or item["title"],
                        (existing.get("slug") or "").strip() or item["slug"],
                        existing.get("start_at") or item["start_at"],
                        existing.get("end_at") or item["end_at"],
                        (existing.get("vote_mode") or "").strip() or item["vote_mode"],
                        int(existing.get("max_actions") or item["max_actions"]),
                        (existing.get("unit_name") or "").strip() or item["unit_name"],
                        (existing.get("unit_icon") or "").strip() or item["unit_icon"],
                        (existing.get("theme_id") or "").strip() or item["theme_id"],
                        (existing.get("waiting_text") or "").strip() or item["waiting_text"],
                        int(existing.get("is_active") if existing.get("is_active") is not None else item["is_active"]),
                        (existing.get("storage_key") or "").strip() or item["storage_key"],
                        keep_id,
                    ),
                )

            duplicate_ids = [int(row["id"]) for row in rows[1:]]
            for duplicate_id in duplicate_ids:
                for table in ("contest_reaction_options", "scoring_rules"):
                    if _table_exists(db, table):
                        db.execute(f"DELETE FROM {table} WHERE contest_id = ?", (duplicate_id,))
                db.execute("DELETE FROM contests WHERE id = ?", (duplicate_id,))

            _ensure_default_reaction_data(db, keep_id)
            _ensure_vote_scoring_defaults(db, keep_id)

    active_rows = db.execute("SELECT id FROM contests WHERE is_active = 1 ORDER BY COALESCE(start_at, '') DESC, id DESC").fetchall()
    if active_rows:
        keep_active = int(active_rows[0]["id"])
        db.execute("UPDATE contests SET is_active = CASE WHEN id = ? THEN 1 ELSE 0 END", (keep_active,))
    else:
        top = db.execute("SELECT id FROM contests ORDER BY COALESCE(start_at, '') DESC, id DESC LIMIT 1").fetchone()
        if top:
            db.execute("UPDATE contests SET is_active = CASE WHEN id = ? THEN 1 ELSE 0 END", (int(top["id"]),))


def init_db():
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            description TEXT,
            uploader TEXT,
            uploaded_at TEXT,
            visible INTEGER DEFAULT 0,
            exclude_from_results INTEGER DEFAULT 0,
            include_in_category_results INTEGER DEFAULT 1,
            contest_year INTEGER DEFAULT 2025,
            contest_id INTEGER
        );

        CREATE TABLE IF NOT EXISTS votes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            image_id INTEGER,
            voter_session_id TEXT,
            contest_year INTEGER DEFAULT 2025,
            contest_id INTEGER,
            vote_option_key TEXT,
            vote_value INTEGER DEFAULT 1,
            vote_label TEXT,
            created_at TEXT,
            UNIQUE(image_id, voter_session_id, contest_year)
        );

        CREATE TABLE IF NOT EXISTS stickers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contest_year INTEGER NOT NULL,
            contest_id INTEGER,
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
            contest_id INTEGER,
            created_at TEXT,
            UNIQUE(image_id, voter_session_id, reaction_type, contest_year)
        );

        CREATE TABLE IF NOT EXISTS contest_year_settings (
            contest_year INTEGER PRIMARY KEY,
            vote_mode TEXT DEFAULT 'toggle',
            max_actions INTEGER DEFAULT 3,
            unit_name TEXT DEFAULT 'Stimme',
            unit_icon TEXT DEFAULT 'â¤ï¸',
            theme_id TEXT DEFAULT 'default',
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS vote_options (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contest_year INTEGER NOT NULL,
            contest_id INTEGER,
            opt_key TEXT NOT NULL,
            label TEXT NOT NULL,
            icon TEXT,
            display_type TEXT DEFAULT 'icon',
            image_filename TEXT,
            value INTEGER DEFAULT 1,
            unique_per_user INTEGER DEFAULT 0,
            exclusive_group TEXT,
            is_special INTEGER DEFAULT 0,
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
            contest_id INTEGER,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS contests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT UNIQUE,
            title TEXT NOT NULL,
            start_at TEXT,
            end_at TEXT,
            vote_mode TEXT DEFAULT 'single_vote',
            max_actions INTEGER DEFAULT 3,
            unit_name TEXT DEFAULT 'Stimme',
            unit_icon TEXT DEFAULT 'â¤ï¸',
            theme_id TEXT DEFAULT 'default',
            waiting_text TEXT,
            published INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 0,
            storage_key TEXT,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS contest_reaction_options (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contest_id INTEGER NOT NULL,
            reaction_key TEXT NOT NULL,
            label TEXT NOT NULL,
            display_type TEXT DEFAULT 'icon',
            icon TEXT,
            image_filename TEXT,
            points INTEGER DEFAULT 0,
            include_in_total INTEGER DEFAULT 1,
            active INTEGER DEFAULT 1,
            sort_order INTEGER DEFAULT 0,
            created_at TEXT,
            UNIQUE(contest_id, reaction_key)
        );

        CREATE TABLE IF NOT EXISTS scoring_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contest_id INTEGER NOT NULL,
            source_type TEXT NOT NULL, -- vote_option | reaction
            source_key TEXT NOT NULL,
            points INTEGER NOT NULL DEFAULT 1,
            created_at TEXT,
            UNIQUE(contest_id, source_type, source_key)
        );

        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            created_at TEXT
        );
        """
    )

    # Backward-compatible columns for pre-existing DBs.
    for table in ("images", "votes", "stickers", "reactions", "duel_votes", "vote_options"):
        _ensure_column(db, table, "contest_id", "contest_id INTEGER")
    _ensure_column(db, "images", "exclude_from_results", "exclude_from_results INTEGER DEFAULT 0")
    _ensure_column(db, "images", "include_in_category_results", "include_in_category_results INTEGER DEFAULT 1")
    _ensure_column(db, "votes", "created_at", "created_at TEXT")
    _ensure_column(db, "vote_options", "display_type", "display_type TEXT DEFAULT 'icon'")
    _ensure_column(db, "vote_options", "image_filename", "image_filename TEXT")
    _ensure_column(db, "contest_reaction_options", "points", "points INTEGER DEFAULT 0")
    _ensure_column(db, "contest_reaction_options", "include_in_total", "include_in_total INTEGER DEFAULT 1")

    migrate_null_years(default_legacy_year=2025)
    migrate_votes_table_rebuild()
    migrate_vote_generic_columns(default_legacy_year=2025)
    _migrate_legacy_contests(db)
    migrate_contest_slugs(db)
    migrate_contest_ids_to_internal_sequence(db)
    _ensure_known_contest_defaults(db)
    cleanup_synthetic_contests(db)
    migrate_reaction_points_from_scoring(db)

    db.execute(
        """
        INSERT INTO app_settings (key, value, created_at)
        VALUES ('block_public_unpublished_all_contests', '0', ?)
        ON CONFLICT(key) DO NOTHING
        """,
        (datetime.now().isoformat(),),
    )

    db.commit()


@click.command("init-db")
@with_appcontext
def init_db_command():
    init_db()
    click.echo("DB initialized.")


def init_app(app):
    app.teardown_appcontext(close_db)
    app.cli.add_command(init_db_command)

