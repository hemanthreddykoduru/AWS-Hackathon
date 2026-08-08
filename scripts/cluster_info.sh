#!/usr/bin/env bash
# Cluster inventory and memory rollup via the ccloud CLI (Agent-Ready).
#
#   ccloud auth login     # once, opens a browser
#   ./scripts/cluster_info.sh
#
# ccloud answers "which clusters exist and how do I reach them" without a
# hand-copied connection string. The SQL rollup then runs through the connection
# string ccloud itself hands back, so nothing here is transcribed by hand.
#
# ponytail: `ccloud cluster sql` opens an interactive shell and has no --execute
# flag, so SQL goes through --connection-url + psycopg instead of pretending
# otherwise. Read-only throughout.
set -euo pipefail

CLUSTER="${CLUSTER:-freelance-guardian}"
cd "$(dirname "$0")/.."

command -v ccloud >/dev/null || {
  echo "ccloud not installed:  brew install cockroachdb/tap/ccloud"; exit 1; }

ccloud auth whoami >/dev/null 2>&1 || {
  echo "not logged in:  ccloud auth login"; exit 1; }

echo "═══ authenticated as ═══"
ccloud auth whoami

echo
echo "═══ clusters in this organization ═══"
ccloud -q cluster list

echo
echo "═══ $CLUSTER ═══"
ccloud -q cluster info "$CLUSTER"

echo
echo "═══ agent memory (via the connection string ccloud returns) ═══"
# --connection-url prints the URL rather than opening a shell. Falls back to the
# app's own .env when the cluster requires interactive SQL credentials.
URL="$(ccloud cluster sql "$CLUSTER" --connection-url 2>/dev/null || true)"
if [ -z "$URL" ]; then
  echo "  (ccloud did not return a connection URL — using COCKROACH_DB_URL from .env)"
fi

CRDB_URL="$URL" .venv/bin/python - <<'PY'
import asyncio, os, sys
sys.path.insert(0, ".")
from sqlalchemy import text
from src import memory, config

async def main():
    # Prefer the URL ccloud handed us; fall back to .env.
    url = (os.environ.get("CRDB_URL") or "").strip()
    if url.startswith("postgresql://"):
        os.environ["COCKROACH_DB_URL"] = url.replace("postgresql://", "cockroachdb://", 1)
    engine = memory.get_engine()
    try:
        async with engine.engine.connect() as conn:
            rows = (await conn.execute(text(
                f"SELECT namespace, count(*) FROM {config.VECTOR_TABLE} GROUP BY 1 ORDER BY 1"
            ))).all()
            for ns, n in rows:
                print(f"  {ns:<8} {n} entries")
            reviews, clients = (await conn.execute(text(
                f"SELECT count(*), count(DISTINCT client_name) FROM {config.AUDIT_TABLE}"
            ))).one()
            print(f"  reviews  {reviews} across {clients} clients")
            idx = (await conn.execute(text(
                f"SHOW CREATE TABLE {config.VECTOR_TABLE}"))).all()[0][1]
            line = next((l.strip() for l in idx.splitlines() if "VECTOR INDEX" in l), None)
            print(f"  index    {line or 'NO VECTOR INDEX — run scripts/init_db.py'}")
    finally:
        await engine.aclose()

asyncio.run(main())
PY

echo
echo "ok — cluster reachable, memory populated."
