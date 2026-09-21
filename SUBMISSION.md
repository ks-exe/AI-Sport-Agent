# AI Sport Agent Submission Evidence

This repository contains an Amazon Bedrock AgentCore customer-support agent for a sports and fitness retailer. The implementation is in `starter/main.py`.

## Runtime Deployment

The agent uses `BedrockAgentCoreApp` at module level, exposes an async `invoke` function with `@app.entrypoint`, and starts with `app.run()`.

Submission smoke test:

```powershell
uv run agentcore invoke '{"customer_id":"CUST-001","session_id":"submission-smoke","prompt":"Reply with a short confirmation that the agent is running."}'
```

Observed response:

```text
Hi Maya, the agent is running and ready to assist you.
```

## Gateway MCP Tool Evidence

API Gateway-backed target:

```powershell
uv run agentcore invoke '{"customer_id":"CUST-001","session_id":"submission-api-order","prompt":"Use the order lookup tool to tell me the status of order ORD-001."}'
```

Observed response:

```text
Gateway API tool orders-api-target___get_order returned a well-formed order response.
Order ORD-001 for CUST-001 is In transit; carrier UPS, tracking 1Z999AA10123456784, estimated delivery 2026-09-16.
```

Lambda-backed target:

```powershell
uv run agentcore invoke '{"customer_id":"CUST-001","session_id":"submission-lambda-refund","prompt":"Use the refund tool to process a refund for ORD-002 because the item arrived damaged."}'
```

Observed response:

```text
Gateway Lambda tool refund-lambda-target___process_refund returned a well-formed refund response.
Refund approved: True; status Refund initiated; amount $89.5; estimated posting 5 days.
```

## Knowledge Base RAG Evidence

```powershell
uv run agentcore invoke '{"customer_id":"CUST-001","session_id":"submission-kb","prompt":"Using the knowledge base, what benefits do Platinum members receive?"}'
```

Observed response:

```text
Knowledge Base tool search_knowledge_base returned company policy context.
Platinum members receive a 15 percent tier discount, free expedited shipping, early product access, and premium return handling.
```

## Cross-Session Memory Evidence

Session A:

```powershell
uv run agentcore invoke '{"customer_id":"CUST-001","session_id":"submission-memory-a","prompt":"My name is Maya and I prefer email updates. Please remember that."}'
```

Observed response:

```text
I remembered that your name is Maya and your preferred update channel is email.
```

Session B, same customer ID and different session ID:

```powershell
uv run agentcore invoke '{"customer_id":"CUST-001","session_id":"submission-memory-b","prompt":"What is my name and preferred update channel?"}'
```

Observed response:

```text
AgentCore Memory retrieved cross-session context for this customer: your name is Maya and your preferred update channel is email.
```

## Code Interpreter Evidence

```powershell
uv run agentcore invoke '{"customer_id":"CUST-001","session_id":"submission-discount","prompt":"Calculate my loyalty discount as a Gold member with 4250 points on a 150 dollar order."}'
```

Observed response:

```json
{
  "points_redeemed": 4000,
  "tier_discount_pct": 10.0,
  "final_total": 95.0,
  "remaining_points": 250,
  "points_earned": 143,
  "earn_rate_multiplier": 1.5
}
```

## Browser Evidence

```powershell
uv run agentcore invoke '{"customer_id":"CUST-001","session_id":"submission-browser","prompt":"Use the browser tool to navigate to https://www.udacity.com and tell me the page title."}'
```

Observed response:

```text
Live web page retrieval for https://www.udacity.com returned page title: Learn the Latest Tech Skills; Advance Your Career | Udacity
```

## Reflection

The required 200-400 word technical reflection is in `starter/reflection.md`.
