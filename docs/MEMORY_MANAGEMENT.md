# Memory Management

> **Project**: Project Manager Agent
> **Approach**: Unified 60K token context with tool-based retrieval

## Overview

**Single configuration**: 60,000 tokens for active context.

- Messages stored in simple `messages` table (db.py)
- Load messages newest-to-oldest until 60K token limit
- Older messages accessible via Grok tool calls
- No auto-compaction, no complex thresholds

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                 ACTIVE CONTEXT (sent to Grok)                    │
├─────────────────────────────────────────────────────────────────┤
│  • System prompt (~500 tokens)                                  │
│  • Recent messages up to 60K tokens                             │
│  • Tool results (when Grok requests them)                       │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ Grok calls tools when needed
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    STORAGE (SQLite messages table)               │
├─────────────────────────────────────────────────────────────────┤
│  • ALL messages stored permanently                               │
│  • Searchable via SQL LIKE                                       │
│  • Indexed by chat_id                                            │
│  • Accessible via memory tools                                   │
└─────────────────────────────────────────────────────────────────┘
```

## Configuration

```python
# memory_manager.py - SINGLE SOURCE OF TRUTH
ACTIVE_CONTEXT_TOKENS = 60_000  # 60K tokens
CHARS_PER_TOKEN = 4             # Conservative estimate
```

## Memory Tools

Grok can call these tools to access history beyond the active context:

| Tool | Description | When to Use |
|------|-------------|-------------|
| `search_history(query)` | Search messages by keyword | "What did we decide about X?" |
| `get_messages_by_date(date)` | Get messages from a date | "What did we discuss yesterday?" |
| `get_extended_context(count)` | Load more messages | Complex questions needing history |

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/memory/stats?chat_id=X` | GET | Memory statistics |
| `/memory/search?chat_id=X&q=term` | GET | Search history |
| `/history?chat_id=X&limit=N` | GET | Get messages |

## Files

| File | Purpose |
|------|---------|
| `memory_manager.py` | Context building, tools (uses db.messages) |
| `grok_client.py` | API calls with tool handling |
| `server.py` | Webhook integration |
| `db.py` | SQLite schema (messages table) |

## Why 60K?

- **Simple**: One number, no competing configurations
- **Generous**: ~240K characters, hundreds of messages
- **Fast**: No compaction overhead
- **Flexible**: Tools for when you need more
