import os
from datetime import datetime
from flask import send_from_directory

from flask import Blueprint, render_template, request, redirect, url_for, session, current_app, jsonify
from werkzeug.utils import secure_filename

from .db import get_db

bp = Blueprint('main', __name__)
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def current_year() -> int:
    return int(current_app.config.get('CURRENT_CONTEST_YEAR', 2026))


def upload_folder_for_year(year: int) -> str:
    base_static = current_app.static_folder
    path = os.path.join(base_static, f'uploads_{year}')
    os.makedirs(path, exist_ok=True)
    return path


def sticker_folder_for_year(year: int) -> str | None:
    base_static = current_app.static_folder
    preferred = os.path.join(base_static, f'stickers_{year}')
    if os.path.isdir(preferred):
        return preferred
    fallback = os.path.join(base_static, 'stickers')
    if os.path.isdir(fallback):
        return fallback
    return None


@bp.route('/')
def root():
    return redirect(url_for('main.contest_year', year=current_year()))


@bp.route('/media/<int:year>/<path:filename>')
def media_year(year: int, filename: str):
    return send_from_directory(upload_folder_for_year(year), filename)


@bp.route('/sticker/<int:year>/<path:filename>')
def sticker_year(year: int, filename: str):
    folder = sticker_folder_for_year(year)
    if not folder:
        return ('Not Found', 404)
    return send_from_directory(folder, filename)


@bp.route('/contest/<int:year>')
def contest_year(year: int):
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

    legacy_years = current_app.config.get('LEGACY_CONTEST_YEARS', [2024])

    return render_template(
        'index_casino_2025.html',
        images=images,
        voted_ids=voted_ids,
        votes_left=votes_left,
        year=year,
        legacy_years=legacy_years,
        voting_end_at=current_app.config.get('VOTING_END_AT')
    )


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
        SELECT images.id, images.filename, images.uploader, images.description, COUNT(votes.id) as vote_count
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
def public_results_legacy():
    # Keep existing endpoint for old integrations (2024 style)
    return redirect(url_for('main.public_results_year', year=2024))


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


def _sticker_index(filename: str) -> int | None:
    base = os.path.splitext(filename)[0]
    return int(base) if base.isdigit() else None


@bp.route('/api/stickers')
def list_stickers():
    # Backward compatible default (all sticker files)
    sticker_folder = os.path.join(current_app.static_folder, 'stickers')
    files = [f for f in os.listdir(sticker_folder) if f.endswith('.webp')]
    return jsonify(sorted(files))


@bp.route('/api/stickers/<int:year>')
def list_stickers_for_year(year: int):
    folder = sticker_folder_for_year(year)
    if not folder:
        return jsonify([])

    files = [f for f in os.listdir(folder) if f.endswith('.webp')]

    # If per-year folders exist (stickers_2026, stickers_2025, ...), return all from that folder.
    if os.path.basename(folder).startswith('stickers_'):
        return jsonify(sorted(files))

    # Fallback compatibility for old single-folder setup with numeric split.
    indexed: list[tuple[int, str]] = []
    for f in files:
        idx = _sticker_index(f)
        if idx is not None:
            indexed.append((idx, f))

    if year <= 2025:
        filtered = [name for idx, name in indexed if idx <= 44]
    else:
        filtered = [name for idx, name in indexed if idx >= 45]

    return jsonify(sorted(filtered, key=lambda x: int(os.path.splitext(x)[0])))
