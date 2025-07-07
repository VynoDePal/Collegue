#!/bin/sh
# entrypoint.sh

# Lance le serveur de santé en arrière-plan
echo "Starting health server..."
python3 /app/collegue/health_server.py &

# Attend un petit peu pour laisser le temps au serveur de santé de démarrer
sleep 3

# Lance l'application principale au premier plan
echo "Starting main application..."
exec fastmcp run /app/collegue/app.py:app --transport sse
