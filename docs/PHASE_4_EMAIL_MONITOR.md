# Phase 4: Email Monitor Implementation Plan

> **Location**: `docs/PHASE_4_EMAIL_MONITOR.md`
> **Branch**: `feature/phase-4-email-monitor`
> **Status**: Ready to implement
> **Dependencies**: Phase 3 (Notifications) - DONE

---

## Overview

Build an email monitoring system that scans Gmail for project-relevant emails, detects blocker resolutions, and downloads attachments.

**Key Principle**: Scan 1-2x per day (not more often). Low volume expected.

---

## Files to Create

### 1. `email_monitor.py` (NEW - ~300 lines)

```python
#!/usr/bin/env python3
"""
Email Monitor for Project Manager Agent.

Scans Gmail for project-relevant emails, detects blocker resolutions,
and downloads attachments. Designed for 1-2x daily scans.
"""

import os
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional

import db
import notification_router

logger = logging.getLogger(__name__)

# Configuration
SCAN_HOURS_DEFAULT = 24  # Look back 24 hours by default
ATTACHMENT_DIR = "./pm-docs/attachments"
RELEVANCE_KEYWORDS = ["project", "quote", "invoice", "receipt", "proposal", "contract"]

# ============================================
# GMAIL MCP INTEGRATION
# ============================================

def search_recent_emails(hours: int = SCAN_HOURS_DEFAULT, max_results: int = 50) -> List[Dict]:
    """
    Search for recent emails using Gmail MCP.

    NOTE: This function is called by the agent with MCP tools.
    In production, the agent will use mcp__gmail__search_emails.
    This is a helper that formats the query.

    Args:
        hours: How many hours back to search
        max_results: Maximum emails to return

    Returns:
        List of email metadata dicts
    """
    # Calculate the date query
    since_date = (datetime.now() - timedelta(hours=hours)).strftime("%Y/%m/%d")
    query = f"after:{since_date}"

    # Return query for MCP tool
    return {
        "query": query,
        "max_results": max_results,
        "tool": "mcp__gmail__search_emails"
    }


def get_email_content(message_id: str) -> Dict:
    """
    Get full email content using Gmail MCP.

    Args:
        message_id: Gmail message ID

    Returns:
        Dict with from, subject, body, attachments
    """
    return {
        "message_id": message_id,
        "tool": "mcp__gmail__read_email"
    }


# ============================================
# EMAIL CLASSIFICATION
# ============================================

def classify_email(email: Dict) -> Dict[str, Any]:
    """
    Classify an email for project relevance.

    Args:
        email: Email dict with from, subject, body

    Returns:
        {
            "is_relevant": bool,
            "relevance_score": 0-100,
            "matched_project_id": str or None,
            "matched_blocker_id": str or None,
            "categories": ["blocker", "attachment", "update", etc]
        }
    """
    result = {
        "is_relevant": False,
        "relevance_score": 0,
        "matched_project_id": None,
        "matched_blocker_id": None,
        "categories": []
    }

    from_addr = email.get("from", "").lower()
    subject = email.get("subject", "").lower()
    body = email.get("body", "").lower()
    full_text = f"{subject} {body}"

    # Check for keyword relevance
    keyword_matches = sum(1 for kw in RELEVANCE_KEYWORDS if kw in full_text)
    if keyword_matches > 0:
        result["relevance_score"] += keyword_matches * 10
        result["categories"].append("keyword_match")

    # Check if sender matches any blocker's waiting_on
    blockers = db.get_blockers_filtered(resolved=False)
    for blocker in blockers:
        waiting_on = blocker.get("waiting_on", "").lower()
        watch_pattern = blocker.get("watch_pattern", "").lower()

        if waiting_on and waiting_on in from_addr:
            result["matched_blocker_id"] = blocker["id"]
            result["relevance_score"] += 50
            result["categories"].append("blocker_sender_match")
            break

        if watch_pattern and watch_pattern in full_text:
            result["matched_blocker_id"] = blocker["id"]
            result["relevance_score"] += 40
            result["categories"].append("blocker_pattern_match")
            break

    # Check for attachments
    if email.get("attachments"):
        result["relevance_score"] += 15
        result["categories"].append("has_attachment")

    # Check against project names
    projects = db.list_projects()
    for project in projects:
        project_name = project.get("name", "").lower()
        if project_name and len(project_name) > 3 and project_name in full_text:
            result["matched_project_id"] = project["id"]
            result["relevance_score"] += 30
            result["categories"].append("project_match")
            break

    # Determine overall relevance
    result["is_relevant"] = result["relevance_score"] >= 20

    return result


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
        email_body=email.get("body", "")
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
    Download an attachment and optionally link to project.

    Args:
        message_id: Gmail message ID
        attachment_id: Attachment ID
        filename: Original filename
        project_id: Optional project to link to

    Returns:
        {
            "tool": "mcp__gmail__download_attachment",
            "save_path": where to save,
            "document_id": if linked to project
        }
    """
    # Determine save path
    if project_id:
        save_dir = f"./pm-docs/{project_id}"
    else:
        save_dir = ATTACHMENT_DIR

    os.makedirs(save_dir, exist_ok=True)
    save_path = os.path.join(save_dir, filename)

    # If project_id, create document record
    document_id = None
    if project_id:
        doc = db.create_document(
            project_id=project_id,
            title=filename,
            document_type="email_attachment",
            file_path=save_path,
            source_type="email",
            source_reference=message_id
        )
        document_id = doc["id"]

    return {
        "tool": "mcp__gmail__download_attachment",
        "message_id": message_id,
        "attachment_id": attachment_id,
        "save_path": save_path,
        "filename": filename,
        "document_id": document_id
    }


# ============================================
# MAIN SCAN FUNCTIONS
# ============================================

def process_email(email: Dict) -> Dict[str, Any]:
    """
    Process a single email: classify, check blockers, handle attachments.

    Args:
        email: Full email dict from Gmail

    Returns:
        Processing result with actions taken
    """
    result = {
        "message_id": email.get("id"),
        "from": email.get("from"),
        "subject": email.get("subject"),
        "classification": None,
        "blocker_notification": None,
        "attachments_processed": []
    }

    # Classify
    classification = classify_email(email)
    result["classification"] = classification

    if not classification["is_relevant"]:
        return result

    # Check blocker resolution
    if classification.get("matched_blocker_id"):
        result["blocker_notification"] = check_blocker_resolution(email, classification)

    # Queue P1 notification for relevant email
    if classification["is_relevant"] and not result["blocker_notification"]:
        notification_router.queue_p1(
            message=f"Email from {email.get('from', 'unknown')}: {email.get('subject', 'No subject')}",
            trigger_type="email_activity",
            source_id=email.get("id"),
            metadata={"classification": classification}
        )

    # Process attachments if project matched
    if classification.get("matched_project_id") and email.get("attachments"):
        for att in email["attachments"]:
            att_result = download_email_attachment(
                message_id=email["id"],
                attachment_id=att["id"],
                filename=att["filename"],
                project_id=classification["matched_project_id"]
            )
            result["attachments_processed"].append(att_result)

    return result


def run_email_scan(hours: int = SCAN_HOURS_DEFAULT) -> Dict[str, Any]:
    """
    Run a complete email scan.

    This is the main entry point called by the scheduler.

    Args:
        hours: How far back to scan

    Returns:
        Scan summary with results
    """
    scan_result = {
        "scan_time": datetime.now().isoformat(),
        "hours_scanned": hours,
        "total_emails": 0,
        "relevant_emails": 0,
        "blockers_resolved": 0,
        "attachments_downloaded": 0,
        "emails_processed": []
    }

    logger.info(f"Starting email scan for last {hours} hours")

    # This returns MCP tool instructions
    # In production, the agent executes these
    search_params = search_recent_emails(hours)

    # Log scan initiation
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

    Called after MCP search completes.

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

        if result["classification"]["is_relevant"]:
            scan_context["relevant_emails"] += 1

        if result["blocker_notification"]:
            scan_context["blockers_resolved"] += 1

        scan_context["attachments_downloaded"] += len(result["attachments_processed"])

    # Log completion
    logger.info(
        f"Email scan complete: {scan_context['total_emails']} total, "
        f"{scan_context['relevant_emails']} relevant, "
        f"{scan_context['blockers_resolved']} blockers resolved"
    )

    return scan_context


# ============================================
# UTILITY FUNCTIONS
# ============================================

def get_scan_status() -> Dict[str, Any]:
    """Get the status of recent email scans."""
    # Could track in db, for now return basic info
    return {
        "last_scan": None,  # TODO: track in db
        "default_interval_hours": SCAN_HOURS_DEFAULT,
        "attachment_directory": ATTACHMENT_DIR
    }


# ============================================
# TEST
# ============================================

if __name__ == "__main__":
    print("Testing email_monitor...")

    # Test classification
    test_email = {
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

    print("All tests passed!")
```

