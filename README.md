# Project Manager Agent

A Telegram bot powered by Grok 4.1 that acts as your personal project management assistant. Chat with it from your phone anytime - it remembers context and helps you manage tasks.

## Quick Start

```bash
# Start the agent
./run.sh
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
Project_manager/
├── server.py          # Flask server - webhook handler, API endpoints
├── grok_client.py     # Grok 4.1 API client with tool support
├── telegram_client.py # Telegram Bot API client
├── memory_manager.py  # 60K token context + history tools
├── db.py              # SQLite persistence layer
├── task_manager.py    # Task/project CRUD
├── toc_engine.py      # Theory of Constraints logic
├── config.py          # Notification settings
├── agent.db           # SQLite database (auto-created)
├── run.sh             # One-command startup script
├── docs/              # Documentation
│   ├── MEMORY_MANAGEMENT.md
│   ├── NOTIFICATION_SYSTEM.md
│   └── PROJECT_ROADMAP.md
└── README.md          # This file
```

## How It Works

1. **You send a message** to `@Alan0130_bot` on Telegram
2. **Telegram pushes** the message to ngrok webhook URL
3. **ngrok forwards** to your local Flask server on port 4000
4. **Flask saves** your message to SQLite
5. **Flask builds context** (up to 60K tokens) from message history
6. **Flask calls Grok 4.1** with context and memory tools
7. **Grok responds** (and can call tools to search history if needed)
8. **Flask sends** the response back to Telegram

## Memory System

- **60K token context**: Recent messages loaded automatically
- **Tool-based retrieval**: Grok can search older messages when needed
- **Simple storage**: All messages in SQLite `messages` table

See `docs/MEMORY_MANAGEMENT.md` for details.

## Starting the Agent

### Option 1: Run Script (Recommended)
```bash
./run.sh
```

This automatically:
- Starts Flask server on port 4000
- Starts ngrok tunnel
- Sets Telegram webhook
- Shows status when ready

### Option 2: Manual Start
```bash
# Terminal 1: Start Flask server
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
| `/memory/stats` | GET | Memory statistics |
| `/memory/search` | GET | Search message history |
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

# Search history
curl "http://localhost:4000/memory/search?q=pricing"

# Memory stats
curl http://localhost:4000/memory/stats

# Clear history
curl -X POST http://localhost:4000/clear
```

## Environment Variables

All stored in `~/.zshrc`:

| Variable | Description |
|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Your chat ID for notifications |
| `XAI_API_KEY` | Grok API key (original) |
| `XAI_API_KEY_FAST` | Grok 4.1 fast key (used by agent) |
| `NGROK_AUTHTOKEN` | ngrok authentication token |

## Database

SQLite database at `./agent.db`

### Tables

**messages** - Conversation history
```sql
id INTEGER PRIMARY KEY
role TEXT          -- 'user' or 'assistant'
content TEXT       -- Message text
chat_id INTEGER    -- Telegram chat ID
created_at TEXT    -- Timestamp
```

### View Database
```bash
sqlite3 agent.db
sqlite> SELECT * FROM messages ORDER BY id DESC LIMIT 10;
sqlite> .quit
```

## Costs

- **Grok 4.1**: ~$0.001-0.01 per message (very cheap)
- **ngrok**: Free tier (new URL each restart)
- **Telegram**: Free

Estimated: **$3-10/month** with moderate usage

## Documentation

- `docs/PROJECT_ROADMAP.md` - Full implementation plan
- `docs/MEMORY_MANAGEMENT.md` - Memory system details
- `docs/NOTIFICATION_SYSTEM.md` - Notification system spec

## Quick Reference

```bash
# Start
./run.sh

# Check status
curl http://localhost:4000/

# View history
curl http://localhost:4000/history

# Stop
Ctrl+C (in run.sh terminal)
```

---

**Bot**: @Alan0130_bot
**Port**: 4000
**ngrok UI**: http://localhost:4040
