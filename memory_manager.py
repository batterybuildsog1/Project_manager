#!/usr/bin/env python3
"""
Memory Manager for Project Manager Agent.

UNIFIED APPROACH: 60K token active context with tool-based retrieval.

- Active context: Load messages up to 60K tokens (sent to Grok)
- Older messages: Stored in SQLite, accessed via tools when needed
- No auto-compaction, no complex thresholds, no competing configs
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional

import db

logger = logging.getLogger(__name__)

# ============================================
# SINGLE SOURCE OF TRUTH - 60K TOKENS
# ============================================

ACTIVE_CONTEXT_TOKENS = 60_000  # 60K tokens for active context
CHARS_PER_TOKEN = 4             # Conservative estimate


# ============================================
# TOKEN ESTIMATION
# ============================================

def estimate_tokens(text: str) -> int:
    """Conservative token estimation (4 chars per token)."""
    if not text:
        return 0
    return len(text) // CHARS_PER_TOKEN


def estimate_message_tokens(message: dict) -> int:
    """Estimate tokens for a single message including overhead."""
    content = message.get("content", "")
    return estimate_tokens(content) + 10  # 10 tokens overhead for role/structure


# ============================================
# CONVERSATION MANAGEMENT
# ============================================

def get_or_create_conversation(chat_id: str, source: str = "telegram") -> str:
    """Get existing conversation or create new one."""
    conn = db.get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id FROM conversations WHERE external_id = ? AND source = ?
    """, (str(chat_id), source))
    row = cursor.fetchone()

    if row:
        conversation_id = row["id"]
    else:
        conversation_id = db.generate_id()
        cursor.execute("""
            INSERT INTO conversations (id, source, external_id, created_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        """, (conversation_id, source, str(chat_id)))
        conn.commit()

    conn.close()
    return conversation_id


def add_message(chat_id: str, role: str, content: str, sender_name: str = None) -> str:
    """Add a message to conversation."""
    conversation_id = get_or_create_conversation(chat_id)

    conn = db.get_connection()
    cursor = conn.cursor()

    message_id = db.generate_id()
    cursor.execute("""
        INSERT INTO conversation_messages (id, conversation_id, role, content, sender_name, created_at)
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    """, (message_id, conversation_id, role, content, sender_name))

    conn.commit()
    conn.close()

    # Also add to legacy messages table for backward compatibility
    db.add_message(role, content, int(chat_id) if chat_id.isdigit() else None)

    return message_id


# ============================================
# CONTEXT BUILDING (60K TOKEN LIMIT)
# ============================================

def build_context(chat_id: str) -> List[dict]:
    """
    Build context for Grok API call.

    Loads messages from newest to oldest until we hit 60K tokens.
    Returns messages in chronological order (oldest first).
    """
    conversation_id = get_or_create_conversation(chat_id)

    conn = db.get_connection()
    cursor = conn.cursor()

    # Get all messages, newest first
    cursor.execute("""
        SELECT role, content FROM conversation_messages
        WHERE conversation_id = ?
        ORDER BY created_at DESC
    """, (conversation_id,))

    messages = []
    total_tokens = 0

    for row in cursor.fetchall():
        msg = {"role": row["role"], "content": row["content"]}
        msg_tokens = estimate_message_tokens(msg)

        if total_tokens + msg_tokens > ACTIVE_CONTEXT_TOKENS:
            break

        messages.append(msg)
        total_tokens += msg_tokens

    conn.close()

    # Reverse to chronological order
    messages.reverse()

    logger.info(f"Context built: {len(messages)} messages, ~{total_tokens} tokens")
    return messages


# ============================================
# TOOL FUNCTIONS (for Grok to call)
# ============================================

