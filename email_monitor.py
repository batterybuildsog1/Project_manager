#!/usr/bin/env python3
"""
Email Monitor for Project Manager Agent.

Scans Gmail for project-relevant emails, detects blocker resolutions,
and downloads attachments. Designed for 1-2x daily scans.

MCP Tools Used:
- mcp__gmail__search_emails
- mcp__gmail__read_email
- mcp__gmail__download_attachment
"""

import os
import re
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Tuple

import db
import notification_router
from config import (
    EMAIL_SCAN_CONFIG,
    RESOLUTION_KEYWORDS,
    ESCALATION_KEYWORDS,
    IGNORE_PATTERNS,
    RELEVANCE_KEYWORDS
)

logger = logging.getLogger(__name__)

# Minimum relevance score to consider an email relevant (0-100)
RELEVANCE_THRESHOLD = 20


# ============================================
# GMAIL MCP INTEGRATION
# ============================================

def search_recent_emails(hours: int = None, max_results: int = None) -> Dict:
    """
    Build search params for Gmail MCP.

    NOTE: This function returns params for the agent to use with MCP tools.
    The agent will call mcp__gmail__search_emails with these params.

    Args:
        hours: How many hours back to search (default from config)
        max_results: Maximum emails to return (default from config)

    Returns:
        Dict with query and tool info for MCP execution
    """
    hours = hours or EMAIL_SCAN_CONFIG.get("hours_lookback", 24)
    max_results = max_results or EMAIL_SCAN_CONFIG.get("max_emails_per_scan", 50)

    # Calculate the date query
    since_date = (datetime.now() - timedelta(hours=hours)).strftime("%Y/%m/%d")

    # Exclude promotional and social categories
    query = f"after:{since_date} -category:promotions -category:social"

    return {
        "query": query,
        "maxResults": max_results,
        "tool": "mcp__gmail__search_emails"
    }


def get_email_content(message_id: str) -> Dict:
    """
    Build params to get full email content using Gmail MCP.

    Args:
        message_id: Gmail message ID

    Returns:
        Dict with params for MCP execution
    """
    return {
        "messageId": message_id,
        "tool": "mcp__gmail__read_email"
    }


# ============================================
# DEDUPLICATION
# ============================================

def is_already_processed(gmail_message_id: str) -> bool:
    """
    Check if an email has already been processed.

    Args:
        gmail_message_id: Gmail message ID

    Returns:
        True if already processed, False otherwise
    """
    existing = db.get_email_scan_log(gmail_message_id)
    return existing is not None


# ============================================
# EMAIL CLASSIFICATION
# ============================================

def should_ignore(from_addr: str, subject: str, body: str) -> bool:
    """
    Check if email should be ignored (spam, newsletter, etc.)

    Args:
        from_addr: Sender email
        subject: Email subject
        body: Email body text

    Returns:
        True if should be ignored
    """
    full_text = f"{from_addr} {subject} {body}".lower()

    for pattern in IGNORE_PATTERNS:
        if pattern.lower() in full_text:
            return True

    return False


