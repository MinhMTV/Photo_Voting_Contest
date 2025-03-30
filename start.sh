#!/bin/bash

echo "📦 Starte Setup..."

# Überprüfen, ob das Projektverzeichnis existiert, wenn nicht, aus GitHub klonen
if [ ! -d .git ]; then
  echo "📥 Klone Projekt aus GitHub..."
  # Entferne alle Dateien und klone das Projekt neu (falls erforderlich)
  git clone https://github.com/MinhMTV/Photo_Voting_Contest.git . || exit 1
else
  echo "🔄 Führe Git Pull aus..."
  git pull || echo "⚠️ Git Pull fehlgeschlagen"
fi

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

# Sicherstellen, dass die notwendigen Systempakete und Abhängigkeiten vorhanden sind
echo "📦 Installiere System-Abhängigkeiten..."
apt-get update && apt-get install -y nano git curl

# Stelle sicher, dass alle Python-Abhängigkeiten installiert sind
if [ -f "requirements.txt" ]; then
  echo "📦 Installiere Python-Abhängigkeiten..."
  pip install --no-cache-dir -r requirements.txt
else
  echo "⚠️ Keine requirements.txt gefunden!"
fi

# 🧠 Starte Flask-App
echo "🚀 Starte Flask-App..."
python3 -m flask run --host=0.0.0.0 --port=5050