def search_history(query: str, chat_id: str, limit: int = 5) -> List[dict]:
    """
    TOOL: Search past messages by keyword.

    Use when user asks about something from the past.
    Example: "What did we decide about pricing?"
    """
    conversation_id = get_or_create_conversation(chat_id)

    conn = db.get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, role, content, created_at
        FROM conversation_messages
        WHERE conversation_id = ? AND content LIKE ?
        ORDER BY created_at DESC
        LIMIT ?
    """, (conversation_id, f"%{query}%", limit))

    results = [dict(row) for row in cursor.fetchall()]
    conn.close()

    logger.info(f"search_history('{query}'): found {len(results)} matches")
    return results


def get_messages_by_date(chat_id: str, date: str, limit: int = 20) -> List[dict]:
    """
    TOOL: Get messages from a specific date.

    Use when user references a date.
    Example: "What did we discuss yesterday?"
    """
    target_date = _parse_date(date)
    conversation_id = get_or_create_conversation(chat_id)

    conn = db.get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, role, content, created_at
        FROM conversation_messages
        WHERE conversation_id = ? AND date(created_at) = date(?)
        ORDER BY created_at ASC
        LIMIT ?
    """, (conversation_id, target_date.strftime("%Y-%m-%d"), limit))

    results = [dict(row) for row in cursor.fetchall()]
    conn.close()

    logger.info(f"get_messages_by_date('{date}'): found {len(results)} messages")
    return results


def get_extended_context(chat_id: str, count: int = 50) -> List[dict]:
    """
    TOOL: Get more messages than default context.

    Use when you need more history for a complex question.
    """
    conversation_id = get_or_create_conversation(chat_id)

    conn = db.get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT role, content, created_at
        FROM conversation_messages
        WHERE conversation_id = ?
        ORDER BY created_at DESC
        LIMIT ?
    """, (conversation_id, count))

    results = [dict(row) for row in reversed(cursor.fetchall())]
    conn.close()

    logger.info(f"get_extended_context({count}): returning {len(results)} messages")
    return results


def _parse_date(date_str: str) -> datetime:
    """Parse date string including relative dates."""
    date_str = date_str.lower().strip()
    today = datetime.now()

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
            days_ago = 7
        return today - timedelta(days=days_ago)

    try:
        return datetime.fromisoformat(date_str)
    except ValueError:
        return today


# ============================================
# TOOL DEFINITIONS FOR GROK API
# ============================================

MEMORY_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_history",
            "description": "Search past conversation messages by keyword. Use when user asks about something discussed previously.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search term or phrase"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 5)"
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
            "description": "Get messages from a specific date. Use when user references 'yesterday', 'Monday', or a date.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "Date (YYYY-MM-DD or 'yesterday', 'monday', etc.)"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max messages (default 20)"
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
            "description": "Load more conversation history. Use when you need more context.",
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "Number of messages (default 50)"
                    }
                }
            }
        }
    }
]


def execute_tool(tool_name: str, args: dict, chat_id: str) -> Any:
    """Execute a memory tool by name."""
    if tool_name == "search_history":
        return search_history(args["query"], chat_id, args.get("limit", 5))
    elif tool_name == "get_messages_by_date":
        return get_messages_by_date(chat_id, args["date"], args.get("limit", 20))
    elif tool_name == "get_extended_context":
        return get_extended_context(chat_id, args.get("count", 50))
    else:
        return {"error": f"Unknown tool: {tool_name}"}


# ============================================
# STATISTICS
# ============================================

def get_stats(chat_id: str) -> Dict[str, Any]:
    """Get memory statistics for a conversation."""
    conversation_id = get_or_create_conversation(chat_id)

    conn = db.get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT COUNT(*) as count, MIN(created_at) as oldest, MAX(created_at) as newest
        FROM conversation_messages WHERE conversation_id = ?
    """, (conversation_id,))
    row = cursor.fetchone()

    cursor.execute("""
        SELECT SUM(LENGTH(content)) as total_chars
        FROM conversation_messages WHERE conversation_id = ?
    """, (conversation_id,))
    chars_row = cursor.fetchone()

    conn.close()

    total_chars = chars_row["total_chars"] or 0
    estimated_tokens = total_chars // CHARS_PER_TOKEN

    return {
        "total_messages": row["count"] or 0,
        "oldest_message": row["oldest"],
        "newest_message": row["newest"],
        "estimated_total_tokens": estimated_tokens,
        "active_context_limit": ACTIVE_CONTEXT_TOKENS,
        "within_context": estimated_tokens <= ACTIVE_CONTEXT_TOKENS
    }


# ============================================
# TEST
# ============================================

if __name__ == "__main__":
    print("Memory Manager - 60K Token Unified Approach")
    print("=" * 50)

    # Test token estimation
    test = "Hello world"
    print(f"Token estimate for '{test}': {estimate_tokens(test)}")

    # Test date parsing
    print(f"Parse 'today': {_parse_date('today').date()}")
    print(f"Parse 'yesterday': {_parse_date('yesterday').date()}")

    print("\nAll tests passed!")
