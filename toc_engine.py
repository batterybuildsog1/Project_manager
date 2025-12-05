#!/usr/bin/env python3
"""
Theory of Constraints (TOC) Engine for Project Manager Agent.
Implements WIP limits, full-kit enforcement, buffer management, and critical chain.
"""

from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Tuple
import db


# Default configuration
DEFAULT_WIP_LIMIT = 3  # Maximum concurrent tasks
BUFFER_RED_ZONE = 66  # % consumed = red zone
BUFFER_YELLOW_ZONE = 33  # % consumed = yellow zone


class WIPViolationError(Exception):
    """Raised when starting a task would violate WIP limits."""
    pass


class FullKitIncompleteError(Exception):
    """Raised when starting a task without complete prerequisites."""
    pass


def get_global_wip_limit() -> int:
    """Get the global WIP limit from config."""
    config = db.get_state("config", {})
    return config.get("wip_limit", DEFAULT_WIP_LIMIT)


def set_global_wip_limit(limit: int) -> None:
    """Set the global WIP limit."""
    config = db.get_state("config", {})
    config["wip_limit"] = max(1, min(limit, 5))  # Clamp between 1-5
    db.set_state("config", config)


def check_wip_limit(project_id: str = None) -> Tuple[int, int, bool]:
    """
    Check current WIP against limit.

    Returns:
        (current_wip, wip_limit, is_within_limit)
    """
    current = db.get_wip_count(project_id)

    if project_id:
        project = db.get_project(project_id)
        limit = project.get("wip_limit", DEFAULT_WIP_LIMIT) if project else DEFAULT_WIP_LIMIT
    else:
        limit = get_global_wip_limit()

    return current, limit, current < limit


def can_start_task(task_id: str) -> Tuple[bool, List[str]]:
    """
    Check if a task can be started.

    Returns:
        (can_start, list_of_reasons_if_not)
    """
    task = db.get_task(task_id)
    if not task:
        return False, ["Task not found"]

    reasons = []

    # Check if already in progress or completed
    if task["status"] == "in_progress":
        reasons.append("Task is already in progress")
    elif task["status"] == "completed":
        reasons.append("Task is already completed")
    elif task["status"] == "cancelled":
        reasons.append("Task is cancelled")

    # Check WIP limit
    current, limit, within = check_wip_limit(task.get("project_id"))
    if not within:
        reasons.append(f"WIP limit reached ({current}/{limit})")

    # Check full kit
    if not db.is_full_kit_complete(task_id):
        missing = db.get_full_kit(task_id)
        unsatisfied = [item["description"] for item in missing if not item["is_satisfied"]]
        if unsatisfied:
            reasons.append(f"Full kit incomplete: {', '.join(unsatisfied[:3])}")

    # Check dependencies
    deps = get_blocking_dependencies(task_id)
    if deps:
        dep_titles = [d["title"] for d in deps[:3]]
        reasons.append(f"Waiting on dependencies: {', '.join(dep_titles)}")

    return len(reasons) == 0, reasons


def start_task(task_id: str, force: bool = False) -> Dict[str, Any]:
    """
    Start a task with TOC enforcement.

    Args:
        task_id: Task to start
        force: If True, ignore WIP limits (use sparingly!)

    Returns:
        Updated task dict

    Raises:
        WIPViolationError: If WIP limit would be exceeded
        FullKitIncompleteError: If prerequisites aren't met
    """
    can_start, reasons = can_start_task(task_id)

    if not can_start and not force:
        # Check specific error types
        for reason in reasons:
            if "WIP limit" in reason:
                raise WIPViolationError(reason)
            if "Full kit" in reason:
                raise FullKitIncompleteError(reason)
        raise Exception("; ".join(reasons))

    # Get current task to log context switch
    active_tasks = db.get_active_tasks()
    current_task_id = active_tasks[0]["id"] if active_tasks else None

    # Log context switch if switching from another task
    if current_task_id and current_task_id != task_id:
        db.log_context_switch(
            from_task_id=current_task_id,
            to_task_id=task_id,
            switch_type="voluntary",
            reason="Started new task"
        )

    # Check if starting with full kit
    kit_complete = db.is_full_kit_complete(task_id)

    # Update task status
    task = db.update_task(
        task_id,
        status="in_progress",
        actual_start=datetime.now().isoformat()
    )

    # Track metrics
    _record_task_start_metrics(task_id, kit_complete)

    return task


