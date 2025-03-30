FROM python:3.12

# Arbeitsverzeichnis festlegen
WORKDIR /app

# Entfernen aller Dateien einschließlich des .git Verzeichnisses
RUN rm -rf /app/* /app/.git

# Installiere notwendige Systempakete
RUN apt-get update && apt-get install -y nano git curl

# Start-Skript kopieren
COPY start.sh /start.sh
RUN chmod +x /start.sh

# Starten per Skript
CMD ["/app/start.sh"]