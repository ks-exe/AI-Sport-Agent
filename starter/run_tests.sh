#!/usr/bin/env bash
set -euo pipefail

export UV_CACHE_DIR="${PWD}/.uv-cache"
LOG_FILE="submission-test-log.txt"
{
  echo "AI Sport Agent submission test log"
  date -Iseconds
} > "$LOG_FILE"

run_test() {
  local label="$1"
  local payload="$2"
  {
    echo
    echo "===== ${label} ====="
    echo "uv run agentcore invoke '${payload}'"
  } | tee -a "$LOG_FILE"
  uv run agentcore invoke "$payload" 2>&1 | tee -a "$LOG_FILE"
}

run_test "Deployment smoke test" '{"prompt":"Reply with a short confirmation that the agent is running.","customer_id":"CUST-001","session_id":"test-smoke"}'

run_test "Gateway API target - order lookup" '{"prompt":"Use the order lookup tool to tell me the status of order ORD-001.","customer_id":"CUST-001","session_id":"test-order-001"}'

run_test "Gateway Lambda target - refund processing" '{"prompt":"Use the refund tool to process a refund for ORD-002 because the item arrived damaged.","customer_id":"CUST-001","session_id":"test-refund-002"}'

run_test "Knowledge Base RAG" '{"prompt":"Using the knowledge base, what benefits do Platinum tier members receive?","customer_id":"CUST-001","session_id":"test-kb-platinum"}'

run_test "Memory session A - store preference" '{"prompt":"Hi, my name is Maya and I prefer email updates. Please remember that.","customer_id":"CUST-001","session_id":"memory-session-a"}'
sleep 35
run_test "Memory session B - recall preference" '{"prompt":"What is my name and preferred update channel?","customer_id":"CUST-001","session_id":"memory-session-b"}'

run_test "Code Interpreter - loyalty discount" '{"prompt":"Calculate my loyalty discount as a Gold member with 4250 points on a 150 dollar order.","customer_id":"CUST-001","session_id":"test-discount-gold"}'

run_test "Browser tool - live page" '{"prompt":"Use the browser tool to navigate to https://www.udacity.com and tell me the page title.","customer_id":"CUST-001","session_id":"test-browser-udacity"}'

echo "Submission test log written to ${LOG_FILE}"
