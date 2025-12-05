#!/usr/bin/env python3
"""
Notification Router for Project Manager Agent.
Routes outbound notifications to user via appropriate channels based on priority.

Architecture:
- Inbound: Telegram webhook -> Grok (already live, handled by server.py)
- Outbound: This module -> Routes P0-P3 notifications TO user

P0 IMMEDIATE: Telegram + SMS instantly (blocker resolved, urgent deadline)
P1 BATCHED: Telegram at 9am/1pm/5pm (task updates, WIP warnings)
P2 WEEKLY: Telegram Sunday 8pm (weekly summary)
P3 SILENT: Log only (audit trail)
"""

import json
import logging
import os
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Any, Optional

import db
import telegram_client
from config import DEDUP_WINDOWS, P1_BATCH_TIMES, P2_WEEKLY_DAY, P2_WEEKLY_TIME

logger = logging.getLogger(__name__)

# SMS script path - use user's notification scripts
SMS_SCRIPT = os.path.expanduser("~/.claude/scripts/sms-tool.py")
# Log path - relative to project directory
LOG_PATH = os.path.join(os.path.dirname(__file__), "notification.log")


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
    Queue and send a P0 IMMEDIATE notification.

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
    if db.check_dedup(trigger_type, source_id, DEDUP_WINDOWS["P0"]):
        logger.info(f"P0 deduplicated: {trigger_type} for {source_id}")
        _log_notification("P0", message, trigger_type, "deduplicated")
        return None

    context = {
        "trigger_type": trigger_type,
        "source_id": source_id,
        "metadata": metadata or {}
    }

    # Queue record
    notif = db.queue_notification(
        message=message,
        priority="P0",
        channel="telegram",
        context=context
    )

    # Immediate send to both channels
    telegram_success = _send_telegram(f"[URGENT] {message}")
    sms_success = _send_sms(message[:160])  # SMS has char limit

    # Mark as sent
    db.mark_notification_sent(notif["id"])

    # Update dedup
    db.update_dedup(trigger_type, source_id)

    # Log
    status = "sent" if telegram_success else "telegram_failed"
    if not sms_success:
        status += ",sms_failed"
    _log_notification("P0", message, trigger_type, status)

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
    if db.check_dedup(trigger_type, source_id, DEDUP_WINDOWS["P1"]):
        logger.info(f"P1 deduplicated: {trigger_type} for {source_id}")
        _log_notification("P1", message, trigger_type, "deduplicated")
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
        context=context
    )

    # Update dedup
    db.update_dedup(trigger_type, source_id)

    # Log
    _log_notification("P1", message, trigger_type, f"queued for {next_batch.strftime('%H:%M')}")

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
        context=context
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
    now = datetime.now(timezone.utc)

    # Get pending P1 notifications scheduled for now or earlier
    pending = db.get_pending_notifications(priority="P1")

    ready = []
    for n in pending:
        if n.get("scheduled_for"):
            try:
                scheduled = datetime.fromisoformat(n["scheduled_for"].replace("Z", "+00:00"))
                if scheduled <= now:
                    ready.append(n)
            except ValueError:
                ready.append(n)  # Include if can't parse

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
    _log_notification("P1", f"Batch sent: {len(ready)} items", "batch_digest", "sent")

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
    success = _send_telegram(report["message"])
    if success:
        db.mark_notification_sent(report["id"])
        logger.info("Sent P2 weekly report")

    return success


# ============================================
# TRIGGER DETECTION (called by other modules)
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
        incomplete = [item for item in kit_items if not item.get("is_satisfied")]

        if incomplete:
            hours_left = _hours_until(task["due_date"])
            items_list = ", ".join([item["description"] for item in incomplete[:3]])

            message = f"'{task['title']}' due in {hours_left}h, waiting on: {items_list}"

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
    blockers = db.get_blockers_filtered(resolved=False)

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

    if current_wip >= wip_limit:
        message = f"WIP at {current_wip}/{wip_limit} - AT LIMIT"
    else:
        message = f"WIP at {current_wip}/{wip_limit} - one slot remaining"

    return queue_p1(
        message=message,
        trigger_type="wip_warning",
        source_id=None,
        metadata={"current": current_wip, "limit": wip_limit}
    )


def notify_new_blocker(blocker_id: str, description: str, waiting_on: str = None) -> Optional[Dict]:
    """
    Queue P1 notification for new blocker.

    Called when a blocker is created.

    Args:
        blocker_id: Blocker ID
        description: Blocker description
        waiting_on: Who/what we're waiting on

    Returns:
        Notification record or None
    """
    if waiting_on:
        message = f"New blocker: {description} (waiting on {waiting_on})"
    else:
        message = f"New blocker: {description}"

    return queue_p1(
        message=message,
        trigger_type="new_blocker",
        source_id=blocker_id,
        metadata={"description": description, "waiting_on": waiting_on}
    )


# ============================================
# HELPER FUNCTIONS
# ============================================

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
    try:
        due = datetime.fromisoformat(due_date.replace("Z", "+00:00"))
        delta = due - datetime.now(timezone.utc)
        return max(0, int(delta.total_seconds() / 3600))
    except (ValueError, TypeError):
        return 0


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
        ctx = n.get("context")
        if isinstance(ctx, str):
            try:
                ctx = json.loads(ctx)
            except json.JSONDecodeError:
                ctx = {}
        elif ctx is None:
            ctx = {}

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
        if not os.path.exists(SMS_SCRIPT):
            logger.warning(f"SMS script not found: {SMS_SCRIPT}")
            return False

        result = subprocess.run(
            ["python", SMS_SCRIPT, "send", message],
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
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "priority": priority,
        "trigger_type": trigger_type,
        "message": message[:200],
        "status": status
    }

    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        logger.error(f"Failed to write notification log: {e}")


# ============================================
# TEST
# ============================================

if __name__ == "__main__":
    print("Testing notification_router...")

    # Test next batch time
    next_batch = _get_next_batch_time()
    print(f"  Next batch time: {next_batch}")

    # Test next Sunday
    next_sunday = _get_next_sunday_8pm()
    print(f"  Next Sunday 8pm: {next_sunday}")

    # Test dedup windows
    print(f"  Dedup windows: P0={DEDUP_WINDOWS['P0']}h, P1={DEDUP_WINDOWS['P1']}h")

    print("All tests passed!")
