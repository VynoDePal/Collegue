#!/usr/bin/env bash
# Smoke test du conteneur Collègue : prouve que le service est PRÊT, pas seulement lancé.
#
# Usage : ci_docker_smoke.sh IMAGE
#
# Le conteneur est démarré sans réseau (--network none) avec un provider LLM qui ne
# valide rien à distance au démarrage (LLM_PROVIDER=anthropic + clé factice) : aucun
# appel LLM/API n'est possible. Les sondes passent par `docker exec … curl` sur le
# loopback DU conteneur (aucun port publié, aucun conflit sur le runner) :
#   - health : GET  :4122/_health            -> {"status":"ok"}
#   - MCP    : POST :4121/mcp/ (initialize)  -> HTTP 200 + résultat JSON-RPC
#   - healthcheck : la commande EXACTE du healthcheck de docker-compose.yml, exécutée dans le
#     conteneur (health server ET `entrypoint.sh mcp-ready`) — un test garantit qu'elle est
#     identique à celle du Compose.
#
# L'attente est bornée. Le script échoue si le conteneur sort (quel que soit son code),
# si la santé est invalide ou si le délai est dépassé. Les journaux du conteneur sont
# TOUJOURS conservés dans $SMOKE_LOG_DIR/$SMOKE_CONTAINER_NAME.log (pas de --rm : un
# conteneur crashé garde ses logs jusqu'au nettoyage explicite) et le nettoyage ne
# masque jamais le statut initial.
#
# Variables : SMOKE_CONTAINER_NAME (collegue-smoke), SMOKE_TIMEOUT_SECONDS (120),
#             SMOKE_POLL_INTERVAL_SECONDS (2), SMOKE_LOG_DIR (smoke-logs)
#
# Codes de sortie : 0 prêt · 2 usage · 10 `docker run` en échec · 11 conteneur sorti
#                   avant d'être prêt · 12 pas prêt dans le délai · 13 mort juste après
#                   être devenu prêt

set -uo pipefail

IMAGE="${1:-}"
if [[ -z "$IMAGE" ]]; then
  echo "usage: $0 IMAGE" >&2
  exit 2
fi

NAME="${SMOKE_CONTAINER_NAME:-collegue-smoke}"
TIMEOUT="${SMOKE_TIMEOUT_SECONDS:-120}"
INTERVAL="${SMOKE_POLL_INTERVAL_SECONDS:-2}"
LOG_DIR="${SMOKE_LOG_DIR:-smoke-logs}"
LOG_FILE="$LOG_DIR/$NAME.log"

if [[ ! "$TIMEOUT" =~ ^[1-9][0-9]*$ ]]; then
  echo "SMOKE_TIMEOUT_SECONDS doit être un entier positif (reçu: $TIMEOUT)" >&2
  exit 2
