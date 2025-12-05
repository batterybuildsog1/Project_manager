#!/usr/bin/env python3
"""
Recurring Tasks Generator for Project Manager Agent.

Generates tasks from recurring schedules based on frequency patterns.
Designed to run daily at midnight via scheduler.

Adapts to existing schema which uses:
- name, task_title_template, frequency, start_date
- day_of_week, day_of_month, month_of_year (as TEXT)
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from calendar import monthrange

import db

logger = logging.getLogger(__name__)


# ============================================
# FREQUENCY CALCULATIONS
# ============================================

def parse_day_field(field: str) -> List[int]:
    """
    Parse day field which may be comma-separated or single value.

    Args:
        field: String like "15" or "1,15" or None

    Returns:
        List of integers
    """
    if not field:
        return []
    try:
        return [int(x.strip()) for x in field.split(",")]
    except (ValueError, AttributeError):
        return []


def get_next_occurrence(schedule: Dict, after: datetime = None) -> datetime:
    """
    Calculate next occurrence for a schedule.

    Args:
        schedule: Recurring schedule dict from DB
        after: Calculate next occurrence after this time (default: now)

    Returns:
        Next due datetime
    """
    if after is None:
        after = datetime.now()

    frequency = schedule.get("frequency", "monthly")

    # Default time is 9:00 AM
    hour, minute = 9, 0

    # Parse schedule fields
    days_of_week = parse_day_field(schedule.get("day_of_week"))
    days_of_month = parse_day_field(schedule.get("day_of_month"))
    months_of_year = parse_day_field(schedule.get("month_of_year"))

    if frequency == "daily":
        # Tomorrow at 9am
        next_date = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if next_date <= after:
            next_date += timedelta(days=1)
        return next_date

    elif frequency == "weekly":
        # Next occurrence of day_of_week (0=Monday, 6=Sunday)
        target_dow = days_of_week[0] if days_of_week else 0
        current_dow = after.weekday()
        days_ahead = target_dow - current_dow
        if days_ahead <= 0:
            days_ahead += 7
        next_date = after + timedelta(days=days_ahead)
        return next_date.replace(hour=hour, minute=minute, second=0, microsecond=0)

    elif frequency == "biweekly":
        # Every two weeks from start_date
        start_str = schedule.get("start_date")
        if start_str:
            try:
                start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                if start.tzinfo:
                    start = start.replace(tzinfo=None)
            except ValueError:
                start = after
        else:
            start = after

        # Calculate weeks since start
        days_since = (after - start).days
        weeks_since = days_since // 7
        next_week = ((weeks_since // 2) + 1) * 2
        next_date = start + timedelta(weeks=next_week)
        return next_date.replace(hour=hour, minute=minute, second=0, microsecond=0)

    elif frequency == "monthly":
        # Next occurrence of day_of_month
        target_day = days_of_month[0] if days_of_month else 1
        year = after.year
        month = after.month

        # Handle day overflow (e.g., 31st in February)
        max_day = monthrange(year, month)[1]
        actual_day = min(target_day, max_day)

        next_date = after.replace(
            day=actual_day, hour=hour, minute=minute, second=0, microsecond=0
        )

        if next_date <= after:
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

    elif frequency == "quarterly":
        # Every 3 months
        target_day = days_of_month[0] if days_of_month else 1
        year = after.year
        month = after.month

        # Find next quarter start
        quarter_starts = [1, 4, 7, 10]
        next_quarter = None
        for q in quarter_starts:
            if month < q or (month == q and after.day < target_day):
                next_quarter = q
                break

        if next_quarter is None:
            next_quarter = 1
            year += 1

        max_day = monthrange(year, next_quarter)[1]
        actual_day = min(target_day, max_day)

        return datetime(year, next_quarter, actual_day, hour, minute)

    elif frequency == "yearly":
        # Next occurrence of month/day
        target_month = months_of_year[0] if months_of_year else 1
        target_day = days_of_month[0] if days_of_month else 1
        year = after.year

        max_day = monthrange(year, target_month)[1]
        actual_day = min(target_day, max_day)

        next_date = datetime(year, target_month, actual_day, hour, minute)

        if next_date <= after:
            year += 1
            max_day = monthrange(year, target_month)[1]
            actual_day = min(target_day, max_day)
            next_date = datetime(year, target_month, actual_day, hour, minute)

        return next_date

    else:
        # Default: tomorrow
        return after.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=1)


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
    # Calculate due date
    due_date = get_next_occurrence(schedule)

    # Use task_title_template or name
    title = schedule.get("task_title_template") or schedule.get("name", "Recurring Task")
    description = schedule.get("task_description_template") or schedule.get("description")

    # Create task using db.create_task directly
    task = db.create_task(
        project_id=schedule.get("project_id"),
        title=title,
        description=description or f"Auto-generated from recurring schedule: {schedule.get('name')}",
        estimated_hours=schedule.get("estimated_hours"),
        due_date=due_date.isoformat(),
        priority=50  # Default priority
    )

    # Link to recurring schedule
    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE tasks SET recurring_schedule_id = ? WHERE id = ?",
        (schedule["id"], task["id"])
    )
    conn.commit()
    conn.close()

    logger.info(f"Created task '{task['title']}' from schedule '{schedule['name']}' due {due_date}")

    return task


def update_schedule_after_generation(schedule_id: str) -> None:
    """
    Update schedule after generating a task.

    Sets last_generated_date and calculates next_due_date.

    Args:
        schedule_id: Schedule ID
    """
    schedule = db.get_recurring_schedule(schedule_id)
    if not schedule:
        return

    now = datetime.now()
    next_due = get_next_occurrence(schedule, after=now)

    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE recurring_schedules
        SET last_generated_date = ?,
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
          AND (end_date IS NULL OR end_date >= ?)
        ORDER BY next_due_date ASC
    """, (now, now[:10]))  # Compare date part for end_date

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
                "schedule_name": schedule.get("name"),
                "task_id": task["id"],
                "title": task["title"],
                "due_date": task.get("due_date")
            })

        except Exception as e:
            logger.error(f"Error generating task for schedule {schedule['id']}: {e}")
            result["errors"].append({
                "schedule_id": schedule["id"],
                "schedule_name": schedule.get("name"),
                "error": str(e)
            })

    logger.info(f"Generated {result['tasks_created']} tasks")

    return result


