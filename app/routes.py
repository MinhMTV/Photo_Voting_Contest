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
            str(base_year): 'Die Abstimmung lÃ¤uft noch. Ergebnisse werden nach Freigabe verÃ¶ffentlicht.'
        }
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


def waiting_text_for_year(year: int, settings: dict | None = None) -> str:
    cfg = settings or get_runtime_settings()
    waiting_map = cfg.get('waiting_text_by_year', {}) or {}
    return waiting_map.get(str(year), 'Die Abstimmung lÃ¤uft noch. Ergebnisse werden nach Freigabe verÃ¶ffentlicht.')


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
    voted = db.execute(
        'SELECT image_id, chip_value, chip_label FROM votes WHERE voter_session_id = ? AND contest_year = ?',
        (voter_session_id, year)
    ).fetchall()
    voted_ids = [row['image_id'] for row in voted]
    user_bets = {str(row['image_id']): {'chip_value': row['chip_value'] or 1, 'chip_label': row['chip_label'] or 'vote'} for row in voted}
    used_labels = {(row['chip_label'] or '').lower() for row in voted}
    used_count = 4 if ('all-in' in used_labels or 'allin' in used_labels) else len(used_labels)
    votes_left = max(0, 4 - used_count)

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


@bp.route('/api/duel-spin/<int:year>')
def duel_spin(year: int):
    db = get_db()
    rows = db.execute(
        'SELECT id, filename, uploader, description FROM images WHERE visible = 1 AND contest_year = ? ORDER BY RANDOM() LIMIT 3',
        (year,)
    ).fetchall()

    if len(rows) < 3:
        return jsonify(success=False, error='Nicht genug Bilder fÃ¼r Duel-Slot'), 400

    return jsonify(success=True, candidates=[{
        'id': r['id'],
        'filename': r['filename'],
        'uploader': r['uploader'] or 'Unbekannt',
        'description': r['description'] or ''
    } for r in rows])


