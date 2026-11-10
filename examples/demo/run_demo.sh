#!/usr/bin/env bash
# One-command Ontos demo: ingest -> ask -> agent over MCP -> audit -> graph.
# Everything runs locally. Requires Ollama (https://ollama.com) with a model.
#
#   examples/demo/run_demo.sh            # uses llama3
#   ONTOS_DEMO_MODEL=qwen2.5:7b examples/demo/run_demo.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
MODEL="${ONTOS_DEMO_MODEL:-llama3}"
PORT="${ONTOS_DEMO_PORT:-8765}"
OUT="$HERE/out"
ONTOS=(uv run --project "$REPO" --quiet ontos)
PY=(uv run --project "$REPO" --quiet python)

step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

step "0. Checks"
curl -s -m 3 http://127.0.0.1:11434/api/tags >/dev/null \
  || { echo "Ollama is not running. Start it with: ollama serve"; exit 1; }
curl -s http://127.0.0.1:11434/api/tags | grep -q "\"${MODEL}" \
  || { echo "Model '$MODEL' is not pulled. Run: ollama pull $MODEL"; exit 1; }
echo "  Ollama is up, model: $MODEL (local - nothing leaves this machine)"

rm -rf "$OUT" && mkdir -p "$OUT"
export ONTOS_AUDIT_DB_URL="sqlite+aiosqlite:///$OUT/audit.sqlite"
export ONTOS_AUDIT_SIGNING_KEY="${ONTOS_AUDIT_SIGNING_KEY:-demo-signing-key}"
STORE="networkx://$OUT/store.pkl"

step "1. Ingest two documents into a knowledge graph"
for f in "$HERE"/corpus/*.md; do echo "  $(basename "$f"): $(cat "$f" | tr '\n' ' ')"; done
# Small local models occasionally return malformed output; re-ingesting is
# idempotent (facts already stored are reaffirmed), so retry the skipped docs.
for attempt in 1 2 3; do
  "${ONTOS[@]}" --storage "$STORE" ingest --source-dir "$HERE/corpus" \
    --ontology "$REPO/docs/ontology/examples/starter.yaml" \
    --ollama-model "$MODEL" --ollama-timeout 300 --error-policy skip_and_log \
    > "$OUT/ingest.json" 2> "$OUT/ingest.err"
  [ "$("${PY[@]}" "$HERE/show.py" errors "$OUT/ingest.json")" = "0" ] && break
  echo "  (model output for a document was unusable; retrying, attempt $((attempt + 1)))"
done
"${PY[@]}" "$HERE/show.py" ingest "$OUT/ingest.json"

step "2. The graph Ontos built (every fact carries its source)"
"${PY[@]}" "$HERE/show.py" graph "$OUT/store.pkl"

step "3. Ask a question in plain English"
echo "  Q: Who works at Acme Corp?"
"${ONTOS[@]}" --storage "$STORE" query "Who works at Acme Corp?" \
  --ontology "$REPO/docs/ontology/examples/starter.yaml" \
  --ollama-model "$MODEL" --ollama-timeout 300 > "$OUT/query.json" 2> "$OUT/query.err"
"${PY[@]}" "$HERE/show.py" query "$OUT/query.json"

step "4. An AI agent queries Ontos over MCP"
"${ONTOS[@]}" serve --storage-path "$OUT/store.pkl" --host 127.0.0.1 --port "$PORT" \
  > "$OUT/serve.log" 2>&1 &
SERVER=$!
trap 'kill $SERVER 2>/dev/null || true' EXIT
for _ in $(seq 30); do curl -s -m 1 -o /dev/null "http://127.0.0.1:$PORT/mcp" && break; sleep 1; done
"${PY[@]}" "$HERE/agent.py" "http://127.0.0.1:$PORT/mcp"
kill $SERVER 2>/dev/null || true

step "5. Tamper-evident audit trail"
"${ONTOS[@]}" audit verify | sed 's/^/  /'

step "6. Visual graph"
"${ONTOS[@]}" export svg --storage-path "$OUT/store.pkl" --output "$OUT/graph.svg" > /dev/null
echo "  wrote $OUT/graph.svg   (open it with: open $OUT/graph.svg)"
