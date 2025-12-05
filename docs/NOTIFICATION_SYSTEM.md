# Notification System Implementation Spec

> **Location**: `docs/NOTIFICATION_SYSTEM.md`
> **Phase**: 3 of implementation plan
> **Status**: Ready for implementation
> **Last Updated**: 2025-12-05

## Quick Reference

| Key | Value |
|-----|-------|
| Priority levels | P0 (immediate), P1 (batched), P2 (weekly), P3 (log) |
| Channels | Telegram (primary), SMS (P0 backup) |
| P1 Batch times | 9am, 1pm, 5pm daily |
| P2 Delivery | Sunday 8pm via Telegram |
| Files to create | `notification_router.py`, `config.py` |
| Files to update | `server.py`, `task_manager.py`, `toc_engine.py` |

---

## Overview

Smart notification routing that protects user attention by:
1. **P0 IMMEDIATE** - Critical path blockers, urgent deadlines, package arrivals, meeting reminders
2. **P1 BATCHED** - Important updates grouped into 3 daily digests
3. **P2 WEEKLY** - Status summary on Sunday evening
4. **P3 SILENT** - Background logging only

**Key Principle**: User's attention is the constraint. Minimize interruptions, maximize signal.

---

## Architecture

```
+---------------------------------------------------------------------+
|                     TRIGGER SOURCES                                  |
+---------------------------------------------------------------------+
|                                                                     |
|  blocker resolution ──┐                                             |
|  deadline <24h ───────┤                                             |
|  package arrival ─────┼──> notification_router.queue_p0()           |
|  meeting in 15min ────┤                                             |
|  critical path change ┘                                             |
|                                                                     |
|  task status change ──┐                                             |
|  WIP approaching ─────┼──> notification_router.queue_p1()           |
|  email activity ──────┘                                             |
|                                                                     |
|  weekly summary ──────────> notification_router.queue_p2()          |
|                                                                     |
|  background events ───────> notification_router.queue_p3()          |
|                                                                     |
+---------------------------------------------------------------------+
                              |
                              v
+---------------------------------------------------------------------+
|                     NOTIFICATION_QUEUE (SQLite)                      |
+---------------------------------------------------------------------+
| id | priority | channel | message | context | scheduled_for | sent_at|
+---------------------------------------------------------------------+
                              |
                              v
+---------------------------------------------------------------------+
|                     NOTIFICATION SENDER                              |
+---------------------------------------------------------------------+
|  P0 -> Telegram + SMS (immediate)                                   |
|  P1 -> Telegram (at 9am, 1pm, 5pm batch)                           |
|  P2 -> Telegram (Sunday 8pm digest)                                 |
|  P3 -> Log file only                                                |
+---------------------------------------------------------------------+
```

---

## Priority Definitions

### P0 IMMEDIATE - Critical Path Triggers

**Channels**: Telegram + SMS (dual notification for reliability)
**Timing**: Immediate (within 60 seconds of trigger)
**Deduplication**: Same notification not repeated within 4 hours

| Trigger | Example | Detection Logic |
|---------|---------|-----------------|
| Blocker resolved | "Geotech report received from ABC Engineering" | `email_monitor` finds email matching `blocker.watch_pattern` |
| Blocker escalation | "Geotech engineer requesting more data for Project Z" | Email from person in `blocker.waiting_on` contains request keywords |
| Critical path change | "Task X now blocking 3 other tasks" | `toc_engine.identify_critical_chain()` changes |
| Deadline <24h + incomplete | "Project Z due in 18h, waiting on 2 full-kit items" | `tasks.due_date < now+24h AND full_kit incomplete` |
| Package arrival | "Package from Amazon arrived at office" | External trigger via Telegram command or email pattern |
| Meeting in 15min | "Meeting with Joyce starts in 15 minutes" | Calendar integration (future) or manual reminder |

### P1 SAME-DAY - Important But Not Urgent

**Channel**: Telegram only
**Timing**: Batched at 9am, 1pm, 5pm daily
**Deduplication**: Same notification type not repeated in same batch

