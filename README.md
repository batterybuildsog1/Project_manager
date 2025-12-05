# Project Manager Agent

A Telegram bot powered by Grok 4.1 that acts as your personal project management assistant. Chat with it from your phone anytime - it remembers context and helps you manage tasks.

## Quick Start

```bash
# Start the agent (from any terminal)
~/.claude/project-manager/run.sh
```

Then message `@Alan0130_bot` on Telegram.

## Architecture

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│  Telegram   │────▶│   ngrok     │────▶│   Flask     │────▶│  Grok 4.1   │
│  (phone)    │◀────│   tunnel    │◀────│   :4000     │◀────│    API      │
└─────────────┘     └─────────────┘     └─────────────┘     └─────────────┘
                                              │
                                              ▼
                                        ┌─────────────┐
                                        │   SQLite    │
                                        │  agent.db   │
                                        └─────────────┘
```

## File Structure

```
~/.claude/project-manager/
├── server.py          # Flask server - webhook handler, API endpoints
├── grok_client.py     # Grok 4.1 API client with conversation
├── telegram_client.py # Telegram Bot API client
├── db.py              # SQLite persistence layer
├── agent.db           # SQLite database (auto-created)
├── run.sh             # One-command startup script
└── README.md          # This file
```

## How It Works

1. **You send a message** to `@Alan0130_bot` on Telegram
2. **Telegram pushes** the message to ngrok webhook URL
3. **ngrok forwards** to your local Flask server on port 4000
4. **Flask saves** your message to SQLite for context
5. **Flask calls Grok 4.1** with conversation history
6. **Grok responds** with helpful project management advice
7. **Flask sends** the response back to Telegram
8. **You see the reply** on your phone

## Starting the Agent

### Option 1: Run Script (Recommended)
```bash
~/.claude/project-manager/run.sh
```

This automatically:
- Starts Flask server on port 4000
- Starts ngrok tunnel
- Sets Telegram webhook
- Shows status when ready

### Option 2: Manual Start
```bash
# Terminal 1: Start Flask server
cd ~/.claude/project-manager
export TELEGRAM_BOT_TOKEN="your-telegram-bot-token"
export TELEGRAM_CHAT_ID="your-chat-id"
export XAI_API_KEY="your-xai-api-key"
python3 server.py

# Terminal 2: Start ngrok
ngrok http 4000

# Terminal 3: Set webhook (use ngrok URL from above)
curl -X POST http://localhost:4000/webhook/set \
  -H "Content-Type: application/json" \
  -d '{"url": "https://YOUR-NGROK-URL.ngrok-free.dev/webhook"}'
```

## Stopping the Agent

- If using `run.sh`: Press `Ctrl+C` (cleans up everything)
- If manual: Kill each process separately

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Health check |
| `/webhook` | POST | Telegram webhook (receives messages) |
| `/send` | POST | Manually send a message |
| `/history` | GET | View conversation history |
| `/clear` | POST | Clear conversation history |
| `/webhook/set` | POST | Set Telegram webhook URL |
| `/webhook/info` | GET | Get current webhook info |
| `/webhook/delete` | POST | Delete webhook |

### Examples

```bash
# Check server health
curl http://localhost:4000/

# View conversation history
curl http://localhost:4000/history

# Clear history
curl -X POST http://localhost:4000/clear

# Send a test message
curl -X POST http://localhost:4000/send \
  -H "Content-Type: application/json" \
  -d '{"message": "Hello from API!"}'
