#!/bin/bash
# Script de test complet pour le MCP Collegue
# À exécuter sur le serveur Coolify

echo "========================================"
echo "RAPPORT DE TEST MCP COLLEGUE"
echo "========================================"
echo ""

# 1. Vérifier que les conteneurs tournent
echo "1. ÉTAT DES CONTENEURS"
echo "----------------------"
docker ps --filter name=collegue --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
echo ""

# 2. Vérifier que le health check répond
echo "2. TEST HEALTH CHECK (port 4122)"
echo "--------------------------------"
curl -s -w "\nHTTP Code: %{http_code}\n" http://localhost:4122/_health
echo ""

# 3. Vérifier que le MCP répond
echo "3. TEST MCP DIRECT (port 4121)"
echo "------------------------------"
curl -s -X POST http://localhost:4121/mcp/ \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}' \
  -w "\nHTTP Code: %{http_code}\n"
echo ""

# 4. Tester depuis l'extérieur (via l'IP publique)
echo "4. TEST DEPUIS L'EXTÉRIEUR"
echo "--------------------------"
echo "IP publique: $(curl -s ifconfig.me)"
echo ""
echo "Test health from outside:"
curl -s -w " Code: %{http_code}\n" http://$(curl -s ifconfig.me):4122/_health 2>&1 | head -5
echo ""
echo "Test MCP from outside:"
curl -s -X POST http://$(curl -s ifconfig.me):4121/mcp/ \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize"}' \
  -w " Code: %{http_code}\n" 2>&1 | head -10
echo ""

# 5. Vérifier les ports exposés
echo "5. PORTS EN ÉCOUTE"
echo "------------------"
netstat -tlnp 2>/dev/null | grep -E "4121|4122" || ss -tlnp | grep -E "4121|4122"
echo ""

# 6. Logs du conteneur MCP
echo "6. DERNIERS LOGS MCP"
echo "--------------------"
MCP_CONTAINER_IDS=$(docker ps -q --filter name=collegue-app)
if [ -z "$MCP_CONTAINER_IDS" ]; then
  echo "Aucun conteneur ne correspond au filtre 'collegue-app'."
elif [ "$(printf '%s\n' $MCP_CONTAINER_IDS | wc -l)" -gt 1 ]; then
  echo "Attention: plusieurs conteneurs correspondent au filtre 'collegue-app'."
  docker ps --filter name=collegue-app
  MCP_CONTAINER_ID=$(printf '%s\n' $MCP_CONTAINER_IDS | head -n 1)
  echo ""
  echo "Affichage des logs du premier conteneur: $MCP_CONTAINER_ID"
  docker logs "$MCP_CONTAINER_ID" 2>&1 | tail -20
else
  docker logs "$MCP_CONTAINER_IDS" 2>&1 | tail -20
fi
echo ""

# 7. Test avec le client MCP
echo "7. TEST AVEC CLIENT MCP (mcp-remote)"
echo "-------------------------------------"
echo "Commande à exécuter:"
echo "  npx -y mcp-remote http://$(curl -s ifconfig.me):4121/mcp/ --transport http-only"
echo ""
echo "OU dans l'IDE avec cette config:"
echo '  {'
echo '    "collegue": {'
echo '      "url": "http://'$(curl -s ifconfig.me)':4121/mcp/",'
echo '      "transport": "streamable-http"'
echo '    }'
echo '  }'
echo ""

echo "========================================"
echo "FIN DU RAPPORT"
echo "========================================"