| Trigger | Example | Detection Logic |
|---------|---------|-----------------|
| Task status change | "Task 'Review contract' moved to in_progress" | `task_manager.start_task()` or `complete_task()` called |
| WIP approaching limit | "WIP at 2/3 - one slot remaining" | `toc_engine.check_wip_limit()` returns warning |
| Project-relevant email | "Email from Joyce RE: Kitchen proposal" | `email_monitor` classifies email as project-relevant |
| New blocker created | "New blocker: Waiting on permit approval" | `db.add_blocker()` called |
| Deadline approaching | "3 tasks due this week" | Daily deadline scan |

### P2 WEEKLY - Status Summary

**Channel**: Telegram (long-form digest message)
**Timing**: Sunday 8pm
**Content**: Weekly summary with actionable insights

| Section | Content |
|---------|---------|
| Completed | Tasks finished this week with completion times |
| Blocked | Current blockers and who we're waiting on |
| Upcoming | Next week's deadlines and priorities |
| Metrics | WIP average, buffer consumption, flow efficiency |
| Recommendations | AI-suggested focus areas |

### P3 SILENT - Background Activity

**Channel**: Log file only (`./notification.log`)
**Timing**: Never sent to user
**Purpose**: Audit trail, debugging, analytics

| Event | Logged Data |
|-------|-------------|
| All notifications queued | priority, channel, message preview, trigger source |
| Notifications sent | delivery confirmation, response time |
| Deduplication skips | reason, original notification time |
| Errors | channel failures, retry attempts |

---

## Database Schema

### Existing Table (already in db.py)

```sql
CREATE TABLE IF NOT EXISTS notification_queue (
    id TEXT PRIMARY KEY,
    priority TEXT NOT NULL CHECK (priority IN ('P0', 'P1', 'P2', 'P3')),
    channel TEXT NOT NULL CHECK (channel IN ('telegram', 'sms', 'email')),
    message TEXT NOT NULL,
    context TEXT,  -- JSON: {trigger_type, source_id, metadata}
    scheduled_for TEXT,  -- ISO timestamp for batched sends
    sent_at TEXT,  -- NULL = pending
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_notifications_priority
    ON notification_queue(priority, scheduled_for);
```

### New Table: notification_dedup

```sql
CREATE TABLE IF NOT EXISTS notification_dedup (
    id TEXT PRIMARY KEY,
    notification_type TEXT NOT NULL,  -- 'blocker_resolved', 'deadline_urgent', etc.
    source_id TEXT,  -- task_id, blocker_id, etc.
    last_sent_at TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_notification_dedup_type
    ON notification_dedup(notification_type, source_id);
```

---

## Files to Create

### 1. `notification_router.py` (NEW)

