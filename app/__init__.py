from flask import Flask
import os
from dotenv import load_dotenv

def create_app():
    app = Flask(__name__, instance_relative_config=True)
    # Eigene .flask_env statt .env laden
    load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '.flask_env'))

    app.config.from_mapping(
        ADMIN_PASSWORD=os.getenv("ADMIN_PASSWORD", "admin123"),
        SECRET_KEY=os.getenv("SECRET_KEY", "dev123"),
        UPLOAD_FOLDER=os.path.join(app.root_path, 'static/uploads'),
        DATABASE=os.path.join(app.instance_path, 'votes.db'),
        CURRENT_CONTEST_YEAR=int(os.getenv("CURRENT_CONTEST_YEAR", "2026")),
        LEGACY_CONTEST_YEARS=[2025],
        VOTING_END_AT=os.getenv("VOTING_END_AT", "2026-12-31T23:59:59")
    )

    load_dotenv()
    # Ordner sicherstellen
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    os.makedirs(app.instance_path, exist_ok=True)
    os.makedirs(os.path.join(app.root_path, f"static/uploads_{app.config['CURRENT_CONTEST_YEAR']}"), exist_ok=True)
    for y in app.config.get('LEGACY_CONTEST_YEARS', []):
        os.makedirs(os.path.join(app.root_path, f"static/uploads_{y}"), exist_ok=True)

    # DB initialisieren + Migrationen sicher ausführen
    from . import db
    db.init_app(app)
    with app.app_context():
        db.init_db()
        db.migrate_uploads_to_year_dirs(default_legacy_year=2025)
        db.migrate_null_years

    # Routen registrieren
    from . import routes
    app.register_blueprint(routes.bp)

    return app