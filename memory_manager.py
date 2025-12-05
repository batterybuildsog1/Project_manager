#!/usr/bin/env python3
"""
Memory Manager for Project Manager Agent.

UNIFIED APPROACH: 60K token active context with tool-based retrieval.
Uses the simple `messages` table in db.py - no duplication.
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Any

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
    return estimate_tokens(content) + 10  # 10 tokens overhead


# ============================================
# MESSAGE STORAGE (uses db.messages table)
# ============================================

def add_message(chat_id: str, role: str, content: str) -> int:
    """Add a message. Uses db.add_message directly."""
    chat_id_int = int(chat_id) if chat_id.isdigit() else None
    return db.add_message(role, content, chat_id_int)


# ============================================
# CONTEXT BUILDING (60K TOKEN LIMIT)
# ============================================

def build_context(chat_id: str) -> List[dict]:
    """
    Build context for Grok API call.
    Loads messages until 60K token limit.
    Returns in chronological order.
    """
    chat_id_int = int(chat_id) if chat_id.isdigit() else None

    conn = db.get_connection()
    cursor = conn.cursor()

    if chat_id_int:
        cursor.execute("""
            SELECT role, content FROM messages
            WHERE chat_id = ?
            ORDER BY id DESC
        """, (chat_id_int,))
    else:
        cursor.execute("""
            SELECT role, content FROM messages
            ORDER BY id DESC
        """)

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
    messages.reverse()

    logger.info(f"Context built: {len(messages)} messages, ~{total_tokens} tokens")
    return messages


# ============================================
# TOOL FUNCTIONS (for Grok to call)
# ============================================

def search_history(query: str, chat_id: str, limit: int = 5) -> List[dict]:
    """
    TOOL: Search past messages by keyword.
    """
    chat_id_int = int(chat_id) if chat_id.isdigit() else None

    conn = db.get_connection()
    cursor = conn.cursor()

    if chat_id_int:
        cursor.execute("""
            SELECT id, role, content, created_at
            FROM messages
            WHERE chat_id = ? AND content LIKE ?
            ORDER BY id DESC
            LIMIT ?
        """, (chat_id_int, f"%{query}%", limit))
    else:
        cursor.execute("""
            SELECT id, role, content, created_at
            FROM messages
            WHERE content LIKE ?
            ORDER BY id DESC
            LIMIT ?
        """, (f"%{query}%", limit))

    results = [dict(row) for row in cursor.fetchall()]
    conn.close()

    logger.info(f"search_history('{query}'): found {len(results)} matches")
    return results


def get_messages_by_date(chat_id: str, date: str, limit: int = 20) -> List[dict]:
    """
    TOOL: Get messages from a specific date.
    """
    target_date = _parse_date(date)
    chat_id_int = int(chat_id) if chat_id.isdigit() else None

    conn = db.get_connection()
    cursor = conn.cursor()

    if chat_id_int:
        cursor.execute("""
            SELECT id, role, content, created_at
            FROM messages
            WHERE chat_id = ? AND date(created_at) = date(?)
            ORDER BY id ASC
            LIMIT ?
        """, (chat_id_int, target_date.strftime("%Y-%m-%d"), limit))
    else:
        cursor.execute("""
            SELECT id, role, content, created_at
            FROM messages
            WHERE date(created_at) = date(?)
            ORDER BY id ASC
            LIMIT ?
        """, (target_date.strftime("%Y-%m-%d"), limit))

    results = [dict(row) for row in cursor.fetchall()]
    conn.close()

    logger.info(f"get_messages_by_date('{date}'): found {len(results)} messages")
    return results


def get_extended_context(chat_id: str, count: int = 50) -> List[dict]:
    """
    TOOL: Get more messages than default context.
    """
    chat_id_int = int(chat_id) if chat_id.isdigit() else None
    messages = db.get_recent_messages(limit=count, chat_id=chat_id_int)

    logger.info(f"get_extended_context({count}): returning {len(messages)} messages")
    return messages


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
            "description": "Search past conversation messages by keyword.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search term"},
                    "limit": {"type": "integer", "description": "Max results (default 5)"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_messages_by_date",
            "description": "Get messages from a specific date.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Date (YYYY-MM-DD or 'yesterday', 'monday', etc.)"},
                    "limit": {"type": "integer", "description": "Max messages (default 20)"}
                },
                "required": ["date"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_extended_context",
            "description": "Load more conversation history.",
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "description": "Number of messages (default 50)"}
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
    """Get memory statistics."""
    chat_id_int = int(chat_id) if chat_id.isdigit() else None

    conn = db.get_connection()
    cursor = conn.cursor()

    if chat_id_int:
        cursor.execute("""
            SELECT COUNT(*) as count, MIN(created_at) as oldest, MAX(created_at) as newest
            FROM messages WHERE chat_id = ?
        """, (chat_id_int,))
    else:
        cursor.execute("""
            SELECT COUNT(*) as count, MIN(created_at) as oldest, MAX(created_at) as newest
            FROM messages
        """)

    row = cursor.fetchone()

    if chat_id_int:
        cursor.execute("SELECT SUM(LENGTH(content)) as total FROM messages WHERE chat_id = ?", (chat_id_int,))
    else:
        cursor.execute("SELECT SUM(LENGTH(content)) as total FROM messages")

    chars_row = cursor.fetchone()
    conn.close()

    total_chars = chars_row["total"] or 0
    estimated_tokens = total_chars // CHARS_PER_TOKEN

    return {
        "total_messages": row["count"] or 0,
        "oldest_message": row["oldest"],
        "newest_message": row["newest"],
        "estimated_total_tokens": estimated_tokens,
        "active_context_limit": ACTIVE_CONTEXT_TOKENS,
        "within_context": estimated_tokens <= ACTIVE_CONTEXT_TOKENS
    }


if __name__ == "__main__":
    print("Memory Manager - 60K Token Unified Approach")
    print(f"Using simple 'messages' table from db.py")
    print(f"ACTIVE_CONTEXT_TOKENS: {ACTIVE_CONTEXT_TOKENS:,}")
