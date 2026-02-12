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
        CURRENT_CONTEST_YEAR=int(os.getenv("CURRENT_CONTEST_YEAR", "2025")),
        LEGACY_CONTEST_YEARS=[2024],
        VOTING_END_AT=os.getenv("VOTING_END_AT", "2025-12-31T23:59:59")
    )

    load_dotenv()
    # Ordner sicherstellen
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    os.makedirs(app.instance_path, exist_ok=True)

    # DB initialisieren + Migrationen sicher ausführen
    from . import db
    db.init_app(app)
    with app.app_context():
        db.init_db()

    # Routen registrieren
    from . import routes
    app.register_blueprint(routes.bp)

    return app