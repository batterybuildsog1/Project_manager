# Project Manager Agent - Full System

> **Location**: `~/.claude/plans/misty-sleeping-willow.md`
> **Project**: `~/.claude/project-manager/`
> **Last Updated**: 2025-12-05

## Related Specs

| Spec | Location | Status |
|------|----------|--------|
| Memory Management | `~/.claude/plans/memory-management-spec.md` | Ready |

---

## EXECUTION TRACKER

### Current Status (RE-ORDERED)

**Rationale**: Scheduler comes before Dashboard because the dashboard displays scheduler activity.

| Phase | Name | Status | Dependencies | Files |
|-------|------|--------|--------------|-------|
| 1 | Database Schema | ✅ DONE | - | `db.py` |
| 2 | Task Management | ✅ DONE | 1 | `task_manager.py`, `toc_engine.py` |
| **2.5** | **Memory** | **🔄 NEXT** | **1** | **`memory_manager.py`** |
| 3 | Notifications | ⏳ Ready | 1 | `notification_router.py`, `config.py` |
| 4 | Email Monitor | ⏳ Ready | 3 | `email_monitor.py` |
| 5 | Recurring Tasks | ⏳ Ready | 2 | Uses existing `db.py` |
| 6 | Documents/RAG | ⏳ Ready | 1 | `document_manager.py`, `rag_engine.py` |
| 7 | Autonomous Exec | ⏸️ Blocked | 3, 4 | `autonomous_executor.py` |
| 8 | Weekly Reports | ⏸️ Blocked | 2, 4 | `report_generator.py` |
| **9** | **Scheduler** | **⏳ Ready** | **3, 4** | **`scheduler.py`** |
| **10** | **Dashboard UI** | **⏸️ Blocked** | **9 (scheduler first!)** | **`static/dashboard.*`** |

### Parallelizable Groups (UPDATED)
**Group A** (NOW - no dependencies): 2.5, 3, 5, 6
**Group B** (after 3 done): 4, 9
**Group C** (after 4, 9 done): 7, 8
**Group D** (LAST - needs scheduler): 10 (Dashboard)

### Agent Execution Plan

**EXECUTING: Group A (4 agents in parallel)**

#### Agent 1: Phase 2.5 - Memory Management
```
Create: memory_manager.py
Update: grok_client.py (add tool support), server.py (project context), db.py (summaries table, search index)

Key requirements:
- 55K tokens per project (not 25K global)
- Messages use existing conversation_messages table (already has project_id)
- Auto-compaction when >55K tokens → summarize oldest into conversation_summaries
- Build context: all-project summary + active project's full messages
- Tool-based search for compacted/old data
```

#### Agent 2: Phase 3 - Notification System
```
Create: notification_router.py, config.py
Update: server.py (add notification endpoints)
Key: P0-P3 priorities, batch notifications, Telegram + SMS channels
```

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

**NEXT: Group B (after Group A completes)**

#### Agent 5: Phase 4 - Email Monitor
```
Create: email_monitor.py
Uses: Gmail MCP tools (already configured)
Key: Priority scan (blockers), full scan (keywords), auto-download attachments
```

