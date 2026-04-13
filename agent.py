#!/usr/bin/env python3
"""
Slack Standup Agent
Monitors Slack standups, detects blockers via Claude, and emails affected team members via Inkbox.
"""

import os
import sys
import json
import logging
from datetime import datetime, timezone

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
import anthropic
from inkbox import Inkbox

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

load_dotenv()

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
INKBOX_API_KEY = os.environ.get("INKBOX_API_KEY", "")
TEAM_EMAIL_MAP = json.loads(os.environ.get("TEAM_EMAIL_MAP", "{}"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "DEBUG")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.DEBUG),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("standup-agent")

# ---------------------------------------------------------------------------
# Validate config
# ---------------------------------------------------------------------------

def validate_config():
    missing = []
    for var in ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_CHANNEL_ID",
                "ANTHROPIC_API_KEY", "INKBOX_API_KEY"]:
        if not os.environ.get(var):
            missing.append(var)
    if missing:
        log.error(f"Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)
    if not TEAM_EMAIL_MAP:
        log.warning("TEAM_EMAIL_MAP is empty - no emails will be sent. Set it in .env")
    log.info(f"Config loaded. Monitoring channel: {SLACK_CHANNEL_ID}")
    log.info(f"Team email map: {json.dumps(TEAM_EMAIL_MAP, indent=2)}")

# ---------------------------------------------------------------------------
# Claude: Parse standup message for blockers
# ---------------------------------------------------------------------------

claude_client = None

def get_claude_client():
    global claude_client
    if claude_client is None:
        claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return claude_client


BLOCKER_PROMPT = """You are analyzing a standup message from a team member. Extract any blockers, dependencies on other people, or requests for help.

Return a JSON object with this exact structure:
{
  "has_blockers": true/false,
  "author_name": "the person who wrote this standup",
  "blockers": [
    {
      "description": "clear description of the blocker",
      "mentioned_people": ["Name1", "Name2"],
      "urgency": "high" | "medium" | "low",
      "action_needed": "what the mentioned person needs to do"
    }
  ],
  "summary": "one-line summary of the standup"
}

Rules:
- Only flag actual blockers — things preventing progress or requiring someone else's action
- "Waiting on X" or "blocked by X" or "need X from Y" are blockers
- General status updates are NOT blockers
- If no blockers, return has_blockers: false with empty blockers array
- Be precise about who is mentioned — use exact names from the message
- Return ONLY valid JSON, no markdown fences"""


def parse_standup(message_text: str, author: str) -> dict:
    """Use Claude to parse a standup message and extract blockers."""
    log.info(f"--- BLOCKER DETECTION START ---")
    log.info(f"Author: {author}")
    log.info(f"Message: {message_text[:500]}")

    client = get_claude_client()

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        messages=[
            {
                "role": "user",
                "content": f"Standup message from **{author}**:\n\n{message_text}"
            }
        ],
        system=BLOCKER_PROMPT,
    )

    raw = response.content[0].text
    log.debug(f"Claude raw response: {raw}")

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        # Try to extract JSON from response
        import re
        match = re.search(r'\{[\s\S]*\}', raw)
        if match:
            result = json.loads(match.group())
        else:
            log.error(f"Failed to parse Claude response as JSON: {raw}")
            result = {"has_blockers": False, "blockers": [], "summary": "Parse error", "author_name": author}

    log.info(f"Blockers detected: {result.get('has_blockers', False)}")
    if result.get("blockers"):
        for i, b in enumerate(result["blockers"]):
            log.info(f"  Blocker {i+1}: {b.get('description', 'N/A')}")
            log.info(f"    People: {b.get('mentioned_people', [])}")
            log.info(f"    Urgency: {b.get('urgency', 'N/A')}")
            log.info(f"    Action: {b.get('action_needed', 'N/A')}")
    log.info(f"--- BLOCKER DETECTION END ---")

    return result

# ---------------------------------------------------------------------------
# Inkbox: Send blocker notification emails
# ---------------------------------------------------------------------------

