#!/bin/bash
set -e

echo "📦 Starte Setup..."

pwd
ls -la

BACKUP_DIR="${BACKUP_DIR:-/app/backups}"
BACKUP_KEEP="${BACKUP_KEEP:-20}"

backup_runtime_data() {
  mkdir -p "$BACKUP_DIR"

  local timestamp
  timestamp="$(date +%Y%m%d_%H%M%S)"
  local backup_file="$BACKUP_DIR/photo_contest_backup_${timestamp}.tar.gz"
  local backup_items=()

  if [ -d "instance" ]; then
    backup_items+=("instance")
  fi

  if [ -f ".env" ]; then
    backup_items+=(".env")
  fi

  if [ -d "app/static" ]; then
    while IFS= read -r dir; do
      [ -n "$dir" ] && backup_items+=("$dir")
    done < <(find app/static -maxdepth 1 -type d \( -name 'uploads*' -o -name 'assets*' -o -name 'stickers*' \) | sort)
  fi

  if [ ${#backup_items[@]} -eq 0 ]; then
    echo "🟡 Kein Backup erstellt, weil noch keine Runtime-Daten vorhanden sind."
    return
  fi

  echo "💾 Erstelle Backup: $backup_file"
  tar -czf "$backup_file" "${backup_items[@]}"

  if [ "$BACKUP_KEEP" -gt 0 ] 2>/dev/null; then
    ls -1t "$BACKUP_DIR"/photo_contest_backup_*.tar.gz 2>/dev/null | tail -n +$((BACKUP_KEEP + 1)) | xargs -r rm -f
  fi
}

# Überprüfen, ob das Projektverzeichnis existiert, wenn nicht, aus GitHub klonen
if [ ! -d .git ]; then
  echo "📥 Klone Projekt aus GitHub..."
  # Entferne alle Dateien und klone das Projekt neu (falls erforderlich)
  git clone https://github.com/MinhMTV/Photo_Voting_Contest.git
else
  backup_runtime_data
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

# Stelle sicher, dass neue Python-Abhängigkeiten nach einem Update
# auch ohne kompletten Docker-Rebuild verfügbar sind.
echo "📦 Installiere/aktualisiere Python-Abhängigkeiten..."
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt

# 🧠 Starte Flask-App
echo "🚀 Starte Flask-App..."
python3 -m flask run --host=0.0.0.0 --port=5050
