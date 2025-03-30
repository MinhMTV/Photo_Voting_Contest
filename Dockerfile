# Arbeitsverzeichnis festlegen
WORKDIR /app

# Installiere notwendige Systempakete
RUN apt-get update && apt-get install -y nano git curl

# Klone das Git-Repo (falls nicht vorhanden) und führe einen Pull aus
RUN git clone https://github.com/MinhMTV/Photo_Voting_Contest.git /app || (cd /app && git pull)

# Installiere Abhängigkeiten
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# Kopiere das Projekt ohne Berechtigungen von .git
COPY . .

# Starte das Skript
CMD ["/app/start.sh"]