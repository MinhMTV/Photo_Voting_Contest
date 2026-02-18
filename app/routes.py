import json
import os
import zipfile
from datetime import datetime
from flask import send_from_directory

from flask import Blueprint, render_template, request, redirect, url_for, session, current_app, jsonify
from werkzeug.utils import secure_filename

try:
    from .db import get_db
except ImportError:
    # Fallback for direct module execution (e.g. python app/routes.py)
    from db import get_db

bp = Blueprint('main', __name__)
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

def get_year_settings(year: int) -> dict:
    db = get_db()
    row = db.execute(
        "SELECT * FROM contest_year_settings WHERE contest_year = ?",
        (year,)
    ).fetchone()
    if not row:
        # Fallback, falls Settings noch nicht existieren
        return {"vote_mode": "toggle", "max_actions": 3, "unit_name": "Stimme", "unit_icon": "❤️"}
    return dict(row)

def get_vote_options(year: int) -> list[dict]:
    db = get_db()
    rows = db.execute("""
        SELECT id, opt_key, label, icon, value, unique_per_user, exclusive_group, is_special, active, sort_order
        FROM vote_options
        WHERE contest_year = ? AND active = 1
        ORDER BY sort_order ASC, id ASC
    """, (year,)).fetchall()
    return [dict(r) for r in rows]

def get_vote_option_map(year: int) -> dict:
    opts = get_vote_options(year)
    return {o["opt_key"]: o for o in opts}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def _settings_path() -> str:
    return os.path.join(current_app.instance_path, 'admin_settings.json')


def get_runtime_settings() -> dict:
    base_year = int(current_app.config.get('CURRENT_CONTEST_YEAR', 2026))
    defaults = {
        'current_contest_year': base_year,
        'legacy_years': current_app.config.get('LEGACY_CONTEST_YEARS', [2025]),
        'voting_end_at': current_app.config.get('VOTING_END_AT', '2026-12-31T23:59:59'),
        'waiting_text_by_year': {
            str(base_year): 'Die Abstimmung läuft noch. Ergebnisse werden nach Freigabe veröffentlicht.'
        },
        # ✅ NEU: Testmodus (default aus)
        'block_public_unpublished_all_years': False,
    }
    path = _settings_path()
    if not os.path.exists(path):
        return defaults

    try:
        with open(path, 'r', encoding='utf-8') as f:
            stored = json.load(f)
            defaults.update(stored or {})
    except Exception:
        return defaults

    return defaults