def complete_task(task_id: str, actual_hours: float = None) -> Dict[str, Any]:
    """
    Mark a task as complete.

    Args:
        task_id: Task to complete
        actual_hours: Actual hours spent (for tracking)

    Returns:
        Updated task dict
    """
    task = db.get_task(task_id)
    if not task:
        raise ValueError("Task not found")

    updates = {
        "status": "completed",
        "actual_end": datetime.now().isoformat()
    }

    if actual_hours is not None:
        updates["actual_hours"] = actual_hours

    task = db.update_task(task_id, **updates)

    # Resolve any blockers on this task
    blockers = db.list_blockers(task_id=task_id)
    for blocker in blockers:
        db.resolve_blocker(blocker["id"], resolved_by="task_completed")

    # Check if any dependent tasks can now be started
    unblock_dependent_tasks(task_id)

    return task


def block_task(task_id: str, reason: str, waiting_on: str = None) -> Dict[str, Any]:
    """
    Mark a task as blocked.

    Args:
        task_id: Task to block
        reason: Why it's blocked
        waiting_on: Who/what we're waiting for

    Returns:
        Created blocker dict
    """
    task = db.update_task(task_id, status="blocked")

    blocker = db.create_blocker(
        description=reason,
        blocker_type="other",
        task_id=task_id,
        waiting_on=waiting_on
    )

    # Log context switch
    db.log_context_switch(
        from_task_id=task_id,
        switch_type="blocked",
        reason=reason
    )

    return blocker


def get_blocking_dependencies(task_id: str) -> List[Dict[str, Any]]:
    """Get tasks that must complete before this one can start."""
    conn = db.get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT t.* FROM tasks t
        JOIN task_dependencies td ON td.depends_on_task_id = t.id
        WHERE td.task_id = ? AND t.status != 'completed'
    """, (task_id,))

    rows = cursor.fetchall()
    conn.close()

    return [dict(row) for row in rows]


def unblock_dependent_tasks(completed_task_id: str) -> List[str]:
    """Check if completing this task unblocks others."""
    conn = db.get_connection()
    cursor = conn.cursor()

    # Find tasks that depend on the completed task
    cursor.execute("""
        SELECT DISTINCT td.task_id
        FROM task_dependencies td
        WHERE td.depends_on_task_id = ?
    """, (completed_task_id,))

    dependent_ids = [row["task_id"] for row in cursor.fetchall()]
    conn.close()

    unblocked = []
    for dep_id in dependent_ids:
        # Check if all dependencies are now complete
        blocking = get_blocking_dependencies(dep_id)
        if not blocking:
            task = db.get_task(dep_id)
            if task and task["status"] == "waiting_for_kit":
                # Check if full kit is also complete
                if db.is_full_kit_complete(dep_id):
                    db.update_task(dep_id, status="ready")
                    unblocked.append(dep_id)

    return unblocked


# ============================================
# BUFFER MANAGEMENT
# ============================================

def calculate_buffer_status(project_id: str) -> Dict[str, Any]:
    """
    Calculate buffer health for a project.

    Returns:
        {
            "progress_percent": float,
            "buffer_consumed_percent": float,
            "status": "green" | "yellow" | "red",
            "penetration_rate": float  # buffer consumed / progress
        }
    """
    project = db.get_project(project_id)
    if not project:
        return None

    progress = project.get("progress_percent", 0) or 0
    consumed = project.get("buffer_consumed_percent", 0) or 0

    # Determine status
    if consumed < BUFFER_YELLOW_ZONE:
        status = "green"
    elif consumed < BUFFER_RED_ZONE:
        status = "yellow"
    else:
        status = "red"

    # Calculate penetration rate (how fast buffer is being consumed vs progress)
    penetration = consumed / progress if progress > 0 else 0

    return {
        "progress_percent": progress,
        "buffer_consumed_percent": consumed,
        "status": status,
        "penetration_rate": penetration,
        "buffer_days": project.get("buffer_days", 0),
        "estimated_days": project.get("estimated_days", 0)
    }


def update_buffer_status(project_id: str, progress: float = None, consumed: float = None):
    """Update buffer tracking for a project."""
    updates = {}
    if progress is not None:
        updates["progress_percent"] = min(100, max(0, progress))
    if consumed is not None:
        updates["buffer_consumed_percent"] = min(100, max(0, consumed))

    if updates:
        db.update_project(project_id, **updates)

        # Record to history for fever chart
        conn = db.get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO buffer_history (id, project_id, progress_percent, consumed_percent)
            VALUES (?, ?, ?, ?)
        """, (
            db.generate_id(),
            project_id,
            updates.get("progress_percent", 0),
            updates.get("buffer_consumed_percent", 0)
        ))
        conn.commit()
        conn.close()


