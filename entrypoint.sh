#!/bin/sh

# Script d'entrée avec deux modes selon MCP_TRANSPORT :
#
# - MCP_TRANSPORT=stdio → le container parle MCP par stdin/stdout
#   (usage "docker run -i --rm ... collegue-mcp" depuis un client MCP).
#   Pas de health server — le client gère le cycle de vie du process.
#
# - MCP_TRANSPORT=http (défaut) → serveur long-running avec healthcheck
#   sur 4122 et MCP streamable sur 4121, pour docker compose.
#
# Mode http — contrat de statut (fail-closed) :
#   - le conteneur n'est déclaré prêt (« All services started successfully! ») que
#     lorsque le health server ET le MCP répondent : le health server seul ne suffit pas ;
#   - un échec de `fastmcp run` (dont un OAuth fail-closed : OAUTH_ENABLED=true sans
#     authentification établissable) fait sortir le conteneur avec le code EXACT de
#     fastmcp (jamais converti en 0 par le nettoyage) ; un MCP qui sort avec 0 avant
#     d'être prêt est aussi un échec (code 1) ;
#   - un MCP ou un health server qui ne devient pas prêt dans le délai borné fait sortir
#     le conteneur en échec ; la mort du health server en service aussi ;
#   - à la sortie, TOUS les processus fils sont arrêtés ; SIGTERM/SIGINT = arrêt propre (0) ;
#   - `entrypoint.sh mcp-ready` (sous-commande, aucun processus démarré) : code 0 si le MCP
#     répond, 1 sinon — utilisé par le healthcheck Compose avec la sonde du health server.
#
# Réglages (valeurs par défaut adaptées au conteneur) :
#   COLLEGUE_APP_DIR (/app) · READY_POLL_INTERVAL (1, secondes) ·
#   HEALTH_READY_ATTEMPTS (30) · MCP_READY_ATTEMPTS (120)

set -e

APP_DIR="${COLLEGUE_APP_DIR:-/app}"
POLL_INTERVAL="${READY_POLL_INTERVAL:-1}"
HEALTH_ATTEMPTS="${HEALTH_READY_ATTEMPTS:-30}"
MCP_ATTEMPTS="${MCP_READY_ATTEMPTS:-120}"

# Code HTTP renvoyé par le MCP à une requête initialize (000 = rien n'écoute).
mcp_http_status() {
    curl -s -o /dev/null -w '%{http_code}' --max-time 3 -X POST http://localhost:4121/mcp/ \
        -H 'Content-Type: application/json' \
        -H 'Accept: application/json, text/event-stream' \
        -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"entrypoint","version":"0"}}}' \
        2>/dev/null || true
}

# Prêt = l'endpoint MCP répond à un initialize COMPLET (bon chemin, bon Accept) par un succès
# (2xx) ou par le refus d'authentification attendu d'un endpoint protégé par OAuth (401/403).
# Tout le reste n'est pas « prêt » : 000 (rien à l'écoute), 404 (mauvais chemin), 405/406
# (mauvais contrat), 400, 408, 429 et 5xx.
mcp_is_ready() {
    case "$(mcp_http_status)" in
        2??|401|403) return 0 ;;
    esac
    return 1
}

# Sous-commande `mcp-ready` : sonde du healthcheck Compose (même critère que l'attente de
# démarrage ci-dessous). Ne démarre aucun processus ; code 0 = à l'écoute, 1 sinon.
if [ "${1:-}" = "mcp-ready" ]; then
    if mcp_is_ready; then exit 0; fi
    exit 1
fi

if [ "${MCP_TRANSPORT:-http}" = "stdio" ]; then
    # Mode stdio : exec direct, pas de background, pas de health server.
    # exec transfère PID 1 à fastmcp pour que les signaux (SIGTERM à l'arrêt
    # du container) soient reçus sans être interceptés par ce wrapper shell.
    exec fastmcp run "$APP_DIR/collegue/app.py:app" \
        --transport stdio \
        --log-level "${FASTMCP_LOG_LEVEL:-WARNING}" \
        --no-banner
fi

HEALTH_PID=""
MCP_PID=""

