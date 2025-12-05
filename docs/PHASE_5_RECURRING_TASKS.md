# Phase 5: Recurring Tasks Implementation Plan

> **Location**: `docs/PHASE_5_RECURRING_TASKS.md`
> **Branch**: `feature/phase-5-recurring-tasks`
> **Status**: Ready to implement
> **Dependencies**: Phase 2 (Task Management) - DONE

---

## Overview

Build a recurring task system that automatically generates tasks from schedules. Supports cron-like patterns for utility bills, maintenance tasks, and regular reviews.

**Key Principle**: Generate tasks daily at midnight. Simple cron-like patterns.

---

## Existing Database Schema (in db.py)

The `recurring_schedules` table already exists:

```sql
CREATE TABLE IF NOT EXISTS recurring_schedules (
    id TEXT PRIMARY KEY,
    project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    description TEXT,
    frequency TEXT NOT NULL,  -- 'daily', 'weekly', 'monthly', 'yearly', 'custom'
    cron_pattern TEXT,        -- For custom: '0 9 15 * *' (9am on 15th)
    day_of_week INTEGER,      -- 0=Mon, 6=Sun (for weekly)
    day_of_month INTEGER,     -- 1-31 (for monthly)
    month_of_year INTEGER,    -- 1-12 (for yearly)
    time_of_day TEXT,         -- HH:MM format
    next_due_date TEXT,       -- When next task should be generated
    last_generated TEXT,      -- When we last created a task
    estimated_hours REAL DEFAULT 1.0,
    priority INTEGER DEFAULT 3,
    default_assignee TEXT,
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

**Existing db.py functions**:
- `create_recurring_schedule()`
- `get_recurring_schedule()`
- `list_recurring_schedules()`

---

## Files to Create

### 1. `recurring_tasks.py` (NEW - ~250 lines)

```python
#!/usr/bin/env python3
"""
Recurring Tasks Generator for Project Manager Agent.

Generates tasks from recurring schedules based on cron-like patterns.
Designed to run daily at midnight via scheduler.
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Tuple
from calendar import monthrange

import db
import task_manager

logger = logging.getLogger(__name__)


# ============================================
# CRON PATTERN PARSING
# ============================================

def parse_cron_pattern(pattern: str) -> Dict[str, Any]:
    """
    Parse a cron-like pattern.

    Format: minute hour day_of_month month day_of_week
    Example: "0 9 15 * *" = 9:00am on the 15th of every month

    Supports:
    - * = any value
    - number = specific value
    - */n = every n units

    Args:
        pattern: Cron pattern string

    Returns:
        Parsed components dict
    """
    parts = pattern.strip().split()
    if len(parts) != 5:
        raise ValueError(f"Invalid cron pattern: {pattern}. Expected 5 parts.")

    def parse_field(field: str, min_val: int, max_val: int) -> List[int]:
        if field == "*":
            return list(range(min_val, max_val + 1))
        elif field.startswith("*/"):
            step = int(field[2:])
            return list(range(min_val, max_val + 1, step))
        elif "," in field:
            return [int(x) for x in field.split(",")]
        else:
            return [int(field)]

    return {
        "minutes": parse_field(parts[0], 0, 59),
        "hours": parse_field(parts[1], 0, 23),
        "days_of_month": parse_field(parts[2], 1, 31),
        "months": parse_field(parts[3], 1, 12),
        "days_of_week": parse_field(parts[4], 0, 6)  # 0=Mon, 6=Sun
    }


def get_next_occurrence_from_cron(pattern: str, after: datetime = None) -> datetime:
    """
    Calculate next occurrence from cron pattern.

    Args:
        pattern: Cron pattern string
        after: Start searching after this time (default: now)

    Returns:
        Next occurrence datetime
    """
    if after is None:
        after = datetime.now()

    parsed = parse_cron_pattern(pattern)

    # Start from the next minute
    candidate = after.replace(second=0, microsecond=0) + timedelta(minutes=1)

    # Iterate up to 366 days to find match
    for _ in range(366 * 24 * 60):  # Max iterations
        if (candidate.minute in parsed["minutes"] and
            candidate.hour in parsed["hours"] and
            candidate.day in parsed["days_of_month"] and
            candidate.month in parsed["months"] and
            candidate.weekday() in parsed["days_of_week"]):
            return candidate
        candidate += timedelta(minutes=1)

    raise ValueError(f"Could not find next occurrence for pattern: {pattern}")


# ============================================
# SIMPLE FREQUENCY CALCULATIONS
# ============================================

def get_next_occurrence(schedule: Dict) -> datetime:
    """
    Calculate next occurrence for a schedule.

    Args:
        schedule: Recurring schedule dict

    Returns:
        Next due datetime
    """
    frequency = schedule.get("frequency", "monthly")
    now = datetime.now()

    # Parse time of day
    time_str = schedule.get("time_of_day", "09:00")
    try:
        hour, minute = map(int, time_str.split(":"))
    except (ValueError, AttributeError):
        hour, minute = 9, 0

    # Handle custom cron pattern
    if frequency == "custom" and schedule.get("cron_pattern"):
        return get_next_occurrence_from_cron(schedule["cron_pattern"])

    # Calculate based on frequency
    if frequency == "daily":
        # Tomorrow at specified time
        next_date = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if next_date <= now:
            next_date += timedelta(days=1)
        return next_date

    elif frequency == "weekly":
        # Next occurrence of day_of_week
        target_dow = schedule.get("day_of_week", 0)  # Monday default
        current_dow = now.weekday()
        days_ahead = target_dow - current_dow
        if days_ahead <= 0:
            days_ahead += 7
        next_date = now + timedelta(days=days_ahead)
        return next_date.replace(hour=hour, minute=minute, second=0, microsecond=0)

    elif frequency == "monthly":
        # Next occurrence of day_of_month
        target_day = schedule.get("day_of_month", 1)
        year = now.year
        month = now.month

        # Handle day overflow (e.g., 31st in February)
        max_day = monthrange(year, month)[1]
        actual_day = min(target_day, max_day)

        next_date = now.replace(day=actual_day, hour=hour, minute=minute, second=0, microsecond=0)

        if next_date <= now:
            # Move to next month
            if month == 12:
                year += 1
                month = 1
            else:
                month += 1
            max_day = monthrange(year, month)[1]
            actual_day = min(target_day, max_day)
            next_date = datetime(year, month, actual_day, hour, minute)

        return next_date

    elif frequency == "yearly":
        # Next occurrence of month/day
        target_month = schedule.get("month_of_year", 1)
        target_day = schedule.get("day_of_month", 1)
        year = now.year

        max_day = monthrange(year, target_month)[1]
        actual_day = min(target_day, max_day)

        next_date = datetime(year, target_month, actual_day, hour, minute)

        if next_date <= now:
            year += 1
            max_day = monthrange(year, target_month)[1]
            actual_day = min(target_day, max_day)
            next_date = datetime(year, target_month, actual_day, hour, minute)

        return next_date

    else:
        # Default to tomorrow
        return now.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=1)


# ============================================
# TASK GENERATION
# ============================================

def create_task_from_schedule(schedule: Dict) -> Dict[str, Any]:
    """
    Create a task from a recurring schedule.

    Args:
        schedule: Recurring schedule dict

    Returns:
        Created task dict
    """
    # Calculate due date (next occurrence)
    due_date = get_next_occurrence(schedule)

    # Create task
    task = task_manager.create_task(
        project_id=schedule.get("project_id"),
        title=schedule["title"],
        description=schedule.get("description", f"Auto-generated from recurring schedule"),
        estimated_hours=schedule.get("estimated_hours", 1.0),
        priority=schedule.get("priority", 3),
        due_date=due_date.isoformat(),
        recurring_schedule_id=schedule["id"]
    )

    logger.info(f"Created task '{task['title']}' from schedule '{schedule['id']}' due {due_date}")

    return task


def update_schedule_after_generation(schedule_id: str) -> None:
    """
    Update schedule after generating a task.

    Sets last_generated and calculates next_due_date.

    Args:
        schedule_id: Schedule ID
    """
    conn = db.get_connection()
    cursor = conn.cursor()

    schedule = db.get_recurring_schedule(schedule_id)
    if not schedule:
        return

    now = datetime.now()
    next_due = get_next_occurrence(schedule)

    cursor.execute("""
        UPDATE recurring_schedules
        SET last_generated = ?,
            next_due_date = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
    """, (now.isoformat(), next_due.isoformat(), schedule_id))

    conn.commit()
    conn.close()


# ============================================
# MAIN GENERATION FUNCTIONS
# ============================================

def get_due_schedules() -> List[Dict]:
    """
    Get schedules that are due for task generation.

    Returns:
        List of schedules where next_due_date <= now
    """
    conn = db.get_connection()
    cursor = conn.cursor()

    now = datetime.now().isoformat()

    cursor.execute("""
        SELECT * FROM recurring_schedules
        WHERE is_active = 1
          AND (next_due_date IS NULL OR next_due_date <= ?)
        ORDER BY next_due_date ASC
    """, (now,))

    results = [dict(row) for row in cursor.fetchall()]
    conn.close()

    return results


def generate_due_tasks() -> Dict[str, Any]:
    """
    Generate tasks for all due recurring schedules.

    This is the main entry point called by the scheduler.

    Returns:
        Summary of tasks generated
    """
    result = {
        "generated_at": datetime.now().isoformat(),
        "schedules_checked": 0,
        "tasks_created": 0,
        "tasks": [],
        "errors": []
    }

    due_schedules = get_due_schedules()
    result["schedules_checked"] = len(due_schedules)

    logger.info(f"Found {len(due_schedules)} schedules due for task generation")

    for schedule in due_schedules:
        try:
            task = create_task_from_schedule(schedule)
            update_schedule_after_generation(schedule["id"])

            result["tasks_created"] += 1
            result["tasks"].append({
                "schedule_id": schedule["id"],
                "task_id": task["id"],
                "title": task["title"]
            })

        except Exception as e:
            logger.error(f"Error generating task for schedule {schedule['id']}: {e}")
            result["errors"].append({
                "schedule_id": schedule["id"],
                "error": str(e)
            })

    logger.info(f"Generated {result['tasks_created']} tasks")

    return result


# ============================================
# SCHEDULE MANAGEMENT
# ============================================

def create_schedule(
    title: str,
    frequency: str,
    project_id: str = None,
    description: str = None,
    day_of_week: int = None,
    day_of_month: int = None,
    month_of_year: int = None,
    time_of_day: str = "09:00",
    cron_pattern: str = None,
    estimated_hours: float = 1.0,
    priority: int = 3
) -> Dict[str, Any]:
    """
    Create a new recurring schedule.

    Args:
        title: Task title template
        frequency: 'daily', 'weekly', 'monthly', 'yearly', 'custom'
        project_id: Optional project to link tasks to
        description: Task description template
        day_of_week: For weekly (0=Mon, 6=Sun)
        day_of_month: For monthly/yearly (1-31)
        month_of_year: For yearly (1-12)
        time_of_day: HH:MM format
        cron_pattern: For custom frequency
        estimated_hours: Default estimate
        priority: Default priority (1-5)

    Returns:
        Created schedule dict
    """
    schedule = db.create_recurring_schedule(
        project_id=project_id,
        title=title,
        description=description,
        frequency=frequency,
        cron_pattern=cron_pattern,
        day_of_week=day_of_week,
        day_of_month=day_of_month,
        month_of_year=month_of_year,
        time_of_day=time_of_day,
        estimated_hours=estimated_hours,
        priority=priority
    )

    # Set initial next_due_date
    next_due = get_next_occurrence(schedule)
    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE recurring_schedules SET next_due_date = ? WHERE id = ?",
        (next_due.isoformat(), schedule["id"])
    )
    conn.commit()
    conn.close()

    schedule["next_due_date"] = next_due.isoformat()

    return schedule


def list_schedules(active_only: bool = True) -> List[Dict]:
    """List all recurring schedules."""
    return db.list_recurring_schedules(active_only=active_only)


def deactivate_schedule(schedule_id: str) -> bool:
    """Deactivate a recurring schedule."""
    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE recurring_schedules SET is_active = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (schedule_id,)
    )
    updated = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return updated


def activate_schedule(schedule_id: str) -> bool:
    """Activate a recurring schedule."""
    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE recurring_schedules SET is_active = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (schedule_id,)
    )
    updated = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return updated


# ============================================
# TEST
# ============================================

if __name__ == "__main__":
    print("Testing recurring_tasks...")

    # Test cron parsing
    parsed = parse_cron_pattern("0 9 15 * *")
    print(f"  Parsed cron: {parsed}")
    assert 9 in parsed["hours"]
    assert 15 in parsed["days_of_month"]

    # Test frequency calculation
    test_schedule = {
        "frequency": "monthly",
        "day_of_month": 15,
        "time_of_day": "09:00"
    }
    next_occ = get_next_occurrence(test_schedule)
    print(f"  Next monthly (15th): {next_occ}")

    test_schedule_weekly = {
        "frequency": "weekly",
        "day_of_week": 0,  # Monday
        "time_of_day": "10:00"
    }
    next_weekly = get_next_occurrence(test_schedule_weekly)
    print(f"  Next weekly (Monday): {next_weekly}")

    print("All tests passed!")
```

---

## Files to Modify

### `server.py` - Add Recurring Task Endpoints

Add this section **at the end of the file, before `if __name__ == "__main__":`**:

```python
# ============================================
# RECURRING TASKS ENDPOINTS (Phase 5)
# ============================================

@app.route("/api/recurring", methods=["GET"])
def list_recurring_schedules():
    """List all recurring schedules."""
    import recurring_tasks

    active_only = request.args.get("active_only", "true").lower() == "true"
    schedules = recurring_tasks.list_schedules(active_only=active_only)

    return jsonify({
        "ok": True,
        "count": len(schedules),
        "schedules": schedules
    })


@app.route("/api/recurring", methods=["POST"])
def create_recurring_schedule():
    """Create a new recurring schedule."""
    import recurring_tasks

    data = request.get_json() or {}

    if not data.get("title") or not data.get("frequency"):
        return jsonify({"ok": False, "error": "title and frequency required"}), 400

    schedule = recurring_tasks.create_schedule(
        title=data["title"],
        frequency=data["frequency"],
        project_id=data.get("project_id"),
        description=data.get("description"),
        day_of_week=data.get("day_of_week"),
        day_of_month=data.get("day_of_month"),
        month_of_year=data.get("month_of_year"),
        time_of_day=data.get("time_of_day", "09:00"),
        cron_pattern=data.get("cron_pattern"),
        estimated_hours=data.get("estimated_hours", 1.0),
        priority=data.get("priority", 3)
    )

    return jsonify({
        "ok": True,
        "schedule": schedule
    })


@app.route("/api/recurring/<schedule_id>", methods=["GET"])
def get_recurring_schedule(schedule_id):
    """Get a specific recurring schedule."""
    schedule = db.get_recurring_schedule(schedule_id)

    if not schedule:
        return jsonify({"ok": False, "error": "Schedule not found"}), 404

    return jsonify({
        "ok": True,
        "schedule": schedule
    })


@app.route("/api/recurring/<schedule_id>", methods=["DELETE"])
def delete_recurring_schedule(schedule_id):
    """Deactivate a recurring schedule."""
    import recurring_tasks

    success = recurring_tasks.deactivate_schedule(schedule_id)

    return jsonify({
        "ok": success,
        "message": "Schedule deactivated" if success else "Schedule not found"
    })


@app.route("/api/recurring/generate", methods=["POST"])
def trigger_task_generation():
    """Manually trigger recurring task generation."""
    import recurring_tasks

    result = recurring_tasks.generate_due_tasks()

    return jsonify({
        "ok": True,
        "result": result
    })
```

---

## Implementation Steps

### Step 1: Create Branch
```bash
cd /Users/alanknudson/Project_manager
git checkout -b feature/phase-5-recurring-tasks
```

### Step 2: Create recurring_tasks.py
Create the file with the code above.

### Step 3: Add Server Endpoints
Add the endpoint section to `server.py` (at end, before main block).

### Step 4: Test Import
```bash
python3 -c "import recurring_tasks; print('OK')"
```

### Step 5: Commit
```bash
git add recurring_tasks.py server.py
git commit -m "Add Phase 5: Recurring Tasks

- Create recurring_tasks.py with cron pattern support
- Add schedule management (create, list, deactivate)
- Add task generation from schedules
- Add /api/recurring/* endpoints to server.py

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Frequency Types | daily, weekly, monthly, yearly, custom | Cover common use cases |
| Cron Pattern | 5-field (minute hour day month dow) | Standard, flexible |
| Generation Timing | Daily at midnight | Simple, predictable |
| next_due_date | Stored in DB | Efficient query for due schedules |
| Task Linking | recurring_schedule_id | Track which tasks came from schedules |

---

## Example Schedules

```python
# Pay electric bill on 15th of each month at 9am
create_schedule(
    title="Pay Electric Bill",
    frequency="monthly",
    day_of_month=15,
    time_of_day="09:00",
    priority=4
)

# Weekly review every Monday at 10am
create_schedule(
    title="Weekly Project Review",
    frequency="weekly",
    day_of_week=0,  # Monday
    time_of_day="10:00"
)

# Daily standup reminder
create_schedule(
    title="Daily Standup",
    frequency="daily",
    time_of_day="08:45"
)

# Custom: Every 15th and last day of month
create_schedule(
    title="Semi-monthly Report",
    frequency="custom",
    cron_pattern="0 9 15,28 * *"
)
```

---

## Dependencies

**Imports (existing files - READ ONLY)**:
- `db` - For recurring_schedules table and functions
- `task_manager` - For create_task()

---

## Testing Checklist

- [ ] `recurring_tasks.py` imports without error
- [ ] Cron pattern parsing works
- [ ] Monthly schedule calculates correct next date
- [ ] Weekly schedule calculates correct next date
- [ ] Task generation creates tasks with correct due dates
- [ ] `/api/recurring` GET lists schedules
- [ ] `/api/recurring` POST creates schedule
- [ ] `/api/recurring/generate` triggers generation

---

## Notes for Agent

1. **DO NOT modify `db.py`** - All needed functions exist
2. **DO NOT modify `task_manager.py`** - Import and use only
3. **Add endpoints to server.py in a clearly marked section**
4. **Use existing db.create_recurring_schedule()** - Don't recreate
5. **Test with**: `python3 -c "import recurring_tasks"`
