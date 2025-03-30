FROM python:3.12

# Arbeitsverzeichnis
WORKDIR /usr/src/app

# Abhängigkeiten
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# Projektcode & Start-Skript kopieren
COPY . .
# Kopiert das komplette Verzeichnis ins Arbeitsverzeichnis des Containers

# Stelle sicher, dass das start.sh-Skript ausführbar ist
RUN chmod +x /usr/src/app/start.sh  # Achtung, es muss auf den richtigen Pfad verweisen

# Nano + Git (optional)
RUN apt-get update && apt-get install -y nano git

# Starte per Skript
CMD ["/usr/src/app/start.sh"]  # Ändere den Pfad hier, um das Skript korrekt auszuführen