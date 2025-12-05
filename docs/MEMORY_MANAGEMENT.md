# Memory Management Implementation Spec

> **Location**: `~/.claude/plans/memory-management-spec.md`
> **Project**: Project Manager Agent (`~/.claude/project-manager/`)
> **Phase**: 2.5 of implementation plan
> **Status**: Ready for implementation
> **Last Updated**: 2025-12-05

## Quick Reference

| Key | Value |
|-----|-------|
| Context per project | **55K tokens** (full recall) |
| Storage | SQLite `conversation_messages` table (project-linked) |
| Compaction | Auto-summarize when >55K tokens |
| History access | Tool-based for compacted data |
| Files to create | `memory_manager.py` |
| Files to update | `grok_client.py`, `server.py`, `db.py` |

---

## Overview

**Project-scoped unlimited memory** with 55K token active context + auto-compaction.

**KEY PRINCIPLES**:
1. **Per-project memory** - each project has its own conversation history
2. **Full recall (55K)** - active project loads ALL messages up to 55K tokens
3. **Auto-compaction** - when >55K, oldest messages get summarized
4. **Dashboard overview** - all projects show status/schedule, active project gets full context
5. **Tool access** - search_history() works across compacted and active data

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                     CONTEXT (sent to Grok API)                       │
│                     LEAN BY DEFAULT                                  │
├─────────────────────────────────────────────────────────────────────┤
│  • System prompt (~2K tokens)                                       │
│  • Current task context (~2K tokens) - optional                     │
│  • Recent messages ONLY (last 10-15) (~20K tokens)                  │
│  • Tool results (when Grok requests them)                           │
│                                                                     │
│  TOTAL: ~25-30K tokens typical                                      │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              │ Grok calls tool when needed
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     STORAGE (SQLite)                                 │
├─────────────────────────────────────────────────────────────────────┤
│  • ALL messages stored permanently (never deleted)                   │
│  • Full text searchable via SQL LIKE                                │
│  • Indexed by date, chat_id                                         │
│  • Accessible via search_history() TOOL                             │
│  • Future: embeddings for semantic search                           │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Token Budgets

### Default Context (LEAN)
| Component | Tokens | Notes |
|-----------|--------|-------|
| System prompt | 2,000 | Includes tool descriptions |
| Task context | 2,000 | Current task + blockers (optional) |
| Recent messages | 20,000 | Last 10-15 messages |
| **TOTAL** | **~25,000** | Lean, fast, cheap |

### With Tool Retrieval (on-demand)
| Component | Tokens | When |
|-----------|--------|------|
| Search results | 5,000-20,000 | Grok calls `search_history` |
| Extended context | 10,000-50,000 | Grok calls `get_extended_context` |

### Cost Comparison
| Approach | Tokens | Cost/call |
|----------|--------|-----------|
| Lean (default) | ~25K | ~$0.005 |
| With tool use | ~50K | ~$0.01 |
| Pre-loaded history (OLD) | ~600K | ~$0.12 |

---

## Files to Create/Modify

### 1. `memory_manager.py` (NEW)