#### Agent 6: Phase 9 - Scheduler
```
Create: scheduler.py
Uses: APScheduler
Key: Email scans (5/15 min), deadline checks, batch notifications, recurring task generation
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
┌─────────────────────────────────────────────────────────────────────────┐
│                         USER INTERFACES                                  │
├─────────────────────────────────────────────────────────────────────────┤
│  Telegram (@Alan0130_bot)  │  Dashboard (localhost:4000)  │  SMS backup │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
┌─────────────────────────────────────────────────────────────────────────┐
│                       FLASK SERVER (:4000)                               │
├─────────────────────────────────────────────────────────────────────────┤
│  Webhook Handler  │  REST APIs  │  SSE Events  │  Static Files          │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
┌─────────────────────────────────────────────────────────────────────────┐
│                        AGENT MODULES                                     │
├─────────────────────────────────────────────────────────────────────────┤
│ Grok 4.1    │ Email Monitor │ Task Manager │ Scheduler │ Report Gen    │
│ (AI brain) │ (Gmail MCP)   │ (TOC logic)  │ (APSched) │ (Weekly)      │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
┌─────────────────────────────────────────────────────────────────────────┐
│                        STORAGE                                           │
├─────────────────────────────────────────────────────────────────────────┤
│  SQLite (agent.db)  │  Document Store (~/.claude/pm-docs/)  │  Embeddings│
└─────────────────────────────────────────────────────────────────────────┘
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

### Phase 2.5: Memory Management (CRITICAL)

**Summary** (UPDATED):
- **55K token context** per project (not global)
- **Project-scoped memory** - conversations linked to specific project via `conversation_messages` table
- **Full recall on active project** - load ALL messages up to 55K tokens when working on that project
- **Dashboard summary for all** - schedule/blockers/status visible across projects
- **Auto-compaction** - when over 55K, oldest messages get summarized into `conversation_summaries`
- **Tool-based search** - search_history() still available for compacted/old data

**Architecture**:
```
┌─────────────────────────────────────────────────────────────────────┐
│                     ACTIVE PROJECT CONTEXT (55K)                     │
├─────────────────────────────────────────────────────────────────────┤
│  • System prompt (~3K tokens)                                       │
│  • Project summaries if any (~5K tokens) - compacted old msgs       │
│  • Recent messages for THIS PROJECT (~45K tokens) - full text       │
│  • Tool definitions for history search                              │
│                                                                     │
│  TOTAL: ~55K tokens max                                             │
└─────────────────────────────────────────────────────────────────────┘
                              │
        ┌─────────────────────┴─────────────────────┐
        ▼                                           ▼