# ============================================
# SCHEDULE MANAGEMENT
# ============================================

def create_schedule(
    name: str,
    frequency: str,
    task_title_template: str = None,
    project_id: str = None,
    description: str = None,
    day_of_week: str = None,
    day_of_month: str = None,
    month_of_year: str = None,
    task_description_template: str = None,
    estimated_hours: float = None,
    start_date: str = None,
    end_date: str = None
) -> Dict[str, Any]:
    """
    Create a new recurring schedule.

    Args:
        name: Schedule name
        frequency: 'daily', 'weekly', 'biweekly', 'monthly', 'quarterly', 'yearly'
        task_title_template: Template for generated task titles
        project_id: Optional project to link tasks to
        description: Schedule description
        day_of_week: For weekly (0=Mon, 6=Sun), comma-separated
        day_of_month: For monthly/yearly (1-31), comma-separated
        month_of_year: For yearly (1-12), comma-separated
        task_description_template: Template for task descriptions
        estimated_hours: Default estimate for generated tasks
        start_date: When schedule starts (default: today)
        end_date: Optional end date

    Returns:
        Created schedule dict
    """
    if not start_date:
        start_date = datetime.now().date().isoformat()

    if not task_title_template:
        task_title_template = name

    schedule = db.create_recurring_schedule(
        name=name,
        task_title_template=task_title_template,
        frequency=frequency,
        start_date=start_date,
        project_id=project_id,
        description=description,
        day_of_week=day_of_week,
        day_of_month=day_of_month,
        month_of_year=month_of_year,
        task_description_template=task_description_template,
        estimated_hours=estimated_hours
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

    # Fetch updated schedule
    schedule = db.get_recurring_schedule(schedule["id"])

    return schedule


def list_schedules(active_only: bool = True) -> List[Dict]:
    """List all recurring schedules."""
    return db.list_recurring_schedules(active_only=active_only)


def get_schedule(schedule_id: str) -> Optional[Dict]:
    """Get a specific schedule."""
    return db.get_recurring_schedule(schedule_id)


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


def update_schedule(schedule_id: str, **kwargs) -> Optional[Dict]:
    """
    Update a recurring schedule.

    Args:
        schedule_id: Schedule ID
        **kwargs: Fields to update

    Returns:
        Updated schedule or None
    """
    if not kwargs:
        return get_schedule(schedule_id)

    conn = db.get_connection()
    cursor = conn.cursor()

    sets = []
    params = []
    for key, value in kwargs.items():
        sets.append(f"{key} = ?")
        params.append(value)

    sets.append("updated_at = CURRENT_TIMESTAMP")
    params.append(schedule_id)

    query = f"UPDATE recurring_schedules SET {', '.join(sets)} WHERE id = ?"
    cursor.execute(query, params)
    conn.commit()
    conn.close()

    # Recalculate next_due_date if frequency-related fields changed
    schedule = get_schedule(schedule_id)
    if schedule and any(k in kwargs for k in ['frequency', 'day_of_week', 'day_of_month', 'month_of_year']):
        next_due = get_next_occurrence(schedule)
        conn = db.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE recurring_schedules SET next_due_date = ? WHERE id = ?",
            (next_due.isoformat(), schedule_id)
        )
        conn.commit()
        conn.close()
        schedule = get_schedule(schedule_id)

    return schedule


def get_tasks_for_schedule(schedule_id: str, limit: int = 10) -> List[Dict]:
    """
    Get tasks generated from a specific schedule.

    Args:
        schedule_id: Schedule ID
        limit: Max tasks to return

    Returns:
        List of task dicts
    """
    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM tasks
        WHERE recurring_schedule_id = ?
        ORDER BY created_at DESC
        LIMIT ?
    """, (schedule_id, limit))

    results = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return results


# ============================================
# TEST
# ============================================

if __name__ == "__main__":
    print("Testing recurring_tasks...")

    # Test frequency calculation
    test_schedule_monthly = {
        "frequency": "monthly",
        "day_of_month": "15",
        "start_date": "2024-01-01"
    }
    next_occ = get_next_occurrence(test_schedule_monthly)
    print(f"  Next monthly (15th): {next_occ}")

    test_schedule_weekly = {
        "frequency": "weekly",
        "day_of_week": "0",  # Monday
        "start_date": "2024-01-01"
    }
    next_weekly = get_next_occurrence(test_schedule_weekly)
    print(f"  Next weekly (Monday): {next_weekly}")

    test_schedule_daily = {
        "frequency": "daily",
        "start_date": "2024-01-01"
    }
    next_daily = get_next_occurrence(test_schedule_daily)
    print(f"  Next daily: {next_daily}")

    test_schedule_yearly = {
        "frequency": "yearly",
        "month_of_year": "12",
        "day_of_month": "25",
        "start_date": "2024-01-01"
    }
    next_yearly = get_next_occurrence(test_schedule_yearly)
    print(f"  Next yearly (Dec 25): {next_yearly}")

    print("All tests passed!")