# Arrête tous les processus fils puis sort avec le statut demandé ($1, 0 par défaut).
# Le statut est transmis explicitement : le nettoyage ne le remplace jamais par 0.
cleanup() {
    status="${1:-0}"
    trap - TERM INT
    echo "Shutting down services (exit code $status)..."
    if [ -n "$MCP_PID" ]; then kill "$MCP_PID" 2>/dev/null || true; fi
    if [ -n "$HEALTH_PID" ]; then kill "$HEALTH_PID" 2>/dev/null || true; fi
    wait 2>/dev/null || true
    exit "$status"
}

trap 'cleanup 0' TERM INT

is_running() {
    [ -n "$1" ] && kill -0 "$1" 2>/dev/null
}

echo "Starting health server on port 4122..."
python3 "$APP_DIR/collegue/health_server.py" &
HEALTH_PID=$!

# Attendre que le health server soit prêt (attente bornée, échec sinon)
echo "Waiting for health server to be ready..."
attempt=0
until curl -s -f http://localhost:4122/_health > /dev/null 2>&1; do
    if ! is_running "$HEALTH_PID"; then
        if wait "$HEALTH_PID"; then health_status=0; else health_status=$?; fi
        HEALTH_PID=""
        echo "ERROR: health server exited before becoming ready (exit code $health_status)" >&2
        if [ "$health_status" -eq 0 ]; then health_status=1; fi
        cleanup "$health_status"
    fi
    attempt=$((attempt + 1))
    if [ "$attempt" -ge "$HEALTH_ATTEMPTS" ]; then
        echo "ERROR: health server not ready after $HEALTH_ATTEMPTS attempts" >&2
        cleanup 1
    fi
    echo "Health server not ready yet (attempt $attempt/$HEALTH_ATTEMPTS), retrying in ${POLL_INTERVAL}s..."
    sleep "$POLL_INTERVAL"
done
echo "Health server is ready!"

echo "Starting MCP server on port 4121..."
fastmcp run "$APP_DIR/collegue/app.py:app" \
  --transport http \
  --host 0.0.0.0 \
  --port 4121 \
  --path /mcp/ \
  --log-level "${FASTMCP_LOG_LEVEL:-DEBUG}" &
MCP_PID=$!

# Attendre que le MCP soit prêt : sa mort ou un délai dépassé est un échec, jamais un succès.
echo "Waiting for MCP server to be ready..."
attempt=0
until mcp_is_ready; do
    if ! is_running "$MCP_PID"; then
        if wait "$MCP_PID"; then mcp_status=0; else mcp_status=$?; fi
        MCP_PID=""
        echo "ERROR: MCP server exited before becoming ready (exit code $mcp_status)" >&2
        if [ "$mcp_status" -eq 0 ]; then mcp_status=1; fi
        cleanup "$mcp_status"
    fi
    attempt=$((attempt + 1))
    if [ "$attempt" -ge "$MCP_ATTEMPTS" ]; then
        echo "ERROR: MCP server not ready after $MCP_ATTEMPTS attempts" >&2
        cleanup 1
    fi
    echo "MCP server not ready yet (attempt $attempt/$MCP_ATTEMPTS), retrying in ${POLL_INTERVAL}s..."
    sleep "$POLL_INTERVAL"
done
echo "MCP server is ready!"

echo ""
echo "========================================"
echo "All services started successfully!"
echo "- Health check: http://localhost:4122/_health"
echo "- MCP server:   http://localhost:4121/mcp/"
echo "========================================"
echo ""

# Surveiller les deux processus : la mort du health server en service est un échec.
while is_running "$MCP_PID"; do
    if ! is_running "$HEALTH_PID"; then
        if wait "$HEALTH_PID"; then health_status=0; else health_status=$?; fi
        HEALTH_PID=""
        echo "ERROR: health server exited unexpectedly (exit code $health_status)" >&2
        if [ "$health_status" -eq 0 ]; then health_status=1; fi
        cleanup "$health_status"
    fi
    sleep "$POLL_INTERVAL"
done

# Le MCP s'est arrêté seul : restituer son code exact (le nettoyage ne le masque pas).
if wait "$MCP_PID"; then mcp_status=0; else mcp_status=$?; fi
MCP_PID=""
echo "MCP server exited with code $mcp_status"
cleanup "$mcp_status"