fi
if [[ ! "$INTERVAL" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
  echo "SMOKE_POLL_INTERVAL_SECONDS doit être un nombre positif (reçu: $INTERVAL)" >&2
  exit 2
fi

HEALTH_URL="http://127.0.0.1:4122/_health"
MCP_URL="http://127.0.0.1:4121/mcp/"
# Doit rester identique au healthcheck de collegue-app dans docker-compose.yml (test dédié).
HEALTHCHECK_CMD='curl -f http://localhost:4122/_health >/dev/null && ./entrypoint.sh mcp-ready'
INIT_PAYLOAD='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"ci-smoke","version":"0"}}}'

mkdir -p "$LOG_DIR" || {
  echo "::error::Impossible de créer $LOG_DIR" >&2
  exit 2
}

# Journaux puis nettoyage, quel que soit le chemin de sortie. Le statut de sortie
# initial est capturé en premier et restitué tel quel : un échec de nettoyage est
# signalé en avertissement mais ne transforme jamais un échec en succès (ni l'inverse).
cleanup() {
  local rc=$?
  trap - EXIT INT TERM
  if ! docker logs "$NAME" >"$LOG_FILE" 2>&1; then
    echo "::warning::docker logs a échoué pour $NAME (journal partiel dans $LOG_FILE)"
  fi
  echo "::group::Journaux du conteneur $NAME (200 dernières lignes, complet: $LOG_FILE)"
  tail -n 200 "$LOG_FILE"
  echo "::endgroup::"
  docker stop -t 10 "$NAME" >/dev/null 2>&1 || echo "::warning::docker stop a échoué pour $NAME"
  docker rm -f "$NAME" >/dev/null 2>&1 || echo "::warning::docker rm -f a échoué pour $NAME (conteneur potentiellement laissé)"
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

probe_health() {
  local body
  body=$(docker exec "$NAME" curl -fsS --max-time 3 "$HEALTH_URL" 2>/dev/null) || return 1
  [[ "$body" =~ \"status\"[[:space:]]*:[[:space:]]*\"ok\" ]]
}

probe_mcp() {
  local out code body
  out=$(docker exec "$NAME" curl -sS --max-time 5 -w $'\n%{http_code}' -X POST \
    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    -d "$INIT_PAYLOAD" "$MCP_URL" 2>/dev/null) || return 1
  code="${out##*$'\n'}"
  body="${out%$'\n'*}"
  [[ "$code" == "200" && "$body" == *'"result"'* ]]
}

probe_compose_healthcheck() {
  docker exec "$NAME" sh -c "$HEALTHCHECK_CMD" >/dev/null 2>&1
}

# Écrit « running exit_code oom » dans les variables globales ; échoue si le conteneur a disparu.
container_state() {
  local state
  state=$(docker inspect -f '{{.State.Running}} {{.State.ExitCode}} {{.State.OOMKilled}}' "$NAME" 2>&1) || {
    running="false"
    exit_code="inconnu"
    oom="inconnu"
    echo "docker inspect a échoué: $state" >&2
    return 1
  }
  read -r running exit_code oom <<<"$state"
}

# Résidu éventuel d'un run précédent (exécution locale répétée) : pré-nettoyage, l'absence
# du conteneur est le cas normal.
docker rm -f "$NAME" >/dev/null 2>&1 || true

if ! run_output=$(docker run -d --name "$NAME" --network none \
  -e LLM_PROVIDER=anthropic -e LLM_API_KEY=test-key -e LLM_MODEL=test-model \
  -e FASTMCP_CHECK_FOR_UPDATES=off \
  "$IMAGE" 2>&1); then
  echo "::error::docker run a échoué pour l'image $IMAGE"
  printf '%s\n' "$run_output" >&2
  exit 10
fi
echo "Conteneur $NAME lancé (${run_output:0:12}) ; attente de la disponibilité (max ${TIMEOUT}s)"

deadline=$((SECONDS + TIMEOUT))
running="" exit_code="" oom=""
while :; do
  if ! container_state || [[ "$running" != "true" ]]; then
    echo "::error::Le conteneur s'est arrêté avant d'être prêt (exit code: $exit_code, OOMKilled: $oom)"
    exit 11
  fi
  if probe_health && probe_mcp && probe_compose_healthcheck; then
    break
  fi
  if ((SECONDS >= deadline)); then
    echo "::error::Service non prêt après ${TIMEOUT}s (santé ${HEALTH_URL}, MCP ${MCP_URL} et healthcheck Compose attendus)"
    exit 12
  fi
  sleep "$INTERVAL"
done

# Un service « prêt » qui meurt aussitôt n'est pas un service démarré.
sleep "$INTERVAL"
if ! container_state || [[ "$running" != "true" ]]; then
  echo "::error::Le conteneur s'est arrêté juste après être devenu prêt (exit code: $exit_code, OOMKilled: $oom)"
  exit 13
fi

echo "Service prêt : santé OK, MCP initialize HTTP 200 et healthcheck Compose OK après ${SECONDS}s"
exit 0
