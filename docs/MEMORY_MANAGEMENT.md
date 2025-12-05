# Memory Management

> **Project**: Project Manager Agent (`~/.claude/project-manager/`)
> **Approach**: Unified 60K token context with tool-based retrieval

## Overview

**Single configuration**: 60,000 tokens for active context.

- Messages loaded newest-to-oldest until 60K token limit
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
│                    STORAGE (SQLite)                              │
├─────────────────────────────────────────────────────────────────┤
│  • ALL messages stored permanently                               │
│  • Searchable via SQL LIKE                                       │
│  • Indexed by conversation_id, created_at                        │
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

### Tool Definitions

```python
MEMORY_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_history",
            "description": "Search past messages by keyword",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "default": 5}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_messages_by_date",
            "description": "Get messages from a specific date",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string"},
                    "limit": {"type": "integer", "default": 20}
                },
                "required": ["date"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_extended_context",
            "description": "Load more conversation history",
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "default": 50}
                }
            }
        }
    }
]
```

## Flow

```
User: "What did we decide about pricing last week?"
                    │
                    ▼
┌─────────────────────────────────────────┐
│ 1. Save message to SQLite               │
│ 2. Build context (up to 60K tokens)     │
│ 3. Send to Grok with tool definitions   │
└─────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────┐
│ Grok sees "pricing" not in context      │
│ → calls search_history("pricing")       │
└─────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────┐
│ Tool returns matching messages          │
│ Grok generates response with context    │
└─────────────────────────────────────────┘
                    │
                    ▼
User: "On November 28th, you discussed..."
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/memory/stats?chat_id=X` | GET | Memory statistics |
| `/memory/search?chat_id=X&q=term` | GET | Search history |
| `/history?chat_id=X&limit=N` | GET | Get messages |

## Files

| File | Purpose |
|------|---------|
| `memory_manager.py` | Context building, tools, storage |
| `grok_client.py` | API calls with tool handling |
| `server.py` | Webhook integration |
| `db.py` | SQLite schema and indexes |

## Why 60K?

- **Simple**: One number, no competing configurations
- **Generous**: ~240K characters, hundreds of messages
- **Fast**: No compaction overhead
- **Flexible**: Tools for when you need more
