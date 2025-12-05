# Project Manager Agent - Full System

> **Location**: `docs/PROJECT_ROADMAP.md`
> **Last Updated**: 2025-12-05

## Related Specs

| Spec | Location | Status |
|------|----------|--------|
| Memory Management | `docs/MEMORY_MANAGEMENT.md` | **DONE** |
| Notification System | `docs/NOTIFICATION_SYSTEM.md` | **DONE** |

---

## EXECUTION TRACKER

### Current Status (RE-ORDERED)

**Rationale**: Scheduler comes before Dashboard because the dashboard displays scheduler activity.

| Phase | Name | Status | Dependencies | Files |
|-------|------|--------|--------------|-------|
| 1 | Database Schema | DONE | - | `db.py` |
| 2 | Task Management | DONE | 1 | `task_manager.py`, `toc_engine.py` |
| **2.5** | **Memory** | **DONE** | **1** | **`memory_manager.py`** |
| 3 | Notifications | **DONE** | 1 | `notification_router.py`, `config.py` |
| 4 | Email Monitor | Ready | 3 | `email_monitor.py` |
| 5 | Recurring Tasks | Ready | 2 | Uses existing `db.py` |
| 6 | Documents/RAG | Ready | 1 | `document_manager.py`, `rag_engine.py` |
| 7 | Autonomous Exec | Blocked | 3, 4 | `autonomous_executor.py` |
| 8 | Weekly Reports | Blocked | 2, 4 | `report_generator.py` |
| **9** | **Scheduler** | **Ready** | **3, 4** | **`scheduler.py`** |
| **10** | **Dashboard UI** | **Blocked** | **9 (scheduler first!)** | **`static/dashboard.*`** |

### Parallelizable Groups (UPDATED)
**Group A** (DONE): 2.5, 3
**Group B** (READY - no dependencies): 4, 5, 6, 9
**Group C** (after 4, 9 done): 7, 8
**Group D** (LAST - needs scheduler): 10 (Dashboard)

### Agent Execution Plan

**COMPLETED: Group A**

#### Agent 1: Phase 2.5 - Memory Management ✅ DONE
```
COMPLETED:
- memory_manager.py: 60K token context, tool definitions, date parsing
- grok_client.py: chat_with_tools() for automatic tool execution
- server.py: Unified context builder

Implementation:
- 60K token active context (simple, no compaction)
- Uses simple `messages` table in db.py
- Tool-based retrieval for older messages (search_history, get_messages_by_date)
```

#### Agent 2: Phase 3 - Notification System ✅ DONE
```
COMPLETED:
- notification_router.py: P0-P3 priority routing, deduplication
- config.py: Notification settings, batch times
- server.py: Added /api/notifications/* endpoints
- task_manager.py: Hooks for task status notifications
- toc_engine.py: WIP warning notifications
- db.py: notification_dedup table, helper functions

Implementation:
- P0 IMMEDIATE: Telegram + SMS (blocker resolved, deadline <24h)
- P1 BATCHED: Telegram at 9am/1pm/5pm (task status, WIP warnings)
- P2 WEEKLY: Telegram Sunday 8pm (weekly summary)
- P3 SILENT: Log only
- Deduplication: 4h (P0), 8h (P1), 7d (P2) windows
```

---

**NEXT: Group B (after Group A completes)**

#### Agent 3: Phase 5 - Recurring Tasks
```
Update: task_manager.py (add recurring generation logic)
Create: None (uses existing recurring_schedules table in db.py)
Key: Cron-like schedules, auto-generate tasks from templates
```

#### Agent 4: Phase 6 - Document Management & RAG
```
Create: document_manager.py, rag_engine.py
Update: server.py (add document endpoints)
Key: PDF/receipt handling, chunking, JSON embeddings, cosine similarity
```

---

**THEN: After 3 completes**

#### Agent 5: Phase 4 - Email Monitor
```
Create: email_monitor.py
Uses: Gmail MCP tools (already configured)
Key: Daily scan (1-2x), project relevance classification, attachment handling
```

#### Agent 6: Phase 9 - Scheduler
```
Create: scheduler.py
Uses: APScheduler
Key: Email scans (1-2x/day), deadline checks, batch notifications, recurring task generation
```

---

**THEN: Group C (after Group B)**

#### Agent 7: Phase 7 - Autonomous Execution
```
Create: autonomous_executor.py
Key: Level 1-3 actions, approval queue, Telegram approval flow
```

#### Agent 8: Phase 8 - Weekly Reports
```
Create: report_generator.py
Key: Completed tasks, blockers, upcoming, AI recommendations
```

---

