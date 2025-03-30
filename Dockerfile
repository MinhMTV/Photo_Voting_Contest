FROM python:3.12

# Arbeitsverzeichnis festlegen
WORKDIR /app

# Systemabhängigkeiten und git installieren
RUN apt-get update && apt-get install -y nano git curl

# Projektcode & Start-Skript kopieren
COPY . .

# Stelle sicher, dass das start.sh-Skript ausführbar ist
RUN chmod +x /start.sh

# Starten per Skript
CMD ["/start.sh"]