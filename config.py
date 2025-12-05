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
        "requires_acknowledgement": False,
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

# Dedup windows in hours for easy access
DEDUP_WINDOWS = {
    "P0": 4,
    "P1": 8,
    "P2": 168,  # 7 days
    "P3": 1,
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

P1_BATCH_TIMES = ["09:00", "13:00", "17:00"]  # Local time
P2_WEEKLY_TIME = "20:00"  # Sunday 8pm
P2_WEEKLY_DAY = 6  # Sunday = 6

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
    "script_path": "~/.claude/scripts/sms-tool.py",
    "max_length": 160,
    "enabled": True,
}

# ============================================
# LOGGING SETTINGS
# ============================================

LOG_CONFIG = {
    "notification_log": "./notification.log",  # Relative to project root
    "max_log_size_mb": 10,
    "keep_logs_days": 30,
}

# ============================================
# EMAIL MONITOR SETTINGS (Phase 4)
# ============================================

EMAIL_SCAN_CONFIG = {
    "enabled": True,
    "hours_lookback": 24,
    "max_emails_per_scan": 50,
    "docs_base_path": "./pm-docs",
    "scan_times": ["09:00", "15:00"],
}

# Keywords indicating blocker resolution
RESOLUTION_KEYWORDS = [
    "attached", "here is", "completed", "finished",
    "done", "ready", "sent", "enclosed", "please find"
]

# Keywords indicating need for more info (escalation)
ESCALATION_KEYWORDS = [
    "need more", "additional", "question", "clarify",
    "missing", "waiting", "require", "please provide"
]

# Patterns to ignore (spam/newsletters)
IGNORE_PATTERNS = [
    "unsubscribe", "no-reply@", "noreply@", "newsletter",
    "marketing", "automated message"
]

# Keywords for project relevance scoring
RELEVANCE_KEYWORDS = [
    "project", "quote", "invoice", "receipt", "proposal", "contract"
]