**LAST: Group D**

#### Agent 9: Phase 10 - Dashboard UI (AFTER SCHEDULER)
```
Create: static/dashboard.html, static/dashboard.css, static/dashboard.js
Update: server.py (SSE endpoint, static file serving)
Key: WIP gauge, project tree, buffer chart, blockers list, scheduler status
NOTE: Dashboard displays scheduler activity, so scheduler must come first!
```

---

## Vision
Autonomous project manager that maximizes user output by handling tasks, tracking progress, and only interrupting when intervention is needed. Built on Theory of Constraints (TOC) principles.

## User's Attention = The Bottleneck
The entire system design optimizes for protecting the user's focus:
- Agent executes autonomously where possible
- Smart notifications (not spam)
- Visual dashboard for at-a-glance status
- Weekly reports on progress and priorities

---

## Architecture Overview

```
+-------------------------------------------------------------------------+
|                         USER INTERFACES                                  |
+-------------------------------------------------------------------------+
|  Telegram (@Alan0130_bot)  |  Dashboard (localhost:4000)  |  SMS backup |
+-------------------------------------------------------------------------+
                                    |
+-------------------------------------------------------------------------+
|                       FLASK SERVER (:4000)                               |
+-------------------------------------------------------------------------+
|  Webhook Handler  |  REST APIs  |  SSE Events  |  Static Files          |
+-------------------------------------------------------------------------+
                                    |
+-------------------------------------------------------------------------+
|                        AGENT MODULES                                     |
+-------------------------------------------------------------------------+
| Grok 4.1    | Email Monitor | Task Manager | Scheduler | Report Gen    |
| (AI brain) | (Gmail MCP)   | (TOC logic)  | (APSched) | (Weekly)      |
+-------------------------------------------------------------------------+
                                    |
+-------------------------------------------------------------------------+
|                        STORAGE                                           |
+-------------------------------------------------------------------------+
|  SQLite (agent.db)  |  Document Store (./pm-docs/)  |  Embeddings|
+-------------------------------------------------------------------------+
```

---

## Implementation Phases

### Phase 1: Database & Core Schema
**Goal**: Expand SQLite with TOC-ready schema

**Files to modify/create**:
- `db.py` - Add new tables, migration from old schema

**New tables**:
- `projects` - with buffer tracking, WIP limits, constraints
- `tasks` - hierarchical with dependencies, full-kit, status
- `task_dependencies` - finish-to-start relationships
- `task_full_kit` - prerequisite checklist per task
- `documents` - file metadata, extracted text for RAG
- `document_chunks` - chunked text with embeddings (JSON vectors)
- `conversations` - project-linked message threads
- `recurring_schedules` - utility bills, maintenance tasks
- `blockers` - what's waiting on what/whom
- `autonomous_actions` - agent actions pending/executed
- `toc_metrics_snapshots` - weekly metrics for trends

**Memory strategy** (replaces 10-message limit):
See detailed **Phase 2.5: Memory Management** section below.

---

### Phase 2.5: Memory Management ✅ DONE

**Implementation** (SIMPLIFIED):
- **60K token context** - single unified limit
- **Simple `messages` table** - no project scoping needed yet
- **Tool-based retrieval** - search_history(), get_messages_by_date(), get_extended_context()
- **No compaction** - just load newest messages up to 60K tokens

**Architecture**:
```
+---------------------------------------------------------------------+
|                     ACTIVE CONTEXT (60K tokens)                      |
+---------------------------------------------------------------------+
|  * System prompt (~500 tokens)                                      |
|  * Recent messages up to 60K tokens                                 |
|  * Tool definitions for history search                              |
+---------------------------------------------------------------------+
                              |
                              | Grok calls tools when needed
                              v
+---------------------------------------------------------------------+
|                     STORAGE (SQLite messages table)                  |
+---------------------------------------------------------------------+
|  * ALL messages stored permanently                                  |
|  * Searchable via SQL LIKE                                          |
|  * Accessible via memory tools                                      |
+---------------------------------------------------------------------+
```

**Files**: `memory_manager.py`, `grok_client.py`, `server.py`

See `docs/MEMORY_MANAGEMENT.md` for full specification.

---

### Phase 2: Task Management System
**Goal**: CRUD for projects/tasks with TOC enforcement

**Files to create**:
- `task_manager.py` - Project/task CRUD, dependency resolution
- `toc_engine.py` - WIP enforcement, buffer calculations, critical chain