@bp.route('/vote/<int:image_id>', methods=['POST'])
def vote(image_id):
    payload = request.json or {}
    voter_session_id = payload.get('voter_session_id')
    contest_year = int(payload.get('contest_year', current_year()))
    chip_label = (payload.get('chip_label') or '5').strip().lower()

    chip_map = {
        '5': 5,
        '25': 25,
        '50': 50,
        '100': 100,
        'all-in': 180,
        'allin': 180
    }
    chip_value = chip_map.get(chip_label, 5)
    if chip_label not in chip_map:
        chip_label = str(chip_value)

    db = get_db()

    vote_exists = db.execute(
        'SELECT id, chip_value FROM votes WHERE image_id = ? AND voter_session_id = ? AND contest_year = ?',
        (image_id, voter_session_id, contest_year)
    ).fetchone()

    # each chip can be used only once per user/year
    chip_in_use = db.execute(
        'SELECT id, image_id FROM votes WHERE voter_session_id = ? AND contest_year = ? AND chip_label = ?',
        (voter_session_id, contest_year, chip_label)
    ).fetchone()
    all_in_vote = db.execute(
        'SELECT id, image_id FROM votes WHERE voter_session_id = ? AND contest_year = ? AND chip_label IN ("all-in", "allin")',
        (voter_session_id, contest_year)
    ).fetchone()

    if vote_exists:
        # Same chip on same image => remove vote
        if int(vote_exists['chip_value'] or 0) == chip_value:
            db.execute('DELETE FROM votes WHERE id = ?', (vote_exists['id'],))
        else:
            # Two-step replace behavior on same image: first click removes old chip only.
            db.execute('DELETE FROM votes WHERE id = ?', (vote_exists['id'],))
            db.commit()
            updated_vote_count = db.execute(
                'SELECT COUNT(*) FROM votes WHERE voter_session_id = ? AND contest_year = ?',
                (voter_session_id, contest_year)
            ).fetchone()[0]
            return jsonify(success=True, vote_count=updated_vote_count, removed_only=True)
    else:
        if chip_in_use:
            return jsonify(success=False, error=f'Chip {chip_label.upper()} wurde bereits benutzt'), 403

        if chip_label in ('all-in', 'allin'):
            other_votes = db.execute(
                'SELECT COUNT(*) FROM votes WHERE voter_session_id = ? AND contest_year = ?',
                (voter_session_id, contest_year)
            ).fetchone()[0]
            if other_votes > 0:
                return jsonify(success=False, error='All-in geht nur, wenn keine anderen Chips gesetzt sind'), 403
        elif all_in_vote:
            return jsonify(success=False, error='All-in ist bereits gesetzt. Erst All-in entfernen.'), 403

        vote_count = db.execute(
            'SELECT COUNT(*) FROM votes WHERE voter_session_id = ? AND contest_year = ?',
            (voter_session_id, contest_year)
        ).fetchone()[0]

        if vote_count >= 4:
            return jsonify(success=False, error='Du hast das Chip-Limit (4) erreicht'), 403

        db.execute(
            'INSERT INTO votes (image_id, voter_session_id, contest_year, chip_value, chip_label) VALUES (?, ?, ?, ?, ?)',
            (image_id, voter_session_id, contest_year, chip_value, chip_label)
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
    if not voter_session_id:
        return jsonify(voted_ids=[], vote_count=0, votes_left=4, bets=[])

    db = get_db()
    voted = db.execute(
        'SELECT image_id, chip_label FROM votes WHERE voter_session_id = ? AND contest_year = ?',
        (voter_session_id, year)
    ).fetchall()
    voted_ids = [row['image_id'] for row in voted]
    vote_count = len(voted_ids)
    bets = [{'image_id': row['image_id'], 'chip_label': (row['chip_label'] or '').lower()} for row in voted]
    labels = {b['chip_label'] for b in bets}
    used_count = 4 if ('all-in' in labels or 'allin' in labels) else len(labels)
    return jsonify(voted_ids=voted_ids, vote_count=vote_count, votes_left=max(0, 4 - used_count), bets=bets)


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
        return jsonify(success=False, error='UngÃ¼ltige Reaktion'), 400

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

    if request.method == 'POST' and 'files' in request.files:
        files = request.files.getlist('files')
        year = int(request.form.get('contest_year', current_year()))

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
        return redirect(url_for('main.upload'))

    images = db.execute('SELECT * FROM images ORDER BY uploaded_at DESC').fetchall()
    return render_template('upload.html', images=images, current_year=current_year())


@bp.route('/admin/settings', methods=['GET', 'POST'])
def admin_settings():
    if not session.get('admin'):
        return redirect(url_for('main.login'))

    settings = get_runtime_settings()

    if request.method == 'POST':
        selected_year = int(request.form.get('current_contest_year', settings.get('current_contest_year', 2026)))
        voting_end_at = request.form.get('voting_end_at', settings.get('voting_end_at'))
        legacy_raw = request.form.get('legacy_years', '')

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
            'waiting_text_by_year': waiting_text_by_year
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

    images = db.execute('SELECT id FROM images').fetchall()
    for image in images:
        image_id = image['id']
        uploader = request.form.get(f'uploader_{image_id}', '')
        description = request.form.get(f'description_{image_id}', '')
        visible = 1 if request.form.get(f'visible_{image_id}') else 0

        db.execute(
            'UPDATE images SET uploader = ?, description = ?, visible = ? WHERE id = ?',
            (uploader, description, visible, image_id)
        )

    db.commit()
    return redirect(url_for('main.upload'))


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
    # Legacy admin results page preserved
    db = get_db()
    top_images = db.execute('''
        SELECT
            images.id,
            images.filename,
            images.uploader,
            images.description,
            images.contest_year,
            COUNT(votes.id) as vote_count,
            COALESCE(SUM(votes.chip_value), 0) as vote_points,
            SUM(CASE WHEN reactions.reaction_type = 'hype' THEN 1 ELSE 0 END) as hype_count,
            SUM(CASE WHEN reactions.reaction_type = 'creative' THEN 1 ELSE 0 END) as creative_count,
            SUM(CASE WHEN reactions.reaction_type = 'funny' THEN 1 ELSE 0 END) as funny_count,
            SUM(CASE WHEN reactions.reaction_type = 'underrated' THEN 1 ELSE 0 END) as underrated_count,
            (COALESCE(SUM(votes.chip_value), 0)
             + SUM(CASE WHEN reactions.reaction_type = 'hype' THEN 1 ELSE 0 END) * 2
             + SUM(CASE WHEN reactions.reaction_type = 'creative' THEN 1 ELSE 0 END) * 2
             + SUM(CASE WHEN reactions.reaction_type = 'funny' THEN 1 ELSE 0 END) * 1
             + SUM(CASE WHEN reactions.reaction_type = 'underrated' THEN 1 ELSE 0 END) * 1) as weighted_score
        FROM images
        LEFT JOIN votes ON images.id = votes.image_id
        LEFT JOIN reactions ON images.id = reactions.image_id AND reactions.contest_year = images.contest_year
        WHERE votes.id IS NOT NULL OR reactions.id IS NOT NULL
        GROUP BY images.id
        ORDER BY weighted_score DESC, vote_count DESC
    ''').fetchall()

    total_votes = db.execute('SELECT COUNT(*) FROM votes WHERE id IS NOT NULL').fetchone()[0]
    voters = db.execute('SELECT COUNT(DISTINCT voter_session_id) FROM votes').fetchone()[0]

    flag_path = os.path.join(current_app.root_path, 'published_flag.txt')
    published = os.path.exists(flag_path) and open(flag_path).read().strip() == '1'

    return render_template('results.html', top_images=top_images, voters=voters, total_votes=total_votes, published=published, show_stats=True)


@bp.route('/public-results')
def public_results():
    # Default should always point to current contest year's public results
    return redirect(url_for('main.public_results_year', year=current_year()))


@bp.route('/public-results/<int:year>')
def public_results_year(year: int):
    # Hide current year's public results until admin publishes them
    flag_path = os.path.join(current_app.root_path, 'published_flag.txt')
    published = os.path.exists(flag_path) and open(flag_path).read().strip() == '1'
    settings = get_runtime_settings()
    if year == current_year() and not published:
        return render_template(waiting_template_for_year(year), year=year, waiting_text=waiting_text_for_year(year, settings))

    db = get_db()

    top_images = db.execute('''
        SELECT
            images.id,
            images.filename,
            images.uploader,
            images.description,
            COUNT(votes.id) as vote_count,
            COALESCE(SUM(votes.chip_value), 0) as vote_points,
            SUM(CASE WHEN reactions.reaction_type = 'hype' THEN 1 ELSE 0 END) as hype_count,
            SUM(CASE WHEN reactions.reaction_type = 'creative' THEN 1 ELSE 0 END) as creative_count,
            SUM(CASE WHEN reactions.reaction_type = 'funny' THEN 1 ELSE 0 END) as funny_count,
            SUM(CASE WHEN reactions.reaction_type = 'underrated' THEN 1 ELSE 0 END) as underrated_count,
            (COALESCE(SUM(votes.chip_value), 0)
             + SUM(CASE WHEN reactions.reaction_type = 'hype' THEN 1 ELSE 0 END) * 2
             + SUM(CASE WHEN reactions.reaction_type = 'creative' THEN 1 ELSE 0 END) * 2
             + SUM(CASE WHEN reactions.reaction_type = 'funny' THEN 1 ELSE 0 END) * 1
             + SUM(CASE WHEN reactions.reaction_type = 'underrated' THEN 1 ELSE 0 END) * 1) as weighted_score
        FROM images
        LEFT JOIN votes ON images.id = votes.image_id
        LEFT JOIN reactions ON images.id = reactions.image_id AND reactions.contest_year = images.contest_year
        WHERE images.visible = 1 AND images.contest_year = ?
        GROUP BY images.id
        ORDER BY weighted_score DESC, vote_count DESC
        LIMIT 5
    ''', (year,)).fetchall()

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

    return render_template(template, top_images=top_images, year=year)


@bp.route('/toggle-publish', methods=['POST'])
def toggle_publish():
    if not session.get('admin'):
        return redirect(url_for('main.login'))

    action = request.form.get('action')
    flag_path = os.path.join(current_app.root_path, 'published_flag.txt')

    with open(flag_path, 'w') as f:
        f.write('1' if action == 'show' else '0')

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


