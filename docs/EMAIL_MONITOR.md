# Email Monitor - Phase 4

> **Status**: IMPLEMENTED
> **Date**: 2025-12-05
> **Location**: `/Users/alanknudson/Project_manager/email_monitor.py`

---

## Overview

Email monitoring system that scans Gmail 1-2x daily for project-relevant emails, detects blocker resolutions, and downloads attachments.

**Key Features**:
- Score-based email classification (0-100, threshold 20)
- Blocker resolution detection via sender and pattern matching
- Automatic P0 notifications for resolved blockers
- P1 batched notifications for relevant emails
- Attachment tracking and download management
- Deduplication to prevent re-processing

---

## Files

| File | Purpose |
|------|---------|
| `email_monitor.py` | Core module (~350 lines) |
| `db.py` | Added 2 tables, 5 functions |
| `config.py` | Added EMAIL_SCAN_CONFIG and keyword lists |
| `server.py` | Added 3 API endpoints |

---

## Database Schema

### `email_scan_log` - Track Processed Emails

```sql
CREATE TABLE IF NOT EXISTS email_scan_log (
    id TEXT PRIMARY KEY,
    gmail_message_id TEXT UNIQUE NOT NULL,
    gmail_thread_id TEXT,
    from_address TEXT,
    from_name TEXT,
    subject TEXT,
    received_at TEXT,
    classification TEXT CHECK (classification IN (
        'blocker_match', 'project_relevant', 'attachment', 'ignore'
    )),
    matched_blocker_id TEXT REFERENCES blockers(id),
    matched_project_id TEXT REFERENCES projects(id),
    processed_at TEXT DEFAULT CURRENT_TIMESTAMP,
    has_attachment INTEGER DEFAULT 0,
    attachment_downloaded INTEGER DEFAULT 0,
    notification_sent INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

### `email_attachments` - Track Downloaded Files

```sql
CREATE TABLE IF NOT EXISTS email_attachments (
    id TEXT PRIMARY KEY,
    email_scan_log_id TEXT REFERENCES email_scan_log(id),
    document_id TEXT REFERENCES documents(id),
    gmail_attachment_id TEXT,
    filename TEXT NOT NULL,
    mime_type TEXT,
    file_size_bytes INTEGER,
    local_path TEXT,
    download_status TEXT CHECK (download_status IN ('pending', 'downloaded', 'failed')),
    download_error TEXT,
    blocker_id TEXT REFERENCES blockers(id),
    project_id TEXT REFERENCES projects(id),
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

---

## Configuration

In `config.py`:

```python
EMAIL_SCAN_CONFIG = {
    "enabled": True,
    "hours_lookback": 24,
    "max_emails_per_scan": 50,
    "docs_base_path": "./pm-docs",
    "scan_times": ["09:00", "15:00"],
}

RESOLUTION_KEYWORDS = [
    "attached", "here is", "completed", "finished",
    "done", "ready", "sent", "enclosed", "please find"
]

ESCALATION_KEYWORDS = [
    "need more", "additional", "question", "clarify",
    "missing", "waiting", "require", "please provide"
]

IGNORE_PATTERNS = [
    "unsubscribe", "no-reply@", "noreply@", "newsletter",
    "marketing", "automated message"
]

RELEVANCE_KEYWORDS = [
    "project", "quote", "invoice", "receipt", "proposal", "contract"
]
```

---

## Scoring System

| Match Type | Points |
|------------|--------|
| Blocker sender match (`waiting_on` in sender) | +50 |
| Blocker pattern match (`watch_pattern` in content) | +40 |
| Project name match | +30 |
| Has attachment | +15 |
| Relevance keyword (each) | +10 |
| **Minimum threshold** | **20** |

---

## Core Functions

```python
# Gmail MCP integration (returns params for agent to execute)
search_recent_emails(hours=24, max_results=50) -> Dict
get_email_content(message_id) -> Dict

# Classification
classify_email(email) -> Dict  # Score-based classification
is_already_processed(gmail_message_id) -> bool  # Deduplication

# Blocker detection
check_blocker_match(from_addr, from_name, subject, body) -> Optional[Dict]
check_blocker_resolution(email, classification) -> Optional[Dict]

# Attachments
download_email_attachment(message_id, attachment_id, filename, project_id=None) -> Dict

# Main flow
process_email(email) -> Dict
run_email_scan(hours=24) -> Dict  # Entry point
process_search_results(emails, scan_context) -> Dict  # Callback after MCP search

# Utility
get_scan_status() -> Dict
```

---

## API Endpoints

### `POST /api/email/scan`
Trigger manual email scan.

```bash
curl -X POST http://localhost:4000/api/email/scan \
  -H "Content-Type: application/json" \
  -d '{"hours": 24}'
```

### `GET /api/email/status`
Get scan configuration and status.

```bash
curl http://localhost:4000/api/email/status
```

### `POST /api/email/classify`
Test email classification.

```bash
curl -X POST http://localhost:4000/api/email/classify \
  -H "Content-Type: application/json" \
  -d '{
    "from": "joyce@example.com",
    "subject": "Quote attached",
    "body": "Here is the quote you requested",
    "attachments": [{"id": "1", "filename": "quote.pdf"}]
  }'
```

---

## Workflow

```
1. Agent/Scheduler calls email_monitor.run_email_scan()
         |
         v
2. Module returns MCP params for Gmail search
         |
         v
3. Agent executes mcp__gmail__search_emails
         |
         v
4. Agent calls email_monitor.process_search_results() with results
         |
         v
5. For each email:
   - Check deduplication (skip if already processed)
   - Classify email (score-based)
   - Log to email_scan_log table
   - If blocker match -> notification_router.check_blocker_updates() -> P0
   - If relevant (non-blocker) -> notification_router.queue_p1()
   - If attachments -> queue download params
         |
         v
6. Return summary with attachment download instructions
```

---

## Integration Points

1. **`notification_router.py:318`** - `check_blocker_updates(email_from, email_subject, email_body)` for blocker resolution
2. **`db.py`** - `get_blockers_filtered(resolved=False)` and `list_projects()` for matching
3. **Gmail MCP** - `mcp__gmail__search_emails`, `mcp__gmail__read_email`, `mcp__gmail__download_attachment`

---

## DB Helper Functions

```python
# In db.py
get_email_scan_log(gmail_message_id) -> Optional[Dict]
create_email_scan_log(gmail_message_id, gmail_thread_id, from_address, ...) -> Dict
get_recent_email_scans(limit=50) -> List[Dict]
create_email_attachment(email_scan_log_id, gmail_attachment_id, filename, ...) -> Dict
update_email_attachment(attachment_id, **kwargs) -> Optional[Dict]
```

---

## Blocker Resolution Flow

1. Task "Get pricing from Joyce" is blocked with `waiting_on="joyce"`, `watch_pattern="pricing"`
2. Email scan runs at 9am
3. Email from `joyce@example.com` with subject "RE: Pricing - attached" found
4. `check_blocker_match()` matches blocker (+50 sender, +10 keyword = 60 points)
5. `notification_router.check_blocker_updates()` called
6. Resolution keywords ("attached") detected -> blocker auto-resolved
7. P0 notification sent: "UNBLOCKED: Get pricing from Joyce - Email from joyce@example.com"
8. Attachment queued for download to `./pm-docs/{project_id}/`

---

## Attachment Storage

```
./pm-docs/
├── {project_id}/       # If project matched
│   ├── quote.pdf
│   └── contract.docx
└── unassigned/         # If no project match
    └── misc.pdf
```

---

## Next Steps

- **Phase 9 (Scheduler)**: Will call `email_monitor.run_email_scan()` at 9am and 3pm daily
- **Phase 6 (RAG)**: Attachments can be indexed for retrieval

---

## Test

```bash
python3 email_monitor.py
```

Output:
```
Testing email_monitor...
  Classification: {'is_relevant': True, 'relevance_score': 45, ...}
  Search params: {'query': 'after:2025/12/04 -category:promotions -category:social', ...}
  Status: {'enabled': True, 'hours_lookback': 24, ...}
  Sanitized filename: file<name>:with/bad\chars?.txt -> file_name__with_bad_chars_.txt
All tests passed!
```
