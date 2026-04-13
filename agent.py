#!/usr/bin/env python3
"""
Slack Standup Agent
Monitors Slack standups, detects blockers via Claude, sends email alerts via Inkbox,
stores blocker history in Inkbox Vault, escalates high-priority items, and replies in Slack threads.
"""

import os
import sys
import json
import logging
import hashlib
from datetime import datetime, timezone, timedelta

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
ESCALATION_EMAIL = os.environ.get("ESCALATION_EMAIL", "")
ESCALATION_HOURS = int(os.environ.get("ESCALATION_HOURS", "24"))
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
    if not ESCALATION_EMAIL:
        log.warning("ESCALATION_EMAIL not set - priority escalation disabled")
    log.info(f"Config loaded. Monitoring channel: {SLACK_CHANNEL_ID}")
    log.info(f"Team email map: {json.dumps(TEAM_EMAIL_MAP, indent=2)}")
    log.info(f"Escalation email: {ESCALATION_EMAIL or '(disabled)'}")
    log.info(f"Escalation threshold: {ESCALATION_HOURS}h")

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
      "action_needed": "what the mentioned person needs to do",
      "category": "design" | "engineering" | "devops" | "review" | "decision" | "external" | "other"
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
- Assign urgency: "high" if words like urgent, critical, ASAP, blocking release, deadline; "medium" if normal dependency; "low" if nice-to-have
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
            log.info(f"    Category: {b.get('category', 'N/A')}")
            log.info(f"    Action: {b.get('action_needed', 'N/A')}")
    log.info(f"--- BLOCKER DETECTION END ---")

    return result

# ---------------------------------------------------------------------------
# Inkbox Vault: Store blocker history
# ---------------------------------------------------------------------------

