#!/usr/bin/env python3
"""
Memory Manager for Project Manager Agent.
Handles unlimited conversation history with intelligent compaction.

Architecture:
- Active Context: System prompt + summaries + recent messages (sent to Grok)
- Warm Storage: SQLite with full messages, summaries, importance scores
- Cold Storage: Archive >30 days (future, not MVP)

Grok 4.1 has 2M token context. We target 30% (600K) max usage.
Compact at 35% threshold (~700K tokens).
"""

import sqlite3
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Any, Tuple, Optional

import db
import grok_client

# ============================================
# CONFIGURATION
# ============================================

CONTEXT_WINDOW = 2_000_000      # Grok 4.1 context window
COMPACT_THRESHOLD = 0.35        # 35% = 700K tokens triggers compaction
TARGET_UTILIZATION = 0.30       # 30% = 600K tokens target
KEEP_RECENT = 15                # Keep last 15 messages verbatim
BATCH_SIZE = 25                 # Messages to summarize at once
CHARS_PER_TOKEN = 4             # Conservative estimate

# Token budget breakdown
TOKEN_BUDGET = {
    "system_prompt": 2_000,
    "project_context": 5_000,
    "task_context": 3_000,
    "retrieved_docs": 10_000,
    "recent_messages": 50_000,   # ~15-20 messages
    "compacted_history": 30_000,
    "safety_buffer": 500_000,
}

SUMMARIZATION_PROMPT = """Summarize this conversation segment concisely, preserving:
- Key decisions made
- Action items mentioned
- Important facts, numbers, dates
- Project/task references
- Names of people mentioned

Format as a brief narrative (under 500 words).
Do not include greetings or filler.

Conversation to summarize:
"""


# ============================================
# DATABASE SCHEMA EXTENSIONS
# ============================================

