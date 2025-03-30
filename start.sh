#!/bin/bash

echo "📦 Starte Setup..."

if [ ! -d .git ]; then
  echo "📥 Klone Projekt aus GitHub..."
  rm -rf * && \
  git clone https://github.com/MinhMTV/Photo_Voting_Contest.git .;
else
  echo "🔄 Führe Git Pull aus..."
  git pull
fi

if [ ! -f .env ]; then
  echo "📝 Erstelle .env Datei mit Platzhaltern..."
  cat <<EOF > .env
FLASK_APP=run.py
FLASK_ENV=production
FLASK_RUN_HOST=0.0.0.0
FLASK_RUN_PORT=5050
ADMIN_PASSWORD=changeme
SECRET_KEY=changeme
EOF
fi

echo "🚀 Starte Flask-App..."
python3 -m flask run --host=0.0.0.0 --port=5050