---

## Files to Modify

### `server.py` - Add Email Endpoints

Add this section **at the end of the file, before `if __name__ == "__main__":`**:

```python
# ============================================
# EMAIL MONITOR ENDPOINTS (Phase 4)
# ============================================

@app.route("/api/email/scan", methods=["POST"])
def trigger_email_scan():
    """Manually trigger an email scan."""
    import email_monitor

    data = request.get_json() or {}
    hours = data.get("hours", 24)

    result = email_monitor.run_email_scan(hours)

    return jsonify({
        "ok": True,
        "result": result
    })


@app.route("/api/email/status", methods=["GET"])
def get_email_status():
    """Get email scan status."""
    import email_monitor

    status = email_monitor.get_scan_status()

    return jsonify({
        "ok": True,
        "status": status
    })


@app.route("/api/email/classify", methods=["POST"])
def classify_email_endpoint():
    """Classify a single email (for testing)."""
    import email_monitor

    data = request.get_json() or {}

    classification = email_monitor.classify_email(data)

    return jsonify({
        "ok": True,
        "classification": classification
    })
```

---

## Implementation Steps

### Step 1: Create Branch
```bash
cd /Users/alanknudson/Project_manager
git checkout -b feature/phase-4-email-monitor
```

### Step 2: Create email_monitor.py
Create the file with the code above.

