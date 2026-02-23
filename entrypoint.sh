#!/bin/sh

# Script d'entrée qui gère les deux services :
# - health_server sur le port 4122 (FastAPI)
# - MCP server sur le port 4121 (FastMCP)

set -e

echo "Starting health server on port 4122..."
python3 /app/collegue/health_server.py &
HEALTH_PID=$!

# Attendre que le health server soit prêt
echo "Waiting for health server to be ready..."
for i in 1 2 3 4 5; do
    if curl -s -f http://localhost:4122/_health > /dev/null 2>&1; then
        echo "Health server is ready!"
        break
    fi
    echo "Health server not ready yet, retrying in 1s..."
    sleep 1
done

echo "Starting MCP server on port 4121..."
fastmcp run /app/collegue/app.py:app \
  --transport http \
  --host 0.0.0.0 \
  --port 4121 \
  --path /mcp/ \
  --log-level ${FASTMCP_LOG_LEVEL:-DEBUG} &
MCP_PID=$!

# Fonction pour nettoyer les process à la sortie
cleanup() {
    echo "Shutting down services..."
    kill $MCP_PID 2>/dev/null || true
    kill $HEALTH_PID 2>/dev/null || true
    wait
    exit 0
}

trap cleanup TERM INT

# Attendre que le MCP soit prêt
echo "Waiting for MCP server to be ready..."
for i in 1 2 3 4 5 6 7 8 9 10; do
    if curl -s -f http://localhost:4121/mcp/ > /dev/null 2>&1 || \
       curl -s -X POST http://localhost:4121/mcp/ \
         -H "Content-Type: application/json" \
         -d '{"jsonrpc":"2.0","id":1,"method":"initialize"}' \
         2>/dev/null | grep -q "result\|error"; then
        echo "MCP server is ready!"
        break
    fi
    echo "MCP server not ready yet (attempt $i/10), retrying in 1s..."
    sleep 1
done

echo ""
echo "========================================"
echo "All services started successfully!"
echo "- Health check: http://localhost:4122/_health"
echo "- MCP server:   http://localhost:4121/mcp/"
echo "========================================"
echo ""

# Attendre que l'un des process se termine
wait $MCP_PID
EXIT_CODE=$?

echo "MCP server exited with code $EXIT_CODE"
cleanup
