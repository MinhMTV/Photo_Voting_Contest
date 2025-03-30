import os

from app import create_app
from app.db import init_db

app = create_app()

if not os.path.exists(app.config['DATABASE']):
    print("➕ Initialisiere Datenbank automatisch ...")
    with app.app_context():
        init_db()

# App lokal mit Hot Reload starten (nur bei python run.py)
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)