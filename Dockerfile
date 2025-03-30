FROM python:3.12

# Arbeitsverzeichnis festlegen
WORKDIR /app

# Entfernen aller Dateien einschließlich des .git Verzeichnisses (falls vorhanden)
RUN rm -rf /app/* /app/.git

# Installiere notwendige Systempakete
RUN apt-get update && apt-get install -y nano git curl

# Kopiere die requirements.txt in den Container und installiere die Abhängigkeiten
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# Projektcode und Start-Skript kopieren (aber ohne das .git Verzeichnis)
COPY . .

# Stelle sicher, dass das .git Verzeichnis korrekt erhalten bleibt
# Hole das Git-Repository erneut, wenn es nicht vorhanden ist
RUN git init && git remote add origin https://github.com/MinhMTV/Photo_Voting_Contest.git

# Setze Berechtigungen für das start.sh-Skript
RUN ls -l /app/start.sh && chmod +x /app/start.sh

# Starten per Skript
CMD ["/app/start.sh"]