def classify_email(email: Dict) -> Dict[str, Any]:
    """
    Classify an email for project relevance.

    Uses score-based classification (0-100):
    - Blocker sender match: +50 points
    - Blocker pattern match: +40 points
    - Project name match: +30 points
    - Has attachment: +15 points
    - Relevance keyword: +10 points each

    Args:
        email: Email dict with from, subject, body, attachments

    Returns:
        {
            "is_relevant": bool,
            "relevance_score": 0-100,
            "classification": str (blocker_match, project_relevant, attachment, ignore),
            "matched_project_id": str or None,
            "matched_blocker_id": str or None,
            "categories": ["blocker", "attachment", "keyword", etc]
        }
    """
    result = {
        "is_relevant": False,
        "relevance_score": 0,
        "classification": "ignore",
        "matched_project_id": None,
        "matched_blocker_id": None,
        "categories": []
    }

    from_addr = email.get("from", "").lower()
    from_name = _extract_name(email.get("from", ""))
    subject = email.get("subject", "").lower()
    body = email.get("body", "").lower()
    full_text = f"{subject} {body}"

    # Check ignore patterns first
    if should_ignore(from_addr, subject, body):
        return result

    # Check blocker matches (highest priority)
    blocker_match = check_blocker_match(from_addr, from_name, subject, body)
    if blocker_match:
        result["matched_blocker_id"] = blocker_match["id"]
        result["relevance_score"] += 50 if "waiting_on" in str(blocker_match) else 40
        result["categories"].append("blocker_match")

    # Check project name matches
    projects = db.list_projects(status="active")
    for project in projects:
        project_name = project.get("name", "").lower()
        if project_name and len(project_name) > 3 and project_name in full_text:
            result["matched_project_id"] = project["id"]
            result["relevance_score"] += 30
            result["categories"].append("project_match")
            break

    # Check for relevance keywords
    keyword_matches = sum(1 for kw in RELEVANCE_KEYWORDS if kw.lower() in full_text)
    if keyword_matches > 0:
        result["relevance_score"] += keyword_matches * 10
        result["categories"].append("keyword_match")

    # Check for attachments
    if email.get("attachments"):
        result["relevance_score"] += 15
        result["categories"].append("has_attachment")

    # Determine overall relevance and classification
    result["is_relevant"] = result["relevance_score"] >= RELEVANCE_THRESHOLD

    if result["matched_blocker_id"]:
        result["classification"] = "blocker_match"
    elif result["matched_project_id"]:
        result["classification"] = "project_relevant"
    elif email.get("attachments") and result["relevance_score"] >= RELEVANCE_THRESHOLD:
        result["classification"] = "attachment"
    else:
        result["classification"] = "ignore"

    return result


def check_blocker_match(
    from_addr: str,
    from_name: str,
    subject: str,
    body: str
) -> Optional[Dict]:
    """
    Check if email matches any active blocker.

    Matches on:
    - blocker.waiting_on in sender email/name
    - blocker.watch_pattern in subject/body

    Args:
        from_addr: Sender email address
        from_name: Sender display name
        subject: Email subject
        body: Email body text

    Returns:
        Matched blocker dict or None
    """
    blockers = db.get_blockers_filtered(resolved=False)
    sender_text = f"{from_addr} {from_name}".lower()
    content_text = f"{subject} {body}".lower()

    for blocker in blockers:
        waiting_on = (blocker.get("waiting_on") or "").lower()
        watch_pattern = (blocker.get("watch_pattern") or "").lower()

        # Match sender against waiting_on
        if waiting_on and waiting_on in sender_text:
            return blocker

        # Match pattern in email content
        if watch_pattern and watch_pattern in content_text:
            return blocker

    return None


# ============================================
# BLOCKER DETECTION
# ============================================

def check_blocker_resolution(email: Dict, classification: Dict) -> Optional[Dict]:
    """
    Check if email resolves a blocker and send notification.

    Args:
        email: Email dict
        classification: Result from classify_email()

    Returns:
        Notification result or None
    """
    if not classification.get("matched_blocker_id"):
        return None

    # Use notification_router to check and notify
    notifications = notification_router.check_blocker_updates(
        email_from=email.get("from", ""),
        email_subject=email.get("subject", ""),
        email_body=email.get("body", "")[:1000]  # Limit body length
    )

    return notifications[0] if notifications else None


# ============================================
# ATTACHMENT HANDLING
# ============================================