def get_buffer_history(project_id: str, days: int = 30) -> List[Dict[str, Any]]:
    """Get buffer history for fever chart."""
    conn = db.get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT progress_percent, consumed_percent, recorded_at
        FROM buffer_history
        WHERE project_id = ? AND recorded_at > datetime('now', ?)
        ORDER BY recorded_at
    """, (project_id, f"-{days} days"))

    rows = cursor.fetchall()
    conn.close()

    return [dict(row) for row in rows]


# ============================================
# CRITICAL CHAIN
# ============================================

def identify_critical_chain(project_id: str) -> List[str]:
    """
    Identify the critical chain (longest resource-dependent path).

    This is a simplified version - true CCPM would consider resource constraints.
    Here we find the longest dependency chain.
    """
    tasks = db.list_tasks(project_id=project_id)

    # Build dependency graph
    deps = {}
    for task in tasks:
        task_deps = get_blocking_dependencies(task["id"])
        deps[task["id"]] = [d["id"] for d in task_deps]

    # Find longest path using DFS
    def longest_path(task_id: str, memo: dict) -> int:
        if task_id in memo:
            return memo[task_id]

        if task_id not in deps or not deps[task_id]:
            memo[task_id] = 1
            return 1

        max_len = 0
        for dep_id in deps[task_id]:
            path_len = longest_path(dep_id, memo)
            max_len = max(max_len, path_len)

        memo[task_id] = max_len + 1
        return memo[task_id]

    # Calculate path lengths
    memo = {}
    max_length = 0
    end_task = None

    for task in tasks:
        path_len = longest_path(task["id"], memo)
        if path_len > max_length:
            max_length = path_len
            end_task = task["id"]

    # Trace back the critical chain
    chain = []
    current = end_task
    sequence = 0

    while current:
        chain.append(current)
        db.update_task(current, is_critical_chain=1, critical_chain_sequence=sequence)
        sequence += 1

        # Find the dependency with the longest remaining path
        if current in deps and deps[current]:
            next_task = max(deps[current], key=lambda t: memo.get(t, 0))
            current = next_task
        else:
            current = None

    return chain


# ============================================
# METRICS & TRACKING
# ============================================

def _record_task_start_metrics(task_id: str, full_kit_complete: bool):
    """Record metrics when a task starts."""
    # We'll aggregate these in daily/weekly snapshots
    # For now, just track full-kit vs partial-kit starts
    if not full_kit_complete:
        # Log a partial kit start - this is bad behavior to track
        db.log_context_switch(
            to_task_id=task_id,
            switch_type="voluntary",
            reason="PARTIAL_KIT_START"
        )


def calculate_flow_efficiency(project_id: str) -> float:
    """
    Calculate flow efficiency (touch time / total lead time).

    This measures how much of the total time was spent actually working vs waiting.
    """
    conn = db.get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            SUM(actual_hours) as touch_time,
            SUM(
                CASE WHEN actual_end IS NOT NULL AND actual_start IS NOT NULL
                THEN julianday(actual_end) - julianday(actual_start)
                ELSE 0 END
            ) * 24 as lead_time_hours
        FROM tasks
        WHERE project_id = ? AND status = 'completed'
    """, (project_id,))

    row = cursor.fetchone()
    conn.close()

    touch = row["touch_time"] or 0
    lead = row["lead_time_hours"] or 0

    return (touch / lead * 100) if lead > 0 else 0


def get_wip_status() -> Dict[str, Any]:
    """Get current WIP status for dashboard."""
    current, limit, within = check_wip_limit()
    active = db.get_active_tasks()
    switches_today = db.get_context_switches_today()

    return {
        "current": current,
        "limit": limit,
        "within_limit": within,
        "active_tasks": active,
        "context_switches_today": switches_today
    }


def get_project_tree(project_id: str = None) -> List[Dict[str, Any]]:
    """
    Get hierarchical project/task tree for dashboard.

    Returns nested structure:
    [
        {
            "project": {...},
            "tasks": [
                {"task": {...}, "subtasks": [...]}
            ]
        }
    ]
    """
    projects = db.list_projects(status="active")
    if project_id:
        projects = [p for p in projects if p["id"] == project_id]

    result = []
    for project in projects:
        # Get top-level tasks
        top_tasks = db.list_tasks(project_id=project["id"], parent_task_id="")

        task_tree = []
        for task in top_tasks:
            subtasks = db.list_tasks(project_id=project["id"], parent_task_id=task["id"])
            task_tree.append({
                "task": task,
                "subtasks": subtasks,
                "full_kit": db.get_full_kit(task["id"]),
                "blockers": db.list_blockers(task_id=task["id"])
            })

        buffer_status = calculate_buffer_status(project["id"])

        result.append({
            "project": project,
            "tasks": task_tree,
            "buffer_status": buffer_status
        })

    return result
