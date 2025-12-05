#!/usr/bin/env python3
"""
SQLite persistence for Project Manager Agent.
Full TOC-ready schema with projects, tasks, documents, and more.
"""

import sqlite3
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional

DB_PATH = Path(__file__).parent / "agent.db"
SCHEMA_VERSION = 2  # Bump when schema changes


def get_connection() -> sqlite3.Connection:
    """Get database connection with row factory."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def generate_id() -> str:
    """Generate a short unique ID."""
    return uuid.uuid4().hex[:16]


def init_db():
    """Initialize database schema with all tables."""
    conn = get_connection()
    cursor = conn.cursor()

    # Check schema version
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY
        )
    """)
    cursor.execute("SELECT version FROM schema_version LIMIT 1")
    row = cursor.fetchone()
    current_version = row["version"] if row else 0

    # ============================================
    # CORE TABLES (v1 - original)
    # ============================================

    # Messages table - conversation history (legacy, kept for compatibility)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            chat_id INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Agent state - key-value store with namespacing
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS agent_state (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ============================================
    # TOC SCHEMA (v2 - new)
    # ============================================

    # Projects - top-level containers with TOC properties
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'on_hold', 'completed', 'archived')),

            -- TOC: Constraint Identification
            current_constraint TEXT,
            constraint_type TEXT
                CHECK (constraint_type IN ('resource', 'time', 'knowledge', 'dependency', 'external', NULL)),

            -- TOC: Buffer Management
            estimated_days REAL,
            buffer_days REAL DEFAULT 0,
            buffer_consumed_percent REAL DEFAULT 0,
            progress_percent REAL DEFAULT 0,

            -- TOC: WIP Control
            wip_limit INTEGER DEFAULT 3,

            -- Hierarchy
            parent_project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,

            -- Metadata
            priority INTEGER DEFAULT 50,
            due_date TEXT,
            started_at TEXT,
            completed_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Tasks - hierarchical with dependencies and full-kit
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
            parent_task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE,

            title TEXT NOT NULL,
            description TEXT,

            -- Status tracking
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'waiting_for_kit', 'ready', 'in_progress', 'blocked', 'completed', 'cancelled')),

            -- TOC: Aggressive estimates (50% confidence)
            estimated_hours REAL,
            actual_hours REAL,

            -- TOC: Critical Chain
            is_critical_chain INTEGER DEFAULT 0,
            critical_chain_sequence INTEGER,

            -- Timing
            planned_start TEXT,
            actual_start TEXT,
            planned_end TEXT,
            actual_end TEXT,
            due_date TEXT,

            -- Priority (for multi-project staggering)
            priority INTEGER DEFAULT 50,
            sort_order INTEGER DEFAULT 0,

            -- Recurring reference
            recurring_schedule_id TEXT REFERENCES recurring_schedules(id) ON DELETE SET NULL,

            -- Metadata
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Task Dependencies (finish-to-start by default)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS task_dependencies (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            depends_on_task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            dependency_type TEXT DEFAULT 'finish_to_start'
                CHECK (dependency_type IN ('finish_to_start', 'start_to_start', 'finish_to_finish')),

            -- TOC: Feeding buffer
            feeding_buffer_hours REAL DEFAULT 0,

            created_at TEXT DEFAULT CURRENT_TIMESTAMP,

            UNIQUE(task_id, depends_on_task_id)
        )
    """)

    # Full Kit Checklist (TOC)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS task_full_kit (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,

            requirement_type TEXT NOT NULL
                CHECK (requirement_type IN ('information', 'resource', 'dependency', 'approval', 'tool', 'other')),
            description TEXT NOT NULL,
            is_satisfied INTEGER DEFAULT 0,
            satisfied_at TEXT,
            notes TEXT,

            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Blockers - what's stopping work
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS blockers (
            id TEXT PRIMARY KEY,
            task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE,
            project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,

            blocker_type TEXT NOT NULL
                CHECK (blocker_type IN ('email', 'document', 'approval', 'deadline', 'resource', 'external', 'other')),
            description TEXT NOT NULL,
            waiting_on TEXT,
            watch_pattern TEXT,

            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            resolved_at TEXT,
            resolved_by TEXT
        )
    """)

    # Documents - file metadata for RAG
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            id TEXT PRIMARY KEY,
            project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
            task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,

            filename TEXT NOT NULL,
            file_path TEXT,
            file_type TEXT,
            file_size_bytes INTEGER,
            file_hash TEXT,

            document_type TEXT
                CHECK (document_type IN ('receipt', 'invoice', 'quote', 'contract', 'manual', 'note', 'screenshot', 'email', 'meeting_notes', 'other')),

            -- Extracted metadata (for receipts/invoices)
            vendor TEXT,
            amount REAL,
            currency TEXT DEFAULT 'USD',
            transaction_date TEXT,
            category TEXT,

            -- RAG support
            content_text TEXT,
            content_summary TEXT,
            embedding_model TEXT,

            tags TEXT,  -- JSON array
            notes TEXT,

            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Document Chunks - for RAG retrieval
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS document_chunks (
            id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,

            chunk_index INTEGER NOT NULL,
            chunk_text TEXT NOT NULL,
            chunk_tokens INTEGER,

            -- Embedding stored as JSON array (migrate to pgvector later)
            embedding TEXT,
            embedding_model TEXT,

            created_at TEXT DEFAULT CURRENT_TIMESTAMP,

            UNIQUE(document_id, chunk_index)
        )
    """)

    # Conversations - enhanced from messages with project linking
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY,
            project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
            task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,

            source TEXT NOT NULL
                CHECK (source IN ('telegram', 'email', 'meeting', 'phone', 'manual')),
            external_id TEXT,

            title TEXT,
            participants TEXT,  -- JSON array

            summary TEXT,
            summary_updated_at TEXT,

            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Conversation Messages - replaces flat messages for new conversations
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS conversation_messages (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,

            role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system', 'external')),
            content TEXT NOT NULL,

            is_summarized INTEGER DEFAULT 0,
            importance_score REAL,

            sender_name TEXT,
            external_message_id TEXT,

            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Email Threads - for tracking email context
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS email_threads (
            id TEXT PRIMARY KEY,
            project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
            conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,

            gmail_thread_id TEXT UNIQUE,
            subject TEXT,
            participants TEXT,  -- JSON array

            summary TEXT,

            last_message_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Recurring Schedules - for bills, maintenance, etc.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS recurring_schedules (
            id TEXT PRIMARY KEY,
            project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,

            name TEXT NOT NULL,
            description TEXT,

            frequency TEXT NOT NULL
                CHECK (frequency IN ('daily', 'weekly', 'biweekly', 'monthly', 'quarterly', 'yearly', 'custom')),

            day_of_week TEXT,
            day_of_month TEXT,
            month_of_year TEXT,

            task_title_template TEXT NOT NULL,
            task_description_template TEXT,
            estimated_hours REAL,

            expected_document_type TEXT,

            start_date TEXT NOT NULL,
            end_date TEXT,

            last_generated_date TEXT,
            next_due_date TEXT,

            is_active INTEGER DEFAULT 1,

            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Autonomous Actions - actions agent takes or wants to take
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS autonomous_actions (
            id TEXT PRIMARY KEY,

            action_type TEXT NOT NULL,
            target TEXT,
            context TEXT,  -- JSON

            status TEXT DEFAULT 'pending'
                CHECK (status IN ('pending', 'approved', 'executed', 'cancelled', 'failed')),
            requires_approval INTEGER DEFAULT 0,

            approved_at TEXT,
            executed_at TEXT,
            result TEXT,  -- JSON
            error TEXT,

            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # TOC Metrics Snapshots - for trend analysis
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS toc_metrics_snapshots (
            id TEXT PRIMARY KEY,

            snapshot_date TEXT NOT NULL,

            total_wip_count INTEGER,
            wip_limit_violations INTEGER,

            avg_buffer_consumed_percent REAL,
            projects_in_red_zone INTEGER,
            projects_in_yellow_zone INTEGER,

            tasks_completed INTEGER,
            avg_flow_efficiency REAL,

            context_switches INTEGER,
            full_kit_starts INTEGER,
            partial_kit_starts INTEGER,

            tasks_started_late INTEGER,
            tasks_expanded_to_fill INTEGER,

            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Context Switches - for behavioral tracking
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS context_switches (
            id TEXT PRIMARY KEY,

            from_task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
            to_task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,

            switch_type TEXT CHECK (switch_type IN ('voluntary', 'blocked', 'interrupt', 'scheduled')),
            reason TEXT,

            occurred_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Notification Queue - for batch notifications
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notification_queue (
            id TEXT PRIMARY KEY,

            priority TEXT NOT NULL CHECK (priority IN ('P0', 'P1', 'P2', 'P3')),
            channel TEXT NOT NULL CHECK (channel IN ('telegram', 'sms', 'email')),

            message TEXT NOT NULL,
            context TEXT,  -- JSON

            scheduled_for TEXT,
            sent_at TEXT,

            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ============================================
    # INDEXES
    # ============================================

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_projects_parent ON projects(parent_project_id)")

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_task_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_critical_chain ON tasks(is_critical_chain, critical_chain_sequence)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_due_date ON tasks(due_date)")

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_dependencies_task ON task_dependencies(task_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_dependencies_depends ON task_dependencies(depends_on_task_id)")

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_documents_project ON documents(project_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_documents_type ON documents(document_type)")

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_chunks_document ON document_chunks(document_id)")

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_conversations_project ON conversations(project_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_conv_messages_conversation ON conversation_messages(conversation_id)")

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_blockers_task ON blockers(task_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_blockers_resolved ON blockers(resolved_at)")

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_recurring_next_due ON recurring_schedules(next_due_date) WHERE is_active = 1")

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_notifications_priority ON notification_queue(priority, scheduled_for)")

    # Update schema version
    if current_version < SCHEMA_VERSION:
        cursor.execute("DELETE FROM schema_version")
        cursor.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))

    conn.commit()
    conn.close()


def add_message(role: str, content: str, chat_id: int = None) -> int:
    """Add a message to conversation history."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO messages (role, content, chat_id) VALUES (?, ?, ?)",
        (role, content, chat_id)
    )
    conn.commit()
    msg_id = cursor.lastrowid
    conn.close()
    return msg_id


