FROM python:3.12

# Arbeitsverzeichnis festlegen
WORKDIR /app

# Entfernen aller Dateien einschließlich des .git Verzeichnisses
RUN rm -rf /app/* /app/.git


# Installiere notwendige Systempakete
RUN apt-get update && apt-get install -y nano git curl

# Klone das Git-Repo, falls nicht vorhanden
RUN git clone https://github.com/MinhMTV/Photo_Voting_Contest.git /app

# Abhängigkeiten installieren
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# Projektcode & Start-Skript kopieren (inkl. start.sh)
COPY . .

# Überprüfen, ob die start.sh-Datei kopiert wurde und Berechtigungen setzen
RUN ls -l /app/start.sh && chmod +x /app/start.sh

# Starten per Skript
CMD ["/app/start.sh"]