def save_runtime_settings(data: dict) -> None:
    with open(_settings_path(), 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def current_year() -> int:
    return int(get_runtime_settings().get('current_contest_year', 2026))

def _publish_flag_path(year: int) -> str:
    return os.path.join(current_app.root_path, f'published_flag_{year}.txt')

def is_published(year: int) -> bool:
    path = _publish_flag_path(year)
    if not os.path.exists(path):
        return False
    with open(path, 'r', encoding='utf-8') as f:
        return f.read().strip() == '1'


def waiting_text_for_year(year: int, settings: dict | None = None) -> str:
    cfg = settings or get_runtime_settings()
    waiting_map = cfg.get('waiting_text_by_year', {}) or {}
    return waiting_map.get(str(year), 'Die Abstimmung läuft noch. Ergebnisse werden nach Freigabe veröffentlicht.')


def waiting_template_for_year(year: int) -> str:
    templates_dir = os.path.join(current_app.root_path, 'templates')
    year_name = f'public_waiting_{year}.html'
    year_path = os.path.join(templates_dir, year_name)

    # Enforce year-specific waiting templates. If missing, bootstrap from base template once.
    if not os.path.exists(year_path):
        base_path = os.path.join(templates_dir, 'public_waiting.html')
        if os.path.exists(base_path):
            with open(base_path, 'r', encoding='utf-8') as src, open(year_path, 'w', encoding='utf-8') as dst:
                dst.write(src.read())

    return year_name


def upload_folder_for_year(year: int) -> str:
    base_static = current_app.static_folder
    path = os.path.join(base_static, f'uploads_{year}')
    os.makedirs(path, exist_ok=True)
    return path


def sticker_folder_for_year(year: int, create: bool = False) -> str:
    base_static = current_app.static_folder
    preferred = os.path.join(base_static, f'stickers_{year}')

    if create:
        os.makedirs(preferred, exist_ok=True)
        return preferred

    if os.path.isdir(preferred) and any(allowed_file(f) for f in os.listdir(preferred)):
        return preferred

    legacy = os.path.join(base_static, 'stickers')
    if os.path.isdir(legacy):
        return legacy

    return preferred


def ensure_sticker_records_for_year(year: int) -> None:
    db = get_db()
    folder = sticker_folder_for_year(year)
    files = [f for f in os.listdir(folder) if allowed_file(f)]
    for filename in files:
        exists = db.execute(
            'SELECT id FROM stickers WHERE contest_year = ? AND filename = ?',
            (year, filename)
        ).fetchone()
        if not exists:
            max_sort = db.execute(
                'SELECT COALESCE(MAX(sort_order), 0) FROM stickers WHERE contest_year = ?',
                (year,)
            ).fetchone()[0]
            db.execute(
                'INSERT INTO stickers (contest_year, filename, sort_order, active, created_at) VALUES (?, ?, ?, 1, ?)',
                (year, filename, max_sort + 1, datetime.now().isoformat())
            )
    db.commit()


@bp.route('/')
def root():
    return redirect(url_for('main.contest_year', year=current_year()))


@bp.route('/media/<int:year>/<path:filename>')
def media_year(year: int, filename: str):
    return send_from_directory(upload_folder_for_year(year), filename)


@bp.route('/sticker/<int:year>/<path:filename>')
def sticker_year(year: int, filename: str):
    folder = sticker_folder_for_year(year)
    return send_from_directory(folder, filename)


@bp.route('/contest/<int:year>')
def contest_year(year: int):
    # Rule: every non-active year redirects to that year's public results
    if year != current_year():
        return redirect(url_for('main.public_results_year', year=year))

    db = get_db()

    images = db.execute(
        'SELECT * FROM images WHERE visible = 1 AND contest_year = ? ORDER BY uploaded_at DESC',
        (year,)
    ).fetchall()

    voter_session_id = request.cookies.get('voter_session_id')
    year_cfg = get_year_settings(year)
    max_actions = int(year_cfg.get("max_actions", 4))

    voted = db.execute(
        'SELECT image_id, vote_value, vote_label, vote_option_key FROM votes WHERE voter_session_id = ? AND contest_year = ?',
        (voter_session_id, year)
    ).fetchall()

    voted_ids = [row['image_id'] for row in voted]
    user_bets = {
        str(row['image_id']): {
            'vote_value': row['vote_value'] or 1,
            'vote_label': row['vote_label'] or '',
            'vote_option_key': row['vote_option_key'] or ''
        } for row in voted
    }

    used_keys = {(row['vote_option_key'] or '').lower() for row in voted}
    vote_mode = (year_cfg.get("vote_mode") or "toggle").strip()

    if vote_mode == "unique_options":
        used_count = max_actions if ('all_in' in used_keys) else len(used_keys)
    else:
        used_count = len(voted)

    votes_left = max(0, max_actions - used_count)


    user_reactions = {}
    if voter_session_id:
        reaction_rows = db.execute(
            'SELECT image_id, reaction_type FROM reactions WHERE voter_session_id = ? AND contest_year = ?',
            (voter_session_id, year)
        ).fetchall()
        for row in reaction_rows:
            key = str(row['image_id'])
            user_reactions.setdefault(key, set()).add(row['reaction_type'])

    settings = get_runtime_settings()
    legacy_years = settings.get('legacy_years', [2025])

    return render_template(
        'contest_index.html',
        images=images,
        voted_ids=voted_ids,
        votes_left=votes_left,
        year=year,
        legacy_years=legacy_years,
        voting_end_at=settings.get('voting_end_at'),
        user_reactions=user_reactions,
        user_bets=user_bets
    )


@bp.route('/archive')
def archive():
    settings = get_runtime_settings()
    years = [int(y) for y in settings.get('legacy_years', [])]
    return render_template('archive.html', years=sorted(set(years), reverse=True))


@bp.route('/public-waiting/<int:year>')
def public_waiting_preview(year: int):
    settings = get_runtime_settings()
    template = waiting_template_for_year(year)
    return render_template(template, year=year, waiting_text=waiting_text_for_year(year, settings))


@bp.route('/duel/<int:year>')
def duel_year(year: int):
    if year != current_year():
        return redirect(url_for('main.public_results_year', year=year))

    db = get_db()
    candidates = db.execute(
        'SELECT * FROM images WHERE visible = 1 AND contest_year = ? ORDER BY RANDOM() LIMIT 3',
        (year,)
    ).fetchall()

    if len(candidates) < 3:
        return redirect(url_for('main.contest_year', year=year))

    return render_template('duel.html', year=year, candidates=candidates)


def duel_spins_used(voter_session_id: str, year: int) -> int:
    db = get_db()
    return db.execute(
        'SELECT COUNT(*) FROM duel_votes WHERE voter_session_id = ? AND contest_year = ?',
        (voter_session_id, year)
    ).fetchone()[0]


@bp.route('/api/duel-state/<int:year>')
def duel_state(year: int):
    voter_session_id = (request.args.get('voter_session_id') or '').strip()
    if not voter_session_id:
        return jsonify(success=True, used=0, remaining=10)
    used = duel_spins_used(voter_session_id, year)
    return jsonify(success=True, used=used, remaining=max(0, 10 - used))



@bp.route('/api/duel-spin/<int:year>')
def duel_spin(year: int):
    voter_session_id = (request.args.get('voter_session_id') or '').strip()
    if voter_session_id:
        used = duel_spins_used(voter_session_id, year)
        if used >= 10:
            return jsonify(success=False, error='Keine Spins mehr übrig', remaining=0), 403

    db = get_db()
    rows = db.execute(
        'SELECT id, filename, uploader, description FROM images WHERE visible = 1 AND contest_year = ? ORDER BY RANDOM() LIMIT 3',
        (year,)
    ).fetchall()

    if len(rows) < 3:
        return jsonify(success=False, error='Nicht genug Bilder für Duel-Slot'), 400

    return jsonify(success=True, candidates=[{
        'id': r['id'],
        'filename': r['filename'],
        'uploader': r['uploader'] or 'Unbekannt',
        'description': r['description'] or ''
    } for r in rows])


@bp.route('/api/duel-vote/<int:image_id>', methods=['POST'])
def duel_vote(image_id: int):
    payload = request.json or {}
    voter_session_id = (payload.get('voter_session_id') or '').strip()
    contest_year = int(payload.get('contest_year', current_year()))

    if not voter_session_id:
        return jsonify(success=False, error='Session fehlt'), 400

    used = duel_spins_used(voter_session_id, contest_year)
    if used >= 10:
        return jsonify(success=False, error='Keine Spins mehr übrig', remaining=0), 403

    db = get_db()
    db.execute(
        'INSERT INTO duel_votes (image_id, voter_session_id, contest_year, created_at) VALUES (?, ?, ?, ?)',
        (image_id, voter_session_id, contest_year, datetime.now().isoformat())
    )
    db.commit()

    used_after = duel_spins_used(voter_session_id, contest_year)
    return jsonify(success=True, used=used_after, remaining=max(0, 10 - used_after))


@bp.route('/vote/<int:image_id>', methods=['POST'])
def vote(image_id: int):
    payload = request.json or {}
    voter_session_id = (payload.get('voter_session_id') or '').strip()
    contest_year = int(payload.get('contest_year', current_year()))

    # Frontend soll künftig vote_option_key schicken (z.B. "chip_25", "all_in", "heart")
    vote_option_key = (payload.get('vote_option_key') or '').strip()

    if not vote_option_key:
        chip_label = (payload.get('chip_label') or '').strip().lower()
        chip_map = {
            '5': 'chip_5',
            '25': 'chip_25',
            '50': 'chip_50',
            '100': 'chip_100',
            'all-in': 'all_in',
            'allin': 'all_in',
            'vote': 'heart',
            'heart': 'heart',
        }
        vote_option_key = chip_map.get(chip_label, '')


    if not voter_session_id:
        return jsonify(success=False, error='Session fehlt'), 400
    if not vote_option_key:
        return jsonify(success=False, error='vote_option_key fehlt'), 400

    db = get_db()

    year_cfg = get_year_settings(contest_year)
    max_actions = int(year_cfg.get("max_actions", 4))

    opt_map = get_vote_option_map(contest_year)
    opt = opt_map.get(vote_option_key)
    if not opt:
        return jsonify(success=False, error='Ungültige Vote-Option'), 400

    vote_value = int(opt.get("value") or 1)
    vote_label = str(opt.get("label") or vote_option_key)
    unique_per_user = int(opt.get("unique_per_user") or 0)
    exclusive_group = (opt.get("exclusive_group") or '').strip().lower()

    # 1) Prüfen ob User schon auf dieses Bild gevotet hat (pro Bild nur 1 Vote)
    vote_exists = db.execute(
        'SELECT id, vote_option_key, vote_value FROM votes WHERE image_id = ? AND voter_session_id = ? AND contest_year = ?',
        (image_id, voter_session_id, contest_year)
    ).fetchone()

    # 2) All-in / Exklusivlogik: "all_in" blockt alles andere (wie bisher)
    all_in_vote = db.execute(
        'SELECT id, image_id FROM votes WHERE voter_session_id = ? AND contest_year = ? AND vote_option_key = ?',
        (voter_session_id, contest_year, 'all_in')
    ).fetchone()

    # 3) Pro User/Jahr Option nur einmal (falls unique_per_user)
    opt_in_use = None
    if unique_per_user:
        opt_in_use = db.execute(
            'SELECT id, image_id FROM votes WHERE voter_session_id = ? AND contest_year = ? AND vote_option_key = ?',
            (voter_session_id, contest_year, vote_option_key)
        ).fetchone()

    # Toggle-Behaviour (gleiches Bild + gleiche Option => entfernen)
    if vote_exists:
        if (vote_exists['vote_option_key'] or '') == vote_option_key:
            db.execute('DELETE FROM votes WHERE id = ?', (vote_exists['id'],))
            db.commit()
        else:
            # "Replace" auf demselben Bild: erst löschen und Client bekommt removed_only=True
            db.execute('DELETE FROM votes WHERE id = ?', (vote_exists['id'],))
            db.commit()

            updated_vote_count = db.execute(
                'SELECT COUNT(*) FROM votes WHERE voter_session_id = ? AND contest_year = ?',
                (voter_session_id, contest_year)
            ).fetchone()[0]
            return jsonify(success=True, vote_count=updated_vote_count, removed_only=True)

    else:
        # Block: Option bereits woanders verwendet?
        if opt_in_use:
            return jsonify(success=False, error=f'Option {vote_label} wurde bereits benutzt'), 403

        # Block: all_in Regeln
        if vote_option_key == 'all_in' or exclusive_group == 'allin':
            other_votes = db.execute(
                'SELECT COUNT(*) FROM votes WHERE voter_session_id = ? AND contest_year = ?',
                (voter_session_id, contest_year)
            ).fetchone()[0]
            if other_votes > 0:
                return jsonify(success=False, error='All-in geht nur, wenn keine anderen Optionen gesetzt sind'), 403
        else:
            if all_in_vote:
                return jsonify(success=False, error='All-in ist bereits gesetzt. Erst All-in entfernen.'), 403

        # Limit pro User/Jahr (max_actions)
        vote_count = db.execute(
            'SELECT COUNT(*) FROM votes WHERE voter_session_id = ? AND contest_year = ?',
            (voter_session_id, contest_year)
        ).fetchone()[0]
        if vote_count >= max_actions:
            return jsonify(success=False, error=f'Du hast das Limit ({max_actions}) erreicht'), 403

        db.execute(
            'INSERT INTO votes (image_id, voter_session_id, contest_year, vote_option_key, vote_value, vote_label) VALUES (?, ?, ?, ?, ?, ?)',
            (image_id, voter_session_id, contest_year, vote_option_key, vote_value, vote_label)
        )
        db.commit()

    updated_vote_count = db.execute(
        'SELECT COUNT(*) FROM votes WHERE voter_session_id = ? AND contest_year = ?',
        (voter_session_id, contest_year)
    ).fetchone()[0]

    return jsonify(success=True, vote_count=updated_vote_count)


@bp.route('/api/voter-state/<int:year>')
def voter_state(year: int):
    voter_session_id = request.args.get('voter_session_id', '').strip()

    year_cfg = get_year_settings(year)
    max_actions = int(year_cfg.get("max_actions", 4))
    vote_mode = (year_cfg.get("vote_mode") or "toggle").strip()

    if not voter_session_id:
        return jsonify(voted_ids=[], vote_count=0, votes_left=max_actions, bets=[])

    db = get_db()
    voted = db.execute(
        'SELECT image_id, vote_option_key, vote_label, vote_value FROM votes WHERE voter_session_id = ? AND contest_year = ?',
        (voter_session_id, year)
    ).fetchall()

    voted_ids = [row['image_id'] for row in voted]
    vote_count = len(voted_ids)

    bets = [{
        'image_id': row['image_id'],
        'vote_option_key': (row['vote_option_key'] or '').lower(),
        'vote_label': row['vote_label'] or '',
        'vote_value': row['vote_value'] or 1,
    } for row in voted]

    used_keys = {(row['vote_option_key'] or '').lower() for row in voted}

    if vote_mode == "unique_options":
        used_count = max_actions if ('all_in' in used_keys) else len(used_keys)
    else:
        used_count = len(voted)

    votes_left = max(0, max_actions - used_count)

    return jsonify(
        voted_ids=voted_ids,
        vote_count=vote_count,
        votes_left=votes_left,
        bets=bets,
        vote_mode=vote_mode,
        max_actions=max_actions
    )

@bp.route('/admin/vote-options', methods=['GET', 'POST'])
def admin_vote_options():
    if not session.get('admin'):
        return redirect(url_for('main.login'))

    db = get_db()
    year = int(request.args.get('year', current_year()))

    if request.method == 'POST':
        action = (request.form.get('action') or '').strip()

        if action == 'save_order':
            order_csv = request.form.get('order', '')
            ids = [int(x) for x in order_csv.split(',') if x.strip().isdigit()]
            for idx, opt_id in enumerate(ids, start=1):
                db.execute(
                    'UPDATE vote_options SET sort_order = ? WHERE id = ? AND contest_year = ?',
                    (idx, opt_id, year)
                )

            # active toggles
            rows = db.execute(
                'SELECT id FROM vote_options WHERE contest_year = ?',
                (year,)
            ).fetchall()
            for r in rows:
                active_val = 1 if request.form.get(f'active_{r["id"]}') else 0
                db.execute('UPDATE vote_options SET active = ? WHERE id = ?', (active_val, r['id']))

            db.commit()
            return redirect(url_for('main.admin_vote_options', year=year))

        elif action == 'upsert':
            opt_id = int(request.form.get('id', 0) or 0)
            opt_key = (request.form.get('opt_key') or '').strip()
            label = (request.form.get('label') or '').strip()
            icon = (request.form.get('icon') or '').strip()
            value = int(request.form.get('value') or 1)
            unique_per_user = 1 if request.form.get('unique_per_user') else 0
            exclusive_group = (request.form.get('exclusive_group') or '').strip().lower()
            is_special = 1 if request.form.get('is_special') else 0
            active = 1 if request.form.get('active') else 0

            if not opt_key or not label:
                return jsonify(success=False, error='opt_key und label sind Pflicht'), 400

            if opt_id > 0:
                db.execute("""
                    UPDATE vote_options
                    SET opt_key=?, label=?, icon=?, value=?, unique_per_user=?, exclusive_group=?,
                        is_special=?, active=?
                    WHERE id=? AND contest_year=?
                """, (opt_key, label, icon, value, unique_per_user, exclusive_group, is_special, active, opt_id, year))
            else:
                # sort_order ans Ende
                max_sort = db.execute(
                    'SELECT COALESCE(MAX(sort_order), 0) FROM vote_options WHERE contest_year = ?',
                    (year,)
                ).fetchone()[0]
                db.execute("""
                    INSERT INTO vote_options
                      (contest_year, opt_key, label, icon, value, unique_per_user, exclusive_group, is_special, active, sort_order, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (year, opt_key, label, icon, value, unique_per_user, exclusive_group, is_special, active, max_sort + 1, datetime.now().isoformat()))

            db.commit()
            return redirect(url_for('main.admin_vote_options', year=year))

        elif action == 'delete':
            opt_id = int(request.form.get('id', 0) or 0)
            if opt_id > 0:
                db.execute('DELETE FROM vote_options WHERE id = ? AND contest_year = ?', (opt_id, year))
                db.commit()
            return redirect(url_for('main.admin_vote_options', year=year))

    # GET
    opts = db.execute("""
        SELECT *
        FROM vote_options
        WHERE contest_year = ?
        ORDER BY sort_order ASC, id ASC
    """, (year,)).fetchall()

    year_cfg = get_year_settings(year)
    settings = get_runtime_settings()
    available_years = sorted(set([current_year(), *[int(y) for y in (settings.get('legacy_years') or [])]]), reverse=True)

    return render_template(
        'admin_vote_options.html',
        year=year,
        available_years=available_years,
        year_cfg=year_cfg,
        options=[dict(o) for o in opts],
        current_year=current_year()
    )


@bp.route('/api/vote-options/<int:year>')
def api_vote_options(year: int):
    return jsonify(get_vote_options(year))

@bp.route('/api/reset-votes/<int:year>', methods=['POST'])
def reset_votes(year: int):
    payload = request.json or {}
    voter_session_id = (payload.get('voter_session_id') or '').strip()
    if not voter_session_id:
        return jsonify(success=False, error='Session fehlt'), 400

    db = get_db()
    db.execute('DELETE FROM votes WHERE voter_session_id = ? AND contest_year = ?', (voter_session_id, year))
    db.commit()
    return jsonify(success=True)


@bp.route('/react/<int:image_id>', methods=['POST'])
def react(image_id):
    payload = request.json or {}
    voter_session_id = payload.get('voter_session_id')
    contest_year = int(payload.get('contest_year', current_year()))
    reaction_type = (payload.get('reaction_type') or '').strip().lower()

    allowed = {'funny', 'creative', 'underrated', 'hype'}
    if reaction_type not in allowed:
        return jsonify(success=False, error='Ungültige Reaktion'), 400

    if not voter_session_id:
        return jsonify(success=False, error='Session fehlt'), 400

    db = get_db()
    exists = db.execute(
        'SELECT id FROM reactions WHERE image_id = ? AND voter_session_id = ? AND reaction_type = ? AND contest_year = ?',
        (image_id, voter_session_id, reaction_type, contest_year)
    ).fetchone()

    active = False
    if exists:
        db.execute('DELETE FROM reactions WHERE id = ?', (exists['id'],))
    else:
        db.execute(
            'INSERT INTO reactions (image_id, voter_session_id, reaction_type, contest_year, created_at) VALUES (?, ?, ?, ?, ?)',
            (image_id, voter_session_id, reaction_type, contest_year, datetime.now().isoformat())
        )
        active = True

    db.commit()

    count = db.execute(
        'SELECT COUNT(*) FROM reactions WHERE image_id = ? AND contest_year = ? AND reaction_type = ?',
        (image_id, contest_year, reaction_type)
    ).fetchone()[0]

    return jsonify(success=True, active=active, count=count, reaction_type=reaction_type)


@bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form['password'] == current_app.config['ADMIN_PASSWORD']:
            session['admin'] = True
            return redirect(url_for('main.upload'))
    return render_template('login.html')


@bp.route('/logout')
def logout():
    session.pop('admin', None)
    return redirect(url_for('main.root'))


@bp.route('/upload', methods=['GET', 'POST'])
def upload():
    if not session.get('admin'):
        return redirect(url_for('main.login'))

    db = get_db()
    year = int(request.args.get('year', request.form.get('contest_year', current_year())))

    if request.method == 'POST' and 'files' in request.files:
        files = request.files.getlist('files')

        target_upload_folder = upload_folder_for_year(year)
        for file in files:
            if file and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                file.save(os.path.join(target_upload_folder, filename))
                db.execute(
                    'INSERT INTO images (filename, uploaded_at, visible, contest_year) VALUES (?, ?, ?, ?)',
                    (filename, datetime.now().isoformat(), 1, year)
                )
        db.commit()
        return redirect(url_for('main.upload', year=year))

    images = db.execute(
        'SELECT * FROM images WHERE contest_year = ? ORDER BY uploaded_at DESC',
        (year,)
    ).fetchall()

    settings = get_runtime_settings()
    available_years = sorted(
        set([current_year(), *[int(y) for y in (settings.get('legacy_years') or [])]]),
        reverse=True
    )

    return render_template(
        'upload.html',
        images=images,
        current_year=current_year(),
        year=year,
        available_years=available_years
    )



@bp.route('/admin/settings', methods=['GET', 'POST'])
def admin_settings():
    if not session.get('admin'):
        return redirect(url_for('main.login'))

    settings = get_runtime_settings()

    if request.method == 'POST':
        selected_year = int(request.form.get('current_contest_year', settings.get('current_contest_year', 2026)))
        voting_end_at = request.form.get('voting_end_at', settings.get('voting_end_at'))
        legacy_raw = request.form.get('legacy_years', '')
        block_all = bool(request.form.get('block_public_unpublished_all_years'))

        legacy_years = []
        for token in legacy_raw.split(','):
            token = token.strip()
            if token.isdigit():
                y = int(token)
                if y != selected_year:
                    legacy_years.append(y)

        if not legacy_years:
            # fallback to previous year
            legacy_years = [selected_year - 1]

        all_years = sorted(set([selected_year, *legacy_years]), reverse=True)
        waiting_map_existing = (settings.get('waiting_text_by_year') or {})
        waiting_text_by_year = {}
        for y in all_years:
            raw = request.form.get(f'waiting_text_{y}', '').strip()
            waiting_text_by_year[str(y)] = raw or waiting_map_existing.get(str(y), '') or waiting_text_for_year(y, settings)

        new_settings = {
            'current_contest_year': selected_year,
            'legacy_years': sorted(set(legacy_years), reverse=True),
            'voting_end_at': voting_end_at,
            'waiting_text_by_year': waiting_text_by_year,
            'block_public_unpublished_all_years': block_all
        }
        save_runtime_settings(new_settings)

        # Ensure year folders exist
        upload_folder_for_year(selected_year)
        sticker_folder_for_year(selected_year, create=True)
        for y in new_settings['legacy_years']:
            upload_folder_for_year(int(y))
            sticker_folder_for_year(int(y), create=True)

        return redirect(url_for('main.admin_settings'))

    available_years = sorted(set([int(settings.get('current_contest_year', current_year())), *[int(y) for y in settings.get('legacy_years', [])]]), reverse=True)
    waiting_texts = {str(y): waiting_text_for_year(int(y), settings) for y in available_years}
    return render_template('admin_settings.html', settings=settings, available_years=available_years, waiting_texts=waiting_texts)


@bp.route('/admin/stickers', methods=['GET', 'POST'])
def admin_stickers():
    if not session.get('admin'):
        return redirect(url_for('main.login'))

    db = get_db()
    year = int(request.args.get('year', request.form.get('year', current_year())))
    folder = sticker_folder_for_year(year, create=True)

    if request.method == 'POST':
        action = request.form.get('action', 'upload')

        if action == 'upload_single':
            files = request.files.getlist('files')
            for file in files:
                if file and allowed_file(file.filename):
                    filename = secure_filename(file.filename)
                    file.save(os.path.join(folder, filename))

        elif action == 'upload_zip':
            zip_file = request.files.get('zip_file')
            if zip_file and zip_file.filename.lower().endswith('.zip'):
                with zipfile.ZipFile(zip_file.stream) as zf:
                    for entry in zf.infolist():
                        if entry.is_dir():
                            continue
                        entry_name = os.path.basename(entry.filename)
                        if not entry_name or not allowed_file(entry_name):
                            continue
                        safe_name = secure_filename(entry_name)
                        with zf.open(entry) as src, open(os.path.join(folder, safe_name), 'wb') as dst:
                            dst.write(src.read())

        elif action == 'save_order':
            order_csv = request.form.get('order', '')
            order_ids = [int(x) for x in order_csv.split(',') if x.strip().isdigit()]
            for idx, sticker_id in enumerate(order_ids, start=1):
                db.execute(
                    'UPDATE stickers SET sort_order = ? WHERE id = ? AND contest_year = ?',
                    (idx, sticker_id, year)
                )

            for sticker in db.execute('SELECT id FROM stickers WHERE contest_year = ?', (year,)).fetchall():
                active_val = 1 if request.form.get(f'active_{sticker["id"]}') else 0
                db.execute('UPDATE stickers SET active = ? WHERE id = ?', (active_val, sticker['id']))

            db.commit()

        elif action == 'delete':
            sticker_id = int(request.form.get('sticker_id', 0))
            row = db.execute('SELECT filename FROM stickers WHERE id = ? AND contest_year = ?', (sticker_id, year)).fetchone()
            if row:
                file_path = os.path.join(folder, row['filename'])
                if os.path.exists(file_path):
                    os.remove(file_path)
                db.execute('DELETE FROM stickers WHERE id = ?', (sticker_id,))
                db.commit()

        ensure_sticker_records_for_year(year)
        return redirect(url_for('main.admin_stickers', year=year))

    ensure_sticker_records_for_year(year)
    stickers = db.execute(
        'SELECT * FROM stickers WHERE contest_year = ? ORDER BY sort_order ASC, id ASC',
        (year,)
    ).fetchall()
    return render_template('admin_stickers.html', year=year, stickers=stickers, current_year=current_year())


@bp.route('/update-images', methods=['POST'])
def update_images():
    if not session.get('admin'):
        return redirect(url_for('main.login'))

    db = get_db()
    year = int(request.form.get('contest_year', current_year()))

    images = db.execute(
        'SELECT id FROM images WHERE contest_year = ?',
        (year,)
    ).fetchall()

    for image in images:
        image_id = image['id']
        uploader = (request.form.get(f'uploader_{image_id}', '') or '').strip()
        description = (request.form.get(f'description_{image_id}', '') or '').strip()
        visible = 1 if request.form.get(f'visible_{image_id}') else 0

        db.execute(
            'UPDATE images SET uploader = ?, description = ?, visible = ? WHERE id = ?',
            (uploader, description, visible, image_id)
        )

    db.commit()
    return redirect(url_for('main.upload', year=year))


@bp.route('/delete-image/<int:image_id>', methods=['POST'])
def delete_image(image_id):
    if not session.get('admin'):
        return redirect(url_for('main.login'))

    db = get_db()
    image = db.execute('SELECT filename, contest_year FROM images WHERE id = ?', (image_id,)).fetchone()
    if image:
        image_path = os.path.join(upload_folder_for_year(int(image['contest_year'] or current_year())), image['filename'])
        if os.path.exists(image_path):
            os.remove(image_path)

        db.execute('DELETE FROM images WHERE id = ?', (image_id,))
        db.execute('DELETE FROM votes WHERE image_id = ?', (image_id,))
        db.commit()

    return redirect(url_for('main.upload'))


@bp.route('/results')
def results():
    if not session.get('admin'):
        return redirect(url_for('main.login'))

    year = int(request.args.get('year', current_year()))
    db = get_db()

    top_images = db.execute('''
        SELECT
            images.id,
            images.filename,
            images.uploader,
            images.description,
            images.contest_year,
            COUNT(votes.id) as vote_count,
            COALESCE(SUM(votes.vote_value), 0) as vote_points,
            SUM(CASE WHEN reactions.reaction_type = 'hype' THEN 1 ELSE 0 END) as hype_count,
            SUM(CASE WHEN reactions.reaction_type = 'creative' THEN 1 ELSE 0 END) as creative_count,
            SUM(CASE WHEN reactions.reaction_type = 'funny' THEN 1 ELSE 0 END) as funny_count,
            SUM(CASE WHEN reactions.reaction_type = 'underrated' THEN 1 ELSE 0 END) as underrated_count,
            (COALESCE(SUM(votes.vote_value), 0)
            + SUM(CASE WHEN reactions.reaction_type = 'hype' THEN 1 ELSE 0 END) * 2
            + SUM(CASE WHEN reactions.reaction_type = 'creative' THEN 1 ELSE 0 END) * 2
            + SUM(CASE WHEN reactions.reaction_type = 'funny' THEN 1 ELSE 0 END) * 1
            + SUM(CASE WHEN reactions.reaction_type = 'underrated' THEN 1 ELSE 0 END) * 1) as weighted_score
        FROM images
        LEFT JOIN votes
        ON images.id = votes.image_id AND votes.contest_year = images.contest_year
        LEFT JOIN reactions
        ON images.id = reactions.image_id AND reactions.contest_year = images.contest_year
        WHERE images.contest_year = ?
        AND (votes.id IS NOT NULL OR reactions.id IS NOT NULL)
        GROUP BY images.id
        ORDER BY weighted_score DESC, vote_count DESC
    ''', (year,)).fetchall()

    total_votes = db.execute(
        'SELECT COUNT(*) FROM votes WHERE contest_year = ?',
        (year,)
    ).fetchone()[0]
    voters = db.execute(
        'SELECT COUNT(DISTINCT voter_session_id) FROM votes WHERE contest_year = ?',
        (year,)
    ).fetchone()[0]

    settings = get_runtime_settings()
    available_years = sorted(
        set([current_year(), *[int(y) for y in (settings.get('legacy_years') or [])]]),
        reverse=True
    )

    published = is_published(year)


    return render_template(
        'results.html',
        top_images=top_images,
        voters=voters,
        total_votes=total_votes,
        published=published,
        show_stats=True,
        current_year=current_year(),
        year=year,
        available_years=available_years
    )



@bp.route('/public-results')
def public_results():
    # Default should always point to current contest year's public results
    return redirect(url_for('main.public_results_year', year=current_year()))


@bp.route('/public-results/<int:year>')
def public_results_year(year: int):
    settings = get_runtime_settings()

    published = is_published(year)

    # ✅ Optionaler Testmodus: alle Jahre blocken, wenn nicht published
    block_all = bool(settings.get('block_public_unpublished_all_years', False))

    if (block_all and not published) or (year == current_year() and not published):
        return render_template(
            waiting_template_for_year(year),
            year=year,
            waiting_text=waiting_text_for_year(year, settings)
        )

    db = get_db()

    ranking_rows = db.execute('''
        SELECT
            images.id,
            images.filename,
            images.uploader,
            images.description,
            COUNT(votes.id) as vote_count,
            COALESCE(SUM(votes.vote_value), 0) as vote_points,
            SUM(CASE WHEN reactions.reaction_type = 'hype' THEN 1 ELSE 0 END) as hype_count,
            SUM(CASE WHEN reactions.reaction_type = 'creative' THEN 1 ELSE 0 END) as creative_count,
            SUM(CASE WHEN reactions.reaction_type = 'funny' THEN 1 ELSE 0 END) as funny_count,
            SUM(CASE WHEN reactions.reaction_type = 'underrated' THEN 1 ELSE 0 END) as underrated_count,
            (COALESCE(SUM(votes.vote_value), 0)
             + SUM(CASE WHEN reactions.reaction_type = 'hype' THEN 1 ELSE 0 END) * 2
             + SUM(CASE WHEN reactions.reaction_type = 'creative' THEN 1 ELSE 0 END) * 2
             + SUM(CASE WHEN reactions.reaction_type = 'funny' THEN 1 ELSE 0 END) * 1
             + SUM(CASE WHEN reactions.reaction_type = 'underrated' THEN 1 ELSE 0 END) * 1) as weighted_score
        FROM images
        LEFT JOIN votes ON images.id = votes.image_id AND votes.contest_year = images.contest_year
        LEFT JOIN reactions ON images.id = reactions.image_id AND reactions.contest_year = images.contest_year
        WHERE images.visible = 1 AND images.contest_year = ?
        GROUP BY images.id
        ORDER BY weighted_score DESC, vote_count DESC
    ''', (year,)).fetchall()

    top_images = ranking_rows[:5]
    top_10_images = ranking_rows[:10]
    top_images_json = [dict(r) for r in top_images]
    ranking_images_json = [dict(r) for r in ranking_rows]

    templates_dir = os.path.join(current_app.root_path, 'templates')
    year_template_name = f'public_results_{year}.html'
    year_template_path = os.path.join(templates_dir, year_template_name)

    if os.path.exists(year_template_path):
        template = year_template_name
    else:
        current_template_name = f'public_results_{current_year()}.html'
        current_template_path = os.path.join(templates_dir, current_template_name)
        if os.path.exists(current_template_path):
            template = current_template_name
        else:
            # Last-resort fallback to any yearly public results template
            if not os.path.isdir(templates_dir):
                return ('Templates directory missing', 500)
            candidates = sorted(
                [f for f in os.listdir(templates_dir) if f.startswith('public_results_') and f.endswith('.html')]
            )
            if not candidates:
                return ('No public results template found', 500)
            template = candidates[-1]

    return render_template(
        template,
        top_images=top_images,
        top_10_images=top_10_images,
        top_images_json=top_images_json,
        ranking_images_json=ranking_images_json,
        year=year
    )


@bp.route('/toggle-publish', methods=['POST'])
def toggle_publish():
    if not session.get('admin'):
        return redirect(url_for('main.login'))

    year = int(request.form.get('year', current_year()))
    action = request.form.get('action')

    set_published(year, action == 'show')
    return redirect(url_for('main.results', year=year))



@bp.route('/admin/reset-year-votes', methods=['POST'])
def reset_year_votes_admin():
    if not session.get('admin'):
        return redirect(url_for('main.login'))

    try:
        year = int(request.form.get('year', current_year()))
    except Exception:
        year = current_year()

    db = get_db()
    db.execute('DELETE FROM votes WHERE contest_year = ?', (year,))
    db.execute('DELETE FROM reactions WHERE contest_year = ?', (year,))
    db.execute('DELETE FROM duel_votes WHERE contest_year = ?', (year,))
    db.commit()

    return redirect(url_for('main.results'))


@bp.route('/api/stickers')
def list_stickers():
    # Backward compatible default for current year
    return list_stickers_for_year(current_year())


@bp.route('/api/stickers/<int:year>')
def list_stickers_for_year(year: int):
    db = get_db()
    ensure_sticker_records_for_year(year)

    rows = db.execute(
        'SELECT filename FROM stickers WHERE contest_year = ? AND active = 1 ORDER BY sort_order ASC, id ASC',
        (year,)
    ).fetchall()
    return jsonify([r['filename'] for r in rows])


