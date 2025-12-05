#!/usr/bin/env python3
"""
Task Manager for Project Manager Agent.
High-level task management operations and API for Flask server.
"""

from datetime import datetime
from typing import Dict, List, Any, Optional
import db
import toc_engine


def create_project_with_tasks(
    name: str,
    tasks: List[Dict[str, Any]],
    description: str = None,
    estimated_days: float = None,
    due_date: str = None,
    **kwargs
) -> Dict[str, Any]:
    """
    Create a project with initial tasks.

    Args:
        name: Project name
        tasks: List of task dicts with at least 'title'
        description: Project description
        estimated_days: Estimated duration (50% confidence)
        due_date: Target completion date

    Returns:
        Project dict with tasks
    """
    # Calculate buffer (typically 50% of estimate)
    buffer_days = estimated_days * 0.5 if estimated_days else None

    project = db.create_project(
        name=name,
        description=description,
        estimated_days=estimated_days,
        buffer_days=buffer_days,
        due_date=due_date,
        **kwargs
    )

    created_tasks = []
    for i, task_data in enumerate(tasks):
        task = db.create_task(
            project_id=project["id"],
            title=task_data.get("title"),
            description=task_data.get("description"),
            estimated_hours=task_data.get("estimated_hours"),
            due_date=task_data.get("due_date"),
            priority=task_data.get("priority", 50)
        )
        # Set sort order
        db.update_task(task["id"], sort_order=i)

        # Add full kit items if provided
        for kit_item in task_data.get("full_kit", []):
            if isinstance(kit_item, str):
                db.add_full_kit_item(task["id"], kit_item)
            else:
                db.add_full_kit_item(
                    task["id"],
                    kit_item.get("description", kit_item),
                    kit_item.get("type", "other")
                )

        created_tasks.append(task)

    project["tasks"] = created_tasks
    return project


def add_dependency(
    task_id: str,
    depends_on_task_id: str,
    dependency_type: str = "finish_to_start",
    feeding_buffer_hours: float = 0
) -> Dict[str, Any]:
    """Add a dependency between tasks."""
    conn = db.get_connection()
    cursor = conn.cursor()

    dep_id = db.generate_id()
    cursor.execute("""
        INSERT INTO task_dependencies (
            id, task_id, depends_on_task_id, dependency_type, feeding_buffer_hours
        ) VALUES (?, ?, ?, ?, ?)
    """, (dep_id, task_id, depends_on_task_id, dependency_type, feeding_buffer_hours))

    conn.commit()
    conn.close()

    # Update task status if dependencies not met
    blocking = toc_engine.get_blocking_dependencies(task_id)
    if blocking:
        db.update_task(task_id, status="waiting_for_kit")

    return {
        "id": dep_id,
        "task_id": task_id,
        "depends_on": depends_on_task_id,
        "type": dependency_type
    }


def start_task_safe(task_id: str) -> Dict[str, Any]:
    """
    Start a task with user-friendly error handling.
    Sends P1 notification on success.

    Returns:
        {
            "success": bool,
            "task": task dict if successful,
            "error": error message if not,
            "reasons": list of reasons why can't start
        }
    """
    # Get old status before starting
    old_task = db.get_task(task_id)
    old_status = old_task["status"] if old_task else "unknown"

    can_start, reasons = toc_engine.can_start_task(task_id)

    if can_start:
        try:
            task = toc_engine.start_task(task_id)

            # Send notification
            try:
                import notification_router
                notification_router.notify_task_status_change(
                    task_id=task_id,
                    old_status=old_status,
                    new_status="in_progress"
                )
            except Exception:
                pass  # Don't fail task start if notification fails

            return {"success": True, "task": task}
        except Exception as e:
            return {"success": False, "error": str(e), "reasons": [str(e)]}
    else:
        return {"success": False, "error": "Cannot start task", "reasons": reasons}