def store_blocker_in_vault(parsed: dict, channel: str, message_ts: str):
    """Store blocker data in Inkbox Vault for history tracking and escalation."""
    if not parsed.get("has_blockers") or not parsed.get("blockers"):
        return

    log.info("--- VAULT STORAGE START ---")

    with Inkbox(api_key=INKBOX_API_KEY) as inkbox:
        vault = inkbox.vault

        for i, blocker in enumerate(parsed["blockers"]):
            blocker_id = hashlib.sha256(
                f"{message_ts}-{i}-{blocker.get('description', '')}".encode()
            ).hexdigest()[:16]

            vault_key = f"blocker:{blocker_id}"
            record = {
                "id": blocker_id,
                "author": parsed.get("author_name", "unknown"),
                "description": blocker.get("description", ""),
                "mentioned_people": blocker.get("mentioned_people", []),
                "urgency": blocker.get("urgency", "medium"),
                "category": blocker.get("category", "other"),
                "action_needed": blocker.get("action_needed", ""),
                "channel": channel,
                "message_ts": message_ts,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "resolved": False,
                "escalated": False,
            }

            try:
                vault.store(vault_key, json.dumps(record))
                log.info(f"  Stored blocker {blocker_id} in vault: {blocker.get('description', '')[:60]}")
            except Exception as e:
                log.error(f"  Failed to store blocker in vault: {e}")

        # Also store a daily summary index
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        index_key = f"daily-index:{today}"
        try:
            existing = vault.retrieve(index_key)
            index = json.loads(existing) if existing else []
        except Exception:
            index = []

        for i, blocker in enumerate(parsed["blockers"]):
            blocker_id = hashlib.sha256(
                f"{message_ts}-{i}-{blocker.get('description', '')}".encode()
            ).hexdigest()[:16]
            index.append({
                "blocker_id": blocker_id,
                "author": parsed.get("author_name", "unknown"),
                "urgency": blocker.get("urgency", "medium"),
                "description": blocker.get("description", "")[:100],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

        try:
            vault.store(index_key, json.dumps(index))
            log.info(f"  Updated daily index ({today}): {len(index)} blockers total")
        except Exception as e:
            log.error(f"  Failed to update daily index: {e}")

    log.info("--- VAULT STORAGE END ---")


def check_stale_blockers_for_escalation():
    """Check vault for unresolved blockers older than ESCALATION_HOURS and escalate."""
    if not ESCALATION_EMAIL:
        return []

    log.info("--- ESCALATION CHECK START ---")
    stale_blockers = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=ESCALATION_HOURS)

    with Inkbox(api_key=INKBOX_API_KEY) as inkbox:
        vault = inkbox.vault

        # Check last 7 days of indices
        for days_ago in range(7):
            date = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d")
            index_key = f"daily-index:{date}"
            try:
                raw = vault.retrieve(index_key)
                if not raw:
                    continue
                index = json.loads(raw)
                for entry in index:
                    blocker_key = f"blocker:{entry['blocker_id']}"
                    try:
                        blocker_raw = vault.retrieve(blocker_key)
                        if not blocker_raw:
                            continue
                        blocker = json.loads(blocker_raw)
                        created = datetime.fromisoformat(blocker["created_at"])
                        if not blocker.get("resolved") and not blocker.get("escalated") and created < cutoff:
                            stale_blockers.append(blocker)
                            # Mark as escalated
                            blocker["escalated"] = True
                            blocker["escalated_at"] = datetime.now(timezone.utc).isoformat()
                            vault.store(blocker_key, json.dumps(blocker))
                    except Exception as e:
                        log.debug(f"  Could not check blocker {entry.get('blocker_id')}: {e}")
            except Exception as e:
                log.debug(f"  Could not read index for {date}: {e}")

    if stale_blockers:
        log.info(f"  Found {len(stale_blockers)} stale blockers to escalate")
    else:
        log.info("  No stale blockers found")

    log.info("--- ESCALATION CHECK END ---")
    return stale_blockers


def mark_blocker_resolved(blocker_id: str):
    """Mark a blocker as resolved in the vault."""
    with Inkbox(api_key=INKBOX_API_KEY) as inkbox:
        vault = inkbox.vault
        key = f"blocker:{blocker_id}"
        try:
            raw = vault.retrieve(key)
            if raw:
                blocker = json.loads(raw)
                blocker["resolved"] = True
                blocker["resolved_at"] = datetime.now(timezone.utc).isoformat()
                vault.store(key, json.dumps(blocker))
                log.info(f"Marked blocker {blocker_id} as resolved")
                return True
        except Exception as e:
            log.error(f"Failed to mark blocker resolved: {e}")
    return False

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
                category = blocker.get("category", "other").title()

                urgency_color = "#dc2626" if urgency_label == "HIGH" else "#f59e0b" if urgency_label == "MEDIUM" else "#3b82f6"

                body_html = f"""
<div style="font-family: sans-serif; max-width: 600px;">
    <h2 style="color: {urgency_color};">
        [{urgency_label}] Blocker Detected
    </h2>
    <p><strong>{parsed.get('author_name', 'A teammate')}</strong> flagged a blocker that involves you:</p>
    <blockquote style="border-left: 4px solid #e5e7eb; padding-left: 16px; color: #374151;">
        {blocker.get('description', 'No description')}
    </blockquote>
    <table style="border-collapse: collapse; margin: 16px 0;">
        <tr><td style="padding: 4px 12px 4px 0; color: #6b7280;">Category:</td><td><strong>{category}</strong></td></tr>
        <tr><td style="padding: 4px 12px 4px 0; color: #6b7280;">Urgency:</td><td><strong style="color: {urgency_color};">{urgency_label}</strong></td></tr>
    </table>
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
                    f"Category: {category}\n"
                    f"Action needed from you: {blocker.get('action_needed', 'Please follow up')}\n\n"
                    f"---\nOriginal standup:\n{original_message[:500]}\n\n"
                    f"-- Sent by Slack Standup Agent via Inkbox"
                )

                log.info(f"Sending email to {email} (for {person})")
                log.debug(f"  Subject: {subject}")
                log.debug(f"  Urgency: {urgency_label} | Category: {category}")

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


def send_escalation_email(stale_blockers: list):
    """Send escalation email to the team lead with all stale unresolved blockers."""
    if not stale_blockers or not ESCALATION_EMAIL:
        return

    log.info(f"--- ESCALATION EMAIL START ---")
    log.info(f"Escalating {len(stale_blockers)} stale blockers to {ESCALATION_EMAIL}")

    with Inkbox(api_key=INKBOX_API_KEY) as inkbox:
        identity = inkbox.create_identity(
            "standup-agent",
            display_name="Standup Agent - ESCALATION"
        )

        blocker_rows = ""
        for b in stale_blockers:
            urgency = b.get("urgency", "medium").upper()
            color = "#dc2626" if urgency == "HIGH" else "#f59e0b" if urgency == "MEDIUM" else "#3b82f6"
            created = b.get("created_at", "unknown")[:10]
            blocker_rows += f"""
            <tr>
                <td style="padding: 8px; border-bottom: 1px solid #e5e7eb;">{b.get('author', 'unknown')}</td>
                <td style="padding: 8px; border-bottom: 1px solid #e5e7eb;">{b.get('description', '')[:80]}</td>
                <td style="padding: 8px; border-bottom: 1px solid #e5e7eb;"><span style="color: {color}; font-weight: bold;">{urgency}</span></td>
                <td style="padding: 8px; border-bottom: 1px solid #e5e7eb;">{', '.join(b.get('mentioned_people', []))}</td>
                <td style="padding: 8px; border-bottom: 1px solid #e5e7eb;">{created}</td>
            </tr>"""

        body_html = f"""
<div style="font-family: sans-serif; max-width: 800px;">
    <h2 style="color: #dc2626;">Priority Escalation: {len(stale_blockers)} Unresolved Blockers</h2>
    <p>The following blockers have been open for more than <strong>{ESCALATION_HOURS} hours</strong> without resolution:</p>
    <table style="border-collapse: collapse; width: 100%; margin: 16px 0;">
        <thead>
            <tr style="background: #f9fafb;">
                <th style="padding: 8px; text-align: left; border-bottom: 2px solid #e5e7eb;">Author</th>
                <th style="padding: 8px; text-align: left; border-bottom: 2px solid #e5e7eb;">Blocker</th>
                <th style="padding: 8px; text-align: left; border-bottom: 2px solid #e5e7eb;">Urgency</th>
                <th style="padding: 8px; text-align: left; border-bottom: 2px solid #e5e7eb;">Assigned To</th>
                <th style="padding: 8px; text-align: left; border-bottom: 2px solid #e5e7eb;">Created</th>
            </tr>
        </thead>
        <tbody>{blocker_rows}</tbody>
    </table>
    <p>Please review these blockers and take action to unblock your team.</p>
    <hr style="border: none; border-top: 1px solid #e5e7eb;">
    <p style="color: #9ca3af; font-size: 12px;">Sent by Slack Standup Agent via Inkbox - Priority Escalation</p>
</div>"""

        text_lines = [f"ESCALATION: {len(stale_blockers)} Unresolved Blockers (>{ESCALATION_HOURS}h)\n"]
        for b in stale_blockers:
            text_lines.append(
                f"- [{b.get('urgency', 'medium').upper()}] {b.get('author', '?')}: "
                f"{b.get('description', '')[:80]} -> {', '.join(b.get('mentioned_people', []))}"
            )

        try:
            sent = identity.send_email(
                to=[ESCALATION_EMAIL],
                subject=f"ESCALATION: {len(stale_blockers)} unresolved blocker(s) need attention",
                body_text="\n".join(text_lines),
                body_html=body_html,
            )
            log.info(f"  Escalation email sent to {ESCALATION_EMAIL} (message id: {sent.id})")
        except Exception as e:
            log.error(f"  Failed to send escalation email: {e}")

    log.info(f"--- ESCALATION EMAIL END ---")

# ---------------------------------------------------------------------------
# Name -> Email resolution
# ---------------------------------------------------------------------------

def resolve_email(name: str) -> str | None:
    """Fuzzy-match a person's name to their email from TEAM_EMAIL_MAP."""
    if name in TEAM_EMAIL_MAP:
        return TEAM_EMAIL_MAP[name]

    name_lower = name.lower()
    for key, email in TEAM_EMAIL_MAP.items():
        if key.lower() == name_lower:
            return email

    for key, email in TEAM_EMAIL_MAP.items():
        if name_lower in key.lower() or key.lower() in name_lower:
            return email

    return None

# ---------------------------------------------------------------------------
# Slack: Thread replies
# ---------------------------------------------------------------------------

def post_thread_reply(client, channel: str, thread_ts: str, parsed: dict, escalated_count: int = 0):
    """Reply in the Slack thread with a summary of detected blockers and actions taken."""
    if not parsed.get("has_blockers") or not parsed.get("blockers"):
        return

    blockers = parsed["blockers"]
    lines = [f"*Standup Agent detected {len(blockers)} blocker(s):*\n"]

    for i, b in enumerate(blockers, 1):
        urgency = b.get("urgency", "medium")
        emoji = ":red_circle:" if urgency == "high" else ":large_orange_circle:" if urgency == "medium" else ":large_blue_circle:"
        people = ", ".join(b.get("mentioned_people", ["(unspecified)"]))
        lines.append(f"{emoji} *Blocker {i}:* {b.get('description', 'N/A')}")
        lines.append(f"   _Assigned to:_ {people} | _Action:_ {b.get('action_needed', 'Follow up')}")

    notified = []
    for b in blockers:
        for person in b.get("mentioned_people", []):
            email = resolve_email(person)
            if email:
                notified.append(f"{person} ({email})")

    if notified:
        lines.append(f"\n:email: *Notified via email:* {', '.join(set(notified))}")

    if escalated_count > 0:
        lines.append(f"\n:rotating_light: *{escalated_count} stale blocker(s) escalated to team lead*")

    lines.append("\n_Blocker history saved to Inkbox Vault_ :lock:")

    try:
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text="\n".join(lines),
        )
        log.info(f"Thread reply posted in {channel} (thread: {thread_ts})")
    except Exception as e:
        log.error(f"Failed to post thread reply: {e}")

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
    message_ts = event.get("ts", "")

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
    log.info(f"Message TS: {message_ts}")
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

    # Check for resolution keywords
    resolution_keywords = ["unblocked", "resolved", "fixed", "no longer blocked", "cleared"]
    if any(kw in text.lower() for kw in resolution_keywords):
        log.info("Resolution keyword detected - checking for blocker resolution")
        # Still parse normally, but also check resolution

    # Step 1: Parse with Claude
    try:
        parsed = parse_standup(text, author)
    except Exception as e:
        log.error(f"Claude parsing failed: {e}", exc_info=True)
        return

    # Step 2: Store blockers in Inkbox Vault
    if parsed.get("has_blockers"):
        try:
            store_blocker_in_vault(parsed, channel, message_ts)
        except Exception as e:
            log.error(f"Vault storage failed: {e}", exc_info=True)

    # Step 3: Send emails for blockers
    if parsed.get("has_blockers"):
        log.info(f"Blockers found! Dispatching {len(parsed.get('blockers', []))} notifications...")
        try:
            send_blocker_emails(parsed, text)
        except Exception as e:
            log.error(f"Email dispatch failed: {e}", exc_info=True)
    else:
        log.info(f"No blockers detected. Summary: {parsed.get('summary', 'N/A')}")

    # Step 4: Check for stale blockers to escalate
    escalated_count = 0
    try:
        stale = check_stale_blockers_for_escalation()
        if stale:
            send_escalation_email(stale)
            escalated_count = len(stale)
    except Exception as e:
        log.error(f"Escalation check failed: {e}", exc_info=True)

    # Step 5: Post thread reply in Slack
    if parsed.get("has_blockers"):
        try:
            post_thread_reply(client, channel, message_ts, parsed, escalated_count)
        except Exception as e:
            log.error(f"Thread reply failed: {e}", exc_info=True)

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
    log.info(f"Features: Email alerts, Vault storage, Priority escalation, Thread replies")

    handler.start()


if __name__ == "__main__":
    main()