```python
#!/usr/bin/env python3
"""
Notification Router for Project Manager Agent.
Routes notifications to appropriate channels based on priority.
"""

import json
import logging
import subprocess
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
import db
import telegram_client

logger = logging.getLogger(__name__)

# ============================================
# CONFIGURATION
# ============================================

DEDUP_WINDOWS = {
    "P0": timedelta(hours=4),   # Don't repeat P0 within 4 hours
    "P1": timedelta(hours=8),   # Don't repeat P1 within 8 hours
    "P2": timedelta(days=7),    # Weekly only
    "P3": timedelta(hours=1),   # Log dedup
}

P1_BATCH_TIMES = ["09:00", "13:00", "17:00"]  # Local time
P2_WEEKLY_TIME = "20:00"  # Sunday 8pm
P2_WEEKLY_DAY = 6  # Sunday = 6

SMS_SCRIPT = "~/.claude/scripts/sms-tool.py"  # External script location

# ============================================
# QUEUEING FUNCTIONS
# ============================================

def queue_p0(
    message: str,
    trigger_type: str,
    source_id: str = None,
    metadata: Dict = None
) -> Optional[Dict]:
    """
    Queue a P0 IMMEDIATE notification.

    Sends to Telegram + SMS immediately.
    Respects 4-hour deduplication window.

    Args:
        message: Notification text
        trigger_type: 'blocker_resolved', 'deadline_urgent', 'package', 'meeting', 'critical_path'
        source_id: Related entity ID (task_id, blocker_id, etc.)
        metadata: Additional context for logging

    Returns:
        Notification record or None if deduplicated
    """
    # Check deduplication
    if _is_duplicate("P0", trigger_type, source_id):
        logger.info(f"P0 deduplicated: {trigger_type} for {source_id}")
        return None

    context = {
        "trigger_type": trigger_type,
        "source_id": source_id,
        "metadata": metadata or {}
    }

    # Queue for both channels
    notif = db.queue_notification(
        message=message,
        priority="P0",
        channel="telegram",
        context=json.dumps(context)
    )

    # Immediate send
    _send_telegram(message)
    _send_sms(message)

    # Mark as sent
    db.mark_notification_sent(notif["id"])

    # Update dedup
    _update_dedup("P0", trigger_type, source_id)

    # Log
    _log_notification("P0", message, trigger_type, "sent")

    return notif


def queue_p1(
    message: str,
    trigger_type: str,
    source_id: str = None,
    metadata: Dict = None
) -> Optional[Dict]:
    """
    Queue a P1 SAME-DAY notification.

    Batched for next 9am/1pm/5pm digest.
    Respects 8-hour deduplication window.

    Args:
        message: Notification text
        trigger_type: 'task_status', 'wip_warning', 'email_activity', 'new_blocker', 'deadline_week'
        source_id: Related entity ID
        metadata: Additional context

    Returns:
        Notification record or None if deduplicated
    """
    # Check deduplication
    if _is_duplicate("P1", trigger_type, source_id):
        logger.info(f"P1 deduplicated: {trigger_type} for {source_id}")
        return None

    context = {
        "trigger_type": trigger_type,
        "source_id": source_id,
        "metadata": metadata or {}
    }

    # Calculate next batch time
    next_batch = _get_next_batch_time()

    notif = db.queue_notification(
        message=message,
        priority="P1",
        channel="telegram",
        scheduled_for=next_batch.isoformat(),
        context=json.dumps(context)
    )

    # Update dedup
    _update_dedup("P1", trigger_type, source_id)

    # Log
    _log_notification("P1", message, trigger_type, f"queued for {next_batch}")

    return notif


def queue_p2(message: str, metadata: Dict = None) -> Dict:
    """
    Queue a P2 WEEKLY notification.

    Sent Sunday 8pm as digest.

    Args:
        message: Full weekly report text
        metadata: Report sections for formatting

    Returns:
        Notification record
    """
    context = {
        "trigger_type": "weekly_report",
        "metadata": metadata or {}
    }

    # Calculate next Sunday 8pm
    next_sunday = _get_next_sunday_8pm()

    notif = db.queue_notification(
        message=message,
        priority="P2",
        channel="telegram",
        scheduled_for=next_sunday.isoformat(),
        context=json.dumps(context)
    )

    _log_notification("P2", message[:100] + "...", "weekly_report", f"queued for {next_sunday}")

    return notif


def queue_p3(message: str, trigger_type: str, metadata: Dict = None) -> None:
    """
    Log a P3 SILENT notification.

    Never sent to user, only logged.

    Args:
        message: Event description
        trigger_type: Event type for categorization
        metadata: Additional context
    """
    _log_notification("P3", message, trigger_type, "logged")


# ============================================
# BATCH PROCESSING
# ============================================

def process_pending_batch() -> int:
    """
    Process and send pending P1 notifications.

    Called by scheduler at 9am, 1pm, 5pm.
    Groups notifications into single digest message.

    Returns:
        Number of notifications sent
    """
    now = datetime.now()

    # Get pending P1 notifications scheduled for now or earlier
    pending = db.get_pending_notifications(priority="P1")

    ready = [
        n for n in pending
        if n["scheduled_for"] and datetime.fromisoformat(n["scheduled_for"]) <= now
    ]

    if not ready:
        logger.info("No P1 notifications ready for batch")
        return 0

    # Group into digest
    digest = _format_digest(ready)

    # Send
    _send_telegram(digest)

    # Mark all as sent
    for notif in ready:
        db.mark_notification_sent(notif["id"])

    logger.info(f"Sent P1 batch digest with {len(ready)} notifications")
    return len(ready)


def process_weekly_report() -> bool:
    """
    Process and send weekly P2 report.

    Called by scheduler Sunday 8pm.

    Returns:
        True if sent successfully
    """
    pending = db.get_pending_notifications(priority="P2")

    if not pending:
        logger.info("No P2 weekly report pending")
        return False

    # Take most recent P2 (should only be one)
    report = pending[-1]

    # Send
    _send_telegram(report["message"])
    db.mark_notification_sent(report["id"])

    logger.info("Sent P2 weekly report")
    return True


# ============================================
# TRIGGER DETECTION
# ============================================

def check_urgent_deadlines() -> List[Dict]:
    """
    Check for tasks with <24h deadline and incomplete full-kit.

    Called by scheduler every hour.

    Returns:
        List of P0 notifications queued
    """
    notifications = []

    # Get tasks due within 24 hours
    urgent_tasks = db.get_tasks_due_within(hours=24, status_not="completed")

    for task in urgent_tasks:
        # Check full-kit
        kit_items = db.get_full_kit_items(task["id"])
        incomplete = [item for item in kit_items if not item["is_satisfied"]]

        if incomplete:
            hours_left = _hours_until(task["due_date"])
            items_list = ", ".join([item["description"] for item in incomplete[:3]])

            message = f"URGENT: '{task['title']}' due in {hours_left}h but waiting on: {items_list}"

            notif = queue_p0(
                message=message,
                trigger_type="deadline_urgent",
                source_id=task["id"],
                metadata={"hours_left": hours_left, "incomplete_items": len(incomplete)}
            )

            if notif:
                notifications.append(notif)

    return notifications


def check_blocker_updates(email_from: str, email_subject: str, email_body: str) -> List[Dict]:
    """
    Check if incoming email resolves or escalates a blocker.

    Called by email_monitor when new email found.

    Args:
        email_from: Sender email/name
        email_subject: Email subject line
        email_body: Email body text

    Returns:
        List of P0 notifications queued
    """
    notifications = []

    # Get active blockers
    blockers = db.get_blockers(resolved=False)

    for blocker in blockers:
        # Check watch pattern match
        if _matches_blocker(blocker, email_from, email_subject, email_body):

            # Determine if resolution or escalation
            if _is_resolution(blocker, email_subject, email_body):
                message = f"UNBLOCKED: {blocker['description']} - Email from {email_from}"
                trigger = "blocker_resolved"

                # Auto-resolve blocker
                db.resolve_blocker(blocker["id"], resolved_by="email_match")

            else:
                message = f"BLOCKER UPDATE: {blocker['description']} - {email_from} sent update"
                trigger = "blocker_escalation"

            notif = queue_p0(
                message=message,
                trigger_type=trigger,
                source_id=blocker["id"],
                metadata={"email_from": email_from, "email_subject": email_subject}
            )

            if notif:
                notifications.append(notif)

    return notifications


def notify_task_status_change(task_id: str, old_status: str, new_status: str) -> Optional[Dict]:
    """
    Queue P1 notification for task status change.

    Called by task_manager on status updates.

    Args:
        task_id: Task identifier
        old_status: Previous status
        new_status: New status

    Returns:
        Notification record or None
    """
    task = db.get_task(task_id)
    if not task:
        return None

    message = f"Task '{task['title']}': {old_status} -> {new_status}"

    return queue_p1(
        message=message,
        trigger_type="task_status",
        source_id=task_id,
        metadata={"old_status": old_status, "new_status": new_status}
    )


def notify_wip_warning(current_wip: int, wip_limit: int) -> Optional[Dict]:
    """
    Queue P1 notification for WIP limit approaching.

    Called by toc_engine when WIP nears limit.

    Args:
        current_wip: Current WIP count
        wip_limit: Maximum allowed WIP

    Returns:
        Notification record or None
    """
    if current_wip < wip_limit - 1:
        return None  # Not yet warning territory

    message = f"WIP at {current_wip}/{wip_limit} - {'AT LIMIT' if current_wip >= wip_limit else 'one slot remaining'}"

    return queue_p1(
        message=message,
        trigger_type="wip_warning",
        source_id=None,
        metadata={"current": current_wip, "limit": wip_limit}
    )


# ============================================
# HELPER FUNCTIONS
# ============================================

def _is_duplicate(priority: str, trigger_type: str, source_id: str) -> bool:
    """Check if notification was sent within dedup window."""
    window = DEDUP_WINDOWS.get(priority, timedelta(hours=1))
    cutoff = datetime.now() - window

    conn = db.get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT last_sent_at FROM notification_dedup
        WHERE notification_type = ? AND (source_id = ? OR source_id IS NULL)
        ORDER BY last_sent_at DESC LIMIT 1
    """, (trigger_type, source_id))

    row = cursor.fetchone()
    conn.close()

    if not row:
        return False

    last_sent = datetime.fromisoformat(row["last_sent_at"])
    return last_sent > cutoff


def _update_dedup(priority: str, trigger_type: str, source_id: str) -> None:
    """Update deduplication tracking."""
    import uuid

    conn = db.get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT OR REPLACE INTO notification_dedup
        (id, notification_type, source_id, last_sent_at)
        VALUES (?, ?, ?, ?)
    """, (str(uuid.uuid4()), trigger_type, source_id, datetime.now().isoformat()))

    conn.commit()
    conn.close()


def _get_next_batch_time() -> datetime:
    """Get next 9am, 1pm, or 5pm."""
    now = datetime.now()
    today = now.date()

    for time_str in P1_BATCH_TIMES:
        hour, minute = map(int, time_str.split(":"))
        batch_time = datetime.combine(today, datetime.min.time().replace(hour=hour, minute=minute))

        if batch_time > now:
            return batch_time

    # Next day 9am
    tomorrow = today + timedelta(days=1)
    return datetime.combine(tomorrow, datetime.min.time().replace(hour=9, minute=0))


def _get_next_sunday_8pm() -> datetime:
    """Get next Sunday 8pm."""
    now = datetime.now()
    days_until_sunday = (P2_WEEKLY_DAY - now.weekday()) % 7

    if days_until_sunday == 0 and now.hour >= 20:
        days_until_sunday = 7

    next_sunday = now.date() + timedelta(days=days_until_sunday)
    return datetime.combine(next_sunday, datetime.min.time().replace(hour=20, minute=0))


def _hours_until(due_date: str) -> int:
    """Calculate hours until due date."""
    due = datetime.fromisoformat(due_date)
    delta = due - datetime.now()
    return max(0, int(delta.total_seconds() / 3600))


def _matches_blocker(blocker: Dict, email_from: str, subject: str, body: str) -> bool:
    """Check if email matches blocker watch pattern."""
    pattern = blocker.get("watch_pattern", "")
    waiting_on = blocker.get("waiting_on", "")

    if not pattern and not waiting_on:
        return False

    # Check sender matches waiting_on
    if waiting_on and waiting_on.lower() in email_from.lower():
        return True

    # Check pattern in subject or body
    if pattern:
        pattern_lower = pattern.lower()
        if pattern_lower in subject.lower() or pattern_lower in body.lower():
            return True

    return False


def _is_resolution(blocker: Dict, subject: str, body: str) -> bool:
    """Determine if email resolves blocker vs escalates."""
    # Simple heuristic: check for resolution keywords
    resolution_keywords = ["attached", "here is", "completed", "finished", "done", "ready", "sent", "enclosed"]
    escalation_keywords = ["need more", "additional", "question", "clarify", "missing", "waiting"]

    text = (subject + " " + body).lower()

    resolution_score = sum(1 for kw in resolution_keywords if kw in text)
    escalation_score = sum(1 for kw in escalation_keywords if kw in text)

    return resolution_score > escalation_score


def _format_digest(notifications: List[Dict]) -> str:
    """Format P1 notifications into digest message."""
    lines = ["=== Daily Update ===", ""]

    # Group by trigger type
    by_type = {}
    for n in notifications:
        ctx = json.loads(n.get("context", "{}"))
        trigger = ctx.get("trigger_type", "other")
        by_type.setdefault(trigger, []).append(n)

    for trigger_type, items in by_type.items():
        lines.append(f"[{trigger_type.replace('_', ' ').title()}]")
        for item in items:
            lines.append(f"  - {item['message']}")
        lines.append("")

    return "\n".join(lines)


def _send_telegram(message: str) -> bool:
    """Send message via Telegram."""
    try:
        telegram_client.send_message(message)
        return True
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")
        return False


def _send_sms(message: str) -> bool:
    """Send message via SMS (Sinch)."""
    try:
        import os
        script_path = os.path.expanduser(SMS_SCRIPT)
        result = subprocess.run(
            ["python", script_path, "send", message],
            capture_output=True,
            text=True,
            timeout=30
        )
        return result.returncode == 0
    except Exception as e:
        logger.error(f"SMS send failed: {e}")
        return False


def _log_notification(priority: str, message: str, trigger_type: str, status: str) -> None:
    """Log notification to file."""
    import os
    log_path = "./notification.log"

    entry = {
        "timestamp": datetime.now().isoformat(),
        "priority": priority,
        "trigger_type": trigger_type,
        "message": message[:200],
        "status": status
    }

    with open(log_path, "a") as f:
        f.write(json.dumps(entry) + "\n")


# ============================================
# DATABASE HELPERS (add to db.py)
# ============================================

# These functions need to be added to db.py:
# - get_tasks_due_within(hours, status_not) -> List[Dict]
# - get_blockers(resolved=False) -> List[Dict]
# - resolve_blocker(blocker_id, resolved_by) -> bool


# ============================================
# TEST
# ============================================

if __name__ == "__main__":
    print("Testing notification_router...")

    # Test dedup window calculation
    assert DEDUP_WINDOWS["P0"] == timedelta(hours=4)
    print("  Dedup windows: OK")

    # Test next batch time
    next_batch = _get_next_batch_time()
    print(f"  Next batch time: {next_batch}")

    # Test next Sunday
    next_sunday = _get_next_sunday_8pm()
    print(f"  Next Sunday 8pm: {next_sunday}")

    print("All tests passed!")
```