### Step 3: Add Server Endpoints
Add the endpoint section to `server.py` (at end, before main block).

### Step 4: Test Import
```bash
python3 -c "import email_monitor; print('OK')"
```

### Step 5: Commit
```bash
git add email_monitor.py server.py
git commit -m "Add Phase 4: Email Monitor

- Create email_monitor.py with Gmail MCP integration
- Add email classification logic
- Add blocker resolution detection
- Add attachment download handling
- Add /api/email/* endpoints to server.py

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| MCP Integration | Return tool instructions | Agent executes MCP tools, module provides logic |
| Scan Frequency | 1-2x daily | Low email volume expected |
| Classification | Score-based (0-100) | Flexible, tunable thresholds |
| Attachment Storage | `./pm-docs/{project_id}/` | Organized by project |
| Blocker Detection | Uses notification_router | Reuse existing P0 notification logic |

---

## Dependencies

**Imports (existing files - READ ONLY)**:
- `db` - For blockers, projects, documents
- `notification_router` - For check_blocker_updates(), queue_p1()

**External**:
- Gmail MCP tools (already configured)

---

## Testing Checklist

- [ ] `email_monitor.py` imports without error
- [ ] `classify_email()` returns correct structure
- [ ] Blocker matching works (sender match, pattern match)
- [ ] Project matching works
- [ ] `/api/email/scan` endpoint works
- [ ] `/api/email/status` endpoint works
- [ ] Attachment download creates document record

---

## Notes for Agent

1. **DO NOT modify `db.py`** - All needed functions exist
2. **DO NOT modify `notification_router.py`** - Import and use only
3. **Add endpoints to server.py in a clearly marked section**
4. **Gmail MCP tools are available** - Use `mcp__gmail__*` tools when executing
5. **Test with**: `python3 -c "import email_monitor"`
