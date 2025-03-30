import uuid

from flask import Blueprint, render_template, request, redirect, url_for, session, current_app, jsonify
import os
from werkzeug.utils import secure_filename
from datetime import datetime
from .db import get_db

bp = Blueprint('main', __name__)
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}

# === Hilfsfunktionen ===
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_voter_ip():
    # Versuche, die voter_session_id aus dem Cookie zu bekommen
    voter_session_id = request.cookies.get('voter_session_id')

    # Wenn noch kein Cookie gesetzt wurde, erstellen wir eine neue ID
    if not voter_session_id:
        # Erstelle eine neue eindeutige ID, wenn der Benutzer noch nicht identifiziert wurde
        voter_session_id = str(uuid.uuid4())

        # Setze den Cookie für den Benutzer (Gültig für 1 Jahr)
        response = jsonify(success=True)  # Erstelle eine Antwort
        response.set_cookie('voter_session_id', voter_session_id, max_age=365*24*60*60)  # Setze das Cookie
        print(f"Cookies values {request.cookies}")
        # Gib die Antwort zurück, die den Cookie gesetzt hat
        return voter_session_id  # Rückgabe nur der ID

    # Wenn der Cookie schon existiert, einfach die ID zurückgeben
    return voter_session_id

# === Startseite / Galerie ===
@bp.route('/')
def index():
    db = get_db()
    images = db.execute('SELECT * FROM images WHERE visible = 1 ORDER BY uploaded_at DESC').fetchall()

    # Hole die voter_session_id aus dem localStorage (vom Frontend geschickt)
    voter_session_id = request.cookies.get('voter_session_id')  # oder aus localStorage, falls nötig

    # Hole alle Bild-IDs, die der Benutzer bereits abgestimmt hat
    voted = db.execute('SELECT image_id FROM votes WHERE voter_session_id = ?', (voter_session_id,)).fetchall()
    voted_ids = [row['image_id'] for row in voted]

    votes_left = max(0, 3 - len(voted_ids))  # max. 3 Stimmen

    return render_template('index.html', images=images, voted_ids=voted_ids, votes_left=votes_left)

# === Voting (IP-gebunden, 1 Stimme pro Bild) ===
@bp.route('/vote/<int:image_id>', methods=['POST'])
def vote(image_id):
    # Hole die voter_session_id aus dem localStorage
    voter_session_id = request.json.get('voter_session_id')
    db = get_db()

    # Prüfe, ob der Benutzer schon für dieses Bild abgestimmt hat
    vote_exists = db.execute(
        'SELECT 1 FROM votes WHERE image_id = ? AND voter_session_id = ?',
        (image_id, voter_session_id)
    ).fetchone()

    if vote_exists:
        # Entferne die Stimme, falls sie existiert
        db.execute('DELETE FROM votes WHERE image_id = ? AND voter_session_id = ?', (image_id, voter_session_id))
    else:
        # Wenn nicht, füge die Stimme hinzu
        vote_count = db.execute(
            'SELECT COUNT(*) FROM votes WHERE voter_session_id = ?',
            (voter_session_id,)
        ).fetchone()[0]

        # Begrenze die Stimmen auf 3
        if vote_count >= 3:
            return jsonify(success=False, error="Du hast das Stimmenlimit erreicht"), 403

        db.execute('INSERT INTO votes (image_id, voter_session_id) VALUES (?, ?)', (image_id, voter_session_id))

    db.commit()

    updated_vote_count = db.execute(
        'SELECT COUNT(*) FROM votes WHERE voter_session_id = ?',
        (voter_session_id,)
    ).fetchone()[0]

    return jsonify(success=True, vote_count=updated_vote_count)


# === Login (einfacher Passwortschutz) ===
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
    return redirect(url_for('main.index'))

# === Admin-Bereich: Upload ===
@bp.route('/upload', methods=['GET', 'POST'])
def upload():
    if not session.get('admin'):
        return redirect(url_for('main.login'))

    db = get_db()

    if request.method == 'POST' and 'files' in request.files:
        files = request.files.getlist('files')
        for file in files:
            if file and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                file.save(os.path.join(current_app.config['UPLOAD_FOLDER'], filename))
                db.execute(
                    'INSERT INTO images (filename, uploaded_at, visible) VALUES (?, ?, ?)',
                    (filename, datetime.now().isoformat(), 1)
                )
                db.commit()
        return redirect(url_for('main.upload'))

    images = db.execute('SELECT * FROM images ORDER BY uploaded_at DESC').fetchall()
    return render_template('upload.html', images=images)

