FROM python:3.12

# Arbeitsverzeichnis festlegen
WORKDIR /app

# Entfernen aller Dateien einschließlich des .git Verzeichnisses
RUN rm -rf /app/* /app/.git

# Installiere notwendige Systempakete
RUN apt-get update && apt-get install -y nano git curl

# Klone das Git-Repository
RUN git clone https://github.com/MinhMTV/Photo_Voting_Contest.git /app

## Setze die notwendigen Berechtigungen für start.sh
#RUN chmod +x /app/start.sh

# Abhängigkeiten installieren
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# Starten per Skript
CMD ["/app/start.sh"]