**Key features**:
- Hierarchical task trees (project -> phase -> task -> subtask)
- Full-kit enforcement (can't start without prerequisites)
- WIP limit: HARD cap of 1-3 concurrent tasks
- Buffer management: 50% estimates + aggregated safety time
- Critical chain identification
- Dependency resolution with feeding buffers

**API endpoints**:
```
GET/POST /api/projects
GET/POST /api/tasks
PATCH /api/tasks/{id}
POST /api/tasks/{id}/start  (checks WIP + full-kit)
POST /api/tasks/{id}/complete
POST /api/tasks/{id}/block
GET /api/wip
GET /api/buffer
```

---

### Phase 3: Notification System
**Goal**: Smart notifications that respect user's attention

**Files to create**:
- `notification_router.py` - Priority routing logic
- `config.py` - Notification preferences

**Priority levels**:
| Priority | Trigger | Channel | Timing |
|----------|---------|---------|--------|
| P0 IMMEDIATE | Unblocks critical path, urgent deadline | Telegram + SMS | Instant |
| P1 SAME-DAY | Important but not urgent | Telegram | Batched 3x/day |
| P2 WEEKLY | Status updates | Weekly report | Sunday 8pm |
| P3 SILENT | Background activity | Log only | Never |

**P0 triggers**:
- Email received that matches tracked blocker
- Deadline <24h with incomplete prerequisites
- Delivery requiring immediate action

**Batching**: 9am, 1pm, 5pm daily digests

---

### Phase 4: Email Monitoring
**Goal**: Watch Gmail for project-relevant updates

**Files to create**:
- `email_monitor.py` - Gmail scanning logic

**Integration**: Uses existing Gmail MCP tools

**Monitoring strategy**:
- **Scan frequency**: 1-2x per day (not more often)
- **Per scan**: Look for project-relevant emails
- **Expected volume**: Usually 1+ relevant emails, sometimes 0, occasionally several
- Classify emails by project relevance
- Auto-download attachments to staging
- Meeting notes from Gemini arrive as Gmail attachments -> parsed

**Blocker resolution flow**:
1. Task blocked waiting on "Joyce pricing"
2. Daily email scan finds email from Joyce with attachment
3. Agent: P0 notification "Unblocked: Joyce sent pricing"
4. Auto-download attachment, link to project

---

### Phase 5: Recurring Tasks
**Goal**: Utility bills, maintenance schedules

**Implementation**:
- `recurring_schedules` table with cron-like patterns
- Scheduler generates tasks automatically
- Examples: "Pay electric bill" monthly on 15th, "Check receipts" weekly

---

### Phase 6: Document Management & RAG
**Goal**: Handle receipts, invoices, quotes, PDFs

**Files to create**:
- `document_manager.py` - CRUD, chunking, embedding
- `rag_engine.py` - Retrieval for context

**Storage**:
- Files: `./pm-docs/{project_id}/`
- Metadata: `documents` table
- Chunks: `document_chunks` table with JSON embeddings

**RAG approach** (medium scale 100-1000 docs):
- Chunk documents ~500 tokens with overlap
- Generate embeddings via OpenAI or local model
- Store as JSON arrays in SQLite
- Cosine similarity search in Python (fast enough for <10k chunks)
- Migration path to Neon pgvector if needed

---

### Phase 7: Autonomous Execution
**Goal**: Agent takes action without asking (where safe)

**Files to create**:
- `autonomous_executor.py` - Action execution with approval flow

**Action levels**:
- **Level 1 (Always)**: Read emails, check calendar, download attachments
- **Level 2 (Log only)**: Label emails, create drafts, set reminders
- **Level 3 (Pre-approved patterns)**: Send follow-up emails after 5 days

**Approval flow**:
- Unknown actions -> Queue for user approval via Telegram
- User can approve/deny with single tap

---

### Phase 8: Weekly Reports
**Goal**: Automated progress summaries

**Files to create**:
- `report_generator.py` - Weekly report logic

**Report sections**:
1. **Completed this week** - Tasks, emails sent, autonomous actions
2. **Currently blocked** - What's stuck, who we're waiting on
3. **Upcoming** - Next 2 weeks deadlines, meetings
4. **Email activity** - Important received, awaiting response
5. **Recommendations** - AI-generated suggestions

**Delivery**: Telegram message + detailed email

---

### Phase 9: Scheduler Integration
**Goal**: Background jobs for monitoring/reports

**Files to create**:
- `scheduler.py` - APScheduler integration

**Jobs**:
- Email scan: 1-2x per day (morning + afternoon)
- Deadline check: every hour
- Daily batch notifications: 9am, 1pm, 5pm
- Weekly report: Sunday 8pm
- Recurring task generation: daily at midnight

---

### Phase 10: Dashboard UI
**Goal**: Visual project status, TOC metrics

**Files to create**:
- `static/dashboard.html` - Single-page app
- `static/dashboard.css` - Styling
- `static/dashboard.js` - Fetch + render logic

**Technology**: Vanilla HTML/JS + Chart.js (CDN)

**Panels**:
1. **WIP Gauge** - Current vs limit, violation warnings
2. **Project Tree** - Hierarchical view with progress bars
3. **Full-Kit Checklist** - Prerequisites for current task
4. **Buffer Fever Chart** - % consumed vs % complete
5. **Blockers List** - What's stuck and why
6. **Weekly Metrics** - Velocity, cycle time, context switches

**Real-time updates**: Server-Sent Events (SSE) from Flask

**Grok integration**: Grok parses commands from chat, calls APIs, dashboard auto-refreshes

---

## File Structure (Final)

```
Project_manager/
├── server.py              # Flask + API endpoints + SSE
├── db.py                  # SQLite schema (DONE)
├── grok_client.py         # Grok API + tool support (DONE)
├── telegram_client.py     # Telegram bot
├── task_manager.py        # Task/project CRUD (DONE)
├── toc_engine.py          # TOC calculations (DONE)
├── memory_manager.py      # 60K context + tools (DONE)
├── notification_router.py # Priority routing (DONE)
├── email_monitor.py       # Gmail scanning (TODO)
├── autonomous_executor.py # Auto-actions (TODO)
├── document_manager.py    # File handling (TODO)
├── rag_engine.py          # Context retrieval (TODO)
├── report_generator.py    # Weekly reports (TODO)
├── scheduler.py           # Background jobs (TODO)
├── config.py              # Settings
├── static/
│   ├── dashboard.html
│   ├── dashboard.css
│   └── dashboard.js
├── docs/
│   ├── MEMORY_MANAGEMENT.md
│   ├── NOTIFICATION_SYSTEM.md
│   └── PROJECT_ROADMAP.md
├── agent.db               # SQLite database
├── run.sh                 # Startup script
└── README.md              # Setup guide
```

---

## TOC Principles Implemented

1. **Identify the Constraint**: User's attention is THE bottleneck
2. **Exploit**: Maximize value of every interruption
3. **Subordinate**: Everything else runs autonomously
4. **Elevate**: Dashboard gives instant visibility without effort

**Specific TOC features**:
- WIP limits (HARD: 1-3 tasks max)
- Full-kit enforcement (can't start without prerequisites)
- Buffer management (50% estimates + safety buffers)
- Critical chain visualization
- Student syndrome detection (started late?)
- Parkinson's law detection (expanded to fill time?)
- Context switch tracking

---

## Implementation Order (UPDATED)

| Phase | Deliverable | Depends On | Group | Status |
|-------|-------------|------------|-------|--------|
| 1 | Database schema | - | - | DONE |
| 2 | Task management | Phase 1 | - | DONE |
| **2.5** | **Memory management** | **Phase 1** | **A** | **DONE** |
| **3** | **Notification system** | **Phase 1** | **A** | **DONE** |
| 5 | Recurring tasks | Phase 2 | A | Pending |
| 6 | Document/RAG | Phase 1 | A | Pending |
| 4 | Email monitoring | Phase 3 | B | Pending |
| 9 | **Scheduler** | Phase 3, 4 | B | Pending |
| 7 | Autonomous execution | Phase 3, 4 | C | Pending |
| 8 | Weekly reports | Phase 2, 4 | C | Pending |
| **10** | **Dashboard UI** | **Phase 9 (scheduler!)** | **D** | **Pending** |

**Key Change**: Dashboard UI (phase 10) now depends on Scheduler (phase 9) because the dashboard displays scheduler activity, not the reverse!

---

## Future Enhancements (Not in MVP)

- [ ] Google Calendar integration
- [ ] Payment execution (Stripe/bank API)
- [ ] W-9 collection workflow
- [ ] Tax document organization
- [ ] Neon PostgreSQL migration
- [ ] Multi-user support (Stack Auth)
- [ ] Mobile-responsive dashboard

---

## Key Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Database | SQLite first | Simpler, local, migrate later |
| RAG | JSON embeddings in SQLite | Works for 100-1000 docs |
| Calendar | Deferred | Gmail MCP first priority |
| Meeting notes | Gmail attachment | Gemini emails them |
| UI framework | Vanilla JS | No build step, Gemini-friendly |
| **Memory** | **60K tokens + tool retrieval** | **Simple, no compaction, tools for older messages** |
| **Email scans** | **1-2x per day** | **Low volume, no need for frequent checks** |
