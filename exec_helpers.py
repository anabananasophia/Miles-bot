import os
import time
import requests
from datetime import datetime

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID")

# Shared memory objects
last_reply_time = {}
turns_per_thread = {}
last_message_ts = 0

# Constants
MAX_TURNS_PER_THREAD = 3
COOLDOWN_SECONDS = 30
REVIVER_LOOKBACK_SECONDS = 180
REVIVER_CHECK_INTERVAL = 90

# Handoff rules (example pairs)
HANDOFF_MAP = {
    "isla": ["elena", "talia"],
    "dominic": ["miles"],
    "zara": ["isla"],
    "roman": ["elena"],
}

def is_relevant(message_text, keywords):
    lowered = message_text.lower()
    return any(k in lowered for k in keywords)

def is_within_working_hours():
    now = datetime.utcnow()
    est_hour = (now.hour - 4) % 24
    return now.weekday() < 5 and 9 <= est_hour < 18

def fetch_latest_message(thread_ts):
    try:
        resp = requests.get(
            "https://slack.com/api/conversations.replies",
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
            params={"channel": SLACK_CHANNEL_ID, "ts": thread_ts, "limit": 5}
        )
        messages = resp.json().get("messages", [])
        return messages[-1]["ts"] if messages else thread_ts
    except:
        return thread_ts

def revive_logic(callback):
    global last_message_ts
    while True:
        time.sleep(REVIVER_CHECK_INTERVAL)
        if time.time() - last_message_ts > REVIVER_LOOKBACK_SECONDS:
            print("Reviver triggered")
            callback()

def should_cancel_due_to_new_message(thread_ts):
    latest_ts = fetch_latest_message(thread_ts)
    return latest_ts != thread_ts

def cooldown_active(exec_name):
    now = time.time()
    return now - last_reply_time.get(exec_name, 0) < COOLDOWN_SECONDS

def track_response(exec_name, thread_ts):
    now = time.time()
    last_reply_time[exec_name] = now
    if thread_ts:
        turns_per_thread.setdefault(thread_ts, {})[exec_name] = turns_per_thread.get(thread_ts, {}).get(exec_name, 0) + 1

def has_exceeded_turns(exec_name, thread_ts):
    return turns_per_thread.get(thread_ts, {}).get(exec_name, 0) >= MAX_TURNS_PER_THREAD

def update_last_message_time():
    global last_message_ts
    last_message_ts = time.time()

def set_global_message_ts(ts):
    global last_message_ts
    last_message_ts = ts

def get_stagger_delay(exec_name, min_sec=1.5, max_sec=4.0):
    seed = sum([ord(c) for c in exec_name]) % 100
    offset = (seed % int((max_sec - min_sec) * 10)) / 10.0
    return round(min_sec + offset, 1)

def should_escalate(thread_ts, exec_turns, max_turns, last_responder):
    return (
        sum(exec_turns.get(thread_ts, {}).values()) >= max_turns * 3 and
        last_responder != "elena"
    )

def summarize_thread(thread_ts):
    try:
        resp = requests.get(
            "https://slack.com/api/conversations.replies",
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
            params={"channel": SLACK_CHANNEL_ID, "ts": thread_ts}
        )
        messages = resp.json().get("messages", [])
        thread_text = "\n".join([m.get("text", "") for m in messages])

        from openai import OpenAI
        client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

        summary_response = client.chat.completions.create(
            model="gpt-4.1",
            messages=[
                {"role": "system", "content": "Summarize this Slack thread in 3 bullet points focused on decisions, outcomes, or unresolved issues."},
                {"role": "user", "content": thread_text}
            ]
        )
        return summary_response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Failed to summarize thread: {e}")
        return None

# ===== Response Context Decision Logic =====
def determine_response_context(event):
    """
    Decide whether to reply in a thread or in the channel, based on message content and metadata.
    """
    text = event.get("text", "").lower()
    thread_ts = event.get("thread_ts")
    ts = event.get("ts")
    user_id = event.get("user", "")
    bot_id = event.get("bot_id", "")

    if thread_ts:
        return {"thread_ts": thread_ts}

    if bot_id:
        return {"thread_ts": ts}

    if user_id == os.environ.get("FOUNDER_ID"):
        if any(kw in text for kw in ["reminder", "ping", "fyi", "log", "minor"]):
            return {"thread_ts": ts}
        return {}

    STRATEGIC_KEYWORDS = [
        "strategy", "roadmap", "launch", "vision", "funding", "priority",
        "q3", "quarter", "forecast", "big picture", "alignment"
    ]
    if any(kw in text for kw in STRATEGIC_KEYWORDS):
        return {}

    TACTICAL_KEYWORDS = [
        "bug", "issue", "copy", "feedback", "minor", "follow-up",
        "reminder", "cta", "typo", "handoff", "link", "can you check"
    ]
    if any(kw in text for kw in TACTICAL_KEYWORDS):
        return {"thread_ts": ts}

    if "?" in text:
        return {}

    return {"thread_ts": ts}
