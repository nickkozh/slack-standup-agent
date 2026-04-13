# Slack Standup Agent

AI-powered standup monitor that detects blockers in Slack, emails the right people, stores history in Inkbox Vault, and escalates what's stuck.

## How It Works

1. **Listens** to a Slack channel for standup messages via Socket Mode
2. **Parses** each message using Claude to detect blockers, urgency, categories, and action items
3. **Emails** affected team members via Inkbox with styled HTML notifications
4. **Stores** every blocker in Inkbox Vault with full metadata and daily indices
5. **Escalates** unresolved blockers to the team lead after a configurable threshold
6. **Replies** in the Slack thread with a summary of detected blockers and actions taken

## Features

- **Intelligent Parsing** — Claude Sonnet analyzes standup messages with contextual understanding. Distinguishes real blockers from status updates.
- **Instant Email Alerts** — Affected team members get styled HTML emails via Inkbox within seconds.
- **Inkbox Vault History** — Every blocker is stored with full metadata. Daily indices enable time-based queries.
- **Priority Escalation** — Unresolved blockers older than your threshold (default: 24h) automatically escalate to the team lead.
- **Slack Thread Replies** — The agent replies directly in-thread with detected blockers, who was notified, and any escalations.
- **Smart Categorization** — Blockers categorized as design, engineering, devops, review, decision, or external.
- **Fuzzy Name Matching** — Matches names from standup messages to your team email map (exact, case-insensitive, partial).

## Setup

1. **Create a Slack App** at https://api.slack.com/apps
   - Enable Socket Mode
   - Add Bot Token Scopes: `channels:history`, `channels:read`, `chat:write`
   - Subscribe to Events: `message.channels`
   - Install to workspace

2. **Get API Keys**
   - Slack Bot Token (`xoxb-...`) and App Token (`xapp-...`)
   - Anthropic API key from https://console.anthropic.com
   - Inkbox API key from https://inkbox.ai/console

3. **Configure**
   ```bash
   cp .env.example .env
   # Fill in all values
   ```

4. **Install & Run**
   ```bash
   pip install -r requirements.txt
   python agent.py
   ```

## Team Email Mapping

Set `TEAM_EMAIL_MAP` in `.env` as JSON:
```json
{"Alice": "alice@company.com", "Bob": "bob@company.com", "Sarah": "sarah@company.com"}
```

The agent fuzzy-matches names mentioned in blockers to this map.

## Priority Escalation

Set `ESCALATION_EMAIL` to enable. When a blocker goes unresolved for more than `ESCALATION_HOURS` (default: 24), the team lead gets a summary email with all stale blockers in a table.

## Demo Website

Open `web/index.html` in your browser for a live demo — paste any standup message and see the agent's parsing, email preview, Slack thread reply, and Vault record in action. No API keys needed.

## Architecture

```
Slack Channel → Socket Mode → Claude API (parse blockers)
                                    ↓
                              Inkbox Email (notifications)
                              Inkbox Vault (history + escalation)
                                    ↓
                              Slack Thread Reply (summary)
```

Built with:
- **Slack Bolt** for real-time message listening
- **Claude API** (claude-sonnet-4-20250514) for intelligent blocker detection
- **Inkbox SDK** for email dispatch and Vault storage
