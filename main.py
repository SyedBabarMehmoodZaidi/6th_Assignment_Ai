

import os
import re
import json
from functools import wraps
from datetime import datetime
from typing import Callable, Any, Dict, Optional

# --- Simple .env loader ---
def load_dotenv_file(path=".env"):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

# try loading .env
load_dotenv_file(".env")

# local config
from my_config.gemini_config import CONFIG


LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "tool_logs.txt")

def log_event(event_type: str, payload: Dict[str, Any]):
    entry = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "event_type": event_type,
        "payload": payload,
    }
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")

# === Guardrails ===
OFFENSIVE_WORDS = {"idiot", "stupid", "hate", "kill", "damn", "shut up"}
NEGATIVE_WORDS = {"angry", "upset", "unhappy", "not happy", "complain", "bad", "terrible"}

def detect_offensive_or_negative(text: str) -> Dict[str, bool]:
    text_lower = text.lower()
    found_offensive = any(w in text_lower for w in OFFENSIVE_WORDS)
    found_negative = any(w in text_lower for w in NEGATIVE_WORDS)
    return {"offensive": found_offensive, "negative": found_negative}

def guardrail(func: Callable):
    """
    Fixed version:
    The wrapped function must be a method: first arg = self, second arg = user_input.
    """
    @wraps(func)
    def wrapper(self, user_input: str, *args, **kwargs):
        detected = detect_offensive_or_negative(user_input)
        if detected["offensive"]:
            log_event("guardrail_triggered", {"reason": "offensive_language", "input": user_input})
            return {
                "handled": True,
                "guardrail": True,
                "action": "block_or_rephrase",
                "message": "I'm here to help, but I can't respond to offensive language. Could you rephrase your question?"
            }
        return func(self, user_input, *args, **kwargs)
    return wrapper

# === function_tool system ===
class FunctionTool:
    def __init__(self, func: Callable, name: str = None, is_enabled: Optional[Callable[[str], bool]] = None, error_function: Optional[Callable] = None):
        self.func = func
        self.name = name or func.__name__
        self.is_enabled = is_enabled or (lambda query: True)
        self.error_function = error_function

    def __call__(self, *args, **kwargs):
        return self.func(*args, **kwargs)

def function_tool(name: str = None, is_enabled: Optional[Callable[[str], bool]] = None, error_function: Optional[Callable] = None):
    def decorator(func):
        tool = FunctionTool(func=func, name=name or func.__name__, is_enabled=is_enabled, error_function=error_function)
        @wraps(func)
        def wrapper(*args, **kwargs):
            return tool(*args, **kwargs)
        wrapper._tool = tool
        return wrapper
    return decorator


SIMULATED_ORDERS = {
    "A100": {"status": "shipped", "eta": "2025-09-04", "items": ["Blue T-shirt", "Cap"], "customer_id": "12345"},
    "B201": {"status": "processing", "eta": "2025-09-10", "items": ["Coffee Mug"], "customer_id": "67890"},
    "C303": {"status": "delivered", "eta": "2025-08-20", "items": ["Notebook"], "customer_id": "12345"},
}


def order_tool_enabled_predicate(query: str):
    q = query.lower()
    return "order" in q or re.search(r"\b[A-Z]\d{2,}\b", query) is not None

def order_error_function(order_id: str):
    return {"error": True, "message": f"Order ID '{order_id}' not found. Please check the ID or contact support."}

@function_tool(name="get_order_status", is_enabled=order_tool_enabled_predicate, error_function=order_error_function)
def get_order_status(user_input: str, order_id: str):
    log_event("tool_invoked", {"tool": "get_order_status", "order_id": order_id, "user_input": user_input})
    order = SIMULATED_ORDERS.get(order_id)
    if not order:
        return {"error": True, "message": f"Order {order_id} not found."}
    return {"order_id": order_id, "status": order["status"], "eta": order["eta"], "items": order["items"], "customer_id": order["customer_id"]}

# === Agents ===
class ModelSettings:
    def __init__(self, tool_choice="auto", metadata=None):
        self.tool_choice = tool_choice
        self.metadata = metadata or {}

