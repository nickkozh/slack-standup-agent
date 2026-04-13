# Slack Standup Agent

AI-powered standup monitor that detects blockers in Slack standup messages and automatically emails relevant team members with actionable notifications.

## How It Works

1. **Listens** to a Slack channel for standup messages via Socket Mode
2. **Parses** each message using Claude to detect blockers, dependencies, and action items
3. **Emails** affected team members via Inkbox with specific blocker details and context

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
{"Alice": "alice@company.com", "Bob": "bob@company.com"}
```

The agent fuzzy-matches names mentioned in blockers to this map.

## Architecture

```
Slack Channel --> Socket Mode --> Claude API (parse blockers) --> Inkbox (send emails)
```

Built with:
- **Slack Bolt** for real-time message listening
- **Claude API** (claude-sonnet-4-20250514) for intelligent blocker detection
- **Inkbox SDK** for email dispatch
