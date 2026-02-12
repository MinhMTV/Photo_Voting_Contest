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
    defaults = {
        'current_contest_year': int(current_app.config.get('CURRENT_CONTEST_YEAR', 2026)),
        'legacy_years': current_app.config.get('LEGACY_CONTEST_YEARS', [2025]),
        'voting_end_at': current_app.config.get('VOTING_END_AT', '2026-12-31T23:59:59')
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
        'SELECT image_id FROM votes WHERE voter_session_id = ? AND contest_year = ?',
        (voter_session_id, year)
    ).fetchall()
    voted_ids = [row['image_id'] for row in voted]
    votes_left = max(0, 3 - len(voted_ids))

    settings = get_runtime_settings()
    legacy_years = settings.get('legacy_years', [2025])

    return render_template(
        'index_casino_2025.html',
        images=images,
        voted_ids=voted_ids,
        votes_left=votes_left,
        year=year,
        legacy_years=legacy_years,
        voting_end_at=settings.get('voting_end_at')
    )


@bp.route('/duel/<int:year>')
def duel_year(year: int):
    if year != current_year():
        return redirect(url_for('main.public_results_year', year=year))

    db = get_db()
    candidates = db.execute(
        'SELECT * FROM images WHERE visible = 1 AND contest_year = ? ORDER BY RANDOM() LIMIT 2',
        (year,)
    ).fetchall()

    if len(candidates) < 2:
        return redirect(url_for('main.contest_year', year=year))

    return render_template('duel.html', year=year, a=candidates[0], b=candidates[1])


@bp.route('/vote/<int:image_id>', methods=['POST'])
def vote(image_id):
    voter_session_id = request.json.get('voter_session_id')
    contest_year = int(request.json.get('contest_year', current_year()))
    db = get_db()

    vote_exists = db.execute(
        'SELECT 1 FROM votes WHERE image_id = ? AND voter_session_id = ? AND contest_year = ?',
        (image_id, voter_session_id, contest_year)
    ).fetchone()

    if vote_exists:
        db.execute(
            'DELETE FROM votes WHERE image_id = ? AND voter_session_id = ? AND contest_year = ?',
            (image_id, voter_session_id, contest_year)
        )
    else:
        vote_count = db.execute(
            'SELECT COUNT(*) FROM votes WHERE voter_session_id = ? AND contest_year = ?',
            (voter_session_id, contest_year)
        ).fetchone()[0]

        if vote_count >= 3:
            return jsonify(success=False, error='Du hast das Stimmenlimit erreicht'), 403

        db.execute(
            'INSERT INTO votes (image_id, voter_session_id, contest_year) VALUES (?, ?, ?)',
            (image_id, voter_session_id, contest_year)
        )

    db.commit()

    updated_vote_count = db.execute(
        'SELECT COUNT(*) FROM votes WHERE voter_session_id = ? AND contest_year = ?',
        (voter_session_id, contest_year)
    ).fetchone()[0]

    return jsonify(success=True, vote_count=updated_vote_count)


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

        new_settings = {
            'current_contest_year': selected_year,
            'legacy_years': sorted(set(legacy_years), reverse=True),
            'voting_end_at': voting_end_at
        }
        save_runtime_settings(new_settings)

        # Ensure year folders exist
        upload_folder_for_year(selected_year)
        sticker_folder_for_year(selected_year, create=True)
        for y in new_settings['legacy_years']:
            upload_folder_for_year(int(y))
            sticker_folder_for_year(int(y), create=True)

        return redirect(url_for('main.admin_settings'))

    return render_template('admin_settings.html', settings=settings)


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
        SELECT images.id, images.filename, images.uploader, images.description, images.contest_year, COUNT(votes.id) as vote_count
        FROM images
        LEFT JOIN votes ON images.id = votes.image_id
        WHERE votes.id IS NOT NULL
        GROUP BY images.id
        ORDER BY vote_count DESC
    ''').fetchall()

    total_votes = db.execute('SELECT COUNT(*) FROM votes WHERE id IS NOT NULL').fetchone()[0]
    voters = db.execute('SELECT COUNT(DISTINCT voter_session_id) FROM votes').fetchone()[0]

    flag_path = os.path.join(current_app.root_path, 'published_flag.txt')
    published = os.path.exists(flag_path) and open(flag_path).read().strip() == '1'

    return render_template('results.html', top_images=top_images, voters=voters, total_votes=total_votes, published=published, show_stats=True)


@bp.route('/public-results')
def public_results():
    # Keep existing endpoint for old integrations (redirect to latest legacy year)
    settings = get_runtime_settings()
    legacy_years = settings.get('legacy_years', [current_year() - 1])
    return redirect(url_for('main.public_results_year', year=int(legacy_years[0])))


@bp.route('/public-results/<int:year>')
def public_results_year(year: int):
    db = get_db()

    top_images = db.execute('''
        SELECT images.id, images.filename, images.uploader, images.description,
               COUNT(votes.id) as vote_count
        FROM images
        LEFT JOIN votes ON images.id = votes.image_id
        WHERE images.visible = 1 AND images.contest_year = ?
        GROUP BY images.id
        ORDER BY vote_count DESC
        LIMIT 5
    ''', (year,)).fetchall()

    template = 'public_results_2025_casino.html' if year == current_year() else 'public_results.html'
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