```python
#!/usr/bin/env python3
"""
Memory Manager for Project Manager Agent.
Handles storage and tool-based retrieval of conversation history.
"""

from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
import logging
import db

logger = logging.getLogger(__name__)

# Configuration
DEFAULT_CONTEXT_MESSAGES = 12  # Messages to include by default
MAX_EXTENDED_CONTEXT = 50      # Max messages for extended context

def estimate_tokens(text: str) -> int:
    """Conservative token estimation (4 chars per token)."""
    if not text:
        return 0
    return len(text) // 4

def get_context_token_count(messages: List[dict]) -> int:
    """Calculate total tokens for a list of messages."""
    total = 0
    for msg in messages:
        total += estimate_tokens(msg.get('content', ''))
        total += 10  # Overhead for role, timestamps, etc.
    return total

def build_lean_context(chat_id: str, limit: int = None) -> List[dict]:
    """
    Build LEAN context for Grok API call.
    Only includes last N messages - nothing more.

    Args:
        chat_id: Conversation identifier
        limit: Number of messages (default: DEFAULT_CONTEXT_MESSAGES)

    Returns:
        List of message dicts ready for Grok API
    """
    if limit is None:
        limit = DEFAULT_CONTEXT_MESSAGES

    messages = db.get_recent_messages(limit=limit, chat_id=chat_id)

    token_count = get_context_token_count(messages)
    logger.info(f"Context built: {len(messages)} messages, ~{token_count} tokens")

    return messages


# ============================================
# TOOLS FOR GROK TO CALL
# ============================================

def search_history(query: str, chat_id: str, limit: int = 5) -> List[dict]:
    """
    TOOL: Search through ALL past messages by keyword/phrase.

    Grok calls this when user asks about something from the past.
    Example: "What did we decide about the pricing last week?"

    Args:
        query: Search term or phrase
        chat_id: Conversation to search
        limit: Max results to return

    Returns:
        List of matching messages with timestamps
    """
    conn = db.get_connection()
    cursor = conn.cursor()

    # Simple LIKE search - fast enough for thousands of messages
    cursor.execute("""
        SELECT id, role, content, created_at
        FROM messages
        WHERE chat_id = ? AND content LIKE ?
        ORDER BY created_at DESC
        LIMIT ?
    """, (chat_id, f"%{query}%", limit))

    results = [dict(row) for row in cursor.fetchall()]
    conn.close()

    logger.info(f"search_history('{query}'): found {len(results)} matches")
    return results


def get_messages_by_date(chat_id: str, date: str, limit: int = 20) -> List[dict]:
    """
    TOOL: Retrieve messages from a specific date.

    Grok calls this when user references a date.
    Example: "What did we discuss on Monday?"

    Args:
        chat_id: Conversation identifier
        date: Date string (YYYY-MM-DD or relative like "yesterday", "monday")
        limit: Max messages to return

    Returns:
        List of messages from that date
    """
    # Parse relative dates
    target_date = _parse_date(date)

    conn = db.get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, role, content, created_at
        FROM messages
        WHERE chat_id = ?
          AND date(created_at) = date(?)
        ORDER BY created_at ASC
        LIMIT ?
    """, (chat_id, target_date.isoformat(), limit))

    results = [dict(row) for row in cursor.fetchall()]
    conn.close()

    logger.info(f"get_messages_by_date('{date}'): found {len(results)} messages")
    return results


def get_extended_context(chat_id: str, count: int = 30) -> List[dict]:
    """
    TOOL: Get more messages than the default window.

    Grok calls this when it needs more context for a complex question.

    Args:
        chat_id: Conversation identifier
        count: Number of messages (capped at MAX_EXTENDED_CONTEXT)

    Returns:
        List of recent messages
    """
    count = min(count, MAX_EXTENDED_CONTEXT)
    messages = db.get_recent_messages(limit=count, chat_id=chat_id)

    logger.info(f"get_extended_context({count}): returning {len(messages)} messages")
    return messages


def get_message_by_id(message_id: str) -> Optional[dict]:
    """
    TOOL: Get a specific message by ID.

    Grok calls this to retrieve full text of a referenced message.

    Args:
        message_id: Message ID

    Returns:
        Message dict or None
    """
    conn = db.get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, role, content, chat_id, created_at
        FROM messages
        WHERE id = ?
    """, (message_id,))

    row = cursor.fetchone()
    conn.close()

    return dict(row) if row else None


# ============================================
# HELPER FUNCTIONS
# ============================================

def _parse_date(date_str: str) -> datetime:
    """Parse date string including relative dates."""
    date_str = date_str.lower().strip()
    today = datetime.now()

    # Relative dates
    if date_str == 'today':
        return today
    elif date_str == 'yesterday':
        return today - timedelta(days=1)
    elif date_str in ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']:
        days = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
        target_day = days.index(date_str)
        current_day = today.weekday()
        days_ago = (current_day - target_day) % 7
        if days_ago == 0:
            days_ago = 7  # Last week's same day
        return today - timedelta(days=days_ago)

    # Try ISO format
    try:
        return datetime.fromisoformat(date_str)
    except ValueError:
        pass

    # Default to today
    return today


def get_memory_stats(chat_id: str) -> Dict[str, Any]:
    """
    Get memory usage statistics for debugging/monitoring.

    Returns:
        {
            "total_messages": int,
            "oldest_message": datetime,
            "newest_message": datetime,
            "estimated_total_tokens": int
        }
    """
    conn = db.get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            COUNT(*) as total,
            MIN(created_at) as oldest,
            MAX(created_at) as newest,
            SUM(LENGTH(content)) as total_chars
        FROM messages
        WHERE chat_id = ?
    """, (chat_id,))

    row = cursor.fetchone()
    conn.close()

    return {
        "total_messages": row["total"] or 0,
        "oldest_message": row["oldest"],
        "newest_message": row["newest"],
        "estimated_total_tokens": (row["total_chars"] or 0) // 4
    }


# ============================================
# TOOL DEFINITIONS FOR GROK
# ============================================

MEMORY_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_history",
            "description": "Search through past conversation messages by keyword or phrase. Use when user asks about something discussed previously.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search term or phrase to find in messages"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results to return (default 5)",
                        "default": 5
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_messages_by_date",
            "description": "Get messages from a specific date. Use when user references a date like 'yesterday', 'Monday', or '2024-01-15'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "Date to retrieve (YYYY-MM-DD or 'yesterday', 'monday', etc.)"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max messages to return (default 20)",
                        "default": 20
                    }
                },
                "required": ["date"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_extended_context",
            "description": "Load more conversation history than the default window. Use when you need more context to answer a complex question.",
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "Number of recent messages to load (default 30, max 50)",
                        "default": 30
                    }
                }
            }
        }
    }
]


# Test
if __name__ == "__main__":
    print("Testing memory_manager...")

    # Test token estimation
    assert estimate_tokens("hello world") == 2  # 11 chars / 4 = 2
    print("  Token estimation: OK")

    # Test date parsing
    assert _parse_date("today").date() == datetime.now().date()
    assert _parse_date("yesterday").date() == (datetime.now() - timedelta(days=1)).date()
    print("  Date parsing: OK")

    # Test with real DB if available
    try:
        stats = get_memory_stats("8362761468")
        print(f"  Memory stats: {stats['total_messages']} messages")
    except Exception as e:
        print(f"  Memory stats: skipped ({e})")

    print("All tests passed!")
```

