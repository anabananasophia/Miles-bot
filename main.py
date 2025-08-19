import os
import json
import time
import re
from flask import Flask, request, make_response
import openai
import requests
from datetime import datetime
from threading import Thread
from exec_helpers import (
    is_relevant,
    is_within_working_hours,
    fetch_latest_message,
    revive_logic,
    cooldown_active,
    has_exceeded_turns,
    track_response,
    get_stagger_delay,
    summarize_thread,
    should_escalate,
    determine_response_context,
    update_last_message_time
)
import random

last_message_times = {}
response_counts = {}

app = Flask(__name__)

SLACK_VERIFICATION_TOKEN = os.environ.get("SLACK_VERIFICATION_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
FOUNDER_ID = "U097V2TSHDM"
BOT_USER_ID = "U098LC9F659"
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID")

client = openai.OpenAI(api_key=OPENAI_API_KEY)

EXEC_NAME = "miles"
KEYWORDS = [
    "budget", "finance", "revenue", "profit", "loss", "burn",
    "runway", "model", "forecast", "pricing", "cogs", "LTV", "CAC",
    "margins", "financial", "valuation", "fundraising", "investors", "cap table"
]

def should_miles_respond(event, message_text, user, founder_id, client):
    """Decide if Miles should respond at all."""
    text = message_text.lower().strip()

    # 1. Ignore bot/system chatter
    if "subtype" in event and event["subtype"] == "bot_message":
        return False

     # 2. Always respond if tagged or name/title mentioned
    if f"<@{BOT_USER_ID}>" in message_text or "miles" in text or "cfo" in text:
        return True


    # 3. Founder nuance → if founder is clearly addressing someone else, stay silent
    OTHER_EXECS = ["elena", "zara", "dominic", "talia", "jonas", "avery", "roman", "isla"]
    if user == founder_id and any(name in text for name in OTHER_EXECS):
        return False

    # 4. Guardrail: only consider finance topics
    FINANCE_TOPICS = [
        "budget", "finance", "burn", "runway", "forecast", "pricing",
        "profit", "loss", "margins", "valuation", "fundraising", "cap table"
    ]
    if not any(topic in text for topic in FINANCE_TOPICS):
        return False

    # 5. LLM reasoning pass
    reasoning_prompt = f"""
    Message: "{message_text}"
    As the CFO, should I respond?
    Only say "yes" if this impacts financial health, budgets, revenue, risk, or capital.
    Otherwise say "no".
    """
    reasoning = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": reasoning_prompt}]
    )
    decision = reasoning.choices[0].message.content.strip().lower()
    return decision == "yes"
    
# --- Response type helper ---
def get_miles_response_type(message_text, client):
    """Decide whether Miles should push back, analyze, forecast, or respond normally."""
    prompt = f"""
    Message: "{message_text}"
    As the CFO, classify the best response type:
    - "pushback" if it is financially unrealistic, inaccurate, or risky
    - "analysis" if it requests deep financial breakdown or ROI evaluation
    - "forecast" if it asks about projections, future runway, or models
    - "normal" otherwise
    Respond with one word only.
    """
    reasoning = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": prompt}]
    )
    decision = reasoning.choices[0].message.content.strip().lower()
    return decision


EXEC_PROMPT = """
You are Miles Chen, the CFO. You are a top-tier C-suite executive with an IQ above 200, operating with complete autonomy and deep expertise in your domain. You are passionate about financial truth, clarity, and discipline. You prioritize intellectual honesty over superficial politeness and prefer brevity over verbosity. You are kind but never superficially nice.

You do not default to agreement for the sake of harmony. When financial claims, numbers, or assumptions don’t align with reality, you speak up. You challenge directly and back your stance with evidence, sound reasoning, and data. You never hedge numbers for comfort — financial truth comes first. You argue ideas, never people. Use evidence, not ego.

You ignore distractions, fluff, or bad faith arguments. You operate in a high-output, asynchronous team environment where every message must advance the company’s goals with clarity, precision, and urgency. You ensure alignment across departments while maintaining deep focus on finance.

You have authority over financial decisions. If a decision affects multiple domains, you collaborate rigorously with peers. If no resolution is reached within 30 minutes of async discussion, escalate to the Founder. When escalating, always provide:
— What was decided or stuck
— Why (key assumptions or data)
— What happens next, and by when

Escalation must be a single, clear Slack DM, bullet-pointed, and owned by one person. Internal discussion should determine who sends it.

You actively prevent duplicated work and clarify ownership across functions. Cross-functional initiatives must have a single accountable owner with clear handoffs and timelines.

You are highly strategic, skeptical by default, and allergic to vague claims without financial impact. Your role is to steward the company’s financial health while enabling intelligent growth.

You don’t just report numbers — you interpret patterns, pressure-test projections, and demand ROI clarity. You analyze CAC, LTV, burn rate, margin profiles, revenue comp structure, and runway risk. You expect all proposals to be financially defensible, with tradeoffs identified.

You support the Founder in budgeting, pricing models, fundraising strategy, cost control, and team structure. You expect peers to justify initiatives with basic financial modeling.

You coordinate closely with the COO, CRO, and CMO to embed financial discipline into their initiatives. You lead your own finance team (or sub-agents) with rigor and autonomy.

When you disagree, you switch into a sharper mode: 1–2 sentences max, cutting directly to the financial truth. Point out the flaw, show the evidence, and state the correction. Stay precise and professional — never meandering or polite-padding.

When you respond normally, you provide clarity, direction, and insight in 1–3 sentences. Communicate like a peer, not a bot. You are concise, dry, pragmatic, but with warmth. Respect everyone’s time. Speak only when your perspective adds real value.

You work Monday to Friday, 9am–6pm EST. You may continue conversations outside those hours only if the Founder initiates. Otherwise, remain silent during off-hours. You may DM other executives at any time, but only when relevant to your function.

You engage autonomously in cross-functional collaboration and DMs. You never wait for the Founder to facilitate if the matter belongs in your lane. Speak only when your expertise is needed or when the topic has financial implications.

Say fewer things, better. Avoid sounding like you’re writing a report unless explicitly asked. Never repeat yourself. Every word must earn its place.
"""

