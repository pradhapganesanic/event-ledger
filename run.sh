#!/usr/bin/env bash
# One-click: build + start the whole stack, wait for health, run a smoke test.
# Requires Docker Desktop. For automated tests (no Docker) use `make test`.
set -euo pipefail

GATEWAY="http://localhost:8000"
JAEGER="http://localhost:16686"

if ! docker compose version >/dev/null 2>&1; then
  echo "✗ Docker Compose is not available."
  echo "  Install Docker Desktop first: https://www.docker.com/products/docker-desktop/"
  exit 1
fi

echo "▶ Building and starting services (gateway, account-service, jaeger)…"
docker compose up --build -d

echo "▶ Waiting for the gateway to become healthy…"
ok=0
for _ in $(seq 1 60); do
  if curl -sf "$GATEWAY/health" >/dev/null 2>&1; then ok=1; break; fi
  sleep 1
done
if [ "$ok" -ne 1 ]; then
  echo "✗ gateway did not become healthy in 60s — recent logs:"
  docker compose logs --tail=50
  exit 1
fi
echo "✓ gateway healthy"

echo
echo "── POST /events (CREDIT 150) ──"
curl -s -X POST "$GATEWAY/events" -H 'Content-Type: application/json' \
  -d '{"eventId":"evt-001","accountId":"acct-123","type":"CREDIT","amount":150.00,"currency":"USD","eventTimestamp":"2026-05-15T14:02:11Z"}'
echo
echo "── POST /events (DEBIT 40) ──"
curl -s -X POST "$GATEWAY/events" -H 'Content-Type: application/json' \
  -d '{"eventId":"evt-002","accountId":"acct-123","type":"DEBIT","amount":40.00,"currency":"USD","eventTimestamp":"2026-05-15T15:00:00Z"}'
echo
echo "── Duplicate submit (expect idempotent HTTP 200) ──"
curl -s -o /dev/null -w "HTTP %{http_code}\n" -X POST "$GATEWAY/events" -H 'Content-Type: application/json' \
  -d '{"eventId":"evt-001","accountId":"acct-123","type":"CREDIT","amount":150.00,"currency":"USD","eventTimestamp":"2026-05-15T14:02:11Z"}'
echo "── GET balance (proxy → account service; expect 110) ──"
curl -s "$GATEWAY/accounts/acct-123/balance"; echo
echo "── GET events (ordered by eventTimestamp) ──"
curl -s "$GATEWAY/events?account=acct-123"; echo
echo "── Custom metric (gateway_events_total) ──"
curl -s "$GATEWAY/metrics" | grep '^gateway_events_total' || true

echo
echo "✅ Up and verified."
echo "   Gateway   → $GATEWAY  (API docs: $GATEWAY/docs)"
echo "   Jaeger UI → $JAEGER   (search service 'event-gateway' to see the trace)"
echo "   Stop with: docker compose down -v   (or: make down)"
