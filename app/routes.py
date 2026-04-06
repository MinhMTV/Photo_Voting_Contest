import os
import re
import json
import zipfile
import random
import tarfile
import shutil
import io
from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, session, current_app, jsonify, send_from_directory, flash, send_file
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

try:
    from .db import close_db, get_db
except ImportError:
    from db import close_db, get_db

bp = Blueprint("main", __name__)
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def _slugify(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or f"contest-{datetime.now().strftime('%Y%m%d%H%M%S')}"


def _contest_slug_value(title: str, start_at: str = "", fallback: str = "contest") -> str:
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
    seed = "-".join(part for part in (date_part, raw_title or fallback) if part)
    return _slugify(seed or fallback)


def list_contests() -> list[dict]:
    db = get_db()
    rows = db.execute("SELECT * FROM contests ORDER BY COALESCE(start_at, '') DESC, id DESC").fetchall()
    return [dict(r) for r in rows]


def available_theme_ids() -> list[str]:
    base = os.path.join(current_app.root_path, "templates", "themes")
    themes = set()
    if os.path.isdir(base):
        for name in os.listdir(base):
            full = os.path.join(base, name)
            if os.path.isdir(full):
                themes.add(name)
    # Keep stable defaults visible even if folder is missing.
    themes.update({"default", "casino", "dark_theme", "rose_theme"})
    return sorted(themes)


def _theme_description(theme_id: str) -> str:
    descriptions = {
        "default": "Helles Standard-Theme",
        "casino": "Übernommene Legacy-Ansicht 2026",
        "dark_theme": "Dunkles Casino-Theme",
        "rose_theme": "Übernommene Legacy-Ansicht 2025",
    }
    return descriptions.get(theme_id, "Benutzerdefiniertes Theme")


def theme_catalog() -> list[dict]:
    catalog = []
    base = os.path.join(current_app.root_path, "templates", "themes")
    for theme_id in available_theme_ids():
        row = {
            "id": theme_id,
            "description": _theme_description(theme_id),
            "has_contest": False,
            "has_results": False,
            "has_waiting": False,
            "has_vote_options": False,
            "has_reaction_options": False,
        }
        folder = os.path.join(base, theme_id)
        if os.path.isdir(folder):
            row["has_contest"] = os.path.exists(os.path.join(folder, "contest.html"))
            row["has_results"] = os.path.exists(os.path.join(folder, "public_results.html"))
            row["has_waiting"] = os.path.exists(os.path.join(folder, "public_waiting.html"))
            row["has_vote_options"] = os.path.exists(os.path.join(folder, "vote_options.json"))
            row["has_reaction_options"] = os.path.exists(os.path.join(folder, "reaction_options.json"))
        catalog.append(row)
    return catalog


def _theme_vote_presets(theme_id: str) -> list[dict]:
    """
    Detect vote option presets from a theme.
    Priority:
    1) templates/themes/<theme_id>/vote_options.json
    2) static detection from contest.html (data-chip / data-key literals)
    """
    presets: list[dict] = []
    base = os.path.join(current_app.root_path, "templates", "themes", theme_id)
    json_path = os.path.join(base, "vote_options.json")
    if os.path.exists(json_path):
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, list):
                for idx, item in enumerate(raw, start=1):
                    if not isinstance(item, dict):
                        continue
                    opt_key = str(item.get("opt_key") or "").strip()
                    if not opt_key:
                        continue
                    presets.append(
                        {
                            "opt_key": opt_key,
                            "label": str(item.get("label") or opt_key).strip(),
                            "icon": str(item.get("icon") or "").strip(),
                            "display_type": str(item.get("display_type") or "icon").strip() or "icon",
                            "image_filename": str(item.get("image_filename") or "").strip() or None,
                            "value": int(item.get("value") or 1),
                            "unique_per_user": 1 if item.get("unique_per_user") else 0,
                            "exclusive_group": str(item.get("exclusive_group") or "").strip().lower() or None,
                            "is_special": 1 if item.get("is_special") else 0,
                            "active": 1 if item.get("active", True) else 0,
                            "sort_order": int(item.get("sort_order") or (idx * 10)),
                        }
                    )
            if presets:
                return presets
        except Exception:
            pass

    contest_tpl = os.path.join(base, "contest.html")
    if not os.path.exists(contest_tpl):
        return presets

    try:
        with open(contest_tpl, "r", encoding="utf-8") as f:
            txt = f.read()
    except Exception:
        return presets

    keys: list[str] = []
    for chip in re.findall(r'data-chip="([^"]+)"', txt):
        v = (chip or "").strip().lower()
        if not v:
            continue
        key = "all_in" if v in {"all-in", "all_in", "allin"} else f"chip_{v}"
        if key not in keys:
            keys.append(key)

    for dk in re.findall(r'data-key="([^"]+)"', txt):
        k = (dk or "").strip()
        if not k or "{{" in k or "}}" in k:
            continue
        if k not in keys:
            keys.append(k)

    for idx, key in enumerate(keys, start=1):
        label = key
        value = 1
        is_special = 0
        unique_per_user = 0
        exclusive_group = None
        if key.startswith("chip_"):
            raw = key.split("_", 1)[1]
            label = raw.upper() if raw.lower() == "all_in" else raw
            if raw.isdigit():
                value = int(raw)
        elif key == "all_in":
            label = "ALL-IN"
            value = 500
            is_special = 1
            unique_per_user = 1
            exclusive_group = "allin"
        elif key == "heart":
            label = "Vote"
            value = 1
        presets.append(
            {
                "opt_key": key,
                "label": label,
                "icon": "",
                "display_type": "icon",
                "image_filename": None,
                "value": value,
                "unique_per_user": unique_per_user,
                "exclusive_group": exclusive_group,
                "is_special": is_special,
                "active": 1,
                "sort_order": idx * 10,
            }
        )
    return presets


def _theme_vote_preset_source(theme_id: str) -> str:
    base = os.path.join(current_app.root_path, "templates", "themes", theme_id)
    json_path = os.path.join(base, "vote_options.json")
    if os.path.exists(json_path):
        return "json"
    contest_tpl = os.path.join(base, "contest.html")
    if os.path.exists(contest_tpl):
        return "html"
    return "none"


def _theme_vote_json_path(theme_id: str) -> str:
    return os.path.join(current_app.root_path, "templates", "themes", str(theme_id), "vote_options.json")


def _write_theme_vote_presets(theme_id: str, presets: list[dict]) -> None:
    path = _theme_vote_json_path(theme_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    clean_rows = []
    seen_keys = set()
    for idx, item in enumerate(presets, start=1):
        key = str(item.get("opt_key") or "").strip().lower()
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        clean_rows.append(
            {
                "opt_key": key,
                "label": str(item.get("label") or key).strip(),
                "icon": str(item.get("icon") or "").strip(),
                "display_type": str(item.get("display_type") or "icon").strip() or "icon",
                "image_filename": str(item.get("image_filename") or "").strip() or None,
                "value": int(item.get("value") or 1),
                "unique_per_user": bool(item.get("unique_per_user")),
                "exclusive_group": str(item.get("exclusive_group") or "").strip().lower() or None,
                "active": bool(item.get("active", True)),
                "sort_order": int(item.get("sort_order") or (idx * 10)),
            }
        )
    with open(path, "w", encoding="utf-8") as f:
        json.dump(clean_rows, f, ensure_ascii=False, indent=2)


def _contests_for_theme(theme_id: str) -> list[dict]:
    db = get_db()
    rows = db.execute("SELECT * FROM contests WHERE theme_id = ? ORDER BY id ASC", (theme_id,)).fetchall()
    return [dict(r) for r in rows]


def _apply_theme_vote_preset_change(theme_id: str, original_key: str = "", new_key: str = "", deleted_key: str = "") -> int:
    db = get_db()
    contests = _contests_for_theme(theme_id)
    old_key = str(original_key or deleted_key or "").strip().lower()
    normalized_new_key = str(new_key or "").strip().lower()
    touched = 0
    for contest in contests:
        contest_id = int(contest["id"])
        if deleted_key:
            db.execute(
                "DELETE FROM vote_options WHERE contest_id = ? AND opt_key = ?",
                (contest_id, old_key),
            )
            touched += 1
            continue
        if old_key and normalized_new_key and old_key != normalized_new_key:
            db.execute(
                "DELETE FROM vote_options WHERE contest_id = ? AND opt_key = ?",
                (contest_id, old_key),
            )
        _import_theme_vote_presets(contest_id, theme_id, mode="replace")
        touched += 1
    if touched:
        db.commit()
    return touched


def _theme_reaction_presets(theme_id: str) -> list[dict]:
    presets: list[dict] = []
    base = os.path.join(current_app.root_path, "templates", "themes", theme_id)
    json_path = os.path.join(base, "reaction_options.json")
    if os.path.exists(json_path):
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, list):
                for idx, item in enumerate(raw, start=1):
                    if not isinstance(item, dict):
                        continue
                    reaction_key = str(item.get("reaction_key") or "").strip().lower()
                    if not reaction_key:
                        continue
                    presets.append(
                        {
                            "reaction_key": reaction_key,
                            "label": str(item.get("label") or reaction_key).strip(),
                            "display_type": str(item.get("display_type") or "icon").strip() or "icon",
                            "icon": str(item.get("icon") or "").strip(),
                            "image_filename": str(item.get("image_filename") or "").strip() or None,
                            "points": int(item.get("points") or 0),
                            "include_in_total": 1 if item.get("include_in_total", True) else 0,
                            "active": 1 if item.get("active", True) else 0,
                            "sort_order": int(item.get("sort_order") or (idx * 10)),
                        }
                    )
        except Exception:
            return presets
    return presets


def _theme_reaction_preset_source(theme_id: str) -> str:
    base = os.path.join(current_app.root_path, "templates", "themes", theme_id)
    json_path = os.path.join(base, "reaction_options.json")
    if os.path.exists(json_path):
        return "json"
    return "none"


def _theme_reaction_json_path(theme_id: str) -> str:
    return os.path.join(current_app.root_path, "templates", "themes", str(theme_id), "reaction_options.json")


def _write_theme_reaction_presets(theme_id: str, presets: list[dict]) -> None:
    path = _theme_reaction_json_path(theme_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    clean_rows = []
    seen_keys = set()
    for idx, item in enumerate(presets, start=1):
        key = str(item.get("reaction_key") or "").strip().lower()
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        clean_rows.append(
            {
                "reaction_key": key,
                "label": str(item.get("label") or key).strip(),
                "display_type": str(item.get("display_type") or "icon").strip() or "icon",
                "icon": str(item.get("icon") or "").strip(),
                "image_filename": str(item.get("image_filename") or "").strip() or None,
                "points": int(item.get("points") or 0),
                "include_in_total": 1 if item.get("include_in_total", True) else 0,
                "active": bool(item.get("active", True)),
                "sort_order": int(item.get("sort_order") or (idx * 10)),
            }
        )
    with open(path, "w", encoding="utf-8") as f:
        json.dump(clean_rows, f, ensure_ascii=False, indent=2)


def save_uploaded_theme_asset(theme_id: str, file_obj, prefix: str) -> str | None:
    if not file_obj or not getattr(file_obj, "filename", ""):
        return None
    if not allowed_file(file_obj.filename):
        return None
    folder = theme_asset_folder(theme_id)
    os.makedirs(folder, exist_ok=True)
    safe = secure_filename(file_obj.filename)
    stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    filename = f"{prefix}_{stamp}_{safe}"
    file_obj.save(os.path.join(folder, filename))
    return filename


def _apply_theme_reaction_preset_change(theme_id: str, original_key: str = "", new_key: str = "", deleted_key: str = "") -> int:
    db = get_db()
    contests = _contests_for_theme(theme_id)
    old_key = str(original_key or deleted_key or "").strip().lower()
    normalized_new_key = str(new_key or "").strip().lower()
    touched = 0
    for contest in contests:
        contest_id = int(contest["id"])
        if deleted_key:
            db.execute(
                "DELETE FROM contest_reaction_options WHERE contest_id = ? AND reaction_key = ?",
                (contest_id, old_key),
            )
            touched += 1
            continue
        if old_key and normalized_new_key and old_key != normalized_new_key:
            db.execute(
                "DELETE FROM contest_reaction_options WHERE contest_id = ? AND reaction_key = ?",
                (contest_id, old_key),
            )
        _import_theme_reaction_presets(contest_id, theme_id, mode="replace")
        touched += 1
    if touched:
        db.commit()
    return touched


def _import_theme_vote_presets(contest_id: int, theme_id: str, mode: str = "missing") -> tuple[int, int]:
    db = get_db()
    presets = _theme_vote_presets(theme_id)
    if not presets:
        return (0, 0)

    existing_rows = db.execute("SELECT id, opt_key FROM vote_options WHERE contest_id = ?", (contest_id,)).fetchall()
    existing = {(r["opt_key"] or "").strip().lower(): int(r["id"]) for r in existing_rows}
    max_sort = db.execute("SELECT COALESCE(MAX(sort_order), 0) FROM vote_options WHERE contest_id = ?", (contest_id,)).fetchone()[0]
    inserted = 0
    skipped = 0
    now = datetime.now().isoformat()

    for p in presets:
        key = (p.get("opt_key") or "").strip()
        if not key:
            continue
        normalized_key = key.lower()
        copied_image_filename = _copy_theme_asset_to_contest(theme_id, contest_id, p.get("image_filename"), "theme_vote")
        effective_image_filename = copied_image_filename or p.get("image_filename")
        if normalized_key in existing:
            if mode != "replace":
                skipped += 1
                continue
            db.execute(
                """
                UPDATE vote_options
                SET label = ?, icon = ?, display_type = ?, image_filename = ?, value = ?, unique_per_user = ?,
                    exclusive_group = ?, is_special = 0, active = ?, sort_order = ?, contest_year = ?
                WHERE id = ? AND contest_id = ?
                """,
                (
                    (p.get("label") or key),
                    (p.get("icon") or ""),
                    (p.get("display_type") or "icon"),
                    effective_image_filename,
                    int(p.get("value") or 1),
                    1 if p.get("unique_per_user") else 0,
                    p.get("exclusive_group"),
                    1 if p.get("active", 1) else 0,
                    int(p.get("sort_order") or 0),
                    contest_id,
                    existing[normalized_key],
                    contest_id,
                ),
            )
            inserted += 1
            continue

        max_sort += 10
        db.execute(
            """
            INSERT INTO vote_options
                (contest_year, contest_id, opt_key, label, icon, display_type, image_filename, value, unique_per_user, exclusive_group, is_special, active, sort_order, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                contest_id,
                contest_id,
                key,
                (p.get("label") or key),
                (p.get("icon") or ""),
                (p.get("display_type") or "icon"),
                effective_image_filename,
                int(p.get("value") or 1),
                1 if p.get("unique_per_user") else 0,
                p.get("exclusive_group"),
                0,
                1 if p.get("active", 1) else 0,
                int(p.get("sort_order") or max_sort),
                now,
            ),
        )
        inserted += 1
        existing[normalized_key] = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])

    if inserted > 0:
        db.commit()
    if mode == "replace":
        preset_keys = {(p.get("opt_key") or "").strip().lower() for p in presets if (p.get("opt_key") or "").strip()}
        if preset_keys:
            placeholders = ",".join("?" for _ in preset_keys)
            db.execute(
                f"DELETE FROM vote_options WHERE contest_id = ? AND LOWER(opt_key) NOT IN ({placeholders})",
                (contest_id, *sorted(preset_keys)),
            )
            db.commit()
    return (inserted, skipped)


def _import_theme_reaction_presets(contest_id: int, theme_id: str, mode: str = "missing") -> tuple[int, int]:
    db = get_db()
    presets = _theme_reaction_presets(theme_id)
    if not presets:
        return (0, 0)

    existing_rows = db.execute("SELECT id, reaction_key FROM contest_reaction_options WHERE contest_id = ?", (contest_id,)).fetchall()
    existing = {(r["reaction_key"] or "").strip().lower(): int(r["id"]) for r in existing_rows}
    max_sort = db.execute("SELECT COALESCE(MAX(sort_order), 0) FROM contest_reaction_options WHERE contest_id = ?", (contest_id,)).fetchone()[0]
    changed = 0
    skipped = 0
    now = datetime.now().isoformat()

    for p in presets:
        key = (p.get("reaction_key") or "").strip().lower()
        if not key:
            continue
        copied_image_filename = _copy_theme_asset_to_contest(theme_id, contest_id, p.get("image_filename"), "theme_reaction")
        effective_image_filename = copied_image_filename or p.get("image_filename")
        if key in existing:
            if mode != "replace":
                skipped += 1
                continue
            db.execute(
                """
                UPDATE contest_reaction_options
                SET label = ?, display_type = ?, icon = ?, image_filename = ?, points = ?, include_in_total = ?, active = ?, sort_order = ?
                WHERE id = ? AND contest_id = ?
                """,
                (
                    p.get("label") or key,
                    p.get("display_type") or "icon",
                    p.get("icon") or "",
                    effective_image_filename,
                    int(p.get("points") or 0),
                    1 if p.get("include_in_total", 1) else 0,
                    1 if p.get("active", 1) else 0,
                    int(p.get("sort_order") or 0),
                    existing[key],
                    contest_id,
                ),
            )
            changed += 1
            continue

        max_sort += 10
        db.execute(
            """
            INSERT INTO contest_reaction_options
                (contest_id, reaction_key, label, display_type, icon, image_filename, points, include_in_total, active, sort_order, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                contest_id,
                key,
                p.get("label") or key,
                p.get("display_type") or "icon",
                p.get("icon") or "",
                effective_image_filename,
                int(p.get("points") or 0),
                1 if p.get("include_in_total", 1) else 0,
                1 if p.get("active", 1) else 0,
                int(p.get("sort_order") or max_sort),
                now,
            ),
        )
        changed += 1
        existing[key] = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])

    if changed > 0:
        db.commit()
    if mode == "replace":
        preset_keys = {(p.get("reaction_key") or "").strip().lower() for p in presets if (p.get("reaction_key") or "").strip()}
        if preset_keys:
            placeholders = ",".join("?" for _ in preset_keys)
            db.execute(
                f"DELETE FROM contest_reaction_options WHERE contest_id = ? AND LOWER(reaction_key) NOT IN ({placeholders})",
                (contest_id, *sorted(preset_keys)),
            )
            db.commit()
    return (changed, skipped)