┌─────────────────────┐                 ┌─────────────────────┐
│ OTHER PROJECTS      │                 │ COMPACTED STORAGE   │
│ (dashboard summary) │                 │ (searchable)        │
├─────────────────────┤                 ├─────────────────────┤
│ • Status            │                 │ • Old messages      │
│ • Blockers          │                 │ • Summarized chunks │
│ • Next task         │                 │ • Full text search  │
│ • Due dates         │                 │ • Indexed by date   │
└─────────────────────┘                 └─────────────────────┘
```

**Token Budget Breakdown**:
| Component | Tokens | Notes |
|-----------|--------|-------|
| System prompt | 3,000 | With tool definitions |
| All-project summary | 2,000 | Status of other projects |
| Compacted summaries | 5,000 | Summarized old msgs (if any) |
| Recent messages | 45,000 | Full text for active project |
| **TOTAL** | **55,000** | Per-project max |

**Compaction Logic**:
1. After each message, check project token count
2. If >55K: take oldest 25% of messages
3. Call Grok to summarize those messages (~500 tokens)
4. Store summary in `conversation_summaries` table
5. Mark original messages as `is_summarized=1` (keep for search)
6. Dashboard shows summary count

**Database Changes Required**:
- Add `project_id` column to existing `conversation_messages` table
- Add `conversation_summaries` table:
  ```sql
  CREATE TABLE conversation_summaries (
      id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL REFERENCES projects(id),
      summary_text TEXT NOT NULL,
      messages_from TEXT,  -- oldest msg id
      messages_to TEXT,    -- newest msg id
      message_count INTEGER,
      created_at TEXT DEFAULT CURRENT_TIMESTAMP
  );
  ```
- Add index on `conversation_messages(project_id, created_at)`

**Files**: `memory_manager.py` (create), `grok_client.py` (add tool support), `server.py` (project context), `db.py` (schema updates)

---

### Phase 2: Task Management System
**Goal**: CRUD for projects/tasks with TOC enforcement

**Files to create**:
- `task_manager.py` - Project/task CRUD, dependency resolution
- `toc_engine.py` - WIP enforcement, buffer calculations, critical chain

**Key features**:
- Hierarchical task trees (project → phase → task → subtask)
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
- Priority scan every 5 min (tracked blockers only)
- Full scan every 15 min (keywords, attachments)
- Classify emails by project relevance
- Auto-download attachments to staging
- Meeting notes from Gemini arrive as Gmail attachments → parsed

**Blocker resolution flow**:
1. Task blocked waiting on "Joyce pricing"
2. Email from Joyce arrives with attachment
3. Agent: P0 notification "Unblocked: Joyce sent pricing"
4. Auto-download attachment, link to project

---

### Phase 5: Autonomous Execution
**Goal**: Agent takes action without asking (where safe)

**Files to create**:
- `autonomous_executor.py` - Action execution with approval flow

**Action levels**:
- **Level 1 (Always)**: Read emails, check calendar, download attachments
- **Level 2 (Log only)**: Label emails, create drafts, set reminders
- **Level 3 (Pre-approved patterns)**: Send follow-up emails after 5 days

**Approval flow**:
- Unknown actions → Queue for user approval via Telegram
- User can approve/deny with single tap

---

### Phase 6: Dashboard UI
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

### Phase 7: Document Management & RAG
**Goal**: Handle receipts, invoices, quotes, PDFs

**Files to create**:
- `document_manager.py` - CRUD, chunking, embedding
- `rag_engine.py` - Retrieval for context

**Storage**:
- Files: `~/.claude/pm-docs/{project_id}/`
- Metadata: `documents` table
- Chunks: `document_chunks` table with JSON embeddings

**RAG approach** (medium scale 100-1000 docs):
- Chunk documents ~500 tokens with overlap
- Generate embeddings via OpenAI or local model
- Store as JSON arrays in SQLite
- Cosine similarity search in Python (fast enough for <10k chunks)
- Migration path to Neon pgvector if needed

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

### Phase 9: Recurring Tasks
**Goal**: Utility bills, maintenance schedules

**Implementation**:
- `recurring_schedules` table with cron-like patterns
- Scheduler generates tasks automatically
- Examples: "Pay electric bill" monthly on 15th, "Check receipts" weekly

---

### Phase 10: Scheduler Integration
**Goal**: Background jobs for monitoring/reports

**Files to create**:
- `scheduler.py` - APScheduler integration

**Jobs**:
- Email priority scan: every 5 min
- Email full scan: every 15 min
- Deadline check: every hour
- Daily batch notifications: 9am, 1pm, 5pm
- Weekly report: Sunday 8pm
- Recurring task generation: daily at midnight

---

## File Structure (Final)

```
~/.claude/project-manager/
├── server.py              # Flask + new API endpoints + SSE
├── db.py                  # Expanded schema + migrations ✓ DONE
├── grok_client.py         # Updated system prompt
├── telegram_client.py     # Unchanged
├── task_manager.py        # Task/project CRUD ✓ DONE
├── toc_engine.py          # TOC calculations ✓ DONE
├── memory_manager.py      # NEW: Token tracking, compaction, retrieval
├── notification_router.py # NEW: Priority routing
├── email_monitor.py       # NEW: Gmail scanning
├── autonomous_executor.py # NEW: Auto-actions
├── document_manager.py    # NEW: File handling
├── rag_engine.py          # NEW: Context retrieval
├── report_generator.py    # NEW: Weekly reports
├── scheduler.py           # NEW: Background jobs
├── config.py              # NEW: Settings
├── static/
│   ├── dashboard.html
│   ├── dashboard.css
│   └── dashboard.js
├── agent.db               # Expanded schema
├── run.sh                 # Updated startup
└── README.md              # Updated docs
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
| 1 | Database schema | - | - | ✅ DONE |
| 2 | Task management | Phase 1 | - | ✅ DONE |
| **2.5** | **Memory management** | **Phase 1** | **A** | **🔄 NEXT** |
| 3 | Notification system | Phase 1 | A | Pending |
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
| **Memory** | **55K per project + auto-compact** | **Full recall for active project, summaries for overflow** |