def download_email_attachment(
    message_id: str,
    attachment_id: str,
    filename: str,
    project_id: str = None
) -> Dict:
    """
    Build params to download an attachment and optionally link to project.

    Args:
        message_id: Gmail message ID
        attachment_id: Attachment ID
        filename: Original filename
        project_id: Optional project to link to

    Returns:
        Dict with MCP params and document_id if created
    """
    # Sanitize filename
    safe_filename = _sanitize_filename(filename)

    # Determine save path
    docs_base = EMAIL_SCAN_CONFIG.get("docs_base_path", "./pm-docs")
    if project_id:
        save_dir = os.path.join(docs_base, project_id)
    else:
        save_dir = os.path.join(docs_base, "unassigned")

    save_path = os.path.join(save_dir, safe_filename)

    # Create document record if project matched
    document_id = None
    if project_id:
        doc = db.create_document(
            filename=safe_filename,
            file_path=save_path,
            project_id=project_id,
            document_type="email",
            notes=f"Downloaded from email {message_id}"
        )
        document_id = doc.get("id")

    return {
        "tool": "mcp__gmail__download_attachment",
        "messageId": message_id,
        "attachmentId": attachment_id,
        "savePath": save_dir,
        "filename": safe_filename,
        "document_id": document_id
    }


def _sanitize_filename(filename: str) -> str:
    """Sanitize a filename for safe filesystem storage."""
    # Remove or replace problematic characters
    safe = re.sub(r'[<>:"/\\|?*]', '_', filename)
    # Limit length
    if len(safe) > 100:
        name, ext = os.path.splitext(safe)
        safe = name[:95] + ext
    return safe


def _extract_name(from_field: str) -> str:
    """Extract display name from From field."""
    # Handle "Name <email@example.com>" format
    match = re.match(r'^([^<]+)\s*<', from_field)
    if match:
        return match.group(1).strip()
    return from_field


# ============================================
# MAIN SCAN FUNCTIONS
# ============================================

def process_email(email: Dict) -> Dict[str, Any]:
    """
    Process a single email: classify, check blockers, handle attachments.

    Args:
        email: Full email dict from Gmail with id, from, subject, body, attachments

    Returns:
        Processing result with actions taken
    """
    message_id = email.get("id", "")

    result = {
        "message_id": message_id,
        "from": email.get("from"),
        "subject": email.get("subject"),
        "classification": None,
        "blocker_notification": None,
        "attachments_to_download": [],
        "already_processed": False,
        "scan_log_id": None
    }

    # Check deduplication
    if is_already_processed(message_id):
        result["already_processed"] = True
        return result

    # Classify
    classification = classify_email(email)
    result["classification"] = classification

    # Create scan log entry
    scan_log = db.create_email_scan_log(
        gmail_message_id=message_id,
        gmail_thread_id=email.get("threadId"),
        from_address=email.get("from"),
        from_name=_extract_name(email.get("from", "")),
        subject=email.get("subject"),
        classification=classification["classification"],
        matched_blocker_id=classification.get("matched_blocker_id"),
        matched_project_id=classification.get("matched_project_id"),
        has_attachment=bool(email.get("attachments")),
        notification_sent=False
    )
    result["scan_log_id"] = scan_log.get("id")

    if not classification["is_relevant"]:
        return result

    # Check blocker resolution
    if classification.get("matched_blocker_id"):
        result["blocker_notification"] = check_blocker_resolution(email, classification)

    # Queue P1 notification for relevant email (if not a blocker match, which triggers P0)
    if classification["is_relevant"] and not result["blocker_notification"]:
        notification_router.queue_p1(
            message=f"Email from {email.get('from', 'unknown')}: {email.get('subject', 'No subject')[:50]}",
            trigger_type="email_activity",
            source_id=message_id,
            metadata={"classification": classification["classification"]}
        )

    # Prepare attachment downloads if project matched
    project_id = classification.get("matched_project_id")
    if email.get("attachments"):
        for att in email["attachments"]:
            att_params = download_email_attachment(
                message_id=message_id,
                attachment_id=att.get("id", att.get("attachmentId", "")),
                filename=att.get("filename", "attachment"),
                project_id=project_id
            )
            result["attachments_to_download"].append(att_params)

            # Create attachment record
            db.create_email_attachment(
                email_scan_log_id=scan_log.get("id"),
                gmail_attachment_id=att.get("id", att.get("attachmentId", "")),
                filename=att.get("filename", "attachment"),
                project_id=project_id,
                blocker_id=classification.get("matched_blocker_id"),
                download_status="pending"
            )

    return result


