#!/usr/bin/env bash
# Spin up a throwaway Postgres, load seeds + build the marts, run the
# integration suite, then clean up. Works with docker or podman.
set -euo pipefail

CLI="${CONTAINER_CLI:-docker}"
command -v "$CLI" >/dev/null 2>&1 || CLI=podman
NAME=qg-itest-pg
PORT=5433
DSN="postgresql://querygate:querygate@localhost:${PORT}/warehouse"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

cleanup() { "$CLI" rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "==> starting Postgres ($CLI)"
"$CLI" rm -f "$NAME" >/dev/null 2>&1 || true
"$CLI" run -d --name "$NAME" -e POSTGRES_USER=querygate -e POSTGRES_PASSWORD=querygate \
  -e POSTGRES_DB=warehouse -p "${PORT}:5432" postgres:16 >/dev/null

for _ in $(seq 1 30); do
  "$CLI" exec "$NAME" pg_isready -U querygate -d warehouse >/dev/null 2>&1 && break
  sleep 1
done

echo "==> loading seeds"
"$CLI" exec -i "$NAME" psql -U querygate -d warehouse >/dev/null <<'SQL'
CREATE TABLE raw_tenants(tenant_id text, tenant_name text, industry text);
CREATE TABLE raw_plans(plan_id int, plan_name text, monthly_price numeric, tier text);
CREATE TABLE raw_customers(customer_id int, tenant_id text, customer_name text, plan_id int, signed_up_at text, region text);
CREATE TABLE raw_orders(order_id int, tenant_id text, customer_id int, order_date text, amount numeric, status text);
SQL
for t in raw_tenants raw_plans raw_customers raw_orders; do
  "$CLI" exec -i "$NAME" psql -U querygate -d warehouse \
    -c "\copy $t FROM STDIN WITH (FORMAT csv, HEADER true)" < "$ROOT/dbt/seeds/$t.csv"
done

echo "==> building marts"
"$CLI" exec -i "$NAME" psql -U querygate -d warehouse -q < "$ROOT/scripts/build_marts.sql"

echo "==> running integration tests"
QG_TEST_PG_DSN="$DSN" uv run pytest "$ROOT/tests/querygate/test_warehouse_integration.py" -q
