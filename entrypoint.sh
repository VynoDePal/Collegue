#!/bin/sh
# entrypoint.sh

# Lance le serveur de santé en arrière-plan
echo "Starting health server..."
python3 /app/collegue/health_server.py &

# Configuration Git pour éviter l'erreur "dubious ownership" avec les volumes montés
if command -v git >/dev/null 2>&1; then
    git config --global --add safe.directory /app
fi

# Attend un petit peu pour laisser le temps au serveur de santé de démarrer
sleep 3

# Lance l'application principale au premier plan
echo "Starting main application..."
exec fastmcp run /app/collegue/app.py:app \
  --transport http \
  --host 0.0.0.0 \
  --port 4121 \
  --path /mcp/ \
  --log-level ${FASTMCP_LOG_LEVEL:-DEBUG}
