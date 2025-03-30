FROM python:3.12

# Arbeitsverzeichnis
WORKDIR /usr/src/app

# Abhängigkeiten
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# Projektcode & Start-Skript kopieren
COPY . .
# Hier wird das gesamte Projekt ins Arbeitsverzeichnis kopiert, also auch start.sh

# Stelle sicher, dass das start.sh-Skript ausführbar ist
RUN chmod +x /usr/src/app/start.sh  # Hier wird das Skript ausführbar gemacht

# Nano + Git (optional)
RUN apt-get update && apt-get install -y nano git

# Starte per Skript
CMD ["/usr/src/app/start.sh"]  # Hier wird das Skript korrekt ausgeführt