### 2. `grok_client.py` (UPDATE)

Add tool definitions to the API calls:

```python
# Add import
import memory_manager

# Update SYSTEM_PROMPT to include tool instructions
SYSTEM_PROMPT = """You are a helpful project management assistant.

CONTEXT: You see the last 10-15 messages by default.

HISTORY TOOLS: To access older conversations:
- search_history(query) - Find past messages by keyword
- get_messages_by_date(date) - Get messages from a specific date
- get_extended_context(count) - Load more recent messages

When a user asks about something not in your visible context, USE THE TOOLS.
Don't guess or make up past conversations.
"""

# Update chat() function to include tools
def chat(messages: List[dict], tools: List[dict] = None) -> str:
    """Send chat request to Grok with optional tools."""

    if tools is None:
        tools = memory_manager.MEMORY_TOOLS

    payload = {
        "model": MODEL,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + messages,
        "tools": tools
    }

    response = requests.post(API_URL, headers=HEADERS, json=payload)
    result = response.json()

    # Handle tool calls
    if result.get("choices", [{}])[0].get("message", {}).get("tool_calls"):
        return handle_tool_calls(result, messages)

    return result["choices"][0]["message"]["content"]


def handle_tool_calls(result: dict, messages: List[dict]) -> str:
    """Execute tool calls and continue conversation."""
    tool_calls = result["choices"][0]["message"]["tool_calls"]

    # Add assistant message with tool calls
    messages.append(result["choices"][0]["message"])

    # Execute each tool
    for tool_call in tool_calls:
        func_name = tool_call["function"]["name"]
        args = json.loads(tool_call["function"]["arguments"])

        # Call the appropriate memory_manager function
        if func_name == "search_history":
            result = memory_manager.search_history(
                args["query"],
                chat_id=args.get("chat_id", "default"),
                limit=args.get("limit", 5)
            )
        elif func_name == "get_messages_by_date":
            result = memory_manager.get_messages_by_date(
                chat_id=args.get("chat_id", "default"),
                date=args["date"],
                limit=args.get("limit", 20)
            )
        elif func_name == "get_extended_context":
            result = memory_manager.get_extended_context(
                chat_id=args.get("chat_id", "default"),
                count=args.get("count", 30)
            )
        else:
            result = {"error": f"Unknown tool: {func_name}"}

        # Add tool result
        messages.append({
            "role": "tool",
            "tool_call_id": tool_call["id"],
            "content": json.dumps(result)
        })

    # Continue conversation with tool results
    return chat(messages, tools=[])  # No tools on follow-up
```

