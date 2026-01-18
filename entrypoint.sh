#!/bin/sh

echo "Starting health server..."
python3 /app/collegue/health_server.py &

sleep 3

echo "Starting main application..."
exec fastmcp run /app/collegue/app.py:app \
  --transport http \
  --host 0.0.0.0 \
  --port 4121 \
  --path /mcp/ \
  --log-level ${FASTMCP_LOG_LEVEL:-DEBUG}
