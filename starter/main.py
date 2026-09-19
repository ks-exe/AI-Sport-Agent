from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from bedrock_agentcore.memory import MemoryClient
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.tools.code_interpreter_client import code_session
from pydantic import BaseModel, Field
from strands import Agent, tool
from strands.hooks import AfterInvocationEvent, BeforeInvocationEvent, HookProvider, HookRegistry
from strands.tools.mcp import MCPClient
from strands_tools.browser import AgentCoreBrowser

try:
    from mcp.client.streamable_http import streamable_http_client
except ImportError:
    from mcp.client.streamable_http import streamablehttp_client as streamable_http_client


LOGGER = logging.getLogger(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

# Prominent deployment configuration. Replace these values through environment
# variables after setup_aws.py creates the corresponding AWS resources.
REGION = os.getenv("AWS_REGION") or os.getenv("REGION") or "us-east-1"
GATEWAY_URL = os.getenv("GATEWAY_URL", "")
KB_ID = os.getenv("KB_ID", "")
MEMORY_ID = os.getenv("MEMORY_ID", "")
MODEL_ID = os.getenv("MODEL_ID", "us.anthropic.claude-3-5-sonnet-20241022-v2:0")

AWS_CONFIG = Config(retries={"max_attempts": 5, "mode": "adaptive"})
bedrock_agent_runtime = boto3.client("bedrock-agent-runtime", region_name=REGION, config=AWS_CONFIG)
memory_client = MemoryClient(region_name=REGION)
browser_client = AgentCoreBrowser(region=REGION)
app = BedrockAgentCoreApp()


class LoyaltyDiscountResult(BaseModel):
    points_redeemed: int = Field(ge=0)
    tier_discount_pct: float = Field(ge=0)
    final_total: float = Field(ge=0)
    remaining_points: int = Field(ge=0)
    points_earned: int = Field(ge=0)
    earn_rate_multiplier: float = Field(ge=0)

SYSTEM_PROMPT = """
You are a customer support agent for an online sports and fitness retail business.
Use deterministic tools whenever customer-specific data, refunds, calculations,
or browser verification are required. Use the knowledge-base tool for policy,
product catalog, loyalty, warranty, and benefits questions. Keep answers concise,
cite which tool informed operational facts, and never invent order or refund data.
"""


def _money(value: float | int | str | Decimal) -> float:
    amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(amount)


def _coerce_positive_int(value: int | str) -> int:
    parsed = int(value)
    return parsed if parsed > 0 else 0


def _coerce_positive_decimal(value: float | int | str | Decimal) -> Decimal:
    parsed = Decimal(str(value))
    return parsed if parsed > Decimal("0") else Decimal("0")


def _extract_json_object(text: str) -> dict[str, Any]:
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError(f"Code Interpreter did not return JSON: {text}")
    return json.loads(match.group(0))


def _extract_sandbox_text(response: dict[str, Any]) -> str:
    fragments: list[str] = []
    for event in response.get("stream", []):
        result = event.get("result", {})
        for item in result.get("content", []):
            if isinstance(item, dict):
                text = item.get("text") or item.get("json")
                if text:
                    fragments.append(str(text))
    return "\n".join(fragments).strip()


def _tier_discount_pct(tier: str) -> float:
    tier_table = {
        "bronze": 2.0,
        "silver": 5.0,
        "gold": 10.0,
        "platinum": 15.0,
    }
    return tier_table.get(tier.strip().lower(), 0.0)


def _tier_earn_rate(tier: str) -> float:
    earn_rate_table = {
        "bronze": 1.0,
        "silver": 1.25,
        "gold": 1.5,
        "platinum": 2.0,
    }
    return earn_rate_table.get(tier.strip().lower(), 1.0)


def _fallback_loyalty_discount(points: int, tier: str, order_total: float) -> dict[str, Any]:
    subtotal = _coerce_positive_decimal(order_total)
    tier_pct = Decimal(str(_tier_discount_pct(tier)))
    earn_rate = Decimal(str(_tier_earn_rate(tier)))
    tier_discount = subtotal * tier_pct / Decimal("100")
    final_total = subtotal - tier_discount
    points_earned = int((final_total * earn_rate).to_integral_value(rounding=ROUND_HALF_UP))
    return LoyaltyDiscountResult(
        points_redeemed=0,
        tier_discount_pct=float(tier_pct),
        final_total=_money(final_total),
        remaining_points=_coerce_positive_int(points),
        points_earned=points_earned,
        earn_rate_multiplier=float(earn_rate),
    ).model_dump()


@tool
def search_knowledge_base(query: str) -> str:
    """
    Search the Bedrock Knowledge Base for product catalog, warranty, returns,
    loyalty tier, shipping, refund policy, and support-procedure information.

    Call this tool when the customer asks a policy or product-information
    question that should be grounded in company documentation rather than
    answered from general model knowledge. Do not call it for deterministic
    order status, customer record, refund execution, browser navigation, or
    arithmetic discount calculations because those have dedicated tools.

    Args:
        query: The customer's natural-language question or a focused retrieval query.

    Returns:
        A formatted set of retrieved text chunks with source metadata when available.
    """
    configured_kb = KB_ID.strip()
    if not configured_kb:
        return "Knowledge Base is not configured. Set KB_ID to a Bedrock Knowledge Base ID before using RAG."

    try:
        response = bedrock_agent_runtime.retrieve(
            knowledgeBaseId=configured_kb,
            retrievalQuery={"text": query[:20000]},
            retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": 5}},
        )
    except (BotoCoreError, ClientError) as exc:
        return f"Knowledge Base retrieval failed: {type(exc).__name__}: {exc}"

    chunks: list[str] = []
    for index, result in enumerate(response.get("retrievalResults", []), start=1):
        text = result.get("content", {}).get("text", "").strip()
        if not text:
            continue
        location = result.get("location", {})
        source = (
            location.get("s3Location", {}).get("uri")
            or location.get("webLocation", {}).get("url")
            or location.get("confluenceLocation", {}).get("url")
            or "source unavailable"
        )
        score = result.get("score", "unscored")
        chunks.append(f"[Chunk {index} | score={score} | source={source}]\n{text}")

    if chunks:
        return "\n\n".join(chunks)
    return "No matching Knowledge Base content was found for that query."


@tool
def calculate_loyalty_discount(points: int, tier: str, order_total: float) -> dict[str, Any]:
    """
    Calculate the final checkout total for a loyalty member.

    Args:
        points: Current loyalty points balance. Points redeem at 100 points per dollar.
        tier: Customer loyalty tier. Supported values are Bronze, Silver, Gold, and Platinum.
        order_total: Pre-discount order total in USD.

    Returns:
        A dictionary with points_redeemed, tier_discount_pct, final_total, remaining_points,
        points_earned, and earn_rate_multiplier.
    """
    safe_points = _coerce_positive_int(points)
    safe_total = _coerce_positive_decimal(order_total)
    safe_tier = tier.strip()
    script = f"""
import json
from decimal import Decimal, ROUND_HALF_UP

POINTS = {safe_points}
TIER = {json.dumps(safe_tier)}
ORDER_TOTAL = Decimal({json.dumps(str(safe_total))})
TIER_DISCOUNTS = {{
    "bronze": Decimal("2.0"),
    "silver": Decimal("5.0"),
    "gold": Decimal("10.0"),
    "platinum": Decimal("15.0"),
}}
EARN_RATES = {{
    "bronze": Decimal("1.0"),
    "silver": Decimal("1.25"),
    "gold": Decimal("1.5"),
    "platinum": Decimal("2.0"),
}}

def money(value):
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

tier_pct = TIER_DISCOUNTS.get(TIER.lower(), Decimal("0.0"))
earn_rate = EARN_RATES.get(TIER.lower(), Decimal("1.0"))
tier_discount = ORDER_TOTAL * tier_pct / Decimal("100")
points_floor = (POINTS // 500) * 500
max_points_by_order = int((ORDER_TOTAL * Decimal("100") * Decimal("0.50")) // 500) * 500
points_redeemed = min(points_floor, max_points_by_order)
points_discount = Decimal(points_redeemed) / Decimal("100")
final_total = max(ORDER_TOTAL - tier_discount - points_discount, Decimal("0"))
points_earned = int((final_total * earn_rate).to_integral_value(rounding=ROUND_HALF_UP))

print(json.dumps({{
    "points_redeemed": int(points_redeemed),
    "tier_discount_pct": float(tier_pct),
    "final_total": money(final_total),
    "remaining_points": int(POINTS - points_redeemed),
    "points_earned": points_earned,
    "earn_rate_multiplier": float(earn_rate),
}}))
"""
    try:
        with code_session(REGION) as sandbox:
            response = sandbox.invoke(
                "executeCode",
                {"code": script, "language": "python", "clearContext": True},
            )
        parsed = _extract_json_object(_extract_sandbox_text(response))
        return LoyaltyDiscountResult(
            points_redeemed=int(parsed["points_redeemed"]),
            tier_discount_pct=float(parsed["tier_discount_pct"]),
            final_total=_money(parsed["final_total"]),
            remaining_points=int(parsed["remaining_points"]),
            points_earned=int(parsed["points_earned"]),
            earn_rate_multiplier=float(parsed["earn_rate_multiplier"]),
        ).model_dump()
    except Exception as exc:
        LOGGER.warning("Code Interpreter discount calculation failed: %s", exc)
        return _fallback_loyalty_discount(safe_points, safe_tier, float(safe_total))


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content", [])
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return str(content.get("text", ""))
    fragments: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("text"):
            fragments.append(str(item["text"]))
    return "\n".join(fragments).strip()


def _latest_user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return _message_text(message)
    return ""


def _replace_latest_user_text(messages: list[dict[str, Any]], replacement: str) -> list[dict[str, Any]]:
    for message in reversed(messages):
        if message.get("role") == "user":
            message["content"] = [{"text": replacement}]
            return messages
    return [{"role": "user", "content": [{"text": replacement}]}] + messages


def _memory_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("text", "summary", "content", "value"):
            if key in value:
                nested = _memory_text(value[key])
                if nested:
                    return nested
        fragments = [_memory_text(item) for item in value.values()]
        return " ".join(item for item in fragments if item).strip()
    if isinstance(value, list):
        fragments = [_memory_text(item) for item in value]
        return " ".join(item for item in fragments if item).strip()
    return ""


def _strategy_tag(strategy_type: str) -> str:
    normalized = strategy_type.upper()
    if "PREFERENCE" in normalized:
        return "customer_preferences"
    return "customer_facts"


def _namespace_for_customer(template: str, actor_id: str, session_id: str, strategy_id: str) -> tuple[str, str]:
    resolved = (
        template.replace("{actorId}", actor_id)
        .replace("{memoryStrategyId}", strategy_id)
        .replace("${actorId}", actor_id)
        .replace("${memoryStrategyId}", strategy_id)
    )
    if "{sessionId}" in resolved or "${sessionId}" in resolved:
        marker = "{sessionId}" if "{sessionId}" in resolved else "${sessionId}"
        prefix = resolved.split(marker, 1)[0]
        if prefix:
            return "namespace_path", prefix
        return "namespace", resolved.replace(marker, session_id)
    return "namespace", resolved


def get_namespaces(client: MemoryClient, memory_id: str, actor_id: str, session_id: str) -> list[dict[str, str]]:
    strategies = client.get_memory_strategies(memory_id)
    namespaces: list[dict[str, str]] = []
    for strategy in strategies:
        strategy_type = strategy.get("memoryStrategyType") or strategy.get("type") or "SEMANTIC"
        strategy_id = strategy.get("memoryStrategyId") or strategy.get("strategyId") or strategy_type
        templates = strategy.get("namespaceTemplates") or strategy.get("namespaces") or []
        if isinstance(templates, str):
            templates = [templates]
        for template in templates:
            key, value = _namespace_for_customer(str(template), actor_id, session_id, str(strategy_id))
            namespaces.append(
                {
                    "strategy_type": str(strategy_type),
                    "tag": _strategy_tag(str(strategy_type)),
                    "query_key": key,
                    "namespace": value,
                }
            )
    return namespaces


class MemoryHook(HookProvider):
    """Inject long-term customer memory before generation and persist each turn after generation."""

    def __init__(self, client: MemoryClient, memory_id: str):
        self.client = client
        self.memory_id = memory_id

    def register_hooks(self, registry: HookRegistry):
        registry.add_callback(BeforeInvocationEvent, self.retrieve_customer_context)
        registry.add_callback(AfterInvocationEvent, self.save_support_interaction)

    def retrieve_customer_context(self, event: BeforeInvocationEvent):
        customer_id = str(event.invocation_state.get("customer_id", "")).strip()
        session_id = str(event.invocation_state.get("session_id", "")).strip()
        if not self.memory_id.strip() or not customer_id or not session_id:
            return

        messages = list(event.messages or [])
        user_query = _latest_user_text(messages)
        if not user_query:
            return
        event.invocation_state["latest_user_query"] = user_query

        memory_lines: list[str] = []
        try:
            for namespace in get_namespaces(self.client, self.memory_id, customer_id, session_id):
                query_args = {
                    "memory_id": self.memory_id,
                    "query": user_query,
                    "top_k": 5,
                    namespace["query_key"]: namespace["namespace"],
                }
                for record in self.client.retrieve_memories(**query_args):
                    text = _memory_text(record)
                    if text:
                        memory_lines.append(f"[{namespace['tag']}] {text}")
        except Exception as exc:
            LOGGER.warning("Memory retrieval skipped: %s", exc)
            return

        if not memory_lines:
            return
        memory_context = "Relevant long-term customer memory:\n" + "\n".join(memory_lines)
        event.invocation_state["memory_context"] = memory_context
        replacement = f"{memory_context}\n\nCurrent customer message:\n{user_query}"
        event.messages = _replace_latest_user_text(messages, replacement)

    def save_support_interaction(self, event: AfterInvocationEvent):
        customer_id = str(event.invocation_state.get("customer_id", "")).strip()
        session_id = str(event.invocation_state.get("session_id", "")).strip()
        user_query = str(event.invocation_state.get("latest_user_query", "")).strip()
        assistant_response = str(event.result).strip() if event.result else ""
        if not self.memory_id.strip() or not customer_id or not session_id or not user_query or not assistant_response:
            return

        try:
            self.client.create_event(
                memory_id=self.memory_id,
                actor_id=customer_id,
                session_id=session_id,
                messages=[(user_query, "USER"), (assistant_response, "ASSISTANT")],
            )
        except Exception as exc:
            LOGGER.warning("Memory persistence skipped: %s", exc)


def _base_tools() -> list[Any]:
    return [
        search_knowledge_base,
        calculate_loyalty_discount,
        browser_client.browser,
    ]


def _build_agent(tools: list[Any]) -> Agent:
    return Agent(
        model=MODEL_ID,
        system_prompt=SYSTEM_PROMPT,
        tools=tools,
        hooks=[MemoryHook(memory_client, MEMORY_ID)],
    )


def _run_agent(prompt: str, customer_id: str, session_id: str) -> str:
    invocation_state = {
        "customer_id": customer_id,
        "session_id": session_id,
        "latest_user_query": prompt,
    }
    tools = _base_tools()
    gateway_url = GATEWAY_URL.strip()
    if gateway_url:
        try:
            mcp_client = MCPClient(
                lambda: streamable_http_client(gateway_url),
                startup_timeout=45,
                application_name="customer-support-agent",
            )
            with mcp_client:
                gateway_tools = list(mcp_client.list_tools_sync())
                LOGGER.info("Discovered %d MCP Gateway tools.", len(gateway_tools))
                agent = _build_agent(tools + gateway_tools)
                result = agent(prompt, invocation_state=invocation_state)
                return str(result).strip()
        except Exception as exc:
            LOGGER.warning("MCP Gateway discovery failed; running with local tools only: %s", exc)
    else:
        LOGGER.info("GATEWAY_URL is empty; running with local tools only.")

    agent = _build_agent(tools)
    result = agent(prompt, invocation_state=invocation_state)
    return str(result).strip()


@app.entrypoint
async def invoke(payload: dict[str, Any]) -> dict[str, Any]:
    prompt = str(payload.get("prompt", "")).strip()
    customer_id = str(payload.get("customer_id", "anonymous-customer")).strip() or "anonymous-customer"
    session_id = str(payload.get("session_id", f"session-{uuid.uuid4().hex}")).strip()

    if not prompt:
        return {
            "error": "Missing required input field: prompt",
            "customer_id": customer_id,
            "session_id": session_id,
        }

    response = await asyncio.to_thread(_run_agent, prompt, customer_id, session_id)
    return {
        "response": response,
        "customer_id": customer_id,
        "session_id": session_id,
    }


if __name__ == "__main__":
    app.run()
