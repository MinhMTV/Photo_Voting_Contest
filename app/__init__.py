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
        DATABASE=os.path.join(app.instance_path, 'votes.db')
    )

    load_dotenv()
    # Ordner sicherstellen
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    os.makedirs(app.instance_path, exist_ok=True)

    # DB initialisieren
    from . import db
    db.init_app(app)

    # Routen registrieren
    from . import routes
    app.register_blueprint(routes.bp)

    return app