def get_contest(contest_id: int) -> dict | None:
    db = get_db()
    row = db.execute("SELECT * FROM contests WHERE id = ?", (contest_id,)).fetchone()
    contest = dict(row) if row else None
    if contest:
        maybe_create_contest_end_backup(contest)
    return contest


def get_contest_by_slug(slug: str) -> dict | None:
    db = get_db()
    row = db.execute("SELECT * FROM contests WHERE slug = ?", ((slug or "").strip(),)).fetchone()
    return dict(row) if row else None


def current_year() -> int:
    db = get_db()
    row = db.execute("SELECT id FROM contests WHERE is_active = 1 ORDER BY COALESCE(start_at, '') DESC, id DESC LIMIT 1").fetchone()
    if row:
        return int(row["id"])
    row = db.execute("SELECT id FROM contests ORDER BY COALESCE(start_at, '') DESC, id DESC LIMIT 1").fetchone()
    return int(row["id"]) if row else datetime.now().year


def get_year_settings(year: int) -> dict:
    c = get_contest(year)
    if not c:
        return {"vote_mode": "single_vote", "max_actions": 1, "unit_name": "Stimme", "unit_icon": "*", "theme_id": "default"}
    return {
        "vote_mode": c.get("vote_mode") or "single_vote",
        "max_actions": int(c.get("max_actions") or 1),
        "unit_name": c.get("unit_name") or "Stimme",
        "unit_icon": c.get("unit_icon") or "*",
        "theme_id": c.get("theme_id") or "default",
    }