### 2. `config.py` (NEW)

```python
#!/usr/bin/env python3
"""
Configuration for Project Manager Agent.
Central location for notification and system settings.
"""

from datetime import timedelta

# ============================================
# NOTIFICATION SETTINGS
# ============================================

NOTIFICATION_CONFIG = {
    "P0": {
        "channels": ["telegram", "sms"],
        "send_immediately": True,
        "dedup_window": timedelta(hours=4),
        "requires_acknowledgement": False,  # Future feature
    },
    "P1": {
        "channels": ["telegram"],
        "batched": True,
        "batch_times": ["09:00", "13:00", "17:00"],
        "dedup_window": timedelta(hours=8),
    },
    "P2": {
        "channels": ["telegram"],
        "batched": True,
        "batch_day": 6,  # Sunday
        "batch_time": "20:00",
    },
    "P3": {
        "channels": ["log"],
        "dedup_window": timedelta(hours=1),
    }
}

# P0 Trigger Types
P0_TRIGGERS = [
    "blocker_resolved",
    "blocker_escalation",
    "deadline_urgent",
    "package_arrival",
    "meeting_reminder",
    "critical_path_change",
]

# P1 Trigger Types
P1_TRIGGERS = [
    "task_status",
    "wip_warning",
    "email_activity",
    "new_blocker",
    "deadline_week",
]

# ============================================
# SCHEDULER SETTINGS
# ============================================

SCHEDULER_JOBS = {
    "deadline_check": {
        "interval": timedelta(hours=1),
        "function": "notification_router.check_urgent_deadlines",
    },
    "p1_batch_morning": {
        "cron": "0 9 * * *",  # 9am daily
        "function": "notification_router.process_pending_batch",
    },
    "p1_batch_afternoon": {
        "cron": "0 13 * * *",  # 1pm daily
        "function": "notification_router.process_pending_batch",
    },
    "p1_batch_evening": {
        "cron": "0 17 * * *",  # 5pm daily
        "function": "notification_router.process_pending_batch",
    },
    "p2_weekly": {
        "cron": "0 20 * * 0",  # Sunday 8pm
        "function": "notification_router.process_weekly_report",
    },
}

# ============================================
# CHANNEL SETTINGS
# ============================================

TELEGRAM_CONFIG = {
    "parse_mode": "Markdown",
    "disable_notification": {
        "P0": False,  # Always notify
        "P1": False,
        "P2": True,   # Silent for weekly
        "P3": True,
    }
}

SMS_CONFIG = {
    "script_path": "~/.claude/scripts/sms-tool.py",  # External script
    "max_length": 160,
    "enabled": True,
}

# ============================================
# LOGGING SETTINGS
# ============================================

LOG_CONFIG = {
    "notification_log": "./notification.log",
    "max_log_size_mb": 10,
    "keep_logs_days": 30,
}
```

