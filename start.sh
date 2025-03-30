#!/bin/bash

echo "📦 Starte Setup..."

# Entferne alle alten Dateien und klone das Repo neu
rm -rf /app/*  # Löscht alle Dateien, um eine frische Installation sicherzustellen

echo "📥 Klone Projekt aus GitHub..."
git clone https://github.com/MinhMTV/Photo_Voting_Contest.git /app || exit 1

cd /app

# Wenn .env nicht existiert, wird sie erstellt
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
else
  echo "✅ .env Datei existiert – wird nicht überschrieben."
fi

# 🧠 Starte Flask-App
echo "🚀 Starte Flask-App..."
python3 -m flask run --host=0.0.0.0 --port=5050