def get_vote_options(year: int) -> list[dict]:
    db = get_db()
    rows = db.execute(
        """
        SELECT id, opt_key, label, icon, display_type, image_filename, value, unique_per_user,
               exclusive_group, active, sort_order
        FROM vote_options
        WHERE contest_id = ? AND active = 1
        ORDER BY sort_order ASC, id ASC
        """,
        (year,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_vote_option_map(year: int) -> dict:
    return {o["opt_key"]: o for o in get_vote_options(year)}


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def get_vote_option_points_map(year: int) -> dict[str, int]:
    options = get_vote_options(year)
    points_map = {str(opt["opt_key"]).strip(): _safe_int(opt.get("value"), 1) for opt in options}
    all_in_total = sum(
        _safe_int(opt.get("value"), 1)
        for opt in options
        if str(opt.get("opt_key") or "").strip().lower() != "all_in"
        and str(opt.get("exclusive_group") or "").strip().lower() != "allin"
    )
    if "all_in" in points_map:
        points_map["all_in"] = all_in_total
    return points_map


def get_reaction_options(year: int) -> list[dict]:
    db = get_db()
    rows = db.execute(
        """
        SELECT id, reaction_key, label, display_type, icon, image_filename, points, include_in_total, active, sort_order
        FROM contest_reaction_options
        WHERE contest_id = ? AND active = 1
        ORDER BY sort_order ASC, id ASC
        """,
        (year,),
    ).fetchall()
    items = [dict(r) for r in rows]
    contest = get_contest(year)
    theme_id = str((contest or {}).get("theme_id") or "").strip()
    presets = _theme_reaction_presets(theme_id)
    if presets:
        preset_keys = {(p.get("reaction_key") or "").strip().lower() for p in presets if (p.get("reaction_key") or "").strip()}
        legacy_keys = {"hype", "creative", "funny", "underrated"}
        hidden_legacy_keys = legacy_keys - preset_keys
        if hidden_legacy_keys:
            items = [item for item in items if str(item.get("reaction_key") or "").strip().lower() not in hidden_legacy_keys]
    return items


def get_reaction_key_set(year: int) -> set[str]:
    return {str(r["reaction_key"]).strip().lower() for r in get_reaction_options(year)}


def get_scoring_rule_map(year: int) -> dict[str, int]:
    return {}


def get_app_setting(key: str, default: str = "") -> str:
    db = get_db()
    row = db.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row and row["value"] is not None else default


def set_app_setting(key: str, value: str):
    db = get_db()
    db.execute(
        """
        INSERT INTO app_settings (key, value, created_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, str(value), datetime.now().isoformat()),
    )
    db.commit()


def stored_admin_password_hash() -> str:
    return str(get_app_setting("admin_password_hash", "") or "").strip()


def admin_uses_default_password() -> bool:
    return not stored_admin_password_hash()


def verify_admin_password(password: str) -> bool:
    raw = str(password or "")
    password_hash = stored_admin_password_hash()
    if password_hash:
        try:
            return check_password_hash(password_hash, raw)
        except Exception:
            return False
    return raw == str(current_app.config.get("ADMIN_PASSWORD") or "admin123")


def save_admin_password(password: str) -> None:
    set_app_setting("admin_password_hash", generate_password_hash(str(password or "").strip()))


def results_reveal_version(year: int) -> str:
    return get_app_setting(f"results_reveal_version_{int(year)}", "1")


def _parse_contest_datetime(value: str | None) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None


def maybe_create_contest_end_backup(contest: dict | None) -> None:
    if not contest:
        return
    contest_id = int(contest.get("id") or 0)
    if contest_id <= 0:
        return
    end_at = _parse_contest_datetime(contest.get("end_at"))
    if not end_at:
        return
    now = datetime.now(end_at.tzinfo) if end_at.tzinfo else datetime.now()
    if now < end_at:
        return

    setting_key = f"contest_end_backup_done_{contest_id}"
    if get_app_setting(setting_key, "").strip():
        return

    label = _slugify(f"contest-end-{contest.get('slug') or contest.get('title') or contest_id}")
    filename = create_runtime_backup(keep_last=10, label=label)
    if filename:
        set_app_setting(setting_key, filename)


def contest_storage_key(contest_id: int) -> str:
    c = get_contest(contest_id)
    if not c:
        return str(contest_id)
    return (c.get("storage_key") or str(contest_id)).strip()


def upload_folder_for_year(year: int) -> str:
    path = os.path.join(current_app.static_folder, f"uploads_{contest_storage_key(year)}")
    os.makedirs(path, exist_ok=True)
    return path


def asset_folder_for_year(year: int, create: bool = False) -> str:
    path = os.path.join(current_app.static_folder, f"assets_{contest_storage_key(year)}")
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def theme_asset_folder(theme_id: str) -> str:
    return os.path.join(current_app.root_path, "templates", "themes", str(theme_id), "assets")


def _copy_theme_asset_to_contest(theme_id: str, contest_id: int, image_filename: str | None, prefix: str) -> str | None:
    if not image_filename:
        return None
    source_name = os.path.basename(str(image_filename).strip())
    if not source_name:
        return None
    source_path = os.path.join(theme_asset_folder(theme_id), source_name)
    if not os.path.exists(source_path):
        return None

    folder = asset_folder_for_year(contest_id, create=True)
    safe_name = secure_filename(source_name)
    target_name = f"{prefix}_{theme_id}_{safe_name}"
    target_path = os.path.join(folder, target_name)
    if not os.path.exists(target_path):
        import shutil
        shutil.copyfile(source_path, target_path)
    return target_name


def save_uploaded_asset(year: int, file_obj, prefix: str) -> str | None:
    if not file_obj or not getattr(file_obj, "filename", ""):
        return None
    if not allowed_file(file_obj.filename):
        return None
    folder = asset_folder_for_year(year, create=True)
    safe = secure_filename(file_obj.filename)
    stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    filename = f"{prefix}_{stamp}_{safe}"
    file_obj.save(os.path.join(folder, filename))
    return filename


def sticker_folder_for_year(year: int, create: bool = False) -> str:
    storage_key = contest_storage_key(year)
    preferred = os.path.join(current_app.static_folder, f"stickers_{storage_key}")
    # Old year-based contests (for example storage_key 2025/2026) may still keep
    # their real sticker files in the shared legacy folder.
    legacy_allowed = bool(re.fullmatch(r"\d{4}", str(storage_key or "").strip()))
    legacy = os.path.join(current_app.static_folder, "stickers")

    def _has_sticker_files(path: str) -> bool:
        if not os.path.isdir(path):
            return False
        try:
            return any(allowed_file(name) for name in os.listdir(path))
        except Exception:
            return False

    if create:
        if os.path.isdir(preferred):
            if legacy_allowed and not _has_sticker_files(preferred) and _has_sticker_files(legacy):
                return legacy
            return preferred
        if legacy_allowed and os.path.isdir(legacy):
            return legacy
        os.makedirs(preferred, exist_ok=True)
        return preferred

    if os.path.isdir(preferred):
        # If an empty preferred folder exists from older logic, keep using legacy for legacy contests.
        if legacy_allowed and not _has_sticker_files(preferred) and _has_sticker_files(legacy):
            return legacy
        return preferred
    if legacy_allowed and os.path.isdir(legacy):
        return legacy
    return preferred


def sticker_file_candidates_for_year(year: int, filename: str) -> list[str]:
    safe_name = secure_filename(filename or "")
    if not safe_name:
        return []
    storage_key = contest_storage_key(year)
    preferred = os.path.join(current_app.static_folder, f"stickers_{storage_key}", safe_name)
    legacy = os.path.join(current_app.static_folder, "stickers", safe_name)
    candidates: list[str] = []
    for path in (preferred, legacy):
        if path not in candidates:
            candidates.append(path)
    return candidates


def ensure_sticker_records_for_year(year: int) -> None:
    db = get_db()
    folder = sticker_folder_for_year(year)
    if not os.path.isdir(folder):
        return
    files = [f for f in os.listdir(folder) if allowed_file(f)]
    for filename in files:
        exists = db.execute("SELECT id FROM stickers WHERE contest_id = ? AND filename = ?", (year, filename)).fetchone()
        if not exists:
            max_sort = db.execute("SELECT COALESCE(MAX(sort_order), 0) FROM stickers WHERE contest_id = ?", (year,)).fetchone()[0]
            db.execute(
                "INSERT INTO stickers (contest_year, contest_id, filename, sort_order, active, created_at) VALUES (?, ?, ?, ?, 1, ?)",
                (year, year, filename, max_sort + 1, datetime.now().isoformat()),
            )
    db.commit()


def backup_root_folder() -> str:
    path = current_app.config.get("BACKUP_FOLDER") or os.path.join(os.path.dirname(current_app.root_path), "backups")
    os.makedirs(path, exist_ok=True)
    return path


def project_root_folder() -> str:
    return os.path.abspath(os.path.join(current_app.root_path, os.pardir))


def _normalized_project_relpath(value: str | None) -> str:
    raw = str(value or "").replace("\\", "/").strip().strip("/")
    parts = [part for part in raw.split("/") if part and part not in {".", ".."}]
    return "/".join(parts)


def resolve_project_path(relative_path: str | None = None) -> str:
    repo_root = project_root_folder()
    normalized = _normalized_project_relpath(relative_path)
    target = os.path.abspath(os.path.join(repo_root, normalized))
    if os.path.commonpath([repo_root, target]) != repo_root:
        raise ValueError("Pfad liegt außerhalb des Projekts.")
    return target


def project_relpath(path: str) -> str:
    repo_root = project_root_folder()
    rel = os.path.relpath(os.path.abspath(path), repo_root).replace("\\", "/")
    return "" if rel == "." else rel


def _is_protected_project_path(path: str) -> bool:
    rel = project_relpath(path)
    protected = {"", ".git"}
    return rel in protected or rel.startswith(".git/")


def _format_file_size(size: int) -> str:
    value = float(max(0, int(size or 0)))
    units = ["B", "KB", "MB", "GB"]
    unit = units[0]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            break
        value /= 1024
    if unit == "B":
        return f"{int(value)} {unit}"
    return f"{value:.1f} {unit}"


def project_file_shortcuts() -> list[dict]:
    shortcuts: list[dict] = []
    candidates = [
        ("Projekt", ""),
        ("App", "app"),
        ("Themes", "app/templates/themes"),
        ("Static", "app/static"),
        ("Uploads", "app/static"),
        ("Instance", "instance"),
        ("Backups", "backups"),
    ]
    seen = set()
    for label, rel in candidates:
        try:
            abs_path = resolve_project_path(rel)
        except ValueError:
            continue
        if not os.path.exists(abs_path):
            continue
        key = project_relpath(abs_path)
        if key in seen:
            continue
        seen.add(key)
        shortcuts.append({"label": label, "path": key})
    return shortcuts


GOOGLE_DRIVE_BACKUP_FOLDER_NAME = "Photo Voting Contest Backups"
GOOGLE_DRIVE_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/drive.file",
]


def google_drive_client_id() -> str:
    return str(get_app_setting("google_drive_client_id", "") or "").strip()


def google_drive_client_secret() -> str:
    return str(get_app_setting("google_drive_client_secret", "") or "").strip()


def google_drive_folder_id() -> str:
    return str(get_app_setting("google_drive_folder_id", "") or "").strip()


def google_drive_connected_email() -> str:
    return str(get_app_setting("google_drive_connected_email", "") or "").strip()


def google_drive_tokens() -> dict:
    raw = str(get_app_setting("google_drive_oauth_tokens", "") or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def google_drive_is_configured() -> bool:
    return bool(google_drive_client_id() and google_drive_client_secret())


def google_drive_is_connected() -> bool:
    return bool(google_drive_tokens())


def save_google_drive_oauth_settings(client_id: str, client_secret: str) -> None:
    set_app_setting("google_drive_client_id", (client_id or "").strip())
    set_app_setting("google_drive_client_secret", (client_secret or "").strip())


def clear_google_drive_connection() -> None:
    set_app_setting("google_drive_oauth_tokens", "")
    set_app_setting("google_drive_connected_email", "")
    set_app_setting("google_drive_folder_id", "")


def _google_oauth_client_config() -> dict:
    client_id = google_drive_client_id()
    client_secret = google_drive_client_secret()
    if not client_id or not client_secret:
        raise RuntimeError("Google OAuth Client-ID und Secret sind noch nicht hinterlegt.")
    return {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [url_for("main.google_drive_oauth_callback", _external=True)],
        }
    }


def _save_google_drive_credentials(credentials) -> None:
    payload = {
        "token": credentials.token,
        "refresh_token": credentials.refresh_token,
        "token_uri": credentials.token_uri,
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "scopes": list(credentials.scopes or []),
    }
    set_app_setting("google_drive_oauth_tokens", json.dumps(payload))


def _google_drive_credentials():
    tokens = google_drive_tokens()
    if not tokens:
        raise RuntimeError("Google Drive ist aktuell nicht verbunden.")
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
    except ImportError as exc:
        raise RuntimeError("Google-Drive-Abhängigkeiten fehlen. Bitte Requirements installieren.") from exc

    credentials = Credentials.from_authorized_user_info(tokens, scopes=GOOGLE_DRIVE_SCOPES)
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        _save_google_drive_credentials(credentials)
    return credentials


def _google_drive_client():
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError("Google-Drive-Abhängigkeiten fehlen. Bitte Requirements installieren.") from exc
    return build("drive", "v3", credentials=_google_drive_credentials(), cache_discovery=False)


def ensure_google_drive_backup_folder(service) -> str:
    folder_id = google_drive_folder_id()
    if folder_id:
        try:
            folder = service.files().get(fileId=folder_id, fields="id,name,mimeType,trashed").execute()
            if folder and folder.get("mimeType") == "application/vnd.google-apps.folder" and not folder.get("trashed"):
                return folder_id
        except Exception:
            pass

    safe_folder_name = GOOGLE_DRIVE_BACKUP_FOLDER_NAME.replace("'", "\\'")
    query = (
        "mimeType = 'application/vnd.google-apps.folder' and trashed = false "
        f"and name = '{safe_folder_name}'"
    )
    response = service.files().list(
        q=query,
        pageSize=1,
        fields="files(id,name)",
        spaces="drive",
    ).execute()
    files = response.get("files", [])
    if files:
        folder_id = str(files[0].get("id") or "").strip()
        if folder_id:
            set_app_setting("google_drive_folder_id", folder_id)
            return folder_id

    metadata = {
        "name": GOOGLE_DRIVE_BACKUP_FOLDER_NAME,
        "mimeType": "application/vnd.google-apps.folder",
    }
    created = service.files().create(body=metadata, fields="id,name").execute()
    folder_id = str(created.get("id") or "").strip()
    if not folder_id:
        raise RuntimeError("Google-Drive-Ordner konnte nicht erstellt werden.")
    set_app_setting("google_drive_folder_id", folder_id)
    return folder_id


def _runtime_backup_sources() -> list[str]:
    repo_root = os.path.dirname(current_app.root_path)
    sources: list[str] = []

    for relative in ("instance", ".env"):
        target = os.path.join(repo_root, relative)
        if os.path.exists(target):
            sources.append(target)

    static_root = os.path.join(current_app.root_path, "static")
    if os.path.isdir(static_root):
        for name in sorted(os.listdir(static_root)):
            full = os.path.join(static_root, name)
            if not os.path.isdir(full):
                continue
            if name.startswith(("uploads", "assets", "stickers")):
                sources.append(full)
    return sources


def list_runtime_backups(limit: int = 10) -> list[dict]:
    folder = backup_root_folder()
    rows = []
    for name in os.listdir(folder):
        if not (name.startswith("photo_contest_backup_") and (name.endswith(".tar.gz") or name.endswith(".zip"))):
            continue
        full = os.path.join(folder, name)
        try:
            stat = os.stat(full)
        except OSError:
            continue
        rows.append(
            {
                "filename": name,
                "size": int(stat.st_size),
                "modified_at": datetime.fromtimestamp(stat.st_mtime),
            }
        )
    rows.sort(key=lambda item: item["modified_at"], reverse=True)
    return rows[: max(1, int(limit or 10))]


def list_google_drive_backups(limit: int = 10) -> tuple[list[dict], str | None]:
    if not google_drive_is_connected():
        return ([], None)
    try:
        service = _google_drive_client()
        folder_id = ensure_google_drive_backup_folder(service)
        query = f"'{folder_id}' in parents and trashed = false and name contains 'photo_contest_backup_'"
        response = service.files().list(
            q=query,
            pageSize=max(1, int(limit or 10)),
            orderBy="modifiedTime desc",
            fields="files(id,name,size,modifiedTime,webViewLink)",
        ).execute()
        rows = []
        for item in response.get("files", []):
            rows.append(
                {
                    "id": item.get("id"),
                    "filename": item.get("name"),
                    "size": int(item.get("size") or 0),
                    "modified_at": datetime.fromisoformat(str(item.get("modifiedTime") or "").replace("Z", "+00:00")) if item.get("modifiedTime") else None,
                    "web_view_link": item.get("webViewLink") or "",
                }
            )
        return (rows, None)
    except Exception as exc:
        return ([], str(exc))


def delete_runtime_backup(filename: str) -> tuple[bool, str]:
    safe_name = os.path.basename(filename or "").strip()
    if not safe_name or safe_name != filename:
        return False, "Ungültiger Backup-Dateiname."
    backup_path = os.path.join(backup_root_folder(), safe_name)
    if not os.path.isfile(backup_path):
        return False, "Backup-Datei wurde nicht gefunden."
    try:
        os.remove(backup_path)
        return True, f"Backup gelöscht: {safe_name}"
    except OSError as exc:
        return False, f"Backup konnte nicht gelöscht werden: {exc}"


def delete_google_drive_backup(file_id: str, filename: str = "") -> tuple[bool, str]:
    file_id = str(file_id or "").strip()
    if not file_id:
        return False, "Google-Drive-Datei fehlt."
    try:
        service = _google_drive_client()
        service.files().delete(fileId=file_id).execute()
        label = str(filename or file_id).strip()
        return True, f"Drive-Backup gelöscht: {label}"
    except Exception as exc:
        return False, str(exc)


def create_runtime_backup(keep_last: int = 10, label: str = "manual") -> str | None:
    repo_root = os.path.dirname(current_app.root_path)
    folder = backup_root_folder()
    sources = _runtime_backup_sources()
    if not sources:
        return None

    safe_label = _slugify(label or "manual")
    filename = f"photo_contest_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe_label}.zip"
    backup_path = os.path.join(folder, filename)

    with zipfile.ZipFile(backup_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source in sources:
            if os.path.isdir(source):
                for root, _, files in os.walk(source):
                    for file_name in files:
                        full = os.path.join(root, file_name)
                        archive.write(full, arcname=os.path.relpath(full, repo_root))
            elif os.path.isfile(source):
                archive.write(source, arcname=os.path.relpath(source, repo_root))

    backups = list_runtime_backups(limit=max(int(keep_last or 10), 50))
    if keep_last and keep_last > 0:
        for row in backups[keep_last:]:
            try:
                os.remove(os.path.join(folder, row["filename"]))
            except OSError:
                pass
    return filename


def _safe_backup_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    allowed_prefixes = ("instance/", "app/static/", ".env")
    members: list[tarfile.TarInfo] = []
    for member in archive.getmembers():
        name = str(member.name or "").replace("\\", "/").lstrip("./")
        if not name or name.startswith("/") or ".." in name.split("/"):
            continue
        if name == ".env" or name.startswith(allowed_prefixes):
            members.append(member)
    return members


def restore_runtime_backup(filename: str) -> tuple[bool, str]:
    safe_name = os.path.basename(filename or "").strip()
    if not safe_name or safe_name != filename:
        return False, "Ungültiger Backup-Dateiname."

    backup_path = os.path.join(backup_root_folder(), safe_name)
    if not os.path.isfile(backup_path):
        return False, "Backup-Datei wurde nicht gefunden."

    try:
        create_runtime_backup(keep_last=10, label="pre-restore")
        close_db()

        repo_root = os.path.dirname(current_app.root_path)
        static_root = os.path.join(current_app.root_path, "static")

        for relative in ("instance", ".env"):
            target = os.path.join(repo_root, relative)
            if os.path.isdir(target):
                shutil.rmtree(target, ignore_errors=True)
            elif os.path.isfile(target):
                try:
                    os.remove(target)
                except OSError:
                    pass

        if os.path.isdir(static_root):
            for name in os.listdir(static_root):
                if not name.startswith(("uploads", "assets", "stickers")):
                    continue
                full = os.path.join(static_root, name)
                if os.path.isdir(full):
                    shutil.rmtree(full, ignore_errors=True)
                elif os.path.isfile(full):
                    try:
                        os.remove(full)
                    except OSError:
                        pass

        if safe_name.endswith(".zip"):
            with zipfile.ZipFile(backup_path, "r") as archive:
                members = []
                for name in archive.namelist():
                    normalized = str(name or "").replace("\\", "/").lstrip("./")
                    if not normalized or normalized.startswith("/") or ".." in normalized.split("/"):
                        continue
                    if normalized == ".env" or normalized.startswith(("instance/", "app/static/")):
                        members.append(name)
                if not members:
                    return False, "Backup enthält keine wiederherstellbaren Daten."
                for member in members:
                    archive.extract(member, path=repo_root)
        else:
            with tarfile.open(backup_path, "r:gz") as archive:
                members = _safe_backup_members(archive)
                if not members:
                    return False, "Backup enthält keine wiederherstellbaren Daten."
                archive.extractall(path=repo_root, members=members)

        os.makedirs(current_app.instance_path, exist_ok=True)
        os.makedirs(current_app.config["UPLOAD_FOLDER"], exist_ok=True)
        return True, f"Backup {safe_name} wurde wiederhergestellt."
    except Exception as exc:
        return False, f"Restore fehlgeschlagen: {exc}"


def upload_backup_to_google_drive(filename: str) -> tuple[bool, str]:
    safe_name = os.path.basename(filename or "").strip()
    backup_path = os.path.join(backup_root_folder(), safe_name)
    if not os.path.isfile(backup_path):
        return False, "Backup-Datei wurde lokal nicht gefunden."
    try:
        service = _google_drive_client()
        from googleapiclient.http import MediaFileUpload

        metadata = {"name": safe_name, "parents": [ensure_google_drive_backup_folder(service)]}
        media = MediaFileUpload(backup_path, resumable=True)
        service.files().create(body=metadata, media_body=media, fields="id,name").execute()
        return True, f"Backup {safe_name} wurde zu Google Drive hochgeladen."
    except Exception as exc:
        return False, f"Google-Drive-Upload fehlgeschlagen: {exc}"


def download_google_drive_backup(file_id: str, filename: str) -> tuple[bool, str]:
    safe_name = os.path.basename(filename or "").strip()
    if not file_id or not safe_name:
        return False, "Ungültige Google-Drive-Datei."
    try:
        service = _google_drive_client()
        from googleapiclient.http import MediaIoBaseDownload

        request_obj = service.files().get_media(fileId=file_id)
        target_path = os.path.join(backup_root_folder(), safe_name)
        with open(target_path, "wb") as target:
            downloader = MediaIoBaseDownload(target, request_obj)
            done = False
            while not done:
                _, done = downloader.next_chunk()
        return True, safe_name
    except Exception as exc:
        return False, f"Google-Drive-Download fehlgeschlagen: {exc}"


def waiting_text_for_year(year: int) -> str:
    c = get_contest(year)
    if not c:
        return "Die Abstimmung läuft noch. Ergebnisse werden nach Freigabe veröffentlicht."
    txt = (c.get("waiting_text") or "").strip()
    return txt or "Die Abstimmung läuft noch. Ergebnisse werden nach Freigabe veröffentlicht."


@bp.route("/admin/google-drive/connect")
def google_drive_oauth_connect():
    if not session.get("admin"):
        return redirect(url_for("main.login"))
    if not google_drive_is_configured():
        flash("Bitte zuerst Google Client-ID und Client Secret speichern.", "warning")
        return redirect(url_for("main.admin_settings"))
    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError:
        flash("Google OAuth Abhängigkeiten fehlen. Bitte Requirements installieren.", "danger")
        return redirect(url_for("main.admin_settings"))

    flow = Flow.from_client_config(
        _google_oauth_client_config(),
        scopes=GOOGLE_DRIVE_SCOPES,
        redirect_uri=url_for("main.google_drive_oauth_callback", _external=True),
    )
    authorization_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    session["google_drive_oauth_state"] = state
    return redirect(authorization_url)


@bp.route("/admin/google-drive/callback")
def google_drive_oauth_callback():
    if not session.get("admin"):
        return redirect(url_for("main.login"))
    try:
        from google_auth_oauthlib.flow import Flow
        from googleapiclient.discovery import build
    except ImportError:
        flash("Google OAuth Abhängigkeiten fehlen. Bitte Requirements installieren.", "danger")
        return redirect(url_for("main.admin_settings"))

    state = session.get("google_drive_oauth_state")
    if not state:
        flash("Google OAuth Status fehlt. Bitte die Verbindung erneut starten.", "warning")
        return redirect(url_for("main.admin_settings"))

    try:
        flow = Flow.from_client_config(
            _google_oauth_client_config(),
            scopes=GOOGLE_DRIVE_SCOPES,
            state=state,
            redirect_uri=url_for("main.google_drive_oauth_callback", _external=True),
        )
        flow.fetch_token(authorization_response=request.url)
        credentials = flow.credentials
        _save_google_drive_credentials(credentials)

        oauth2 = build("oauth2", "v2", credentials=credentials, cache_discovery=False)
        profile = oauth2.userinfo().get().execute()
        set_app_setting("google_drive_connected_email", str(profile.get("email") or "").strip())

        drive = _google_drive_client()
        ensure_google_drive_backup_folder(drive)
        session.pop("google_drive_oauth_state", None)
        flash("Google Drive wurde erfolgreich verbunden.", "success")
    except Exception as exc:
        flash(f"Google-Drive-Verbindung fehlgeschlagen: {exc}", "danger")
    return redirect(url_for("main.admin_settings"))


def is_published(year: int) -> bool:
    c = get_contest(year)
    return bool(c and int(c.get("published") or 0) == 1)


def set_published(year: int, published: bool):
    db = get_db()
    db.execute("UPDATE contests SET published = ? WHERE id = ?", (1 if published else 0, year))
    db.commit()


def themed_template(kind: str, contest: dict | None) -> str:
    theme_id = (contest or {}).get("theme_id") or "default"
    if theme_id == "legacy_2026":
        theme_id = "casino"
    if session.get("admin"):
        preview = (request.args.get("theme_preview") or "").strip().lower()
        if preview:
            theme_id = preview
    themed = os.path.join(current_app.root_path, "templates", "themes", str(theme_id), f"{kind}.html")
    if os.path.exists(themed):
        return f"themes/{theme_id}/{kind}.html"
    return f"{kind}_generic.html"


def _clone_contest_setup(source_contest_id: int, new_contest_id: int) -> None:
    db = get_db()
    now = datetime.now().isoformat()

    vote_rows = db.execute(
        """
        SELECT opt_key, label, icon, display_type, image_filename, value, unique_per_user, exclusive_group, is_special, active, sort_order
        FROM vote_options
        WHERE contest_id = ?
        ORDER BY sort_order ASC, id ASC
        """,
        (source_contest_id,),
    ).fetchall()
    for row in vote_rows:
        db.execute(
            """
            INSERT INTO vote_options
                (contest_year, contest_id, opt_key, label, icon, display_type, image_filename, value, unique_per_user, exclusive_group, is_special, active, sort_order, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_contest_id,
                new_contest_id,
                row["opt_key"],
                row["label"],
                row["icon"],
                row["display_type"] or "icon",
                row["image_filename"],
                row["value"] or 1,
                row["unique_per_user"] or 0,
                row["exclusive_group"],
                row["is_special"] or 0,
                row["active"] if row["active"] is not None else 1,
                row["sort_order"] or 0,
                now,
            ),
        )

    cat_rows = db.execute(
        """
        SELECT reaction_key, label, display_type, icon, image_filename, points, active, sort_order
        FROM contest_reaction_options
        WHERE contest_id = ?
        ORDER BY sort_order ASC, id ASC
        """,
        (source_contest_id,),
    ).fetchall()
    for row in cat_rows:
        db.execute(
            """
            INSERT INTO contest_reaction_options
                (contest_id, reaction_key, label, display_type, icon, image_filename, points, active, sort_order, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_contest_id,
                row["reaction_key"],
                row["label"],
                row["display_type"] or "icon",
                row["icon"],
                row["image_filename"],
                int(row["points"] or 0),
                row["active"] if row["active"] is not None else 1,
                row["sort_order"] or 0,
                now,
            ),
        )


def _count_used_actions(voted_rows, vote_mode: str, max_actions: int) -> int:
    used_keys = {(row["vote_option_key"] or "").lower() for row in voted_rows}
    if "all_in" in used_keys:
        return max_actions
    if vote_mode == "unique_options":
        return max_actions if "all_in" in used_keys else len(used_keys)
    return len(voted_rows)


def duel_points_each() -> int:
    return 5


def _calc_ranking(contest_id: int, visible_only: bool = True, include_excluded: bool = False) -> list[dict]:
    db = get_db()
    images = db.execute(
        "SELECT id, filename, uploader, description, uploaded_at, visible, exclude_from_results, include_in_category_results, contest_id, contest_year FROM images WHERE contest_id = ? "
        + ("AND visible = 1 " if visible_only else "")
        + "ORDER BY uploaded_at DESC, id DESC",
        (contest_id,),
    ).fetchall()
    items = [dict(r) for r in images]
    if not items:
        return []
    if not include_excluded:
        items = [item for item in items if not int(item.get("exclude_from_results") or 0)]
        if not items:
            return []

    by_id = {int(i["id"]): i for i in items}
    reaction_options = get_reaction_options(contest_id)
    reaction_points = {str(opt["reaction_key"]).strip().lower(): int(opt.get("points") or 0) for opt in reaction_options}
    reaction_include_in_total = {str(opt["reaction_key"]).strip().lower(): int(opt.get("include_in_total", 1) or 0) for opt in reaction_options}
    for i in items:
        i.update({
            "vote_count": 0,
            "vote_points": 0,
            "duel_vote_count": 0,
            "duel_points": 0,
            "reaction_points_total": 0,
            "reaction_points_in_total": 0,
            "weighted_score": 0,
            "reaction_breakdown": {str(opt["reaction_key"]).strip().lower(): 0 for opt in reaction_options},
            "reaction_points_breakdown": {str(opt["reaction_key"]).strip().lower(): 0 for opt in reaction_options},
        })

    vote_option_points = get_vote_option_points_map(contest_id)

    vote_rows = db.execute(
        "SELECT image_id, vote_option_key, COUNT(*) AS cnt, COALESCE(SUM(vote_value), 0) AS raw_points FROM votes WHERE contest_id = ? GROUP BY image_id, vote_option_key",
        (contest_id,),
    ).fetchall()
    for row in vote_rows:
        image_id = int(row["image_id"] or 0)
        if image_id not in by_id:
            continue
        cnt = int(row["cnt"] or 0)
        raw_points = int(row["raw_points"] or 0)
        key = (row["vote_option_key"] or "").strip()
        effective_points = cnt * int(vote_option_points.get(key, raw_points // max(cnt, 1)))
        by_id[image_id]["vote_count"] += cnt
        by_id[image_id]["vote_points"] += effective_points
        by_id[image_id]["weighted_score"] += effective_points

    duel_rows = db.execute(
        "SELECT image_id, COUNT(*) AS cnt FROM duel_votes WHERE contest_id = ? GROUP BY image_id",
        (contest_id,),
    ).fetchall()
    duel_points = duel_points_each()
    for row in duel_rows:
        image_id = int(row["image_id"] or 0)
        if image_id not in by_id:
            continue
        cnt = int(row["cnt"] or 0)
        total = cnt * duel_points
        by_id[image_id]["duel_vote_count"] = cnt
        by_id[image_id]["duel_points"] = total
        by_id[image_id]["weighted_score"] += total

    reaction_rows = db.execute(
        "SELECT image_id, reaction_type, COUNT(*) AS cnt FROM reactions WHERE contest_id = ? GROUP BY image_id, reaction_type",
        (contest_id,),
    ).fetchall()
    for row in reaction_rows:
        image_id = int(row["image_id"] or 0)
        if image_id not in by_id:
            continue
        key = (row["reaction_type"] or "").strip().lower()
        cnt = int(row["cnt"] or 0)
        by_id[image_id]["reaction_breakdown"][key] = cnt
        reaction_total = cnt * int(reaction_points.get(key, 0))
        by_id[image_id]["reaction_points_breakdown"][key] = reaction_total
        by_id[image_id]["reaction_points_total"] += reaction_total
        if int(reaction_include_in_total.get(key, 1)):
            by_id[image_id]["reaction_points_in_total"] += reaction_total
            by_id[image_id]["weighted_score"] += reaction_total

    items.sort(
        key=lambda x: (
            0 if int(x.get("exclude_from_results") or 0) else 1,
            int(x.get("weighted_score") or 0),
            int(x.get("vote_points") or 0),
            int(x.get("vote_count") or 0),
        ),
        reverse=True,
    )
    rank = 0
    for item in items:
        if int(item.get("exclude_from_results") or 0):
            item["rank"] = None
            continue
        rank += 1
        item["rank"] = rank
    return items


def _admin_ranking_details(contest_id: int) -> dict[int, dict]:
    db = get_db()
    vote_points_map = get_vote_option_points_map(contest_id)
    reaction_meta = {
        str(opt["reaction_key"]).strip().lower(): {
            "label": str(opt.get("label") or opt.get("reaction_key") or "").strip(),
            "points": int(opt.get("points") or 0),
            "include_in_total": 1 if opt.get("include_in_total", 1) else 0,
        }
        for opt in get_reaction_options(contest_id)
    }
    details: dict[int, dict] = {}

    vote_rows = db.execute(
        """
        SELECT image_id, voter_session_id, vote_option_key, vote_label, vote_value, created_at
        FROM votes
        WHERE contest_id = ?
        ORDER BY image_id ASC, COALESCE(created_at, '') DESC, vote_value DESC, voter_session_id ASC
        """,
        (contest_id,),
    ).fetchall()
    for row in vote_rows:
        image_id = int(row["image_id"] or 0)
        detail = details.setdefault(image_id, {"vote_breakdown": {}, "vote_history": [], "reaction_breakdown": {}, "reaction_history": [], "duel_summary": {"count": 0, "points_each": duel_points_each(), "total_points": 0}, "duel_history": []})
        vote_key = str(row["vote_option_key"] or "").strip()
        vote_label = str(row["vote_label"] or vote_key or "Vote").strip()
        points = int(vote_points_map.get(vote_key, row["vote_value"] or 0))
        vote_bucket = detail["vote_breakdown"].setdefault(
            vote_key,
            {"key": vote_key, "label": vote_label, "count": 0, "points_each": points, "total_points": 0},
        )
        vote_bucket["count"] += 1
        vote_bucket["total_points"] += points
        voter = str(row["voter_session_id"] or "").strip()
        detail["vote_history"].append({
            "voter": voter[:8] if voter else "anon",
            "label": vote_label,
            "points": points,
            "created_at": str(row["created_at"] or "").strip(),
        })

    reaction_rows = db.execute(
        """
        SELECT image_id, reaction_type, voter_session_id, created_at
        FROM reactions
        WHERE contest_id = ?
        ORDER BY image_id ASC, COALESCE(created_at, '') DESC, reaction_type ASC
        """,
        (contest_id,),
    ).fetchall()
    for row in reaction_rows:
        image_id = int(row["image_id"] or 0)
        detail = details.setdefault(image_id, {"vote_breakdown": {}, "vote_history": [], "reaction_breakdown": {}, "reaction_history": [], "duel_summary": {"count": 0, "points_each": duel_points_each(), "total_points": 0}, "duel_history": []})
        reaction_key = str(row["reaction_type"] or "").strip().lower()
        meta = reaction_meta.get(reaction_key, {"label": reaction_key, "points": 0})
        reaction_bucket = detail["reaction_breakdown"].setdefault(
            reaction_key,
            {
                "key": reaction_key,
                "label": meta["label"],
                "count": 0,
                "points_each": int(meta["points"] or 0),
                "total_points": 0,
                "include_in_total": 1 if meta.get("include_in_total", 1) else 0,
            },
        )
        reaction_bucket["count"] += 1
        reaction_bucket["total_points"] += int(meta["points"] or 0)
        voter = str(row["voter_session_id"] or "").strip()
        detail["reaction_history"].append({
            "voter": voter[:8] if voter else "anon",
            "label": meta["label"],
            "points": int(meta["points"] or 0),
            "include_in_total": 1 if meta.get("include_in_total", 1) else 0,
            "created_at": str(row["created_at"] or "").strip(),
        })

    duel_rows = db.execute(
        """
        SELECT image_id, voter_session_id, created_at
        FROM duel_votes
        WHERE contest_id = ?
        ORDER BY image_id ASC, COALESCE(created_at, '') DESC, voter_session_id ASC
        """,
        (contest_id,),
    ).fetchall()
    duel_points = duel_points_each()
    for row in duel_rows:
        image_id = int(row["image_id"] or 0)
        detail = details.setdefault(image_id, {"vote_breakdown": {}, "vote_history": [], "reaction_breakdown": {}, "reaction_history": [], "duel_summary": {"count": 0, "points_each": duel_points, "total_points": 0}, "duel_history": []})
        detail["duel_summary"]["count"] += 1
        detail["duel_summary"]["total_points"] += duel_points
        voter = str(row["voter_session_id"] or "").strip()
        detail["duel_history"].append({
            "voter": voter[:8] if voter else "anon",
            "label": "Duell",
            "points": duel_points,
            "created_at": str(row["created_at"] or "").strip(),
        })

    for detail in details.values():
        detail["vote_breakdown"] = sorted(detail["vote_breakdown"].values(), key=lambda item: (-item["total_points"], item["label"]))
        detail["reaction_breakdown"] = sorted(detail["reaction_breakdown"].values(), key=lambda item: (-item["total_points"], item["label"]))

    return details


def _calc_category_rankings(contest_id: int, visible_only: bool = True, include_excluded: bool = False) -> dict[str, list[dict]]:
    db = get_db()
    images = db.execute(
        "SELECT id, filename, uploader, description, uploaded_at, visible, exclude_from_results, include_in_category_results, contest_id, contest_year FROM images WHERE contest_id = ? "
        + ("AND visible = 1 " if visible_only else "")
        + "ORDER BY uploaded_at DESC, id DESC",
        (contest_id,),
    ).fetchall()
    base_items = [dict(r) for r in images]
    if not include_excluded:
        base_items = [item for item in base_items if int(item.get("include_in_category_results", 1) or 0)]

    if not base_items:
        return {}

    reaction_options = [dict(opt) for opt in get_reaction_options(contest_id)]
    rankings: dict[str, list[dict]] = {}
    reactions = db.execute(
        "SELECT image_id, reaction_type, COUNT(*) AS cnt FROM reactions WHERE contest_id = ? GROUP BY image_id, reaction_type",
        (contest_id,),
    ).fetchall()
    grouped_counts: dict[tuple[int, str], int] = {
        (int(row["image_id"] or 0), str(row["reaction_type"] or "").strip().lower()): int(row["cnt"] or 0)
        for row in reactions
    }

    for opt in reaction_options:
        key = str(opt.get("reaction_key") or "").strip().lower()
        if not key:
            continue
        rows: list[dict] = []
        points_each = int(opt.get("points") or 0)
        for item in base_items:
            image_id = int(item["id"])
            count = grouped_counts.get((image_id, key), 0)
            total_points = count * points_each
            row = dict(item)
            row["category_key"] = key
            row["category_label"] = opt.get("label") or key
            row["category_count"] = count
            row["category_points"] = total_points
            rows.append(row)
        rows.sort(
            key=lambda x: (
                0 if int(x.get("include_in_category_results", 1) or 0) else 1,
                int(x.get("category_points") or 0),
                int(x.get("category_count") or 0),
                str(x.get("uploader") or "").lower(),
            ),
            reverse=True,
        )
        rank = 0
        for row in rows:
            if not int(row.get("include_in_category_results", 1) or 0):
                row["rank"] = None
                continue
            rank += 1
            row["rank"] = rank
        rankings[key] = rows

    duel_counts = {
        int(row["image_id"] or 0): int(row["cnt"] or 0)
        for row in db.execute(
            "SELECT image_id, COUNT(*) AS cnt FROM duel_votes WHERE contest_id = ? GROUP BY image_id",
            (contest_id,),
        ).fetchall()
    }
    duel_rows: list[dict] = []
    points_each = duel_points_each()
    for item in base_items:
        image_id = int(item["id"])
        count = duel_counts.get(image_id, 0)
        total_points = count * points_each
        row = dict(item)
        row["category_key"] = "duelmaster"
        row["category_label"] = "Duelmaster"
        row["category_count"] = count
        row["category_points"] = total_points
        duel_rows.append(row)
    duel_rows.sort(
        key=lambda x: (
            0 if int(x.get("include_in_category_results", 1) or 0) else 1,
            int(x.get("category_points") or 0),
            int(x.get("category_count") or 0),
            str(x.get("uploader") or "").lower(),
        ),
        reverse=True,
    )
    rank = 0
    for row in duel_rows:
        if not int(row.get("include_in_category_results", 1) or 0):
            row["rank"] = None
            continue
        rank += 1
        row["rank"] = rank
    rankings["duelmaster"] = duel_rows
    return rankings


@bp.route("/")
def root():
    contest = get_contest(current_year())
    if contest and contest.get("slug"):
        return redirect(url_for("main.contest_slug", slug=contest["slug"]))
    return redirect(url_for("main.contest_year", year=current_year()))


@bp.route("/media/<int:year>/<path:filename>")
def media_year(year: int, filename: str):
    return send_from_directory(upload_folder_for_year(year), filename)


@bp.route("/sticker/<int:year>/<path:filename>")
def sticker_year(year: int, filename: str):
    return send_from_directory(sticker_folder_for_year(year), filename)


@bp.route("/asset/<int:year>/<path:filename>")
def asset_year(year: int, filename: str):
    return send_from_directory(asset_folder_for_year(year, create=True), filename)


def _render_contest_view(year: int):
    contest = get_contest(year)
    if not contest:
        return redirect(url_for("main.root"))
    preview = (request.args.get("theme_preview") or "").strip()
    force_vote_preview = bool(session.get("admin")) and (bool(preview) or request.args.get("preview") == "1")
    if year != current_year() and not force_vote_preview:
        if preview:
            return redirect(url_for("main.public_results_year", year=year, theme_preview=preview))
        return redirect(url_for("main.public_results_year", year=year))

    db = get_db()
    images = db.execute("SELECT * FROM images WHERE visible = 1 AND contest_id = ? ORDER BY uploaded_at DESC", (year,)).fetchall()
    voter_session_id = request.cookies.get("voter_session_id")
    year_cfg = get_year_settings(year)
    max_actions = int(year_cfg.get("max_actions", 1))

    voted = db.execute(
        "SELECT image_id, vote_value, vote_label, vote_option_key FROM votes WHERE voter_session_id = ? AND contest_id = ?",
        (voter_session_id, year),
    ).fetchall()
    voted_ids = [row["image_id"] for row in voted]
    user_bets = {str(row["image_id"]): {"vote_value": row["vote_value"] or 1, "vote_label": row["vote_label"] or "", "vote_option_key": row["vote_option_key"] or ""} for row in voted}

    vote_mode = (year_cfg.get("vote_mode") or "single_vote").strip().lower()
    votes_left = max(0, max_actions - _count_used_actions(voted, vote_mode, max_actions))

    user_reactions = {}
    if voter_session_id:
        reaction_rows = db.execute("SELECT image_id, reaction_type FROM reactions WHERE voter_session_id = ? AND contest_id = ?", (voter_session_id, year)).fetchall()
        for row in reaction_rows:
            user_reactions.setdefault(str(row["image_id"]), set()).add(row["reaction_type"])
    user_reactions_json = {
        image_id: sorted(list(reactions))
        for image_id, reactions in user_reactions.items()
    }

    return render_template(
        themed_template("contest", contest),
        contest=contest,
        images=images,
        voted_ids=voted_ids,
        votes_left=votes_left,
        year=year,
        available_years=[int(c["id"]) for c in list_contests()],
        voting_end_at=contest.get("end_at"),
        user_reactions=user_reactions,
        user_reactions_json=user_reactions_json,
        user_bets=user_bets,
        vote_options=get_vote_options(year),
        reaction_options=get_reaction_options(year),
        year_cfg=year_cfg,
    )


@bp.route("/contest/<int:year>")
def contest_year(year: int):
    return _render_contest_view(year)


@bp.route("/contest/<slug>")
def contest_slug(slug: str):
    contest = get_contest_by_slug(slug)
    if not contest:
        return redirect(url_for("main.root"))
    return _render_contest_view(int(contest["id"]))

@bp.route("/archive")
def archive():
    return render_template("archive.html", contests=list_contests())


def _render_public_waiting(year: int):
    contest = get_contest(year)
    if not contest:
        return redirect(url_for("main.root"))
    return render_template(themed_template("public_waiting", contest), year=year, contest=contest, waiting_text=waiting_text_for_year(year))


@bp.route("/public-waiting/<int:year>")
def public_waiting_preview(year: int):
    return _render_public_waiting(year)


@bp.route("/public-waiting/<slug>")
def public_waiting_slug(slug: str):
    contest = get_contest_by_slug(slug)
    if not contest:
        return redirect(url_for("main.root"))
    return _render_public_waiting(int(contest["id"]))


@bp.route("/duel/<int:year>")
def duel_year(year: int):
    if year != current_year():
        return redirect(url_for("main.public_results_year", year=year))
    db = get_db()
    candidates = db.execute("SELECT * FROM images WHERE visible = 1 AND contest_id = ? ORDER BY RANDOM() LIMIT 3", (year,)).fetchall()
    if len(candidates) < 3:
        return redirect(url_for("main.contest_year", year=year))
    return render_template("duel.html", year=year, candidates=candidates)


def duel_spins_used(voter_session_id: str, year: int) -> int:
    db = get_db()
    return db.execute("SELECT COUNT(*) FROM duel_votes WHERE voter_session_id = ? AND contest_id = ?", (voter_session_id, year)).fetchone()[0]


@bp.route("/api/duel-state/<int:year>")
def duel_state(year: int):
    voter_session_id = (request.args.get("voter_session_id") or "").strip()
    if not voter_session_id:
        return jsonify(success=True, used=0, remaining=10)
    used = duel_spins_used(voter_session_id, year)
    return jsonify(success=True, used=used, remaining=max(0, 10 - used))


@bp.route("/api/duel-spin/<int:year>")
def duel_spin(year: int):
    voter_session_id = (request.args.get("voter_session_id") or "").strip()
    if voter_session_id and duel_spins_used(voter_session_id, year) >= 10:
        return jsonify(success=False, error="Keine Spins mehr übrig", remaining=0), 403
    db = get_db()
    rows = db.execute("SELECT id, filename, uploader, description FROM images WHERE visible = 1 AND contest_id = ? ORDER BY RANDOM() LIMIT 3", (year,)).fetchall()
    if len(rows) < 3:
        return jsonify(success=False, error="Nicht genug Bilder für Duel-Slot"), 400
    return jsonify(success=True, candidates=[{"id": r["id"], "filename": r["filename"], "uploader": r["uploader"] or "Unbekannt", "description": r["description"] or ""} for r in rows])


@bp.route("/api/duel-vote/<int:image_id>", methods=["POST"])
def duel_vote(image_id: int):
    payload = request.json or {}
    voter_session_id = (payload.get("voter_session_id") or "").strip()
    contest_year = int(payload.get("contest_year", current_year()))
    if not voter_session_id:
        return jsonify(success=False, error="Session fehlt"), 400
    if duel_spins_used(voter_session_id, contest_year) >= 10:
        return jsonify(success=False, error="Keine Spins mehr übrig", remaining=0), 403
    db = get_db()
    db.execute(
        "INSERT INTO duel_votes (image_id, voter_session_id, contest_year, contest_id, created_at) VALUES (?, ?, ?, ?, ?)",
        (image_id, voter_session_id, contest_year, contest_year, datetime.now().isoformat()),
    )
    db.commit()
    used_after = duel_spins_used(voter_session_id, contest_year)
    return jsonify(success=True, used=used_after, remaining=max(0, 10 - used_after))


@bp.route("/vote/<int:image_id>", methods=["POST"])
def vote(image_id: int):
    payload = request.json or {}
    voter_session_id = (payload.get("voter_session_id") or "").strip()
    contest_year = int(payload.get("contest_year", current_year()))
    vote_option_key = (payload.get("vote_option_key") or "").strip()
    if not voter_session_id:
        return jsonify(success=False, error="Session fehlt"), 400
    if not vote_option_key:
        return jsonify(success=False, error="vote_option_key fehlt"), 400

    db = get_db()
    year_cfg = get_year_settings(contest_year)
    vote_mode = (year_cfg.get("vote_mode") or "single_vote").strip().lower()
    max_actions = int(year_cfg.get("max_actions", 1))

    opt = get_vote_option_map(contest_year).get(vote_option_key)
    if not opt:
        return jsonify(success=False, error="Ungültige Vote-Option"), 400

    vote_value = int(get_vote_option_points_map(contest_year).get(vote_option_key, opt.get("value") or 1))
    vote_label = str(opt.get("label") or vote_option_key)
    unique_per_user = int(opt.get("unique_per_user") or 0)
    exclusive_group = (opt.get("exclusive_group") or "").strip().lower()

    vote_exists = db.execute("SELECT id, vote_option_key FROM votes WHERE image_id = ? AND voter_session_id = ? AND contest_id = ?", (image_id, voter_session_id, contest_year)).fetchone()
    all_votes = db.execute("SELECT id, image_id, vote_option_key FROM votes WHERE voter_session_id = ? AND contest_id = ?", (voter_session_id, contest_year)).fetchall()

    if vote_exists:
        db.execute("DELETE FROM votes WHERE id = ?", (vote_exists["id"],))
        db.commit()
        updated = db.execute("SELECT image_id, vote_option_key FROM votes WHERE voter_session_id = ? AND contest_id = ?", (voter_session_id, contest_year)).fetchall()
        used_count = _count_used_actions(updated, vote_mode, max_actions)
        return jsonify(success=True, removed_only=True, vote_count=len(updated), votes_left=max(0, max_actions - used_count))
    else:
        all_in_vote = next((r for r in all_votes if (r["vote_option_key"] or "") == "all_in"), None)
        if unique_per_user:
            opt_in_use = next((r for r in all_votes if (r["vote_option_key"] or "") == vote_option_key), None)
            if opt_in_use and (not vote_exists or int(opt_in_use["id"]) != int(vote_exists["id"])):
                return jsonify(success=False, error=f"Option {vote_label} wurde bereits benutzt"), 403

        if vote_option_key == "all_in" or exclusive_group == "allin":
            other_votes = len([v for v in all_votes if not vote_exists or int(v["id"]) != int(vote_exists["id"])])
            if other_votes > 0:
                return jsonify(success=False, error="All-in geht nur, wenn keine anderen Optionen gesetzt sind"), 403
        elif all_in_vote and (not vote_exists or int(all_in_vote["id"]) != int(vote_exists["id"])):
            return jsonify(success=False, error="All-in ist bereits gesetzt. Erst All-in entfernen."), 403

        if vote_mode == "single_vote":
            db.execute("DELETE FROM votes WHERE voter_session_id = ? AND contest_id = ?", (voter_session_id, contest_year))

        if vote_exists:
            db.execute("DELETE FROM votes WHERE id = ?", (vote_exists["id"],))

        remaining_votes = db.execute(
            "SELECT image_id, vote_option_key FROM votes WHERE voter_session_id = ? AND contest_id = ?",
            (voter_session_id, contest_year),
        ).fetchall()
        used_count = _count_used_actions(remaining_votes, vote_mode, max_actions)
        incoming_cost = max_actions if vote_option_key == "all_in" or exclusive_group == "allin" else 1
        if used_count + incoming_cost > max_actions:
            db.commit()
            return jsonify(success=False, error=f"Du hast das Limit ({max_actions}) erreicht"), 403

        db.execute(
            "INSERT INTO votes (image_id, voter_session_id, contest_year, contest_id, vote_option_key, vote_value, vote_label, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (image_id, voter_session_id, contest_year, contest_year, vote_option_key, vote_value, vote_label, datetime.now().isoformat()),
        )
        db.commit()

    updated = db.execute("SELECT image_id, vote_option_key FROM votes WHERE voter_session_id = ? AND contest_id = ?", (voter_session_id, contest_year)).fetchall()
    used_count = _count_used_actions(updated, vote_mode, max_actions)
    return jsonify(success=True, removed_only=False, vote_count=len(updated), votes_left=max(0, max_actions - used_count))


@bp.route("/api/voter-state/<int:year>")
def voter_state(year: int):
    voter_session_id = request.args.get("voter_session_id", "").strip()
    year_cfg = get_year_settings(year)
    max_actions = int(year_cfg.get("max_actions", 1))
    vote_mode = (year_cfg.get("vote_mode") or "single_vote").strip().lower()
    if not voter_session_id:
        return jsonify(voted_ids=[], vote_count=0, votes_left=max_actions, bets=[])
    db = get_db()
    voted = db.execute("SELECT image_id, vote_option_key, vote_label, vote_value FROM votes WHERE voter_session_id = ? AND contest_id = ?", (voter_session_id, year)).fetchall()
    voted_ids = [row["image_id"] for row in voted]
    bets = [{"image_id": row["image_id"], "vote_option_key": (row["vote_option_key"] or "").lower(), "vote_label": row["vote_label"] or "", "vote_value": row["vote_value"] or 1} for row in voted]
    used_count = _count_used_actions(voted, vote_mode, max_actions)
    return jsonify(voted_ids=voted_ids, vote_count=len(voted_ids), votes_left=max(0, max_actions - used_count), bets=bets, vote_mode=vote_mode, max_actions=max_actions)


@bp.route("/api/vote-options/<int:year>")
def api_vote_options(year: int):
    return jsonify(get_vote_options(year))


@bp.route("/api/reaction-options/<int:year>")
def api_reaction_options(year: int):
    return jsonify(get_reaction_options(year))


@bp.route("/api/reset-votes/<int:year>", methods=["POST"])
def reset_votes(year: int):
    payload = request.json or {}
    voter_session_id = (payload.get("voter_session_id") or "").strip()
    if not voter_session_id:
        return jsonify(success=False, error="Session fehlt"), 400
    db = get_db()
    db.execute("DELETE FROM votes WHERE voter_session_id = ? AND contest_id = ?", (voter_session_id, year))
    db.commit()
    return jsonify(success=True)


@bp.route("/react/<int:image_id>", methods=["POST"])
def react(image_id):
    payload = request.json or {}
    voter_session_id = (payload.get("voter_session_id") or "").strip()
    contest_year = int(payload.get("contest_year", current_year()))
    reaction_type = (payload.get("reaction_type") or "").strip().lower()
    if not voter_session_id:
        return jsonify(success=False, error="Session fehlt"), 400
    if reaction_type not in get_reaction_key_set(contest_year):
        return jsonify(success=False, error="Ungültige Reaktion"), 400

    db = get_db()
    exists = db.execute("SELECT id FROM reactions WHERE image_id = ? AND voter_session_id = ? AND reaction_type = ? AND contest_id = ?", (image_id, voter_session_id, reaction_type, contest_year)).fetchone()
    active = False
    if exists:
        db.execute("DELETE FROM reactions WHERE id = ?", (exists["id"],))
    else:
        db.execute(
            "INSERT INTO reactions (image_id, voter_session_id, reaction_type, contest_year, contest_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (image_id, voter_session_id, reaction_type, contest_year, contest_year, datetime.now().isoformat()),
        )
        active = True
    db.commit()

    count = db.execute("SELECT COUNT(*) FROM reactions WHERE image_id = ? AND contest_id = ? AND reaction_type = ?", (image_id, contest_year, reaction_type)).fetchone()[0]
    return jsonify(success=True, active=active, count=count, reaction_type=reaction_type)

@bp.route("/login", methods=["GET", "POST"])
def login():
    error_message = ""
    if request.method == "POST":
        if verify_admin_password(request.form["password"]):
            session["admin"] = True
            return redirect(url_for("main.upload"))
        error_message = "Passwort stimmt nicht."
    return render_template("login.html", default_admin_password=admin_uses_default_password(), error_message=error_message)


@bp.route("/logout")
def logout():
    session.pop("admin", None)
    return redirect(url_for("main.root"))


@bp.route("/upload", methods=["GET", "POST"])
def upload():
    if not session.get("admin"):
        return redirect(url_for("main.login"))

    db = get_db()
    year = int(request.args.get("contest_id", request.args.get("year", request.form.get("contest_year", current_year()))))

    if request.method == "POST" and "files" in request.files:
        files = request.files.getlist("files")
        folder = upload_folder_for_year(year)
        for file in files:
            if file and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                file.save(os.path.join(folder, filename))
                db.execute("INSERT INTO images (filename, uploaded_at, visible, contest_year, contest_id) VALUES (?, ?, ?, ?, ?)", (filename, datetime.now().isoformat(), 1, year, year))
        db.commit()
        return redirect(url_for("main.upload", year=year))

    images = db.execute("SELECT * FROM images WHERE contest_id = ? ORDER BY uploaded_at DESC", (year,)).fetchall()
    contests = list_contests()
    return render_template(
        "upload.html",
        images=images,
        current_year=current_year(),
        year=year,
        available_years=[int(c["id"]) for c in contests],
        contests=contests,
        current_contest=get_contest(year),
    )


@bp.route("/admin/settings", methods=["GET", "POST"])
def admin_settings():
    if not session.get("admin"):
        return redirect(url_for("main.login"))

    db = get_db()
    if request.method == "POST":
        action = (request.form.get("action") or "").strip()

        if action == "create_backup":
            filename = create_runtime_backup(keep_last=10, label="admin")
            if filename:
                flash(f"Backup erstellt: {filename}", "success")
            else:
                flash("Es waren noch keine Runtime-Daten für ein Backup vorhanden.", "warning")

        elif action == "restore_backup":
            backup_name = (request.form.get("backup_name") or "").strip()
            ok, message = restore_runtime_backup(backup_name)
            flash(message, "success" if ok else "danger")

        elif action == "delete_backup":
            backup_name = (request.form.get("backup_name") or "").strip()
            ok, message = delete_runtime_backup(backup_name)
            flash(message, "success" if ok else "danger")

        elif action == "save_google_drive":
            client_id = (request.form.get("google_drive_client_id") or "").strip()
            client_secret = (request.form.get("google_drive_client_secret") or "").strip()
            if client_id or client_secret:
                save_google_drive_oauth_settings(client_id or google_drive_client_id(), client_secret or google_drive_client_secret())
                flash("Google-OAuth-Konfiguration gespeichert.", "success")
            else:
                flash("Bitte Google Client-ID und Client Secret angeben.", "warning")

        elif action == "upload_backup_to_drive":
            backup_name = (request.form.get("backup_name") or "").strip()
            ok, message = upload_backup_to_google_drive(backup_name)
            flash(message, "success" if ok else "danger")

        elif action == "restore_drive_backup":
            file_id = (request.form.get("drive_file_id") or "").strip()
            backup_name = (request.form.get("backup_name") or "").strip()
            ok, download_result = download_google_drive_backup(file_id, backup_name)
            if ok:
                ok, message = restore_runtime_backup(download_result)
                flash(message, "success" if ok else "danger")
            else:
                flash(download_result, "danger")

        elif action == "download_drive_backup":
            file_id = (request.form.get("drive_file_id") or "").strip()
            backup_name = (request.form.get("backup_name") or "").strip()
            ok, message = download_google_drive_backup(file_id, backup_name)
            flash(f"Drive-Backup lokal gespeichert: {message}" if ok else message, "success" if ok else "danger")

        elif action == "delete_drive_backup":
            file_id = (request.form.get("drive_file_id") or "").strip()
            backup_name = (request.form.get("backup_name") or "").strip()
            ok, message = delete_google_drive_backup(file_id, backup_name)
            flash(message, "success" if ok else "danger")

        elif action == "disconnect_google_drive":
            clear_google_drive_connection()
            flash("Google Drive wurde getrennt.", "success")

        elif action == "save_admin_password":
            current_password = (request.form.get("current_admin_password") or "").strip()
            new_password = (request.form.get("new_admin_password") or "").strip()
            confirm_password = (request.form.get("confirm_admin_password") or "").strip()

            if len(new_password) < 4:
                flash("Das neue Admin-Passwort muss mindestens 4 Zeichen lang sein.", "warning")
            elif new_password != confirm_password:
                flash("Die neuen Passwort-Felder stimmen nicht überein.", "warning")
            elif not admin_uses_default_password() and not verify_admin_password(current_password):
                flash("Das aktuelle Admin-Passwort stimmt nicht.", "danger")
            else:
                save_admin_password(new_password)
                flash("Admin-Passwort gespeichert. Ab jetzt gilt nicht mehr der Default `admin123`.", "success")

        elif action == "create_contest":
            title = (request.form.get("title") or "").strip() or "Neuer Contest"
            start_at = (request.form.get("start_at") or "").strip()
            end_at = (request.form.get("end_at") or "").strip()
            slug = _slugify(request.form.get("slug") or _contest_slug_value(title, start_at, "contest"))
            vote_mode = (request.form.get("vote_mode") or "single_vote").strip()
            max_actions = int(request.form.get("max_actions") or 1)
            unit_name = (request.form.get("unit_name") or "Stimme").strip()
            unit_icon = (request.form.get("unit_icon") or "*").strip()
            theme_id = (request.form.get("theme_id") or "default").strip()
            waiting_text = (request.form.get("waiting_text") or "").strip()
            storage_key = _slugify(request.form.get("storage_key") or slug)

            db.execute(
                """
                INSERT INTO contests
                    (slug, title, start_at, end_at, vote_mode, max_actions, unit_name, unit_icon, theme_id, waiting_text, published, is_active, storage_key, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?)
                """,
                (slug, title, start_at or None, end_at or None, vote_mode, max_actions, unit_name, unit_icon, theme_id, waiting_text, storage_key, datetime.now().isoformat()),
            )
            contest_id = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])

            now = datetime.now().isoformat()
            db.execute(
                """
                INSERT INTO vote_options
                    (contest_year, contest_id, opt_key, label, icon, display_type, image_filename, value, unique_per_user, exclusive_group, is_special, active, sort_order, created_at)
                VALUES (?, ?, 'heart', 'Vote', '❤️', 'icon', NULL, 1, 0, NULL, 0, 1, 10, ?)
                ON CONFLICT(contest_year, opt_key) DO NOTHING
                """,
                (contest_id, contest_id, now),
            )

            db.commit()
            _import_theme_vote_presets(contest_id, theme_id)
            _import_theme_reaction_presets(contest_id, theme_id)
            upload_folder_for_year(contest_id)
            sticker_folder_for_year(contest_id, create=True)

        elif action == "clone_contest":
            source_id = int(request.form.get("contest_id") or 0)
            source = get_contest(source_id)
            if source:
                now = datetime.now()
                new_slug = _slugify(f"{source.get('slug') or ('contest-' + str(source_id))}-copy-{now.strftime('%Y%m%d%H%M%S')}")
                new_storage_key = _slugify(f"{source.get('storage_key') or source_id}-copy-{now.strftime('%Y%m%d%H%M%S')}")
                db.execute(
                    """
                    INSERT INTO contests
                        (slug, title, start_at, end_at, vote_mode, max_actions, unit_name, unit_icon, theme_id, waiting_text, published, is_active, storage_key, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?)
                    """,
                    (
                        new_slug,
                        f"{source.get('title') or ('Contest ' + str(source_id))} (Copy)",
                        source.get("start_at"),
                        source.get("end_at"),
                        source.get("vote_mode") or "single_vote",
                        int(source.get("max_actions") or 1),
                        source.get("unit_name") or "Stimme",
                        source.get("unit_icon") or "*",
                        source.get("theme_id") or "default",
                        source.get("waiting_text") or "",
                        new_storage_key,
                        now.isoformat(),
                    ),
                )
                new_id = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
                db.commit()
                _clone_contest_setup(source_id, new_id)
                upload_folder_for_year(new_id)
                sticker_folder_for_year(new_id, create=True)

        elif action == "delete_contest":
            contest_id = int(request.form.get("contest_id") or 0)
            if contest_id > 0:
                # Do not allow deleting the active contest; require switching first.
                if contest_id == current_year():
                    return redirect(url_for("main.admin_settings"))

                row = db.execute("SELECT storage_key FROM contests WHERE id = ?", (contest_id,)).fetchone()
                storage_key = (row["storage_key"] if row and row["storage_key"] else str(contest_id))

                # Remove contest-scoped data first.
                db.execute("DELETE FROM votes WHERE contest_id = ?", (contest_id,))
                db.execute("DELETE FROM reactions WHERE contest_id = ?", (contest_id,))
                db.execute("DELETE FROM duel_votes WHERE contest_id = ?", (contest_id,))
                db.execute("DELETE FROM images WHERE contest_id = ?", (contest_id,))
                db.execute("DELETE FROM stickers WHERE contest_id = ?", (contest_id,))
                db.execute("DELETE FROM vote_options WHERE contest_id = ?", (contest_id,))
                db.execute("DELETE FROM contest_reaction_options WHERE contest_id = ?", (contest_id,))
                db.execute("DELETE FROM scoring_rules WHERE contest_id = ?", (contest_id,))
                db.execute("DELETE FROM contests WHERE id = ?", (contest_id,))
                db.commit()

                # Best-effort cleanup of contest asset folders.
                try:
                    import shutil
                    for folder_name in (f"uploads_{storage_key}", f"stickers_{storage_key}", f"assets_{storage_key}"):
                        folder_path = os.path.join(current_app.static_folder, folder_name)
                        if os.path.isdir(folder_path):
                            shutil.rmtree(folder_path, ignore_errors=True)
                except Exception:
                    pass

        elif action == "save_contest":
            contest_id = int(request.form.get("contest_id") or 0)
            if contest_id > 0:
                db.execute(
                    """
                    UPDATE contests
                    SET title = ?, slug = ?, start_at = ?, end_at = ?, vote_mode = ?, max_actions = ?,
                        unit_name = ?, unit_icon = ?, theme_id = ?, waiting_text = ?, storage_key = ?
                    WHERE id = ?
                    """,
                    (
                        (request.form.get("title") or "").strip() or f"Contest {contest_id}",
                        _slugify(
                            request.form.get("slug")
                            or _contest_slug_value(
                                (request.form.get("title") or "").strip() or f"Contest {contest_id}",
                                (request.form.get("start_at") or "").strip(),
                                f"contest-{contest_id}",
                            )
                        ),
                        (request.form.get("start_at") or "").strip() or None,
                        (request.form.get("end_at") or "").strip() or None,
                        (request.form.get("vote_mode") or "single_vote").strip(),
                        int(request.form.get("max_actions") or 1),
                        (request.form.get("unit_name") or "Stimme").strip(),
                        (request.form.get("unit_icon") or "*").strip(),
                        (request.form.get("theme_id") or "default").strip(),
                        (request.form.get("waiting_text") or "").strip(),
                        _slugify(
                            request.form.get("storage_key")
                            or request.form.get("slug")
                            or _contest_slug_value(
                                (request.form.get("title") or "").strip() or f"Contest {contest_id}",
                                (request.form.get("start_at") or "").strip(),
                                str(contest_id),
                            )
                        ),
                        contest_id,
                    ),
                )
                db.commit()
                _import_theme_vote_presets(contest_id, (request.form.get("theme_id") or "default").strip())

        elif action == "set_active":
            contest_id = int(request.form.get("contest_id") or 0)
            if contest_id > 0:
                db.execute("UPDATE contests SET is_active = CASE WHEN id = ? THEN 1 ELSE 0 END", (contest_id,))
                db.commit()

        elif action == "toggle_block_all":
            enabled = 1 if request.form.get("block_public_unpublished_all_contests") else 0
            set_app_setting("block_public_unpublished_all_contests", str(enabled))

        return redirect(url_for("main.admin_settings"))

    contests = list_contests()
    drive_backups, drive_error = list_google_drive_backups(limit=10)
    return render_template(
        "admin_settings.html",
        contests=contests,
        active_id=current_year(),
        block_all=get_app_setting("block_public_unpublished_all_contests", "0") == "1",
        backups=list_runtime_backups(limit=10),
        google_drive_client_id=google_drive_client_id(),
        google_drive_client_secret=google_drive_client_secret(),
        google_drive_connected=google_drive_is_connected(),
        google_drive_connected_email=google_drive_connected_email(),
        google_drive_folder_id=google_drive_folder_id(),
        google_drive_configured=google_drive_is_configured(),
        google_drive_backups=drive_backups,
        google_drive_error=drive_error,
        admin_uses_default_password=admin_uses_default_password(),
        vote_modes=["single_vote", "multi_vote", "unique_options"],
        theme_options=available_theme_ids(),
    )


@bp.route("/admin/files", methods=["GET", "POST"])
def admin_files():
    if not session.get("admin"):
        return redirect(url_for("main.login"))

    current_path = _normalized_project_relpath(request.values.get("path"))
    try:
        current_abs = resolve_project_path(current_path)
    except ValueError:
        flash("Ungültiger Projektpfad.", "danger")
        return redirect(url_for("main.admin_files"))

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()
        redirect_path = _normalized_project_relpath(request.form.get("path") or current_path)
        try:
            target_dir = resolve_project_path(redirect_path)
        except ValueError:
            flash("Ungültiger Zielpfad.", "danger")
            return redirect(url_for("main.admin_files"))

        if action == "create_folder":
            folder_name = secure_filename((request.form.get("folder_name") or "").strip())
            if not folder_name:
                flash("Bitte einen gültigen Ordnernamen angeben.", "warning")
            else:
                new_folder = os.path.join(target_dir, folder_name)
                os.makedirs(new_folder, exist_ok=True)
                flash(f"Ordner erstellt: {folder_name}", "success")

        elif action == "upload_files":
            files = request.files.getlist("files")
            saved = 0
            for file in files:
                if not file or not getattr(file, "filename", ""):
                    continue
                filename = secure_filename(file.filename)
                if not filename:
                    continue
                file.save(os.path.join(target_dir, filename))
                saved += 1
            if saved:
                flash(f"{saved} Datei(en) hochgeladen.", "success")
            else:
                flash("Keine Datei hochgeladen.", "warning")

        elif action == "delete_entry":
            entry_path = _normalized_project_relpath(request.form.get("entry_path"))
            try:
                entry_abs = resolve_project_path(entry_path)
            except ValueError:
                flash("Ungültiger Pfad.", "danger")
                return redirect(url_for("main.admin_files", path=redirect_path))

            if not os.path.exists(entry_abs):
                flash("Datei oder Ordner wurde nicht gefunden.", "warning")
            elif _is_protected_project_path(entry_abs):
                flash("Dieser Pfad ist geschützt und kann nicht gelöscht werden.", "danger")
            else:
                if os.path.isdir(entry_abs):
                    shutil.rmtree(entry_abs, ignore_errors=True)
                else:
                    os.remove(entry_abs)
                flash(f"Gelöscht: {entry_path or os.path.basename(entry_abs)}", "success")

        return redirect(url_for("main.admin_files", path=redirect_path or None))

    if not os.path.isdir(current_abs):
        flash("Der gewählte Pfad ist kein Ordner.", "warning")
        return redirect(url_for("main.admin_files"))

    entries: list[dict] = []
    try:
        names = sorted(os.listdir(current_abs), key=lambda item: (not os.path.isdir(os.path.join(current_abs, item)), item.lower()))
    except OSError:
        names = []

    for name in names:
        full = os.path.join(current_abs, name)
        try:
            stat = os.stat(full)
        except OSError:
            continue
        rel = project_relpath(full)
        entries.append(
            {
                "name": name,
                "path": rel,
                "is_dir": os.path.isdir(full),
                "size": _format_file_size(stat.st_size if os.path.isfile(full) else 0),
                "modified_at": datetime.fromtimestamp(stat.st_mtime),
                "is_protected": _is_protected_project_path(full),
            }
        )

    breadcrumbs = [{"label": "Projekt", "path": ""}]
    running = []
    for part in [p for p in current_path.split("/") if p]:
        running.append(part)
        breadcrumbs.append({"label": part, "path": "/".join(running)})

    parent_path = ""
    if current_path:
        parent_path = "/".join(current_path.split("/")[:-1])

    return render_template(
        "admin_files.html",
        year=current_year(),
        current_path=current_path,
        current_abs=current_abs,
        parent_path=parent_path,
        breadcrumbs=breadcrumbs,
        entries=entries,
        shortcuts=project_file_shortcuts(),
    )


@bp.route("/admin/files/download")
def admin_files_download():
    if not session.get("admin"):
        return redirect(url_for("main.login"))
    relative_path = _normalized_project_relpath(request.args.get("path"))
    try:
        full_path = resolve_project_path(relative_path)
    except ValueError:
        return redirect(url_for("main.admin_files"))
    if not os.path.isfile(full_path):
        return redirect(url_for("main.admin_files", path="/".join(relative_path.split("/")[:-1]) if relative_path else None))
    return send_file(full_path, as_attachment=True, download_name=os.path.basename(full_path))


@bp.route("/admin/themes", methods=["GET", "POST"])
def admin_themes():
    if not session.get("admin"):
        return redirect(url_for("main.login"))

    db = get_db()
    if request.method == "POST":
        action = (request.form.get("action") or "").strip()
        if action == "assign_theme":
            contest_id = int(request.form.get("contest_id") or 0)
            theme_id = (request.form.get("theme_id") or "").strip()
            if contest_id > 0 and theme_id in set(available_theme_ids()):
                db.execute("UPDATE contests SET theme_id = ? WHERE id = ?", (theme_id, contest_id))
                db.commit()
                _import_theme_vote_presets(contest_id, theme_id)
                _import_theme_reaction_presets(contest_id, theme_id)
        return redirect(url_for("main.admin_themes"))

    contests = list_contests()
    active_id = current_year()
    return render_template(
        "admin_themes.html",
        year=active_id,
        active_id=active_id,
        contests=contests,
        themes=theme_catalog(),
        theme_options=available_theme_ids(),
    )


@bp.route("/admin/vote-options", methods=["GET", "POST"])
def admin_vote_options():
    if not session.get("admin"):
        return redirect(url_for("main.login"))

    db = get_db()
    year = int(request.args.get("year", request.args.get("contest_id", current_year())))

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()
        year = int(request.form.get("year") or year)
        contest = get_contest(year) or {}
        theme_id = (contest.get("theme_id") or "default").strip() or "default"

        if action == "save_order":
            ids = [int(x) for x in request.form.get("order", "").split(",") if x.strip().isdigit()]
            for idx, opt_id in enumerate(ids, start=1):
                db.execute("UPDATE vote_options SET sort_order = ? WHERE id = ? AND contest_id = ?", (idx, opt_id, year))
            rows = db.execute("SELECT id FROM vote_options WHERE contest_id = ?", (year,)).fetchall()
            for r in rows:
                active_val = 1 if request.form.get(f"active_{r['id']}") else 0
                db.execute("UPDATE vote_options SET active = ? WHERE id = ?", (active_val, r["id"]))
            db.commit()
            return redirect(url_for("main.admin_vote_options", year=year))

        if action == "upsert":
            opt_id = int(request.form.get("id", 0) or 0)
            opt_key = (request.form.get("opt_key") or "").strip()
            label = (request.form.get("label") or "").strip()
            icon = (request.form.get("icon") or "").strip()
            display_type = (request.form.get("display_type") or "icon").strip()
            image_filename = (request.form.get("current_image_filename") or "").strip()
            uploaded_image = request.files.get("image_file")
            uploaded_filename = save_uploaded_asset(year, uploaded_image, "voteopt")
            if uploaded_filename:
                image_filename = uploaded_filename
                display_type = "image"
            unique_per_user = 1 if request.form.get("unique_per_user") else 0
            exclusive_group = "allin" if request.form.get("all_in_only") else None
            is_all_in = opt_key.lower() == "all_in" or exclusive_group == "allin"
            value = _safe_int(request.form.get("value"), 1)
            if is_all_in:
                value = get_vote_option_points_map(year).get("all_in", value)
            active = 1 if request.form.get("active") else 0
            if not opt_key or not label:
                return jsonify(success=False, error="opt_key und label sind Pflicht"), 400

            if opt_id > 0:
                db.execute(
                    """
                    UPDATE vote_options
                    SET opt_key=?, label=?, icon=?, display_type=?, image_filename=?, value=?, unique_per_user=?,
                        exclusive_group=?, is_special=0, active=?, contest_year=?
                    WHERE id=? AND contest_id=?
                    """,
                    (opt_key, label, icon, display_type, image_filename or None, value, unique_per_user, exclusive_group, active, year, opt_id, year),
                )
            else:
                max_sort = db.execute("SELECT COALESCE(MAX(sort_order), 0) FROM vote_options WHERE contest_id = ?", (year,)).fetchone()[0]
                db.execute(
                    """
                    INSERT INTO vote_options
                        (contest_year, contest_id, opt_key, label, icon, display_type, image_filename, value, unique_per_user, exclusive_group, is_special, active, sort_order, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (year, year, opt_key, label, icon, display_type, image_filename or None, value, unique_per_user, exclusive_group, 0, active, max_sort + 1, datetime.now().isoformat()),
                )
            db.commit()
            return redirect(url_for("main.admin_vote_options", year=year))

        if action == "theme_upsert":
            theme_rows = _theme_vote_presets(theme_id)
            original_key = (request.form.get("theme_original_key") or "").strip().lower()
            opt_key = (request.form.get("theme_opt_key") or "").strip().lower()
            label = (request.form.get("theme_label") or "").strip()
            icon = (request.form.get("theme_icon") or "").strip()
            display_type = (request.form.get("theme_display_type") or "icon").strip() or "icon"
            image_filename = (request.form.get("theme_current_image_filename") or "").strip()
            uploaded_image = request.files.get("theme_image_file")
            uploaded_filename = save_uploaded_theme_asset(theme_id, uploaded_image, "theme_vote")
            if uploaded_filename:
                image_filename = uploaded_filename
                display_type = "image"
            unique_per_user = 1 if request.form.get("theme_unique_per_user") else 0
            exclusive_group = "allin" if request.form.get("theme_all_in_only") else None
            active = 1 if request.form.get("theme_active") else 0
            value = _safe_int(request.form.get("theme_value"), 1)
            if opt_key == "all_in" or exclusive_group == "allin":
                value = 500
            if opt_key and label:
                updated = False
                for row in theme_rows:
                    existing_key = str(row.get("opt_key") or "").strip().lower()
                    if existing_key == original_key and original_key:
                        row.update(
                            {
                                "opt_key": opt_key,
                                "label": label,
                                "icon": icon,
                                "display_type": display_type,
                                "image_filename": image_filename or None,
                                "value": value,
                                "unique_per_user": unique_per_user,
                                "exclusive_group": exclusive_group,
                                "active": active,
                            }
                        )
                        updated = True
                        break
                if not updated:
                    max_sort = max([int(row.get("sort_order") or 0) for row in theme_rows] + [0])
                    theme_rows.append(
                        {
                            "opt_key": opt_key,
                            "label": label,
                            "icon": icon,
                            "display_type": display_type,
                            "image_filename": image_filename or None,
                            "value": value,
                            "unique_per_user": unique_per_user,
                            "exclusive_group": exclusive_group,
                            "active": active,
                            "sort_order": max_sort + 10,
                        }
                    )
                _write_theme_vote_presets(theme_id, theme_rows)
                synced = _apply_theme_vote_preset_change(theme_id, original_key=original_key, new_key=opt_key)
                flash(f"Theme-Stimmoption gespeichert und in {synced} Contest(s) synchronisiert.", "success")
            return redirect(url_for("main.admin_vote_options", year=year))

        if action == "theme_delete":
            delete_key = (request.form.get("theme_opt_key") or "").strip().lower()
            if delete_key:
                theme_rows = [row for row in _theme_vote_presets(theme_id) if str(row.get("opt_key") or "").strip().lower() != delete_key]
                _write_theme_vote_presets(theme_id, theme_rows)
                synced = _apply_theme_vote_preset_change(theme_id, deleted_key=delete_key)
                flash(f"Theme-Stimmoption entfernt und aus {synced} Contest(s) gelöscht.", "success")
            return redirect(url_for("main.admin_vote_options", year=year))

        if action == "delete":
            opt_id = int(request.form.get("id", 0) or 0)
            if opt_id > 0:
                db.execute("DELETE FROM vote_options WHERE id = ? AND contest_id = ?", (opt_id, year))
                db.commit()
            return redirect(url_for("main.admin_vote_options", year=year))

        if action == "import_theme_defaults":
            _import_theme_vote_presets(year, theme_id)
            return redirect(url_for("main.admin_vote_options", year=year))

        if action == "sync_theme_defaults":
            _import_theme_vote_presets(year, theme_id, mode="replace")
            return redirect(url_for("main.admin_vote_options", year=year))

    opts = db.execute("SELECT * FROM vote_options WHERE contest_id = ? ORDER BY sort_order ASC, id ASC", (year,)).fetchall()
    contest = get_contest(year) or {}
    theme_id = contest.get("theme_id") or "default"
    theme_presets = _theme_vote_presets(theme_id)
    theme_preset_source = _theme_vote_preset_source(theme_id)
    existing_keys = {(o["opt_key"] or "").strip().lower() for o in opts}
    detected_theme_keys = [
        {
            "opt_key": p.get("opt_key"),
            "label": p.get("label") or p.get("opt_key"),
            "exists": ((p.get("opt_key") or "").strip().lower() in existing_keys),
        }
        for p in theme_presets
        if (p.get("opt_key") or "").strip()
    ]
    return render_template(
        "admin_vote_options.html",
        year=year,
        available_years=[int(c["id"]) for c in list_contests()],
        contests=list_contests(),
        current_contest=contest,
        year_cfg=get_year_settings(year),
        options=[dict(o) for o in opts],
        all_in_auto_value=get_vote_option_points_map(year).get("all_in", 0),
        current_year=current_year(),
        current_theme_id=theme_id,
        theme_vote_presets=theme_presets,
        theme_preset_source=theme_preset_source,
        detected_theme_keys=detected_theme_keys,
    )


@bp.route("/admin/categories", methods=["GET", "POST"])
def admin_categories():
    if not session.get("admin"):
        return redirect(url_for("main.login"))

    db = get_db()
    year = int(request.args.get("year", request.args.get("contest_id", current_year())))

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()
        year = int(request.form.get("year") or year)
        contest = get_contest(year) or {}
        theme_id = (contest.get("theme_id") or "default").strip() or "default"
        if action == "upsert":
            cat_id = int(request.form.get("id") or 0)
            reaction_key = (request.form.get("reaction_key") or "").strip().lower()
            label = (request.form.get("label") or "").strip()
            display_type = (request.form.get("display_type") or "icon").strip()
            icon = (request.form.get("icon") or "").strip()
            points = int(request.form.get("points") or 0)
            include_in_total = 1 if request.form.get("include_in_total") else 0
            image_filename = (request.form.get("current_image_filename") or "").strip()
            uploaded_image = request.files.get("image_file")
            uploaded_filename = save_uploaded_asset(year, uploaded_image, "category")
            if uploaded_filename:
                image_filename = uploaded_filename
                display_type = "image"
            active = 1 if request.form.get("active") else 0
            if reaction_key and label:
                if cat_id > 0:
                    db.execute("UPDATE contest_reaction_options SET reaction_key=?, label=?, display_type=?, icon=?, image_filename=?, points=?, include_in_total=?, active=? WHERE id=? AND contest_id=?", (reaction_key, label, display_type, icon, image_filename or None, points, include_in_total, active, cat_id, year))
                else:
                    max_sort = db.execute("SELECT COALESCE(MAX(sort_order), 0) FROM contest_reaction_options WHERE contest_id = ?", (year,)).fetchone()[0]
                    db.execute(
                        """
                        INSERT INTO contest_reaction_options
                            (contest_id, reaction_key, label, display_type, icon, image_filename, points, include_in_total, active, sort_order, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(contest_id, reaction_key) DO UPDATE SET label=excluded.label, points=excluded.points, include_in_total=excluded.include_in_total
                        """,
                        (year, reaction_key, label, display_type, icon, image_filename or None, points, include_in_total, active, max_sort + 1, datetime.now().isoformat()),
                    )
                db.commit()
        elif action == "theme_upsert":
            theme_rows = _theme_reaction_presets(theme_id)
            original_key = (request.form.get("theme_original_key") or "").strip().lower()
            reaction_key = (request.form.get("theme_reaction_key") or "").strip().lower()
            label = (request.form.get("theme_label") or "").strip()
            display_type = (request.form.get("theme_display_type") or "icon").strip() or "icon"
            icon = (request.form.get("theme_icon") or "").strip()
            points = int(request.form.get("theme_points") or 0)
            image_filename = (request.form.get("theme_current_image_filename") or "").strip()
            uploaded_image = request.files.get("theme_image_file")
            uploaded_filename = save_uploaded_theme_asset(theme_id, uploaded_image, "theme_reaction")
            if uploaded_filename:
                image_filename = uploaded_filename
                display_type = "image"
            active = 1 if request.form.get("theme_active") else 0
            if reaction_key and label:
                updated = False
                for row in theme_rows:
                    existing_key = str(row.get("reaction_key") or "").strip().lower()
                    if existing_key == original_key and original_key:
                        row.update(
                            {
                                "reaction_key": reaction_key,
                                "label": label,
                                "display_type": display_type,
                                "icon": icon,
                                "image_filename": image_filename or None,
                                "points": points,
                                "include_in_total": 1 if request.form.get("theme_include_in_total") else 0,
                                "active": active,
                            }
                        )
                        updated = True
                        break
                if not updated:
                    max_sort = max([int(row.get("sort_order") or 0) for row in theme_rows] + [0])
                    theme_rows.append(
                        {
                            "reaction_key": reaction_key,
                            "label": label,
                            "display_type": display_type,
                            "icon": icon,
                            "image_filename": image_filename or None,
                            "points": points,
                            "include_in_total": 1 if request.form.get("theme_include_in_total") else 0,
                            "active": active,
                            "sort_order": max_sort + 10,
                        }
                    )
                _write_theme_reaction_presets(theme_id, theme_rows)
                synced = _apply_theme_reaction_preset_change(theme_id, original_key=original_key, new_key=reaction_key)
                flash(f"Theme-Kategorie gespeichert und in {synced} Contest(s) synchronisiert.", "success")
        elif action == "theme_delete":
            delete_key = (request.form.get("theme_reaction_key") or "").strip().lower()
            if delete_key:
                theme_rows = [row for row in _theme_reaction_presets(theme_id) if str(row.get("reaction_key") or "").strip().lower() != delete_key]
                _write_theme_reaction_presets(theme_id, theme_rows)
                synced = _apply_theme_reaction_preset_change(theme_id, deleted_key=delete_key)
                flash(f"Theme-Kategorie entfernt und aus {synced} Contest(s) gelöscht.", "success")
        elif action == "import_theme_defaults":
            _import_theme_reaction_presets(year, theme_id)
        elif action == "sync_theme_defaults":
            _import_theme_reaction_presets(year, theme_id, mode="replace")
        elif action == "delete":
            cat_id = int(request.form.get("id") or 0)
            db.execute("DELETE FROM contest_reaction_options WHERE id = ? AND contest_id = ?", (cat_id, year))
            db.commit()
        return redirect(url_for("main.admin_categories", year=year))

    rows = db.execute("SELECT * FROM contest_reaction_options WHERE contest_id = ? ORDER BY sort_order ASC, id ASC", (year,)).fetchall()
    contest = get_contest(year) or {}
    theme_id = contest.get("theme_id") or "default"
    theme_presets = _theme_reaction_presets(theme_id)
    theme_preset_source = _theme_reaction_preset_source(theme_id)
    existing_keys = {(r["reaction_key"] or "").strip().lower() for r in rows}
    detected_theme_keys = [
        {
            "reaction_key": p.get("reaction_key"),
            "label": p.get("label") or p.get("reaction_key"),
            "exists": ((p.get("reaction_key") or "").strip().lower() in existing_keys),
        }
        for p in theme_presets
        if (p.get("reaction_key") or "").strip()
    ]
    return render_template(
        "admin_categories.html",
        year=year,
        available_years=[int(c["id"]) for c in list_contests()],
        contests=list_contests(),
        current_contest=contest,
        current_theme_id=theme_id,
        theme_preset_source=theme_preset_source,
        theme_reaction_presets=theme_presets,
        detected_theme_keys=detected_theme_keys,
        categories=[dict(r) for r in rows],
    )


@bp.route("/admin/scoring", methods=["GET", "POST"])
def admin_scoring():
    year = int(request.args.get("year", request.args.get("contest_id", current_year())))
    return redirect(url_for("main.admin_vote_options", year=year))


@bp.route("/admin/stickers", methods=["GET", "POST"])
def admin_stickers():
    if not session.get("admin"):
        return redirect(url_for("main.login"))

    db = get_db()
    year = int(request.args.get("contest_id", request.args.get("year", request.form.get("year", current_year()))))
    folder = sticker_folder_for_year(year, create=False)

    if request.method == "POST":
        action = request.form.get("action", "upload")

        if action == "upload_single":
            folder = sticker_folder_for_year(year, create=True)
            files = request.files.getlist("files")
            for file in files:
                if file and allowed_file(file.filename):
                    filename = secure_filename(file.filename)
                    file.save(os.path.join(folder, filename))

        elif action == "upload_zip":
            folder = sticker_folder_for_year(year, create=True)
            zip_file = request.files.get("zip_file")
            if zip_file and zip_file.filename.lower().endswith(".zip"):
                with zipfile.ZipFile(zip_file.stream) as zf:
                    for entry in zf.infolist():
                        if entry.is_dir():
                            continue
                        entry_name = os.path.basename(entry.filename)
                        if not entry_name or not allowed_file(entry_name):
                            continue
                        safe_name = secure_filename(entry_name)
                        with zf.open(entry) as src, open(os.path.join(folder, safe_name), "wb") as dst:
                            dst.write(src.read())

        elif action == "copy_to_contest":
            target_year = int(request.form.get("target_year") or 0)
            if target_year > 0 and target_year != year:
                source_folder = sticker_folder_for_year(year, create=False)
                target_folder = sticker_folder_for_year(target_year, create=True)
                source_rows = db.execute(
                    "SELECT filename, sort_order, active FROM stickers WHERE contest_id = ? ORDER BY sort_order ASC, id ASC",
                    (year,),
                ).fetchall()
                existing_target = {
                    (row["filename"] or "").strip().lower()
                    for row in db.execute("SELECT filename FROM stickers WHERE contest_id = ?", (target_year,)).fetchall()
                }
                for row in source_rows:
                    filename = str(row["filename"] or "").strip()
                    if not filename:
                        continue
                    src_path = os.path.join(source_folder, filename)
                    dst_path = os.path.join(target_folder, filename)
                    if os.path.exists(src_path) and not os.path.exists(dst_path):
                        import shutil
                        shutil.copyfile(src_path, dst_path)
                    if filename.lower() in existing_target:
                        continue
                    db.execute(
                        """
                        INSERT INTO stickers (contest_year, contest_id, filename, sort_order, active, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (target_year, target_year, filename, int(row["sort_order"] or 0), int(row["active"] or 0), datetime.now().isoformat()),
                    )
                    existing_target.add(filename.lower())
                db.commit()
                ensure_sticker_records_for_year(target_year)

        elif action == "save_order":
            order_ids = [int(x) for x in request.form.get("order", "").split(",") if x.strip().isdigit()]
            for idx, sticker_id in enumerate(order_ids, start=1):
                db.execute("UPDATE stickers SET sort_order = ? WHERE id = ? AND contest_id = ?", (idx, sticker_id, year))
            for sticker in db.execute("SELECT id FROM stickers WHERE contest_id = ?", (year,)).fetchall():
                active_val = 1 if request.form.get(f"active_{sticker['id']}") else 0
                db.execute("UPDATE stickers SET active = ? WHERE id = ?", (active_val, sticker["id"]))
            db.commit()

        elif action == "delete":
            sticker_id = int(request.form.get("sticker_id", 0))
            row = db.execute("SELECT filename FROM stickers WHERE id = ? AND contest_id = ?", (sticker_id, year)).fetchone()
            if row:
                for file_path in sticker_file_candidates_for_year(year, row["filename"]):
                    if os.path.exists(file_path):
                        os.remove(file_path)
                db.execute("DELETE FROM stickers WHERE id = ?", (sticker_id,))
                db.commit()

        ensure_sticker_records_for_year(year)
        return redirect(url_for("main.admin_stickers", year=year))

    ensure_sticker_records_for_year(year)
    stickers = db.execute("SELECT * FROM stickers WHERE contest_id = ? ORDER BY sort_order ASC, id ASC", (year,)).fetchall()
    return render_template(
        "admin_stickers.html",
        year=year,
        stickers=stickers,
        contests=list_contests(),
        current_year=current_year(),
        current_contest=get_contest(year),
    )


@bp.route("/update-images", methods=["POST"])
def update_images():
    if not session.get("admin"):
        return redirect(url_for("main.login"))
    db = get_db()
    year = int(request.form.get("contest_year", current_year()))
    images = db.execute("SELECT id FROM images WHERE contest_id = ?", (year,)).fetchall()
    for image in images:
        image_id = image["id"]
        uploader = (request.form.get(f"uploader_{image_id}", "") or "").strip()
        description = (request.form.get(f"description_{image_id}", "") or "").strip()
        visible = 1 if request.form.get(f"visible_{image_id}") else 0
        exclude_from_results = 0 if request.form.get(f"include_in_main_results_{image_id}") else 1
        include_in_category_results = 1 if request.form.get(f"include_in_category_results_{image_id}") else 0
        db.execute(
            "UPDATE images SET uploader = ?, description = ?, visible = ?, exclude_from_results = ?, include_in_category_results = ? WHERE id = ?",
            (uploader, description, visible, exclude_from_results, include_in_category_results, image_id),
        )
    db.commit()
    return redirect(url_for("main.upload", year=year))


@bp.route("/delete-image/<int:image_id>", methods=["POST"])
def delete_image(image_id):
    if not session.get("admin"):
        return redirect(url_for("main.login"))
    db = get_db()
    image = db.execute("SELECT filename, contest_id FROM images WHERE id = ?", (image_id,)).fetchone()
    if image:
        image_path = os.path.join(upload_folder_for_year(int(image["contest_id"] or current_year())), image["filename"])
        if os.path.exists(image_path):
            os.remove(image_path)
        db.execute("DELETE FROM images WHERE id = ?", (image_id,))
        db.execute("DELETE FROM votes WHERE image_id = ?", (image_id,))
        db.execute("DELETE FROM reactions WHERE image_id = ?", (image_id,))
        db.commit()
    return redirect(url_for("main.upload"))


@bp.route("/results")
def results():
    if not session.get("admin"):
        return redirect(url_for("main.login"))
    year = int(request.args.get("contest_id", request.args.get("year", current_year())))
    db = get_db()
    ranked = [
        r for r in _calc_ranking(year, visible_only=False, include_excluded=True)
        if int(r.get("vote_count") or 0) > 0
        or int(r.get("duel_vote_count") or 0) > 0
        or int(r.get("weighted_score") or 0) > 0
        or int(r.get("reaction_points_total") or 0) > 0
        or int(r.get("exclude_from_results") or 0) > 0
    ]
    show_ranking = request.args.get("show_ranking", "0") == "1"
    display_rows = list(ranked)
    if not show_ranking:
        random.shuffle(display_rows)
    total_votes = db.execute("SELECT COUNT(*) FROM votes WHERE contest_id = ?", (year,)).fetchone()[0]
    voters = db.execute("SELECT COUNT(DISTINCT voter_session_id) FROM votes WHERE contest_id = ?", (year,)).fetchone()[0]
    reaction_options = [dict(opt) for opt in get_reaction_options(year)]
    details_by_image = _admin_ranking_details(year)
    contests = list_contests()
    return render_template(
        "results.html",
        top_images=display_rows,
        ranked_images=ranked,
        voters=voters,
        total_votes=total_votes,
        published=is_published(year),
        show_stats=True,
        show_ranking=show_ranking,
        reaction_options=reaction_options,
        details_by_image=details_by_image,
        current_year=current_year(),
        year=year,
        available_years=[int(c["id"]) for c in contests],
        contests=contests,
        current_contest=get_contest(year),
    )


@bp.route("/public-results")
def public_results():
    contest = get_contest(current_year())
    if contest and contest.get("slug"):
        return redirect(url_for("main.public_results_slug", slug=contest["slug"]))
    return redirect(url_for("main.public_results_year", year=current_year()))


def _render_public_results(year: int):
    contest = get_contest(year)
    if not contest:
        return redirect(url_for("main.root"))
    published = is_published(year)
    block_all = get_app_setting("block_public_unpublished_all_contests", "0") == "1"
    admin_preview = bool(session.get("admin")) and (request.args.get("preview") == "1")
    if not admin_preview and ((block_all and not published) or (year == current_year() and not published)):
        return render_template(themed_template("public_waiting", contest), year=year, contest=contest, waiting_text=waiting_text_for_year(year))

    ranking_rows = _calc_ranking(year, visible_only=True)
    category_rankings = _calc_category_rankings(year, visible_only=True)
    details_by_image = _admin_ranking_details(year)
    top_images = ranking_rows[:5]
    top_10_images = ranking_rows[:10]
    return render_template(
        themed_template("public_results", contest),
        contest=contest,
        top_images=top_images,
        top_10_images=top_10_images,
        top_images_json=[dict(r) for r in top_images],
        ranking_images_json=[dict(r) for r in ranking_rows],
        category_rankings_json={key: [dict(row) for row in rows] for key, rows in category_rankings.items()},
        public_details_json={str(image_id): detail for image_id, detail in details_by_image.items()},
        reaction_options=[dict(opt) for opt in get_reaction_options(year)],
        reveal_version=results_reveal_version(year),
        year=year,
    )


@bp.route("/public-results/<int:year>")
def public_results_year(year: int):
    return _render_public_results(year)


@bp.route("/public-results/<slug>")
def public_results_slug(slug: str):
    contest = get_contest_by_slug(slug)
    if not contest:
        return redirect(url_for("main.root"))
    return _render_public_results(int(contest["id"]))


@bp.route("/toggle-publish", methods=["POST"])
def toggle_publish():
    if not session.get("admin"):
        return redirect(url_for("main.login"))
    year = int(request.form.get("year", current_year()))
    action = request.form.get("action")
    set_published(year, action == "show")
    if action == "hide":
        set_app_setting(f"results_reveal_version_{year}", datetime.now().strftime("%Y%m%d%H%M%S"))
    return redirect(url_for("main.results", year=year))


@bp.route("/admin/backups/download/<path:filename>")
def download_backup(filename: str):
    if not session.get("admin"):
        return redirect(url_for("main.login"))
    safe_name = os.path.basename(filename or "").strip()
    backup_path = os.path.join(backup_root_folder(), safe_name)
    if not safe_name or not os.path.isfile(backup_path):
        return redirect(url_for("main.admin_settings"))
    return send_file(backup_path, as_attachment=True, download_name=safe_name)


@bp.route("/admin/reset-year-votes", methods=["POST"])
def reset_year_votes_admin():
    if not session.get("admin"):
        return redirect(url_for("main.login"))
    try:
        year = int(request.form.get("year", current_year()))
    except Exception:
        year = current_year()
    db = get_db()
    db.execute("DELETE FROM votes WHERE contest_id = ?", (year,))
    db.execute("DELETE FROM reactions WHERE contest_id = ?", (year,))
    db.execute("DELETE FROM duel_votes WHERE contest_id = ?", (year,))
    db.commit()
    return redirect(url_for("main.results"))


@bp.route("/api/stickers")
def list_stickers():
    return list_stickers_for_year(current_year())


@bp.route("/api/stickers/<int:year>")
def list_stickers_for_year(year: int):
    db = get_db()
    ensure_sticker_records_for_year(year)
    rows = db.execute("SELECT filename FROM stickers WHERE contest_id = ? AND active = 1 ORDER BY sort_order ASC, id ASC", (year,)).fetchall()
    return jsonify([r["filename"] for r in rows])

