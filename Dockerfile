FROM python:3.12

# Systemabhängigkeiten und git installieren
RUN apt-get update && apt-get install -y nano git curl

# Arbeitsverzeichnis
WORKDIR /app

# Start-Skript kopieren
COPY start.sh /start.sh
RUN chmod +x /start.sh

# Starten per Skript
CMD ["/start.sh"]