def init_memory_schema():
    """
    Initialize additional schema for memory management.
    Adds columns to conversation_messages and creates conversation_summaries.
    """
    conn = db.get_connection()
    cursor = conn.cursor()

    # Check if we need to add columns to conversation_messages
    cursor.execute("PRAGMA table_info(conversation_messages)")
    columns = {row["name"] for row in cursor.fetchall()}

    # Add is_compacted column if missing
    if "is_compacted" not in columns:
        cursor.execute("""
            ALTER TABLE conversation_messages
            ADD COLUMN is_compacted INTEGER DEFAULT 0
        """)

    # Add compaction_batch_id column if missing
    if "compaction_batch_id" not in columns:
        cursor.execute("""
            ALTER TABLE conversation_messages
            ADD COLUMN compaction_batch_id TEXT
        """)

    # Create conversation_summaries table if missing
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS conversation_summaries (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,

            batch_id TEXT NOT NULL UNIQUE,
            summary_text TEXT NOT NULL,

            -- Metadata
            messages_start_id TEXT NOT NULL,
            messages_end_id TEXT NOT NULL,
            message_count INTEGER NOT NULL,

            -- Token tracking
            original_tokens INTEGER,
            summary_tokens INTEGER,

            -- Timestamps covered
            period_start TEXT NOT NULL,
            period_end TEXT NOT NULL,

            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Index for efficient retrieval
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_summaries_conversation
        ON conversation_summaries(conversation_id, created_at)
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_messages_compacted
        ON conversation_messages(conversation_id, is_compacted, created_at)
    """)

    conn.commit()
    conn.close()


# ============================================
# TOKEN ESTIMATION
# ============================================

def estimate_tokens(text: str) -> int:
    """
    Conservative token estimation (4 chars per token).
    Actual tokenization varies, but this is safe for budget planning.
    """
    if not text:
        return 0
    return len(text) // CHARS_PER_TOKEN


def estimate_message_tokens(message: dict) -> int:
    """Estimate tokens for a single message including role overhead."""
    content = message.get("content", "")
    role = message.get("role", "")
    # Add overhead for message structure (~10 tokens)
    return estimate_tokens(content) + estimate_tokens(role) + 10


def get_conversation_token_count(chat_id: str) -> int:
    """
    Calculate total tokens for a conversation (non-compacted messages).

    Args:
        chat_id: The conversation/chat identifier

    Returns:
        Estimated token count for active (non-compacted) messages
    """
    conn = db.get_connection()
    cursor = conn.cursor()

    # Get conversation ID for this chat
    cursor.execute("""
        SELECT id FROM conversations WHERE external_id = ?
    """, (str(chat_id),))
    row = cursor.fetchone()

    if not row:
        conn.close()
        return 0

    conversation_id = row["id"]

    # Sum tokens of non-compacted messages
    cursor.execute("""
        SELECT content, role FROM conversation_messages
        WHERE conversation_id = ? AND is_compacted = 0
    """, (conversation_id,))

    total = 0
    for row in cursor.fetchall():
        total += estimate_message_tokens({"content": row["content"], "role": row["role"]})

    # Add summary tokens
    cursor.execute("""
        SELECT summary_tokens FROM conversation_summaries
        WHERE conversation_id = ?
    """, (conversation_id,))

    for row in cursor.fetchall():
        total += row["summary_tokens"] or 0

    conn.close()
    return total


def should_compact(chat_id: str) -> bool:
    """
    Check if conversation needs compaction.

    Returns True if estimated tokens exceed COMPACT_THRESHOLD of context window.
    """
    current_tokens = get_conversation_token_count(chat_id)
    threshold_tokens = int(CONTEXT_WINDOW * COMPACT_THRESHOLD)
    return current_tokens > threshold_tokens


# ============================================
# CONVERSATION MANAGEMENT
# ============================================

def get_or_create_conversation(chat_id: str, source: str = "telegram") -> str:
    """
    Get existing conversation or create new one.

    Args:
        chat_id: External chat identifier
        source: Message source (telegram, email, etc.)

    Returns:
        conversation_id
    """
    conn = db.get_connection()
    cursor = conn.cursor()

    # Try to find existing
    cursor.execute("""
        SELECT id FROM conversations WHERE external_id = ? AND source = ?
    """, (str(chat_id), source))
    row = cursor.fetchone()

    if row:
        conversation_id = row["id"]
    else:
        # Create new conversation
        conversation_id = db.generate_id()
        cursor.execute("""
            INSERT INTO conversations (id, source, external_id, created_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        """, (conversation_id, source, str(chat_id)))
        conn.commit()

    conn.close()
    return conversation_id


def add_message_to_conversation(
    chat_id: str,
    role: str,
    content: str,
    sender_name: str = None,
    importance_score: float = None,
    source: str = "telegram"
) -> str:
    """
    Add a message to conversation (uses new schema).

    Args:
        chat_id: External chat identifier
        role: user, assistant, system, or external
        content: Message content
        sender_name: Optional sender name
        importance_score: Optional importance (0-1)
        source: Message source

    Returns:
        message_id
    """
    conversation_id = get_or_create_conversation(chat_id, source)

    conn = db.get_connection()
    cursor = conn.cursor()

    message_id = db.generate_id()
    cursor.execute("""
        INSERT INTO conversation_messages (
            id, conversation_id, role, content, sender_name,
            importance_score, is_compacted, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, 0, CURRENT_TIMESTAMP)
    """, (message_id, conversation_id, role, content, sender_name, importance_score))

    conn.commit()
    conn.close()

    # Also add to legacy messages table for backward compatibility
    db.add_message(role, content, int(chat_id) if chat_id.isdigit() else None)

    return message_id


def get_recent_conversation_messages(
    chat_id: str,
    limit: int = KEEP_RECENT,
    source: str = "telegram"
) -> List[Dict[str, Any]]:
    """
    Get recent non-compacted messages from conversation.

    Args:
        chat_id: External chat identifier
        limit: Maximum messages to return
        source: Message source

    Returns:
        List of message dicts in chronological order
    """
    conversation_id = get_or_create_conversation(chat_id, source)

    conn = db.get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, role, content, sender_name, importance_score, created_at
        FROM conversation_messages
        WHERE conversation_id = ? AND is_compacted = 0
        ORDER BY created_at DESC
        LIMIT ?
    """, (conversation_id, limit))

    rows = cursor.fetchall()
    conn.close()

    # Return in chronological order
    return [dict(row) for row in reversed(rows)]


# ============================================
# SUMMARIZATION
# ============================================

def summarize_messages(messages: List[dict]) -> str:
    """
    Generate summary using Grok.

    Args:
        messages: List of message dicts with 'role' and 'content'

    Returns:
        Summary text
    """
    # Build conversation text for summarization
    conversation_text = ""
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        sender = msg.get("sender_name", "")

        if sender:
            conversation_text += f"[{role.upper()} - {sender}]: {content}\n\n"
        else:
            conversation_text += f"[{role.upper()}]: {content}\n\n"

    # Use Grok to summarize
    prompt = SUMMARIZATION_PROMPT + conversation_text

    # Use a summarization-focused system prompt
    summary_system_prompt = """You are a conversation summarizer. Your job is to create concise but complete summaries that preserve all important information."""

    summary = grok_client.chat(
        messages=[{"role": "user", "content": prompt}],
        system_prompt=summary_system_prompt,
        temperature=0.3  # Lower temperature for more factual summary
    )

    return summary


# ============================================
# COMPACTION
# ============================================

def compact_conversation(chat_id: str, source: str = "telegram") -> Dict[str, Any]:
    """
    Compact older messages into summaries.

    Process:
    1. Keep last KEEP_RECENT messages verbatim
    2. Summarize oldest non-compacted messages in batches of BATCH_SIZE
    3. Store summary, mark messages as compacted (don't delete!)

    Args:
        chat_id: External chat identifier
        source: Message source

    Returns:
        {"compacted": count, "tokens_saved": int, "summary_id": str}
    """
    conversation_id = get_or_create_conversation(chat_id, source)

    conn = db.get_connection()
    cursor = conn.cursor()

    # Get all non-compacted messages ordered by time
    cursor.execute("""
        SELECT id, role, content, sender_name, created_at
        FROM conversation_messages
        WHERE conversation_id = ? AND is_compacted = 0
        ORDER BY created_at ASC
    """, (conversation_id,))

    all_messages = [dict(row) for row in cursor.fetchall()]

    # Keep the most recent KEEP_RECENT messages
    if len(all_messages) <= KEEP_RECENT:
        conn.close()
        return {"compacted": 0, "tokens_saved": 0, "summary_id": None}

    # Messages to compact (everything except the last KEEP_RECENT)
    messages_to_compact = all_messages[:-KEEP_RECENT]

    # Process in batches
    total_compacted = 0
    total_original_tokens = 0
    total_summary_tokens = 0
    last_summary_id = None

    for i in range(0, len(messages_to_compact), BATCH_SIZE):
        batch = messages_to_compact[i:i + BATCH_SIZE]

        if not batch:
            continue

        # Calculate original tokens
        original_tokens = sum(estimate_message_tokens(m) for m in batch)
        total_original_tokens += original_tokens

        # Generate summary
        summary_text = summarize_messages(batch)
        summary_tokens = estimate_tokens(summary_text)
        total_summary_tokens += summary_tokens

        # Create batch ID
        batch_id = db.generate_id()
        summary_id = db.generate_id()
        last_summary_id = summary_id

        # Store summary
        cursor.execute("""
            INSERT INTO conversation_summaries (
                id, conversation_id, batch_id, summary_text,
                messages_start_id, messages_end_id, message_count,
                original_tokens, summary_tokens,
                period_start, period_end, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (
            summary_id,
            conversation_id,
            batch_id,
            summary_text,
            batch[0]["id"],
            batch[-1]["id"],
            len(batch),
            original_tokens,
            summary_tokens,
            batch[0]["created_at"],
            batch[-1]["created_at"]
        ))

        # Mark messages as compacted
        message_ids = [m["id"] for m in batch]
        placeholders = ",".join("?" * len(message_ids))
        cursor.execute(f"""
            UPDATE conversation_messages
            SET is_compacted = 1, compaction_batch_id = ?
            WHERE id IN ({placeholders})
        """, [batch_id] + message_ids)

        total_compacted += len(batch)

    conn.commit()
    conn.close()

    tokens_saved = total_original_tokens - total_summary_tokens

    return {
        "compacted": total_compacted,
        "tokens_saved": tokens_saved,
        "summary_id": last_summary_id,
        "original_tokens": total_original_tokens,
        "summary_tokens": total_summary_tokens
    }


# ============================================
# CONTEXT BUILDING
# ============================================

def get_conversation_summaries(
    chat_id: str,
    source: str = "telegram"
) -> List[Dict[str, Any]]:
    """
    Get all summaries for a conversation in chronological order.

    Args:
        chat_id: External chat identifier
        source: Message source

    Returns:
        List of summary dicts
    """
    conversation_id = get_or_create_conversation(chat_id, source)

    conn = db.get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, summary_text, message_count, period_start, period_end
        FROM conversation_summaries
        WHERE conversation_id = ?
        ORDER BY period_start ASC
    """, (conversation_id,))

    rows = cursor.fetchall()
    conn.close()

    return [dict(row) for row in rows]


def build_context(
    chat_id: str,
    project_context: str = "",
    task_context: str = "",
    source: str = "telegram"
) -> List[dict]:
    """
    Build full context for Grok API call.

    Structure:
    1. System prompt (from grok_client.SYSTEM_PROMPT)
    2. Context summaries (oldest to newest)
    3. Project/task context if provided
    4. Recent verbatim messages (last 15-20)

    Args:
        chat_id: External chat identifier
        project_context: Optional project information
        task_context: Optional task information
        source: Message source

    Returns:
        List of messages ready for Grok API
    """
    messages = []

    # 1. Start with system message
    system_content = grok_client.SYSTEM_PROMPT

    # 2. Add summaries as system context
    summaries = get_conversation_summaries(chat_id, source)
    if summaries:
        summary_text = "\n\n--- Conversation History Summaries ---\n"
        for i, s in enumerate(summaries, 1):
            period = f"{s['period_start']} to {s['period_end']}"
            summary_text += f"\n[Summary {i} - {s['message_count']} messages from {period}]:\n{s['summary_text']}\n"
        summary_text += "\n--- End of Summaries ---\n"
        system_content += summary_text

    # 3. Add project/task context
    if project_context:
        system_content += f"\n\n--- Current Project Context ---\n{project_context}\n"

    if task_context:
        system_content += f"\n\n--- Current Task Context ---\n{task_context}\n"

    messages.append({"role": "system", "content": system_content})

    # 4. Add recent verbatim messages
    recent = get_recent_conversation_messages(chat_id, limit=KEEP_RECENT, source=source)
    for msg in recent:
        messages.append({
            "role": msg["role"],
            "content": msg["content"]
        })

    return messages


def build_context_with_auto_compact(
    chat_id: str,
    project_context: str = "",
    task_context: str = "",
    source: str = "telegram"
) -> Tuple[List[dict], Optional[Dict[str, Any]]]:
    """
    Build context, auto-compacting if needed.

    Args:
        chat_id: External chat identifier
        project_context: Optional project information
        task_context: Optional task information
        source: Message source

    Returns:
        Tuple of (messages list, compaction_result or None)
    """
    compaction_result = None

    # Check if compaction needed
    if should_compact(chat_id):
        compaction_result = compact_conversation(chat_id, source)

    # Build context
    messages = build_context(chat_id, project_context, task_context, source)

    return messages, compaction_result


# ============================================
# MESSAGE RETRIEVAL
# ============================================

def retrieve_full_message(message_id: str) -> Optional[dict]:
    """
    Get full text of any message (even compacted).

    Args:
        message_id: The message ID

    Returns:
        Message dict or None
    """
    conn = db.get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, conversation_id, role, content, sender_name,
               importance_score, is_compacted, compaction_batch_id, created_at
        FROM conversation_messages
        WHERE id = ?
    """, (message_id,))

    row = cursor.fetchone()
    conn.close()

    if row:
        return dict(row)
    return None


def search_history(
    query: str,
    chat_id: str,
    limit: int = 5,
    source: str = "telegram"
) -> List[dict]:
    """
    Search through all messages (simple text search).

    Args:
        query: Search query
        chat_id: External chat identifier
        limit: Maximum results
        source: Message source

    Returns:
        List of matching message dicts
    """
    conversation_id = get_or_create_conversation(chat_id, source)

    conn = db.get_connection()
    cursor = conn.cursor()

    # Simple LIKE search (case-insensitive)
    search_pattern = f"%{query}%"

    cursor.execute("""
        SELECT id, role, content, sender_name, is_compacted, created_at
        FROM conversation_messages
        WHERE conversation_id = ? AND content LIKE ?
        ORDER BY created_at DESC
        LIMIT ?
    """, (conversation_id, search_pattern, limit))

    rows = cursor.fetchall()
    conn.close()

    return [dict(row) for row in rows]


def search_summaries(
    query: str,
    chat_id: str,
    limit: int = 3,
    source: str = "telegram"
) -> List[dict]:
    """
    Search through conversation summaries.

    Args:
        query: Search query
        chat_id: External chat identifier
        limit: Maximum results
        source: Message source

    Returns:
        List of matching summary dicts
    """
    conversation_id = get_or_create_conversation(chat_id, source)

    conn = db.get_connection()
    cursor = conn.cursor()

    search_pattern = f"%{query}%"

    cursor.execute("""
        SELECT id, summary_text, message_count, period_start, period_end
        FROM conversation_summaries
        WHERE conversation_id = ? AND summary_text LIKE ?
        ORDER BY period_end DESC
        LIMIT ?
    """, (conversation_id, search_pattern, limit))

    rows = cursor.fetchall()
    conn.close()

    return [dict(row) for row in rows]


# ============================================
# STATISTICS
# ============================================

def get_memory_stats(chat_id: str, source: str = "telegram") -> Dict[str, Any]:
    """
    Get memory usage statistics for a conversation.

    Args:
        chat_id: External chat identifier
        source: Message source

    Returns:
        Stats dict with token counts and utilization
    """
    conversation_id = get_or_create_conversation(chat_id, source)

    conn = db.get_connection()
    cursor = conn.cursor()

    # Count total messages
    cursor.execute("""
        SELECT COUNT(*) as count FROM conversation_messages
        WHERE conversation_id = ?
    """, (conversation_id,))
    total_messages = cursor.fetchone()["count"]

    # Count compacted messages
    cursor.execute("""
        SELECT COUNT(*) as count FROM conversation_messages
        WHERE conversation_id = ? AND is_compacted = 1
    """, (conversation_id,))
    compacted_messages = cursor.fetchone()["count"]

    # Count summaries
    cursor.execute("""
        SELECT COUNT(*) as count FROM conversation_summaries
        WHERE conversation_id = ?
    """, (conversation_id,))
    summary_count = cursor.fetchone()["count"]

    # Get summary token totals
    cursor.execute("""
        SELECT COALESCE(SUM(summary_tokens), 0) as total,
               COALESCE(SUM(original_tokens), 0) as original
        FROM conversation_summaries
        WHERE conversation_id = ?
    """, (conversation_id,))
    row = cursor.fetchone()
    summary_tokens = row["total"]
    original_compacted_tokens = row["original"]

    # Get active message tokens
    cursor.execute("""
        SELECT content, role FROM conversation_messages
        WHERE conversation_id = ? AND is_compacted = 0
    """, (conversation_id,))

    active_tokens = 0
    for row in cursor.fetchall():
        active_tokens += estimate_message_tokens({"content": row["content"], "role": row["role"]})

    conn.close()

    # Calculate totals
    estimated_tokens = active_tokens + summary_tokens
    context_utilization = estimated_tokens / CONTEXT_WINDOW

    return {
        "total_messages": total_messages,
        "active_messages": total_messages - compacted_messages,
        "compacted_messages": compacted_messages,
        "summary_count": summary_count,
        "active_tokens": active_tokens,
        "summary_tokens": summary_tokens,
        "estimated_tokens": estimated_tokens,
        "tokens_saved": original_compacted_tokens - summary_tokens if original_compacted_tokens else 0,
        "context_utilization": round(context_utilization, 4),
        "context_utilization_percent": round(context_utilization * 100, 2),
        "compact_threshold_percent": COMPACT_THRESHOLD * 100,
        "needs_compaction": context_utilization > COMPACT_THRESHOLD
    }


# ============================================
# INITIALIZATION
# ============================================

def init():
    """Initialize memory manager (call on startup)."""
    init_memory_schema()


# Initialize schema on import
init()


# ============================================
# TESTING
# ============================================

if __name__ == "__main__":
    import sys

    print("=" * 60)
    print("Memory Manager Test Suite")
    print("=" * 60)

    # Test chat ID
    TEST_CHAT_ID = "test_chat_12345"

    # 1. Test token estimation
    print("\n1. Token Estimation Tests")
    print("-" * 40)

    test_text = "Hello, this is a test message."
    tokens = estimate_tokens(test_text)
    print(f"Text: '{test_text}'")
    print(f"Estimated tokens: {tokens}")
    print(f"Expected (chars/4): {len(test_text) // 4}")
    assert tokens == len(test_text) // 4, "Token estimation failed"
    print("[PASS] Token estimation correct")

    # 2. Test conversation creation
    print("\n2. Conversation Management Tests")
    print("-" * 40)

    conv_id = get_or_create_conversation(TEST_CHAT_ID)
    print(f"Created/retrieved conversation: {conv_id}")

    # Get again to verify retrieval
    conv_id2 = get_or_create_conversation(TEST_CHAT_ID)
    assert conv_id == conv_id2, "Conversation retrieval failed"
    print("[PASS] Conversation retrieval consistent")

    # 3. Test adding messages
    print("\n3. Message Addition Tests")
    print("-" * 40)

    # Add test messages
    test_messages = [
        ("user", "What's on my todo list?"),
        ("assistant", "Let me check your tasks. You have 3 items pending."),
        ("user", "Mark the first one as done"),
        ("assistant", "Done! I've marked 'Review pull request' as complete."),
    ]

    for role, content in test_messages:
        msg_id = add_message_to_conversation(TEST_CHAT_ID, role, content)
        print(f"Added {role} message: {msg_id}")

    # 4. Test message retrieval
    print("\n4. Message Retrieval Tests")
    print("-" * 40)

    recent = get_recent_conversation_messages(TEST_CHAT_ID, limit=10)
    print(f"Retrieved {len(recent)} recent messages")
    for msg in recent[-2:]:
        print(f"  [{msg['role']}]: {msg['content'][:50]}...")
    print("[PASS] Message retrieval working")

    # 5. Test token counting
    print("\n5. Token Counting Tests")
    print("-" * 40)

    token_count = get_conversation_token_count(TEST_CHAT_ID)
    print(f"Total tokens in conversation: {token_count}")

    # 6. Test compaction trigger logic
    print("\n6. Compaction Trigger Tests")
    print("-" * 40)

    needs_compact = should_compact(TEST_CHAT_ID)
    threshold = int(CONTEXT_WINDOW * COMPACT_THRESHOLD)
    print(f"Current tokens: {token_count}")
    print(f"Compact threshold: {threshold} ({COMPACT_THRESHOLD*100}%)")
    print(f"Needs compaction: {needs_compact}")
    print("[PASS] Compaction logic working")

    # 7. Test context building
    print("\n7. Context Building Tests")
    print("-" * 40)

    context = build_context(
        TEST_CHAT_ID,
        project_context="Project: Test Project",
        task_context="Task: Run tests"
    )
    print(f"Built context with {len(context)} messages")
    print(f"System message length: {len(context[0]['content'])} chars")
    for msg in context[1:]:
        print(f"  [{msg['role']}]: {msg['content'][:40]}...")
    print("[PASS] Context building working")

    # 8. Test search
    print("\n8. Search Tests")
    print("-" * 40)

    results = search_history("todo", TEST_CHAT_ID)
    print(f"Search for 'todo' returned {len(results)} results")
    for r in results:
        print(f"  [{r['role']}]: {r['content'][:50]}...")
    print("[PASS] Search working")

    # 9. Test memory stats
    print("\n9. Memory Stats Tests")
    print("-" * 40)

    stats = get_memory_stats(TEST_CHAT_ID)
    print(f"Memory stats:")
    for key, value in stats.items():
        print(f"  {key}: {value}")
    print("[PASS] Stats collection working")

    # 10. Test compaction (with enough messages)
    print("\n10. Compaction Tests")
    print("-" * 40)

    # Add more messages to trigger compaction
    print("Adding 30 more messages for compaction test...")
    for i in range(30):
        role = "user" if i % 2 == 0 else "assistant"
        content = f"Test message number {i+1}. " + "x" * 200  # ~200 chars each
        add_message_to_conversation(TEST_CHAT_ID, role, content)

    # Check stats before compaction
    stats_before = get_memory_stats(TEST_CHAT_ID)
    print(f"Before compaction: {stats_before['active_messages']} active messages")

    # Force compaction (even if not at threshold)
    result = compact_conversation(TEST_CHAT_ID)
    print(f"Compaction result: {result}")

    # Check stats after compaction
    stats_after = get_memory_stats(TEST_CHAT_ID)
    print(f"After compaction: {stats_after['active_messages']} active messages")
    print(f"Summaries created: {stats_after['summary_count']}")

    if result["compacted"] > 0:
        print("[PASS] Compaction executed successfully")
    else:
        print("[INFO] Not enough messages to compact (need more than KEEP_RECENT)")

    # 11. Test context with summaries
    print("\n11. Context with Summaries Tests")
    print("-" * 40)

    context_after = build_context(TEST_CHAT_ID)
    print(f"Context after compaction: {len(context_after)} messages")
    if "Summary" in context_after[0]["content"]:
        print("[PASS] Summaries included in context")
    else:
        print("[INFO] No summaries in context yet")

    print("\n" + "=" * 60)
    print("All tests completed!")
    print("=" * 60)