class BotAgent:
    def __init__(self, name="BotAgent", model_settings: ModelSettings = None):
        self.name = name
        self.model_settings = model_settings or ModelSettings()
        self.faqs = {
            "what is your return policy": "You can return items within 14 days of delivery for a full refund (item must be unused).",
            "how to contact support": "You can contact support at support@example.com or call +1-800-555-1234.",
            "do you ship internationally": "Yes, we ship to many countries. Shipping costs vary by destination.",
        }
        self.tools = {"get_order_status": get_order_status}

    @guardrail
    def handle(self, user_input: str):
        q = user_input.strip().lower()

        # 1) FAQ
        for k in self.faqs:
            if k in q:
                log_event("faq_answered", {"faq": k, "user_input": user_input})
                return {"handled": True, "message": self.faqs[k]}

        # 2) Order lookup
        tool = self.tools.get("get_order_status")
        tool_enabled = tool._tool.is_enabled(user_input)
        if self.model_settings.tool_choice == "required" and not tool_enabled:
            log_event("escalation_reason", {"reason": "tool_required_but_disabled", "input": user_input})
            return {"handled": False, "handoff": True, "reason": "tool_required_but_disabled",
                    "message": "I need to fetch order details but couldn't. Handing off to human agent."}
        if tool_enabled and self.model_settings.tool_choice in ("auto", "required"):
            m = re.search(r"\b([A-Z]\d{2,})\b", user_input)
            if m:
                order_id = m.group(1)
                result = tool(user_input, order_id)
                if isinstance(result, dict) and result.get("error"):
                    if tool._tool.error_function:
                        err = tool._tool.error_function(order_id)
                        log_event("tool_error", {"tool": "get_order_status", "order_id": order_id, "error": err})
                        return {"handled": True, "message": err["message"], "tool_error": True}
                    return {"handled": True, "message": result.get("message", "Order error"), "tool_error": True}
                msg = f"Order {order_id} is currently '{result['status']}'. ETA: {result['eta']}. Items: {', '.join(result['items'])}."
                if self.model_settings.metadata.get("customer_id") and self.model_settings.metadata.get("customer_id") == result.get("customer_id"):
                    msg += " (Verified customer)"
                log_event("tool_success", {"tool": "get_order_status", "order_id": order_id})
                return {"handled": True, "message": msg}
            else:
                detected = detect_offensive_or_negative(user_input)
                if detected["negative"]:
                    log_event("escalation", {"reason": "negative_sentiment", "input": user_input})
                    return {"handled": False, "handoff": True, "reason": "negative_sentiment",
                            "message": "I can see you're upset. I'll transfer you to a human agent for support."}
                return {"handled": True, "message": "Could you please provide your order ID (format like A100) so I can check the status?"}

       
        log_event("escalation_reason", {"reason": "unknown_or_complex", "input": user_input})
        return {"handled": False, "handoff": True, "reason": "unknown_or_complex", "message": "I'm not sure about that. I'll hand you over to a human agent."}

class HumanAgent:
    def __init__(self, name="HumanAgent"):
        self.name = name

    def handle(self, context: Dict[str, Any]):
        log_event("human_agent_received", {"context": context})
        user_input = context.get("user_input", "")
        reason = context.get("reason", "unspecified")
        reply = f"[Human Agent] Hello — I've received your request about: '{user_input}'. Reason: {reason}. We'll assist you shortly."
        return {"handled": True, "message": reply}

# === CLI Demo ===
def run_cli():
    print("=== Smart Customer Support Bot (CLI demo) ===")
    print("Mode:", CONFIG.get("MODE"))
    meta = {"customer_id": CONFIG.get("CUSTOMER_ID") or None}
    settings = ModelSettings(tool_choice="auto", metadata=meta)
    bot = BotAgent(model_settings=settings)
    human = HumanAgent()

    print("Type 'exit' to quit. Examples: 'what is your return policy', 'check order A100', 'my order B201', etc.")
    while True:
        try:
            user = input("\nYou: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nGoodbye.")
            break
        if not user:
            continue
        if user.lower() in ("exit", "quit"):
            print("Goodbye.")
            break

        response = bot.handle(user)

        if response.get("handled"):
            if response.get("guardrail"):
                print("Bot:", response.get("message"))
                continue
            print("Bot:", response.get("message"))
            continue

        if response.get("handoff"):
            handoff_context = {
                "user_input": user,
                "reason": response.get("reason"),
                "metadata": settings.metadata,
            }
            log_event("handoff_initiated", {"from": bot.name, "to": "HumanAgent", "context": handoff_context})
            human_reply = human.handle(handoff_context)
            print(human_reply.get("message"))
            continue

        print("Bot: Sorry, I couldn't help with that.")

if __name__ == "__main__":
    run_cli()