---

## Files to Update

### 1. `db.py` - Add helper functions

```python
# Add after existing notification functions (around line 1154)

def get_tasks_due_within(hours: int, status_not: str = None) -> List[Dict]:
    """
    Get tasks due within specified hours.

    Args:
        hours: Hours from now
        status_not: Exclude tasks with this status

    Returns:
        List of task dicts
    """
    conn = get_connection()
    cursor = conn.cursor()

    cutoff = (datetime.now() + timedelta(hours=hours)).isoformat()

    if status_not:
        cursor.execute("""
            SELECT * FROM tasks
            WHERE due_date IS NOT NULL
              AND due_date <= ?
              AND status != ?
            ORDER BY due_date ASC
        """, (cutoff, status_not))
    else:
        cursor.execute("""
            SELECT * FROM tasks
            WHERE due_date IS NOT NULL
              AND due_date <= ?
            ORDER BY due_date ASC
        """, (cutoff,))

    results = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return results


def get_blockers(resolved: bool = None) -> List[Dict]:
    """
    Get blockers, optionally filtered by resolved status.

    Args:
        resolved: True for resolved, False for active, None for all

    Returns:
        List of blocker dicts
    """
    conn = get_connection()
    cursor = conn.cursor()

    if resolved is None:
        cursor.execute("SELECT * FROM blockers ORDER BY created_at DESC")
    elif resolved:
        cursor.execute("SELECT * FROM blockers WHERE resolved_at IS NOT NULL ORDER BY resolved_at DESC")
    else:
        cursor.execute("SELECT * FROM blockers WHERE resolved_at IS NULL ORDER BY created_at DESC")

    results = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return results


def resolve_blocker(blocker_id: str, resolved_by: str = "manual") -> bool:
    """
    Mark a blocker as resolved.

    Args:
        blocker_id: Blocker ID
        resolved_by: How it was resolved (manual, email_match, etc.)

    Returns:
        True if updated
    """
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE blockers
        SET resolved_at = CURRENT_TIMESTAMP, resolved_by = ?
        WHERE id = ?
    """, (resolved_by, blocker_id))

    updated = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return updated


# Add notification_dedup table to init_db()
def init_notification_dedup():
    """Initialize notification deduplication table."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notification_dedup (
            id TEXT PRIMARY KEY,
            notification_type TEXT NOT NULL,
            source_id TEXT,
            last_sent_at TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_notification_dedup_type
        ON notification_dedup(notification_type, source_id)
    """)

    conn.commit()
    conn.close()
```

