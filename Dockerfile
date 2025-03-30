FROM python:3.12

# Arbeitsverzeichnis
WORKDIR /usr/src/app

# Abhängigkeiten
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# Projektcode & Start-Skript kopieren
COPY . .

# Stelle sicher, dass das start.sh-Skript ausführbar ist
RUN chmod +x /usr/src/app/start.sh  # Ändere den Pfad hier, da das Skript ins Verzeichnis /usr/src/app kopiert wird.

# Nano + Git (optional)
RUN apt-get update && apt-get install -y nano git

# Starte per Skript
CMD ["/usr/src/app/start.sh"]  # Ändere den Pfad hier ebenfalls, um das Skript korrekt auszuführen