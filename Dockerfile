FROM python:3.12

# Arbeitsverzeichnis festlegen
WORKDIR /app

# Entfernen aller Dateien einschließlich des .git Verzeichnisses
RUN rm -rf /app/* /app/.git

# Installiere notwendige Systempakete
RUN apt-get update && apt-get install -y nano git curl

# Klone das Git-Repository
RUN git clone https://github.com/MinhMTV/Photo_Voting_Contest.git /app

# Setze Git so, dass es keine Dateimodus-Änderungen verfolgt
RUN git config core.fileMode false

# Abhängigkeiten installieren
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# Starten per Skript
CMD ["bash", "-c", "chmod +x /app/start.sh && /app/start.sh"]