### 2. `server.py` - Add notification endpoints

```python
# Add after existing endpoints (around line 200)

@app.route("/api/notifications/pending", methods=["GET"])
def get_pending_notifications():
    """Get all pending notifications for debugging."""
    priority = request.args.get("priority")
    channel = request.args.get("channel")

    pending = db.get_pending_notifications(priority=priority, channel=channel)

    return jsonify({
        "ok": True,
        "count": len(pending),
        "notifications": pending
    })


@app.route("/api/notifications/send", methods=["POST"])
def trigger_notification_batch():
    """Manually trigger P1 batch processing."""
    import notification_router

    count = notification_router.process_pending_batch()

    return jsonify({
        "ok": True,
        "sent": count
    })


@app.route("/api/notifications/test", methods=["POST"])
def test_notification():
    """Send a test notification (for debugging)."""
    data = request.get_json() or {}
    message = data.get("message", "Test notification from Project Manager")
    priority = data.get("priority", "P1")

    import notification_router

    if priority == "P0":
        result = notification_router.queue_p0(message, "test", None)
    elif priority == "P1":
        result = notification_router.queue_p1(message, "test", None)
    else:
        result = {"error": "Invalid priority"}

    return jsonify({"ok": True, "result": result})
```

### 3. `task_manager.py` - Add notification hooks