def send_blocker_emails(parsed: dict, original_message: str):
    """Send email notifications to team members mentioned in blockers."""
    if not parsed.get("has_blockers") or not parsed.get("blockers"):
        log.info("No blockers to notify about.")
        return

    log.info(f"--- EMAIL DISPATCH START ---")

    with Inkbox(api_key=INKBOX_API_KEY) as inkbox:
        identity = inkbox.create_identity(
            "standup-agent",
            display_name="Standup Blocker Alert"
        )

        for blocker in parsed["blockers"]:
            mentioned = blocker.get("mentioned_people", [])
            if not mentioned:
                log.warning(f"Blocker has no mentioned people, skipping: {blocker.get('description')}")
                continue

            for person in mentioned:
                email = resolve_email(person)
                if not email:
                    log.warning(f"No email found for '{person}' - skipping notification")
                    continue

                subject = f"Standup Blocker Alert: {parsed.get('author_name', 'A teammate')} is blocked"
                urgency_label = blocker.get("urgency", "medium").upper()

                body_html = f"""
<div style="font-family: sans-serif; max-width: 600px;">
    <h2 style="color: {'#dc2626' if urgency_label == 'HIGH' else '#f59e0b' if urgency_label == 'MEDIUM' else '#3b82f6'};">
        [{urgency_label}] Blocker Detected
    </h2>
    <p><strong>{parsed.get('author_name', 'A teammate')}</strong> flagged a blocker that involves you:</p>
    <blockquote style="border-left: 4px solid #e5e7eb; padding-left: 16px; color: #374151;">
        {blocker.get('description', 'No description')}
    </blockquote>
    <p><strong>Action needed from you:</strong> {blocker.get('action_needed', 'Please follow up')}</p>
    <hr style="border: none; border-top: 1px solid #e5e7eb;">
    <p style="color: #6b7280; font-size: 14px;"><strong>Original standup message:</strong></p>
    <p style="color: #6b7280; font-size: 14px;">{original_message[:500]}</p>
    <hr style="border: none; border-top: 1px solid #e5e7eb;">
    <p style="color: #9ca3af; font-size: 12px;">Sent by Slack Standup Agent via Inkbox</p>
</div>"""

                body_text = (
                    f"[{urgency_label}] Blocker Detected\n\n"
                    f"{parsed.get('author_name', 'A teammate')} flagged a blocker that involves you:\n\n"
                    f"> {blocker.get('description', 'No description')}\n\n"
                    f"Action needed from you: {blocker.get('action_needed', 'Please follow up')}\n\n"
                    f"---\nOriginal standup:\n{original_message[:500]}\n\n"
                    f"-- Sent by Slack Standup Agent via Inkbox"
                )

                log.info(f"Sending email to {email} (for {person})")
                log.debug(f"  Subject: {subject}")
                log.debug(f"  Body preview: {body_text[:200]}...")

                try:
                    sent = identity.send_email(
                        to=[email],
                        subject=subject,
                        body_text=body_text,
                        body_html=body_html,
                    )
                    log.info(f"  Email sent successfully to {email} (message id: {sent.id})")
                except Exception as e:
                    log.error(f"  Failed to send email to {email}: {e}")

    log.info(f"--- EMAIL DISPATCH END ---")

# ---------------------------------------------------------------------------
# Name -> Email resolution
# ---------------------------------------------------------------------------

def resolve_email(name: str) -> str | None:
    """Fuzzy-match a person's name to their email from TEAM_EMAIL_MAP."""
    # Exact match
    if name in TEAM_EMAIL_MAP:
        return TEAM_EMAIL_MAP[name]

    # Case-insensitive match
    name_lower = name.lower()
    for key, email in TEAM_EMAIL_MAP.items():
        if key.lower() == name_lower:
            return email

    # Partial match (first name)
    for key, email in TEAM_EMAIL_MAP.items():
        if name_lower in key.lower() or key.lower() in name_lower:
            return email

    return None

# ---------------------------------------------------------------------------
# Slack: Listen for standup messages
# ---------------------------------------------------------------------------

app = App(token=SLACK_BOT_TOKEN)


@app.event("message")
def handle_message(event, say, client):
    """Process incoming messages in the monitored channel."""
    channel = event.get("channel", "")
    subtype = event.get("subtype")
    text = event.get("text", "")
    user_id = event.get("user", "")

    # Only process messages in the target channel
    if channel != SLACK_CHANNEL_ID:
        return

    # Skip bot messages, edits, deletions
    if subtype in ("bot_message", "message_changed", "message_deleted"):
        return

    # Skip empty messages
    if not text.strip():
        return

    log.info(f"={'='*60}")
    log.info(f"NEW STANDUP MESSAGE RECEIVED")
    log.info(f"{'='*60}")
    log.info(f"Channel: {channel}")
    log.info(f"User ID: {user_id}")
    log.info(f"Timestamp: {datetime.now(timezone.utc).isoformat()}")

    # Resolve user display name
    author = user_id
    try:
        user_info = client.users_info(user=user_id)
        profile = user_info["user"]["profile"]
        author = profile.get("display_name") or profile.get("real_name") or user_id
        log.info(f"Author resolved: {author}")
    except Exception as e:
        log.warning(f"Could not resolve user name for {user_id}: {e}")

    log.info(f"Message text: {text[:300]}")

    # Step 1: Parse with Claude
    try:
        parsed = parse_standup(text, author)
    except Exception as e:
        log.error(f"Claude parsing failed: {e}", exc_info=True)
        return

    # Step 2: Send emails for blockers
    if parsed.get("has_blockers"):
        log.info(f"Blockers found! Dispatching {len(parsed.get('blockers', []))} notifications...")
        try:
            send_blocker_emails(parsed, text)
        except Exception as e:
            log.error(f"Email dispatch failed: {e}", exc_info=True)
    else:
        log.info(f"No blockers detected. Summary: {parsed.get('summary', 'N/A')}")

    log.info(f"{'='*60}")
    log.info(f"MESSAGE PROCESSING COMPLETE")
    log.info(f"{'='*60}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info("=" * 60)
    log.info("SLACK STANDUP AGENT STARTING")
    log.info("=" * 60)

    validate_config()

    log.info("Connecting to Slack via Socket Mode...")
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)

    log.info("Agent is live! Listening for standup messages...")
    log.info(f"Monitoring channel: {SLACK_CHANNEL_ID}")
    log.info(f"Team members configured: {list(TEAM_EMAIL_MAP.keys())}")

    handler.start()


if __name__ == "__main__":
    main()
