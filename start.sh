#!/bin/bash

echo "📦 Starte Setup..."

# Git Pull (nur wenn .git vorhanden ist)
if [ -d .git ]; then
    echo "🔄 Aktualisiere Projekt via git pull..."
    git pull
else
    echo "⚠️ Kein .git-Verzeichnis gefunden, git pull übersprungen."
fi

# .env Datei anlegen, wenn nicht vorhanden
if [ ! -f .env ]; then
  echo "📝 Erstelle .env Datei mit Platzhaltern..."
  echo 'FLASK_APP=run.py' > .env
  echo 'FLASK_ENV=production' >> .env
  echo 'FLASK_RUN_HOST=0.0.0.0' >> .env
  echo 'FLASK_RUN_PORT=5050' >> .env
  echo 'ADMIN_PASSWORD=changeme' >> .env
  echo 'SECRET_KEY=changeme' >> .env
fi

# Flask starten
echo "🚀 Starte Flask-App..."
flask run --host=0.0.0.0 --port=5050