@bp.route('/update-images', methods=['POST'])
def update_images():
    print("📥 update_images() wurde aufgerufen")
    if not session.get('admin'):
        return redirect(url_for('main.login'))

    db = get_db()
    action = request.form.get('action', 'save')

    images = db.execute('SELECT id FROM images').fetchall()
    for image in images:
        image_id = image['id']
        uploader = request.form.get(f'uploader_{image_id}', '')
        description = request.form.get(f'description_{image_id}', '')

        # Bei "publish" → alle Bilder mit checkbox sichtbar machen
        if action == 'publish':
            visible = 1 if request.form.get(f'visible_{image_id}') else 0
        else:
            # Nur bei "save": Checkbox entscheidet über Sichtbarkeit
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
    # Bilddateiname holen
    image = db.execute('SELECT filename FROM images WHERE id = ?', (image_id,)).fetchone()
    if image:
        image_path = os.path.join(current_app.config['UPLOAD_FOLDER'], image['filename'])
        if os.path.exists(image_path):
            os.remove(image_path)

        # DB-Eintrag löschen
        db.execute('DELETE FROM images WHERE id = ?', (image_id,))
        db.execute('DELETE FROM votes WHERE image_id = ?', (image_id,))
        db.commit()

    return redirect(url_for('main.upload'))

# === Ergebnisseite: Top 10 nach Stimmen ===
@bp.route('/results', methods=['GET', 'POST'])
def results():
    db = get_db()
    show_stats = request.form.get('show_stats') == '1'

    if show_stats:
        # Zählt die Votes für jedes Bild, basierend auf der Anzahl der Einträge in der 'votes' Tabelle
        top_images = db.execute('''
            SELECT images.id, images.filename, images.uploader, images.description, COUNT(votes.id) as vote_count
            FROM images
            LEFT JOIN votes ON images.id = votes.image_id
            WHERE votes.id IS NOT NULL  -- Nur Votes zählen, die noch existieren
            GROUP BY images.id
            ORDER BY vote_count DESC
        ''').fetchall()
    else:
        top_images = db.execute('''
            SELECT images.id, images.filename, images.uploader, images.description
            FROM images
            WHERE visible = 1
            ORDER BY RANDOM()
        ''').fetchall()

    # Zählt nur gültige, nicht zurückgezogene Stimmen
    # Hier werden alle vorhandenen Stimmen gezählt (nicht gelöscht)
    total_votes = db.execute('SELECT COUNT(*) FROM votes WHERE id IS NOT NULL').fetchone()[0]

    # Zählt die einzigartigen Wähler
    voters = db.execute('SELECT COUNT(DISTINCT voter_session_id) FROM votes').fetchone()[0]

    flag_path = os.path.join(current_app.root_path, 'published_flag.txt')
    published = os.path.exists(flag_path) and open(flag_path).read().strip() == '1'

    return render_template('results.html',
                           top_images=top_images,
                           voters=voters,
                           total_votes=total_votes,
                           published=published,
                           show_stats=show_stats)

@bp.route('/public-results')
def public_results():
    flag_path = os.path.join(current_app.root_path, 'published_flag.txt')
    if not os.path.exists(flag_path) or open(flag_path).read().strip() != '1':
        return render_template('public_waiting.html')

    db = get_db()
    top_images = db.execute('''
        SELECT images.id, images.filename, images.uploader, images.description,
               COUNT(votes.id) as vote_count
        FROM images
        LEFT JOIN votes ON images.id = votes.image_id
        WHERE images.visible = 1
        GROUP BY images.id
        ORDER BY vote_count DESC
        LIMIT 5
    ''').fetchall()

    return render_template('public_results.html', top_images=top_images)

@bp.route('/toggle-publish', methods=['POST'])
def toggle_publish():
    if not session.get('admin'):
        return redirect(url_for('main.login'))

    action = request.form.get('action')
    flag_path = os.path.join(current_app.root_path, 'published_flag.txt')

    with open(flag_path, 'w') as f:
        f.write('1' if action == 'show' else '0')

    return redirect(url_for('main.results'))