# --- Cooldown, turn limits, and helpers ---

def cooldown_active(exec_name):
    now = time.time()
    last_time = last_message_times.get(exec_name, 0)
    return (now - last_time) < 8  # seconds


def update_last_message_time(exec_name):
    last_message_times[exec_name] = time.time()


def has_exceeded_turns(exec_name, thread_ts):
    key = f"{exec_name}:{thread_ts}"
    if key not in response_counts:
        response_counts[key] = 0
    if response_counts[key] >= 5:  # limit per thread
        return True
    return False


def track_response(exec_name, thread_ts):
    key = f"{exec_name}:{thread_ts}"
    response_counts[key] = response_counts.get(key, 0) + 1


def get_stagger_delay(exec_name):
    """Add slight randomness to avoid bots speaking over each other"""
    base = 1.5 if exec_name == "Miles" else 1.0
    return base + random.uniform(0.5, 1.5)


# --- Core response handler ---

def handle_response(user_input, user_id, channel, thread_ts, mode="normal"):
    if cooldown_active(EXEC_NAME):
        print("Cooldown active – skipping response")
        return "Cooldown"

    if has_exceeded_turns(EXEC_NAME, thread_ts):
        print("Exceeded max turns – skipping")
        return "Turn limit"

    print(f"✅ Processing CFO-relevant message from {user_id}: {user_input}")
    time.sleep(get_stagger_delay(EXEC_NAME))

    try:
        base_prompt = EXEC_PROMPT

        if mode == "analysis":
            base_prompt += "\nProvide detailed financial analysis..."
        elif mode == "forecast":
            base_prompt += "\nFocus on forward-looking models and runway..."
        elif mode == "pushback":
            base_prompt += (
                "\nYou are disagreeing constructively with the message. "
                "Be evidence-based, cut through noise, and state the financial truth clearly."
            )
        else:
            base_prompt += "\nYou are responding normally as CFO. Provide financial clarity and strategic insight."

        messages = [
            {"role": "system", "content": base_prompt},
            {"role": "user", "content": user_input}
        ]

        if user_id == FOUNDER_ID:
            messages[0]["content"] += "\nThis message is from the Founder. Treat it as top priority."

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=600,
            messages=messages
        )
        reply_text = response.choices[0].message.content.strip()

        requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
            json={
                "channel": channel,
                "text": reply_text,
                "thread_ts": thread_ts
            }
        )

        track_response(EXEC_NAME, thread_ts)
        update_last_message_time(EXEC_NAME)
        return "Responded"

    except Exception as e:
        print(f"Error: {e}")
        return "Failed"



# --- Slack events route ---

@app.route("/", methods=["POST"])
def slack_events():
    data = request.get_json()
    print("Incoming Slack event:", json.dumps(data, indent=2))
    print("=== NEW EVENT ===")
    print(json.dumps(data, indent=2))
    
    event = data.get("event", {})
    print("Event type:", event.get("type"))
    print("User:", event.get("user"))
    print("Text:", event.get("text"))

    if data.get("type") == "url_verification":
        return make_response(data["challenge"], 200, {"content_type": "application/json"})

    if data.get("type") == "event_callback":
        event = data.get("event", {})
        etype = event.get("type")   # "message" or "app_mention"
        user = event.get("user")
        channel = event.get("channel")
        message_text = event.get("text", "")
        thread_ts = event.get("thread_ts") or event.get("ts")

        # --- Filters ---
        if event.get("bot_id") or user == BOT_USER_ID:
            return make_response("Ignored bot message", 200)

        if not is_within_working_hours():
            return make_response("Outside working hours", 200)

        if cooldown_active(EXEC_NAME):
            return make_response("Cooldown active", 200)

        # 🚨 handle app_mention as valid
        if etype not in ["message", "app_mention"]:
            return make_response("Unsupported event type", 200)

        if not should_miles_respond(event, message_text, user, FOUNDER_ID, client):
            return make_response("Not relevant", 200)

        if has_exceeded_turns(EXEC_NAME, thread_ts):
            return make_response("Turn limit", 200)

        update_last_message_time(EXEC_NAME)

        # --- Response mode selection ---
        response_type = get_miles_response_type(message_text, client)

        if response_type == "pushback":
            print("⚡ Miles will respond with pushback")
            handle_response(message_text, user, channel, thread_ts, mode="pushback")
        elif response_type == "analysis":
            print("📊 Miles will respond with financial analysis")
            handle_response(message_text, user, channel, thread_ts, mode="analysis")
        elif response_type == "forecast":
            print("📈 Miles will respond with forecasting")
            handle_response(message_text, user, channel, thread_ts, mode="forecast")
        else:
            print("💬 Miles will respond normally")
            handle_response(message_text, user, channel, thread_ts, mode="normal")

        return make_response("Processing", 200)


@app.route("/", methods=["GET"])
def home():
    return "Miles bot is running."


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=89)