```python
# Add import at top
import notification_router

# Update start_task_safe() to notify
def start_task_safe(task_id: str) -> Dict:
    """Start a task with TOC validation and notification."""
    result = start_task(task_id)  # Existing logic

    if result.get("success"):
        notification_router.notify_task_status_change(
            task_id=task_id,
            old_status="ready",
            new_status="in_progress"
        )

    return result


# Update complete_task_safe() to notify
def complete_task_safe(task_id: str) -> Dict:
    """Complete a task and notify."""
    old_status = db.get_task(task_id).get("status")
    result = complete_task(task_id)  # Existing logic

    if result.get("success"):
        notification_router.notify_task_status_change(
            task_id=task_id,
            old_status=old_status,
            new_status="completed"
        )

    return result
```

### 4. `toc_engine.py` - Add WIP notification hook

```python
# Add import at top
import notification_router

# Update check_wip_limit() to notify when approaching
def check_wip_limit(project_id: str = None) -> Dict:
    """Check WIP limit and notify if approaching."""
    result = _check_wip_limit_internal(project_id)  # Existing logic

    current = result.get("current_wip", 0)
    limit = result.get("wip_limit", 3)

    if current >= limit - 1:
        notification_router.notify_wip_warning(current, limit)

    return result
```

---

## Testing Checklist