def complete_task_safe(task_id: str, actual_hours: float = None) -> Dict[str, Any]:
    """
    Complete a task with user-friendly response.
    Sends P1 notification on success.
    """
    # Get old status before completing
    old_task = db.get_task(task_id)
    old_status = old_task["status"] if old_task else "unknown"

    try:
        task = toc_engine.complete_task(task_id, actual_hours)
        unblocked = toc_engine.unblock_dependent_tasks(task_id)

        # Send notification
        try:
            import notification_router
            notification_router.notify_task_status_change(
                task_id=task_id,
                old_status=old_status,
                new_status="completed"
            )
        except Exception:
            pass  # Don't fail task completion if notification fails

        return {
            "success": True,
            "task": task,
            "unblocked_tasks": unblocked
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def block_task_safe(task_id: str, reason: str, waiting_on: str = None) -> Dict[str, Any]:
    """
    Block a task with user-friendly response.
    Sends P1 notification for new blocker.
    """
    try:
        blocker = toc_engine.block_task(task_id, reason, waiting_on)

        # Send notification for new blocker
        try:
            import notification_router
            notification_router.notify_new_blocker(
                blocker_id=blocker["id"],
                description=reason,
                waiting_on=waiting_on
            )
        except Exception:
            pass  # Don't fail block if notification fails

        return {"success": True, "blocker": blocker}
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_next_tasks(project_id: str = None, limit: int = 5) -> List[Dict[str, Any]]:
    """
    Get suggested next tasks based on:
    1. Tasks that are ready (dependencies met, full kit complete)
    2. Priority
    3. Due date proximity
    """
    conn = db.get_connection()
    cursor = conn.cursor()

    query = """
        SELECT t.*, p.name as project_name
        FROM tasks t
        LEFT JOIN projects p ON t.project_id = p.id
        WHERE t.status IN ('ready', 'pending', 'waiting_for_kit')
    """
    params = []

    if project_id:
        query += " AND t.project_id = ?"
        params.append(project_id)

    query += " ORDER BY t.priority DESC, t.due_date ASC NULLS LAST, t.created_at LIMIT ?"
    params.append(limit)

    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()

    result = []
    for row in rows:
        task = dict(row)
        # Check if actually ready
        can_start, reasons = toc_engine.can_start_task(task["id"])
        task["can_start"] = can_start
        task["blockers"] = reasons if not can_start else []
        result.append(task)

    return result


def get_dashboard_data() -> Dict[str, Any]:
    """Get all data needed for the dashboard."""
    wip_status = toc_engine.get_wip_status()
    project_tree = toc_engine.get_project_tree()
    active_blockers = db.list_blockers(active_only=True)
    next_tasks = get_next_tasks(limit=5)

    return {
        "wip": wip_status,
        "projects": project_tree,
        "blockers": active_blockers,
        "next_tasks": next_tasks,
        "last_updated": datetime.now().isoformat()
    }


def search_tasks(query: str, project_id: str = None) -> List[Dict[str, Any]]:
    """Search tasks by title or description."""
    conn = db.get_connection()
    cursor = conn.cursor()

    sql = """
        SELECT t.*, p.name as project_name
        FROM tasks t
        LEFT JOIN projects p ON t.project_id = p.id
        WHERE (t.title LIKE ? OR t.description LIKE ?)
    """
    params = [f"%{query}%", f"%{query}%"]

    if project_id:
        sql += " AND t.project_id = ?"
        params.append(project_id)

    sql += " ORDER BY t.priority DESC, t.created_at DESC LIMIT 20"

    cursor.execute(sql, params)
    rows = cursor.fetchall()
    conn.close()

    return [dict(row) for row in rows]


def get_task_with_context(task_id: str) -> Dict[str, Any]:
    """Get a task with all related context."""
    task = db.get_task(task_id)
    if not task:
        return None

    return {
        "task": task,
        "project": db.get_project(task["project_id"]) if task.get("project_id") else None,
        "full_kit": db.get_full_kit(task_id),
        "blockers": db.list_blockers(task_id=task_id),
        "dependencies": toc_engine.get_blocking_dependencies(task_id),
        "can_start": toc_engine.can_start_task(task_id)
    }


# ============================================
# PROGRESS TRACKING
# ============================================

def update_progress(project_id: str, progress_percent: float = None, buffer_consumed: float = None):
    """Update project progress and buffer status."""
    if progress_percent is not None or buffer_consumed is not None:
        toc_engine.update_buffer_status(
            project_id,
            progress=progress_percent,
            consumed=buffer_consumed
        )

    return toc_engine.calculate_buffer_status(project_id)


def calculate_progress_from_tasks(project_id: str) -> float:
    """Auto-calculate progress based on completed tasks."""
    tasks = db.list_tasks(project_id=project_id)
    if not tasks:
        return 0

    completed = len([t for t in tasks if t["status"] == "completed"])
    total = len(tasks)

    progress = (completed / total) * 100
    toc_engine.update_buffer_status(project_id, progress=progress)

    return progress


# ============================================
# GROK COMMAND PARSING
# ============================================

def parse_grok_command(text: str) -> Optional[Dict[str, Any]]:
    """
    Parse structured commands from Grok responses.

    Commands:
        COMMAND: start_task name="task name"
        COMMAND: complete_task name="task name"
        COMMAND: block_task name="task name" reason="reason"
        COMMAND: create_task project="project" title="task title"
        COMMAND: add_blocker task="task" description="desc" waiting_on="person"

    Returns:
        Parsed command dict or None
    """
    if "COMMAND:" not in text:
        return None

    try:
        # Extract command line
        cmd_line = text.split("COMMAND:")[1].split("\n")[0].strip()
        parts = cmd_line.split(" ", 1)
        action = parts[0]

        # Parse key="value" pairs
        params = {}
        if len(parts) > 1:
            import re
            matches = re.findall(r'(\w+)="([^"]*)"', parts[1])
            params = dict(matches)

        return {"action": action, "params": params}

    except Exception:
        return None


def execute_grok_command(command: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a parsed Grok command."""
    action = command.get("action")
    params = command.get("params", {})

    if action == "start_task":
        # Find task by name
        tasks = search_tasks(params.get("name", ""))
        if tasks:
            return start_task_safe(tasks[0]["id"])
        return {"success": False, "error": "Task not found"}

    elif action == "complete_task":
        tasks = search_tasks(params.get("name", ""))
        if tasks:
            return complete_task_safe(tasks[0]["id"])
        return {"success": False, "error": "Task not found"}

    elif action == "block_task":
        tasks = search_tasks(params.get("name", ""))
        if tasks:
            return block_task_safe(
                tasks[0]["id"],
                params.get("reason", "Blocked"),
                params.get("waiting_on")
            )
        return {"success": False, "error": "Task not found"}

    elif action == "create_task":
        projects = db.list_projects()
        project = next((p for p in projects if params.get("project", "").lower() in p["name"].lower()), None)
        if project:
            task = db.create_task(
                project_id=project["id"],
                title=params.get("title", "New Task"),
                description=params.get("description")
            )
            return {"success": True, "task": task}
        return {"success": False, "error": "Project not found"}

    elif action == "add_blocker":
        tasks = search_tasks(params.get("task", ""))
        if tasks:
            blocker = db.create_blocker(
                description=params.get("description", "Blocker"),
                task_id=tasks[0]["id"],
                waiting_on=params.get("waiting_on")
            )
            return {"success": True, "blocker": blocker}
        return {"success": False, "error": "Task not found"}

    return {"success": False, "error": f"Unknown action: {action}"}


# Test
if __name__ == "__main__":
    # Quick test
    print("Testing task_manager...")

    # Create a project with tasks
    project = create_project_with_tasks(
        name="Test Project",
        tasks=[
            {"title": "Task 1", "full_kit": ["Have requirements"]},
            {"title": "Task 2", "estimated_hours": 4},
        ],
        estimated_days=10
    )
    print(f"Created project: {project['name']} with {len(project['tasks'])} tasks")

    # Add dependency
    if len(project['tasks']) >= 2:
        dep = add_dependency(
            project['tasks'][1]['id'],
            project['tasks'][0]['id']
        )
        print(f"Added dependency: Task 2 depends on Task 1")

    # Get dashboard data
    dashboard = get_dashboard_data()
    print(f"Dashboard: {dashboard['wip']['current']} active tasks, {len(dashboard['blockers'])} blockers")

    print("All tests passed!")