### 3. `server.py` (UPDATE)

Use lean context builder:

```python
# Add import
import memory_manager

# Update webhook handler
@app.route("/webhook", methods=["POST"])
def webhook():
    # ... existing parsing code ...

    # Save user message
    db.add_message("user", text, chat_id)

    # Build LEAN context (not all messages!)
    context = memory_manager.build_lean_context(chat_id)

    # Get response from Grok (with tool support)
    response = grok_client.chat(context)

    # Save and send response
    db.add_message("assistant", response, chat_id)
    telegram_client.send_message(response, chat_id)

    return jsonify({"ok": True})
```

### 4. `db.py` (UPDATE)

Add search index and function:

```python
# Add to init_db()
cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_messages_content
    ON messages(content)
""")
cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_messages_chat_date
    ON messages(chat_id, created_at)
""")

# Add search function
def search_messages(chat_id: str, query: str, limit: int = 10) -> List[dict]:
    """Search messages by content."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, role, content, created_at
        FROM messages
        WHERE chat_id = ? AND content LIKE ?
        ORDER BY created_at DESC
        LIMIT ?
    """, (chat_id, f"%{query}%", limit))

    results = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return results
```

---

## Flow Diagram

```
User: "What did we decide about pricing last week?"
                    │
                    ▼
┌─────────────────────────────────────────┐
│ 1. Save message to SQLite               │
│ 2. Build LEAN context (last 12 msgs)    │
│ 3. Send to Grok with tool definitions   │
└─────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────┐
│ Grok sees: "pricing last week" not in   │
│ recent context → calls search_history   │
└─────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────┐
│ memory_manager.search_history("pricing")│
│ → Returns 3 matching messages           │
└─────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────┐
│ Grok receives tool results              │
│ → Generates response with context       │
└─────────────────────────────────────────┘
                    │
                    ▼
User receives: "On November 28th, you discussed..."
```

---

## Testing Checklist

- [ ] `memory_manager.py` created and imports work
- [ ] `estimate_tokens()` returns reasonable values
- [ ] `build_lean_context()` returns last N messages
- [ ] `search_history()` finds matching messages
- [ ] `get_messages_by_date()` handles relative dates
- [ ] `get_extended_context()` respects max limit
- [ ] `grok_client.py` includes tool definitions
- [ ] Grok successfully calls tools when needed
- [ ] `server.py` uses lean context builder
- [ ] Full flow works end-to-end via Telegram

---

## Why This Approach

| Aspect | Auto-Load (OLD) | Tool-Based (NEW) |
|--------|-----------------|------------------|
| Default tokens | ~600K | ~25K |
| Cost/call | ~$0.12 | ~$0.005 |
| Response time | Slower | Faster |
| Complexity | Summarization logic | Simple |
| History access | Pre-loaded (wasteful) | On-demand |
| Scalability | Limited by context | Unlimited |

---

## Related Documentation

| Document | Location | Description |
|----------|----------|-------------|
| Main Project Plan | `~/.claude/plans/misty-sleeping-willow.md` | Full 10-phase implementation plan |
| Project README | `~/.claude/project-manager/README.md` | Setup and usage guide |
| Database Schema | `~/.claude/project-manager/db.py` | SQLite schema and CRUD |
| Grok Client | `~/.claude/project-manager/grok_client.py` | API integration |
| Task Manager | `~/.claude/project-manager/task_manager.py` | TOC task management |
| TOC Engine | `~/.claude/project-manager/toc_engine.py` | WIP limits, buffers |

---

## Implementation Checklist

When implementing this spec, an agent should:

1. [ ] Read this spec completely
2. [ ] Read existing `db.py` to understand current schema
3. [ ] Read existing `grok_client.py` to understand API structure
4. [ ] Create `memory_manager.py` with all functions
5. [ ] Update `db.py` with search index and function
6. [ ] Update `grok_client.py` with tool definitions and handling
7. [ ] Update `server.py` to use `build_lean_context()`
8. [ ] Test with real Telegram messages
9. [ ] Verify tool calls work end-to-end

---

## Search Keywords

For future reference, this document covers:
- memory management, context window, token limits
- conversation history, message storage, SQLite
- tool-based retrieval, search_history, get_messages_by_date
- Grok 4.1, xAI API, function calling
- lean context, compaction, summarization (avoided)
- unlimited messages, infinite memory
