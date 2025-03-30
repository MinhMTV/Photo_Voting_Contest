FROM python:3.12

# Arbeitsverzeichnis festlegen
WORKDIR /app

# Installiere notwendige Systempakete
RUN apt-get update && apt-get install -y nano git curl

# Klone das Git-Repo
RUN git clone https://github.com/MinhMTV/Photo_Voting_Contest.git /app

# Abhängigkeiten installieren
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# Projektcode & Start-Skript kopieren
COPY . .

# Stelle sicher, dass das start.sh-Skript ausführbar ist
RUN chmod +x /start.sh

# Starte per Skript
CMD ["/start.sh"]