- [ ] `notification_router.py` created and imports work
- [ ] `config.py` created with all settings
- [ ] `notification_dedup` table created in db
- [ ] `queue_p0()` sends Telegram + SMS immediately
- [ ] `queue_p1()` schedules for next batch time
- [ ] `queue_p2()` schedules for Sunday 8pm
- [ ] `queue_p3()` only logs (no send)
- [ ] Deduplication prevents repeat notifications
- [ ] `process_pending_batch()` sends digest
- [ ] `check_urgent_deadlines()` finds urgent tasks
- [ ] `check_blocker_updates()` matches email patterns
- [ ] Server endpoints work for debugging
- [ ] Task status changes trigger P1
- [ ] WIP warnings trigger P1

---

## Integration with Other Phases

| Phase | Integration Point | Direction |
|-------|-------------------|-----------|
| Phase 4 (Email) | `check_blocker_updates()` called when email found | Email -> Notifications |
| Phase 9 (Scheduler) | Calls `process_pending_batch()` at 9am/1pm/5pm | Scheduler -> Notifications |
| Phase 8 (Reports) | Generates content for `queue_p2()` | Reports -> Notifications |
| Phase 7 (Autonomous) | May trigger P0 for approval requests | Autonomous -> Notifications |

---

## Why This Approach

| Aspect | Choice | Rationale |
|--------|--------|-----------|
| Primary channel | Telegram | Already integrated, instant delivery |
| SMS backup | P0 only | Cost-effective, critical path only |
| No email channel | Telegram digest | Simpler, user preference |
| Batch times | 9am/1pm/5pm | Natural workday breaks |
| Dedup windows | 4-8 hours | Reduce noise without missing updates |
| Immediate P0 | No batching | Critical path requires instant action |

---

## Search Keywords

For future reference, this document covers:
- notification system, priority routing, P0 P1 P2 P3
- Telegram notifications, SMS backup, Sinch
- batch notifications, daily digest, weekly report
- blocker resolution, deadline alerts, WIP warnings
- deduplication, notification queue, scheduled sends