def get_recent_messages(limit: int = 20, chat_id: int = None) -> List[Dict[str, Any]]:
    """Get recent messages for context."""
    conn = get_connection()
    cursor = conn.cursor()

    if chat_id:
        cursor.execute(
            "SELECT role, content FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit)
        )
    else:
        cursor.execute(
            "SELECT role, content FROM messages ORDER BY id DESC LIMIT ?",
            (limit,)
        )

    rows = cursor.fetchall()
    conn.close()

    # Reverse to get chronological order
    return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]


def get_state(key: str, default: Any = None) -> Any:
    """Get agent state value."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM agent_state WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()

    if row is None:
        return default

    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        return row["value"]


def set_state(key: str, value: Any):
    """Set agent state value."""
    conn = get_connection()
    cursor = conn.cursor()

    value_str = json.dumps(value) if not isinstance(value, str) else value

    cursor.execute("""
        INSERT INTO agent_state (key, value, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = CURRENT_TIMESTAMP
    """, (key, value_str, value_str))

    conn.commit()
    conn.close()


def clear_messages():
    """Clear all messages (for testing)."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM messages")
    conn.commit()
    conn.close()


# ============================================
# PROJECT CRUD
# ============================================

def create_project(
    name: str,
    description: str = None,
    estimated_days: float = None,
    buffer_days: float = None,
    due_date: str = None,
    parent_project_id: str = None,
    wip_limit: int = 3,
    priority: int = 50
) -> Dict[str, Any]:
    """Create a new project."""
    conn = get_connection()
    cursor = conn.cursor()

    project_id = generate_id()
    cursor.execute("""
        INSERT INTO projects (
            id, name, description, estimated_days, buffer_days,
            due_date, parent_project_id, wip_limit, priority
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        project_id, name, description, estimated_days, buffer_days,
        due_date, parent_project_id, wip_limit, priority
    ))

    conn.commit()
    conn.close()

    return get_project(project_id)


def get_project(project_id: str) -> Optional[Dict[str, Any]]:
    """Get a project by ID."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
    row = cursor.fetchone()
    conn.close()

    if row:
        return dict(row)
    return None


def list_projects(
    status: str = None,
    parent_id: str = None,
    limit: int = 100
) -> List[Dict[str, Any]]:
    """List projects with optional filters."""
    conn = get_connection()
    cursor = conn.cursor()

    query = "SELECT * FROM projects WHERE 1=1"
    params = []

    if status:
        query += " AND status = ?"
        params.append(status)
    if parent_id is not None:
        if parent_id == "":
            query += " AND parent_project_id IS NULL"
        else:
            query += " AND parent_project_id = ?"
            params.append(parent_id)

    query += " ORDER BY priority DESC, created_at DESC LIMIT ?"
    params.append(limit)

    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()

    return [dict(row) for row in rows]


def update_project(project_id: str, **kwargs) -> Optional[Dict[str, Any]]:
    """Update a project."""
    if not kwargs:
        return get_project(project_id)

    conn = get_connection()
    cursor = conn.cursor()

    # Build SET clause
    sets = []
    params = []
    for key, value in kwargs.items():
        sets.append(f"{key} = ?")
        params.append(value)

    sets.append("updated_at = CURRENT_TIMESTAMP")
    params.append(project_id)

    query = f"UPDATE projects SET {', '.join(sets)} WHERE id = ?"
    cursor.execute(query, params)
    conn.commit()
    conn.close()

    return get_project(project_id)


# ============================================
# TASK CRUD
# ============================================

def create_task(
    project_id: str,
    title: str,
    description: str = None,
    parent_task_id: str = None,
    estimated_hours: float = None,
    due_date: str = None,
    priority: int = 50
) -> Dict[str, Any]:
    """Create a new task."""
    conn = get_connection()
    cursor = conn.cursor()

    task_id = generate_id()
    cursor.execute("""
        INSERT INTO tasks (
            id, project_id, parent_task_id, title, description,
            estimated_hours, due_date, priority
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        task_id, project_id, parent_task_id, title, description,
        estimated_hours, due_date, priority
    ))

    conn.commit()
    conn.close()

    return get_task(task_id)


def get_task(task_id: str) -> Optional[Dict[str, Any]]:
    """Get a task by ID."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
    row = cursor.fetchone()
    conn.close()

    if row:
        return dict(row)
    return None


def list_tasks(
    project_id: str = None,
    status: str = None,
    parent_task_id: str = None,
    limit: int = 100
) -> List[Dict[str, Any]]:
    """List tasks with optional filters."""
    conn = get_connection()
    cursor = conn.cursor()

    query = "SELECT * FROM tasks WHERE 1=1"
    params = []

    if project_id:
        query += " AND project_id = ?"
        params.append(project_id)
    if status:
        query += " AND status = ?"
        params.append(status)
    if parent_task_id is not None:
        if parent_task_id == "":
            query += " AND parent_task_id IS NULL"
        else:
            query += " AND parent_task_id = ?"
            params.append(parent_task_id)

    query += " ORDER BY sort_order, priority DESC, created_at LIMIT ?"
    params.append(limit)

    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()

    return [dict(row) for row in rows]


def update_task(task_id: str, **kwargs) -> Optional[Dict[str, Any]]:
    """Update a task."""
    if not kwargs:
        return get_task(task_id)

    conn = get_connection()
    cursor = conn.cursor()

    sets = []
    params = []
    for key, value in kwargs.items():
        sets.append(f"{key} = ?")
        params.append(value)

    sets.append("updated_at = CURRENT_TIMESTAMP")
    params.append(task_id)

    query = f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?"
    cursor.execute(query, params)
    conn.commit()
    conn.close()

    return get_task(task_id)


def get_active_tasks() -> List[Dict[str, Any]]:
    """Get all in-progress tasks (WIP)."""
    return list_tasks(status='in_progress')


def get_wip_count(project_id: str = None) -> int:
    """Get current WIP count."""
    conn = get_connection()
    cursor = conn.cursor()

    if project_id:
        cursor.execute(
            "SELECT COUNT(*) as count FROM tasks WHERE project_id = ? AND status = 'in_progress'",
            (project_id,)
        )
    else:
        cursor.execute(
            "SELECT COUNT(*) as count FROM tasks WHERE status = 'in_progress'"
        )

    row = cursor.fetchone()
    conn.close()
    return row["count"]


# ============================================
# FULL KIT
# ============================================

def add_full_kit_item(
    task_id: str,
    description: str,
    requirement_type: str = 'other'
) -> Dict[str, Any]:
    """Add a prerequisite to task's full kit."""
    conn = get_connection()
    cursor = conn.cursor()

    item_id = generate_id()
    cursor.execute("""
        INSERT INTO task_full_kit (id, task_id, requirement_type, description)
        VALUES (?, ?, ?, ?)
    """, (item_id, task_id, requirement_type, description))

    conn.commit()
    conn.close()

    return {"id": item_id, "task_id": task_id, "description": description, "is_satisfied": False}


def get_full_kit(task_id: str) -> List[Dict[str, Any]]:
    """Get full kit items for a task."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM task_full_kit WHERE task_id = ? ORDER BY created_at",
        (task_id,)
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def mark_kit_item_satisfied(item_id: str, satisfied: bool = True) -> bool:
    """Mark a full kit item as satisfied or not."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE task_full_kit
        SET is_satisfied = ?, satisfied_at = CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END
        WHERE id = ?
    """, (1 if satisfied else 0, satisfied, item_id))
    conn.commit()
    affected = cursor.rowcount
    conn.close()
    return affected > 0


def is_full_kit_complete(task_id: str) -> bool:
    """Check if all full kit items are satisfied."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) as count FROM task_full_kit WHERE task_id = ? AND is_satisfied = 0",
        (task_id,)
    )
    row = cursor.fetchone()
    conn.close()
    return row["count"] == 0


# ============================================
# BLOCKERS
# ============================================

def create_blocker(
    description: str,
    blocker_type: str = 'other',
    task_id: str = None,
    project_id: str = None,
    waiting_on: str = None,
    watch_pattern: str = None
) -> Dict[str, Any]:
    """Create a blocker."""
    conn = get_connection()
    cursor = conn.cursor()

    blocker_id = generate_id()
    cursor.execute("""
        INSERT INTO blockers (
            id, task_id, project_id, blocker_type, description, waiting_on, watch_pattern
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (blocker_id, task_id, project_id, blocker_type, description, waiting_on, watch_pattern))

    conn.commit()
    conn.close()

    return get_blocker(blocker_id)


def get_blocker(blocker_id: str) -> Optional[Dict[str, Any]]:
    """Get a blocker by ID."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM blockers WHERE id = ?", (blocker_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def list_blockers(
    active_only: bool = True,
    task_id: str = None,
    project_id: str = None
) -> List[Dict[str, Any]]:
    """List blockers."""
    conn = get_connection()
    cursor = conn.cursor()

    query = "SELECT * FROM blockers WHERE 1=1"
    params = []

    if active_only:
        query += " AND resolved_at IS NULL"
    if task_id:
        query += " AND task_id = ?"
        params.append(task_id)
    if project_id:
        query += " AND project_id = ?"
        params.append(project_id)

    query += " ORDER BY created_at DESC"
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()

    return [dict(row) for row in rows]


def resolve_blocker(blocker_id: str, resolved_by: str = None) -> bool:
    """Resolve a blocker."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE blockers
        SET resolved_at = CURRENT_TIMESTAMP, resolved_by = ?
        WHERE id = ?
    """, (resolved_by, blocker_id))
    conn.commit()
    affected = cursor.rowcount
    conn.close()
    return affected > 0


# ============================================
# DOCUMENTS
# ============================================

def create_document(
    filename: str,
    file_path: str = None,
    project_id: str = None,
    task_id: str = None,
    document_type: str = 'other',
    content_text: str = None,
    **metadata
) -> Dict[str, Any]:
    """Create a document record."""
    conn = get_connection()
    cursor = conn.cursor()

    doc_id = generate_id()
    cursor.execute("""
        INSERT INTO documents (
            id, filename, file_path, project_id, task_id, document_type,
            content_text, vendor, amount, transaction_date, category, tags, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        doc_id, filename, file_path, project_id, task_id, document_type,
        content_text,
        metadata.get('vendor'),
        metadata.get('amount'),
        metadata.get('transaction_date'),
        metadata.get('category'),
        json.dumps(metadata.get('tags')) if metadata.get('tags') else None,
        metadata.get('notes')
    ))

    conn.commit()
    conn.close()

    return get_document(doc_id)


def get_document(doc_id: str) -> Optional[Dict[str, Any]]:
    """Get a document by ID."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM documents WHERE id = ?", (doc_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def list_documents(
    project_id: str = None,
    document_type: str = None,
    limit: int = 100
) -> List[Dict[str, Any]]:
    """List documents."""
    conn = get_connection()
    cursor = conn.cursor()

    query = "SELECT * FROM documents WHERE 1=1"
    params = []

    if project_id:
        query += " AND project_id = ?"
        params.append(project_id)
    if document_type:
        query += " AND document_type = ?"
        params.append(document_type)

    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()

    return [dict(row) for row in rows]


# ============================================
# RECURRING SCHEDULES
# ============================================

def create_recurring_schedule(
    name: str,
    task_title_template: str,
    frequency: str,
    start_date: str,
    project_id: str = None,
    **kwargs
) -> Dict[str, Any]:
    """Create a recurring schedule."""
    conn = get_connection()
    cursor = conn.cursor()

    schedule_id = generate_id()
    cursor.execute("""
        INSERT INTO recurring_schedules (
            id, name, task_title_template, frequency, start_date, project_id,
            description, day_of_week, day_of_month, month_of_year,
            task_description_template, estimated_hours, expected_document_type
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        schedule_id, name, task_title_template, frequency, start_date, project_id,
        kwargs.get('description'),
        kwargs.get('day_of_week'),
        kwargs.get('day_of_month'),
        kwargs.get('month_of_year'),
        kwargs.get('task_description_template'),
        kwargs.get('estimated_hours'),
        kwargs.get('expected_document_type')
    ))

    conn.commit()
    conn.close()

    return get_recurring_schedule(schedule_id)


def get_recurring_schedule(schedule_id: str) -> Optional[Dict[str, Any]]:
    """Get a recurring schedule by ID."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM recurring_schedules WHERE id = ?", (schedule_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def list_recurring_schedules(active_only: bool = True) -> List[Dict[str, Any]]:
    """List recurring schedules."""
    conn = get_connection()
    cursor = conn.cursor()

    query = "SELECT * FROM recurring_schedules"
    if active_only:
        query += " WHERE is_active = 1"
    query += " ORDER BY next_due_date"

    cursor.execute(query)
    rows = cursor.fetchall()
    conn.close()

    return [dict(row) for row in rows]


# ============================================
# NOTIFICATIONS
# ============================================

def queue_notification(
    message: str,
    priority: str = 'P1',
    channel: str = 'telegram',
    scheduled_for: str = None,
    context: Dict = None
) -> Dict[str, Any]:
    """Queue a notification."""
    conn = get_connection()
    cursor = conn.cursor()

    notif_id = generate_id()
    cursor.execute("""
        INSERT INTO notification_queue (id, priority, channel, message, scheduled_for, context)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (notif_id, priority, channel, message, scheduled_for, json.dumps(context) if context else None))

    conn.commit()
    conn.close()

    return {"id": notif_id, "priority": priority, "message": message}


def get_pending_notifications(priority: str = None, channel: str = None) -> List[Dict[str, Any]]:
    """Get pending notifications."""
    conn = get_connection()
    cursor = conn.cursor()

    query = "SELECT * FROM notification_queue WHERE sent_at IS NULL"
    params = []

    if priority:
        query += " AND priority = ?"
        params.append(priority)
    if channel:
        query += " AND channel = ?"
        params.append(channel)

    query += " ORDER BY priority, scheduled_for, created_at"
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()

    return [dict(row) for row in rows]


def mark_notification_sent(notif_id: str) -> bool:
    """Mark a notification as sent."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE notification_queue SET sent_at = CURRENT_TIMESTAMP WHERE id = ?",
        (notif_id,)
    )
    conn.commit()
    affected = cursor.rowcount
    conn.close()
    return affected > 0


# ============================================
# CONTEXT SWITCHES
# ============================================

def log_context_switch(
    from_task_id: str = None,
    to_task_id: str = None,
    switch_type: str = 'voluntary',
    reason: str = None
) -> Dict[str, Any]:
    """Log a context switch."""
    conn = get_connection()
    cursor = conn.cursor()

    switch_id = generate_id()
    cursor.execute("""
        INSERT INTO context_switches (id, from_task_id, to_task_id, switch_type, reason)
        VALUES (?, ?, ?, ?, ?)
    """, (switch_id, from_task_id, to_task_id, switch_type, reason))

    conn.commit()
    conn.close()

    return {"id": switch_id, "from": from_task_id, "to": to_task_id, "type": switch_type}


def get_context_switches_today() -> int:
    """Get count of context switches today."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT COUNT(*) as count FROM context_switches
        WHERE date(occurred_at) = date('now')
    """)
    row = cursor.fetchone()
    conn.close()
    return row["count"]


# ============================================
# AUTONOMOUS ACTIONS
# ============================================

def create_autonomous_action(
    action_type: str,
    target: str = None,
    context: Dict = None,
    requires_approval: bool = False
) -> Dict[str, Any]:
    """Create an autonomous action."""
    conn = get_connection()
    cursor = conn.cursor()

    action_id = generate_id()
    cursor.execute("""
        INSERT INTO autonomous_actions (id, action_type, target, context, requires_approval)
        VALUES (?, ?, ?, ?, ?)
    """, (action_id, action_type, target, json.dumps(context) if context else None, 1 if requires_approval else 0))

    conn.commit()
    conn.close()

    return get_autonomous_action(action_id)


def get_autonomous_action(action_id: str) -> Optional[Dict[str, Any]]:
    """Get an autonomous action by ID."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM autonomous_actions WHERE id = ?", (action_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def get_pending_actions(requires_approval: bool = None) -> List[Dict[str, Any]]:
    """Get pending autonomous actions."""
    conn = get_connection()
    cursor = conn.cursor()

    query = "SELECT * FROM autonomous_actions WHERE status = 'pending'"
    params = []

    if requires_approval is not None:
        query += " AND requires_approval = ?"
        params.append(1 if requires_approval else 0)

    query += " ORDER BY created_at"
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()

    return [dict(row) for row in rows]


def update_action_status(
    action_id: str,
    status: str,
    result: Dict = None,
    error: str = None
) -> bool:
    """Update an autonomous action status."""
    conn = get_connection()
    cursor = conn.cursor()

    if status == 'approved':
        cursor.execute(
            "UPDATE autonomous_actions SET status = ?, approved_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, action_id)
        )
    elif status == 'executed':
        cursor.execute(
            "UPDATE autonomous_actions SET status = ?, executed_at = CURRENT_TIMESTAMP, result = ? WHERE id = ?",
            (status, json.dumps(result) if result else None, action_id)
        )
    elif status == 'failed':
        cursor.execute(
            "UPDATE autonomous_actions SET status = ?, error = ? WHERE id = ?",
            (status, error, action_id)
        )
    else:
        cursor.execute(
            "UPDATE autonomous_actions SET status = ? WHERE id = ?",
            (status, action_id)
        )

    conn.commit()
    affected = cursor.rowcount
    conn.close()
    return affected > 0


# Initialize on import
init_db()