```

## Editing with Claude Code

### Edit the Grok System Prompt
The AI's personality is defined in `grok_client.py`:

```bash
# In Claude Code, say:
"Edit the system prompt in ~/.claude/project-manager/grok_client.py"
```

Current prompt location: `grok_client.py:18-26`

```python
SYSTEM_PROMPT = """You are a helpful project manager assistant..."""
```

### Add New Features
Common modifications:

1. **Add slash commands** (e.g., `/status`, `/tasks`):
   - Edit `server.py` webhook handler (~line 45-95)
   - Parse `text.startswith("/command")` and handle accordingly

2. **Change the AI model**:
   - Edit `grok_client.py:16`: `MODEL = "grok-4-latest"`
   - Options: `grok-4-latest`, `grok-3-latest`, etc.

3. **Adjust conversation memory**:
   - Edit `server.py:32`: `CONTEXT_MESSAGES = 10`
   - Higher = more context, more tokens, higher cost

4. **Add rate limiting**:
   - Edit `grok_client.py` to add `time.sleep()` between calls

### File Editing Guide

| What to Change | File | Location |
|----------------|------|----------|
| AI personality/instructions | `grok_client.py` | `SYSTEM_PROMPT` (line 18) |
| AI model | `grok_client.py` | `MODEL` (line 16) |
| Conversation memory length | `server.py` | `CONTEXT_MESSAGES` (line 32) |
| Webhook handling logic | `server.py` | `webhook()` function (line 45) |
| Database schema | `db.py` | `init_db()` function (line 19) |
| Telegram message parsing | `telegram_client.py` | `parse_update()` (line 87) |
| Startup behavior | `run.sh` | Shell script |

## Environment Variables

All stored in `~/.zshrc`:

| Variable | Description |
|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Your chat ID for notifications |
| `XAI_API_KEY` | Grok API key (original) |
| `XAI_API_KEY_FAST` | Grok 4.1 fast key (used by agent) |
| `NGROK_AUTHTOKEN` | ngrok authentication token |

## Troubleshooting

### Error: "Grok API error 403: error code: 1010"
**Cause**: Cloudflare blocking bot-like requests
**Fix**: Ensure `grok_client.py` has browser-like headers (already fixed)

### Webhook not receiving messages
```bash
# Check webhook status
curl http://localhost:4000/webhook/info

# Re-set webhook if URL changed
curl -X POST http://localhost:4000/webhook/set \
  -H "Content-Type: application/json" \
  -d '{"url": "https://NEW-NGROK-URL/webhook"}'
```

### ngrok URL changed
ngrok free tier gives new URLs each restart. Re-run `run.sh` or manually set webhook.

### Server won't start (port in use)
```bash
# Kill process on port 4000
lsof -ti:4000 | xargs kill -9
```

### Check server logs
```bash
# If running in background, check the terminal output
# Or check the Flask logs in the terminal where server.py is running
```

## Database

SQLite database at `~/.claude/project-manager/agent.db`

### Tables

**messages** - Conversation history
```sql
id INTEGER PRIMARY KEY
role TEXT          -- 'user' or 'assistant'
content TEXT       -- Message text
chat_id INTEGER    -- Telegram chat ID
created_at TEXT    -- Timestamp
```

**agent_state** - Key-value store for state
```sql
key TEXT PRIMARY KEY
value TEXT         -- JSON-encoded value
updated_at TEXT    -- Timestamp
```

### View Database
```bash
sqlite3 ~/.claude/project-manager/agent.db
sqlite> SELECT * FROM messages ORDER BY id DESC LIMIT 10;
sqlite> .quit
```

## Costs

- **Grok 4.1**: ~$0.001-0.01 per message (very cheap)
- **ngrok**: Free tier (new URL each restart)
- **Telegram**: Free

Estimated: **$3-10/month** with moderate usage

## Security Notes

- API keys stored in `~/.zshrc` (not in code)
- Database is local only
- ngrok URL is ephemeral (changes each restart)
- Bot only responds to your chat ID

## Future Improvements

Potential features to add:
- [ ] Task tracking with deadlines
- [ ] Email integration (read/send)
- [ ] Daily summaries via cron
- [ ] Web dashboard
- [ ] Multiple project support
- [ ] File attachments

## Quick Reference

```bash
# Start
~/.claude/project-manager/run.sh

# Check status
curl http://localhost:4000/

# View history
curl http://localhost:4000/history

# Clear history
curl -X POST http://localhost:4000/clear

# Stop
Ctrl+C (in run.sh terminal)
```

---

**Bot**: @Alan0130_bot
**Port**: 4000
**ngrok UI**: http://localhost:4040