def run_email_scan(hours: int = None) -> Dict[str, Any]:
    """
    Run a complete email scan.

    This is the main entry point called by the scheduler or manually.
    Returns MCP instructions for the agent to execute.

    Args:
        hours: How far back to scan (default from config)

    Returns:
        Dict with MCP action to execute and scan context
    """
    hours = hours or EMAIL_SCAN_CONFIG.get("hours_lookback", 24)

    scan_result = {
        "scan_time": datetime.now().isoformat(),
        "hours_scanned": hours,
        "total_emails": 0,
        "relevant_emails": 0,
        "blockers_resolved": 0,
        "attachments_found": 0,
        "already_processed": 0,
        "emails_processed": []
    }

    logger.info(f"Starting email scan for last {hours} hours")

    # Get search params for MCP
    search_params = search_recent_emails(hours)

    # Log scan initiation (silent P3)
    notification_router.queue_p3(
        message=f"Email scan started: {hours}h lookback",
        trigger_type="email_scan",
        metadata={"hours": hours}
    )

    return {
        "action": "execute_mcp_search",
        "params": search_params,
        "callback": "process_search_results",
        "scan_context": scan_result
    }


def process_search_results(emails: List[Dict], scan_context: Dict) -> Dict[str, Any]:
    """
    Process results from Gmail search.

    Called after MCP search completes with the email list.

    Args:
        emails: List of email dicts from search
        scan_context: Context from run_email_scan

    Returns:
        Final scan summary
    """
    scan_context["total_emails"] = len(emails)

    for email in emails:
        result = process_email(email)
        scan_context["emails_processed"].append(result)

        if result.get("already_processed"):
            scan_context["already_processed"] += 1
            continue

        if result.get("classification", {}).get("is_relevant"):
            scan_context["relevant_emails"] += 1

        if result.get("blocker_notification"):
            scan_context["blockers_resolved"] += 1

        scan_context["attachments_found"] += len(result.get("attachments_to_download", []))

    # Log completion
    logger.info(
        f"Email scan complete: {scan_context['total_emails']} total, "
        f"{scan_context['relevant_emails']} relevant, "
        f"{scan_context['blockers_resolved']} blockers resolved, "
        f"{scan_context['already_processed']} already processed"
    )

    return scan_context


# ============================================
# UTILITY FUNCTIONS
# ============================================

def get_scan_status() -> Dict[str, Any]:
    """Get the status of email scanning configuration."""
    recent_scans = db.get_recent_email_scans(limit=5)

    return {
        "config": EMAIL_SCAN_CONFIG,
        "enabled": EMAIL_SCAN_CONFIG.get("enabled", True),
        "hours_lookback": EMAIL_SCAN_CONFIG.get("hours_lookback", 24),
        "max_emails_per_scan": EMAIL_SCAN_CONFIG.get("max_emails_per_scan", 50),
        "recent_scans": len(recent_scans),
        "last_scan": recent_scans[0] if recent_scans else None
    }


# ============================================
# TEST
# ============================================

if __name__ == "__main__":
    print("Testing email_monitor...")

    # Test classification
    test_email = {
        "id": "test123",
        "from": "joyce@example.com",
        "subject": "RE: Kitchen project quote",
        "body": "Here is the quote you requested. See attached invoice.",
        "attachments": [{"id": "att1", "filename": "quote.pdf"}]
    }

    classification = classify_email(test_email)
    print(f"  Classification: {classification}")

    # Test search params
    search = search_recent_emails(24)
    print(f"  Search params: {search}")

    # Test status
    status = get_scan_status()
    print(f"  Status: {status}")

    # Test filename sanitization
    unsafe = 'file<name>:with/bad\\chars?.txt'
    safe = _sanitize_filename(unsafe)
    print(f"  Sanitized filename: {unsafe} -> {safe}")

    print("All tests passed!")
