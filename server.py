#!/usr/bin/env python3
"""
Project Manager Agent - Flask Server

Runs on port 4000, receives Telegram webhooks, responds via Grok.
Uses unified 60K token context with tool-based history retrieval.
"""

import os
import sys
import json
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from flask import Flask, request, jsonify
import db
import grok_client
import telegram_client
import memory_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

app = Flask(__name__)


@app.route("/", methods=["GET"])
def health():
    """Health check endpoint."""
    return jsonify({
        "status": "ok",
        "service": "project-manager-agent",
        "context_limit": f"{memory_manager.ACTIVE_CONTEXT_TOKENS:,} tokens"
    })


@app.route("/webhook", methods=["POST"])
def webhook():
    """Telegram webhook endpoint."""
    try:
        update = request.get_json()
        logger.info(f"Received update: {json.dumps(update, indent=2)}")

        parsed = telegram_client.parse_update(update)
        text = parsed.get("text", "").strip()
        chat_id = parsed.get("chat_id")
        from_user = parsed.get("from_user")

        if not text or not chat_id:
            return jsonify({"ok": True})

        logger.info(f"Message from {from_user}: {text}")

        # Save user message
        memory_manager.add_message(str(chat_id), "user", text)

        # Build context (up to 60K tokens)
        context = memory_manager.build_context(str(chat_id))

        # Get response with tool support
        try:
            response = grok_client.chat_with_tools(
                messages=context,
                tools=memory_manager.MEMORY_TOOLS,
                tool_executor=memory_manager.execute_tool,
                chat_id=str(chat_id)
            )
            logger.info(f"Grok response: {response[:100]}...")
        except Exception as e:
            logger.error(f"Grok error: {e}")
            response = f"Sorry, I encountered an error: {str(e)}"

        # Save assistant response
        memory_manager.add_message(str(chat_id), "assistant", response)

        # Send to Telegram
        try:
            telegram_client.send_message(response, chat_id)
        except Exception as e:
            logger.error(f"Telegram send error: {e}")

        return jsonify({"ok": True})

    except Exception as e:
        logger.error(f"Webhook error: {e}", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/send", methods=["POST"])
def send():
    """Manual send endpoint."""
    try:
        data = request.get_json()
        message = data.get("message")
        chat_id = data.get("chat_id")

        if not message:
            return jsonify({"error": "message required"}), 400

        result = telegram_client.send_message(message, chat_id)
        return jsonify({"ok": True, "result": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/history", methods=["GET"])
def history():
    """Get conversation history."""
    chat_id = request.args.get("chat_id")
    limit = request.args.get("limit", 50, type=int)

    if chat_id:
        messages = memory_manager.get_extended_context(chat_id, count=limit)
    else:
        messages = db.get_recent_messages(limit=limit)

    return jsonify({"messages": messages})


@app.route("/memory/stats", methods=["GET"])
def memory_stats():
    """Get memory statistics."""
    chat_id = request.args.get("chat_id", "")
    stats = memory_manager.get_stats(chat_id)
    return jsonify(stats)


@app.route("/memory/search", methods=["GET"])
def memory_search():
    """Search conversation history."""
    chat_id = request.args.get("chat_id", "")
    query = request.args.get("q")
    limit = request.args.get("limit", 5, type=int)

    if not query:
        return jsonify({"error": "q required"}), 400

    messages = memory_manager.search_history(query, chat_id, limit=limit)
    return jsonify({"messages": messages})


@app.route("/clear", methods=["POST"])
def clear():
    """Clear conversation history."""
    db.clear_messages()
    return jsonify({"ok": True, "message": "History cleared"})


@app.route("/webhook/set", methods=["POST"])
def set_webhook():
    """Set Telegram webhook URL."""
    try:
        data = request.get_json()
        url = data.get("url")
        if not url:
            return jsonify({"error": "url required"}), 400
        result = telegram_client.set_webhook(url)
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/webhook/info", methods=["GET"])
def webhook_info():
    """Get current webhook info."""
    try:
        return jsonify(telegram_client.get_webhook_info())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/webhook/delete", methods=["POST"])
def delete_webhook():
    """Delete current webhook."""
    try:
        return jsonify(telegram_client.delete_webhook())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ============================================
# NOTIFICATION ENDPOINTS
# ============================================

@app.route("/api/notifications/pending", methods=["GET"])
def get_pending_notifications():
    """Get all pending notifications for debugging."""
    priority = request.args.get("priority")
    channel = request.args.get("channel")

    pending = db.get_pending_notifications(priority=priority, channel=channel)

    return jsonify({
        "ok": True,
        "count": len(pending),
        "notifications": pending
    })


@app.route("/api/notifications/send", methods=["POST"])
def trigger_notification_batch():
    """Manually trigger P1 batch processing."""
    import notification_router

    count = notification_router.process_pending_batch()

    return jsonify({
        "ok": True,
        "sent": count
    })


@app.route("/api/notifications/test", methods=["POST"])
def test_notification():
    """Send a test notification (for debugging)."""
    import notification_router

    data = request.get_json() or {}
    message = data.get("message", "Test notification from Project Manager")
    priority = data.get("priority", "P1")

    if priority == "P0":
        result = notification_router.queue_p0(message, "test", None)
    elif priority == "P1":
        result = notification_router.queue_p1(message, "test", None)
    elif priority == "P2":
        result = notification_router.queue_p2(message)
    else:
        result = {"error": f"Invalid priority: {priority}"}

    return jsonify({"ok": True, "result": result})


@app.route("/api/notifications/check-deadlines", methods=["POST"])
def check_deadlines():
    """Manually trigger deadline check (for debugging)."""
    import notification_router

    notifications = notification_router.check_urgent_deadlines()

    return jsonify({
        "ok": True,
        "urgent_count": len(notifications),
        "notifications": notifications
    })


# ============================================
# EMAIL MONITOR ENDPOINTS (Phase 4)
# ============================================

@app.route("/api/email/scan", methods=["POST"])
def trigger_email_scan():
    """
    Manually trigger an email scan.

    Request body (optional):
        {"hours": 24}  - How many hours to look back

    Returns:
        MCP instructions for agent to execute gmail search
    """
    import email_monitor

    data = request.get_json() or {}
    hours = data.get("hours")

    result = email_monitor.run_email_scan(hours)

    return jsonify({
        "ok": True,
        "result": result
    })


@app.route("/api/email/status", methods=["GET"])
def get_email_status():
    """
    Get email scan status and configuration.

    Returns:
        Config and recent scan info
    """
    import email_monitor

    status = email_monitor.get_scan_status()

    return jsonify({
        "ok": True,
        "status": status
    })


@app.route("/api/email/classify", methods=["POST"])
def classify_email_endpoint():
    """
    Classify a single email (for testing/debugging).

    Request body:
        {
            "from": "sender@example.com",
            "subject": "Email subject",
            "body": "Email body text",
            "attachments": [{"id": "att1", "filename": "file.pdf"}]
        }

    Returns:
        Classification result
    """
    import email_monitor

    data = request.get_json() or {}

    if not data.get("from") and not data.get("subject"):
        return jsonify({"ok": False, "error": "from or subject required"}), 400

    classification = email_monitor.classify_email(data)

    return jsonify({
        "ok": True,
        "classification": classification
    })


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

    if not data.get("name") or not data.get("frequency"):
        return jsonify({"ok": False, "error": "name and frequency required"}), 400

    valid_frequencies = ["daily", "weekly", "biweekly", "monthly", "quarterly", "yearly", "custom"]
    if data["frequency"] not in valid_frequencies:
        return jsonify({
            "ok": False,
            "error": f"Invalid frequency. Must be one of: {', '.join(valid_frequencies)}"
        }), 400

    # Custom frequency requires cron_pattern
    if data["frequency"] == "custom" and not data.get("cron_pattern"):
        return jsonify({
            "ok": False,
            "error": "cron_pattern required for custom frequency"
        }), 400

    try:
        schedule = recurring_tasks.create_schedule(
            name=data["name"],
            frequency=data["frequency"],
            task_title_template=data.get("task_title_template"),
            project_id=data.get("project_id"),
            description=data.get("description"),
            cron_pattern=data.get("cron_pattern"),
            day_of_week=data.get("day_of_week"),
            day_of_month=data.get("day_of_month"),
            month_of_year=data.get("month_of_year"),
            time_of_day=data.get("time_of_day", "09:00"),
            task_description_template=data.get("task_description_template"),
            estimated_hours=data.get("estimated_hours"),
            priority=data.get("priority", 3),
            start_date=data.get("start_date"),
            end_date=data.get("end_date")
        )
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    return jsonify({
        "ok": True,
        "schedule": schedule
    })


@app.route("/api/recurring/<schedule_id>", methods=["GET"])
def get_recurring_schedule(schedule_id):
    """Get a specific recurring schedule."""
    import recurring_tasks

    schedule = recurring_tasks.get_schedule(schedule_id)

    if not schedule:
        return jsonify({"ok": False, "error": "Schedule not found"}), 404

    # Include generated tasks
    tasks = recurring_tasks.get_tasks_for_schedule(schedule_id)

    return jsonify({
        "ok": True,
        "schedule": schedule,
        "generated_tasks": tasks
    })


@app.route("/api/recurring/<schedule_id>", methods=["PUT"])
def update_recurring_schedule(schedule_id):
    """Update a recurring schedule."""
    import recurring_tasks

    data = request.get_json() or {}

    if not data:
        return jsonify({"ok": False, "error": "No data provided"}), 400

    # Validate frequency if provided
    if "frequency" in data:
        valid_frequencies = ["daily", "weekly", "biweekly", "monthly", "quarterly", "yearly", "custom"]
        if data["frequency"] not in valid_frequencies:
            return jsonify({
                "ok": False,
                "error": f"Invalid frequency. Must be one of: {', '.join(valid_frequencies)}"
            }), 400

    try:
        schedule = recurring_tasks.update_schedule(schedule_id, **data)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    if not schedule:
        return jsonify({"ok": False, "error": "Schedule not found"}), 404

    return jsonify({
        "ok": True,
        "schedule": schedule
    })


@app.route("/api/recurring/<schedule_id>", methods=["DELETE"])
def delete_recurring_schedule(schedule_id):
    """Deactivate a recurring schedule (soft delete)."""
    import recurring_tasks

    success = recurring_tasks.deactivate_schedule(schedule_id)

    return jsonify({
        "ok": success,
        "message": "Schedule deactivated" if success else "Schedule not found"
    })


@app.route("/api/recurring/<schedule_id>/activate", methods=["POST"])
def activate_recurring_schedule(schedule_id):
    """Reactivate a deactivated schedule."""
    import recurring_tasks

    success = recurring_tasks.activate_schedule(schedule_id)

    return jsonify({
        "ok": success,
        "message": "Schedule activated" if success else "Schedule not found"
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


@app.route("/api/recurring/due", methods=["GET"])
def get_due_schedules():
    """Get schedules that are currently due for generation."""
    import recurring_tasks

    due = recurring_tasks.get_due_schedules()

    return jsonify({
        "ok": True,
        "count": len(due),
        "schedules": due
    })


# ============================================
# DOCUMENT MANAGEMENT ENDPOINTS (Phase 6)
# ============================================

@app.route("/api/documents", methods=["GET"])
def list_documents_endpoint():
    """
    List/search documents.

    Query params:
        project_id: Filter by project
        type: Filter by document type
        q: Text search
        vendor: Filter by vendor
        category: Filter by category
        date_from: Date range start (YYYY-MM-DD)
        date_to: Date range end (YYYY-MM-DD)
        amount_min: Minimum amount
        amount_max: Maximum amount
        limit: Max results (default 50)
    """
    import document_manager

    docs = document_manager.search_documents(
        project_id=request.args.get("project_id"),
        query=request.args.get("q"),
        document_type=request.args.get("type"),
        vendor=request.args.get("vendor"),
        category=request.args.get("category"),
        date_from=request.args.get("date_from"),
        date_to=request.args.get("date_to"),
        amount_min=request.args.get("amount_min", type=float),
        amount_max=request.args.get("amount_max", type=float),
        limit=request.args.get("limit", 50, type=int)
    )

    return jsonify({
        "ok": True,
        "count": len(docs),
        "documents": docs
    })


@app.route("/api/documents", methods=["POST"])
def upload_document_endpoint():
    """
    Upload a new document.

    Request body:
        {
            "file_path": "/path/to/file",  (required)
            "project_id": "project_123",
            "task_id": "task_456",
            "document_type": "receipt",
            "metadata": {
                "vendor": "Amazon",
                "amount": 29.99,
                ...
            }
        }

    Returns:
        Document record and extraction request for AI processing
    """
    import document_manager

    data = request.get_json() or {}

    if not data.get("file_path"):
        return jsonify({"ok": False, "error": "file_path required"}), 400

    try:
        result = document_manager.upload_document(
            file_path=data["file_path"],
            project_id=data.get("project_id"),
            task_id=data.get("task_id"),
            document_type=data.get("document_type", "other"),
            copy_file=data.get("copy_file", True),
            metadata=data.get("metadata")
        )

        return jsonify({
            "ok": True,
            "document": result["document"],
            "extraction_method": result["extraction_method"],
            "needs_ai_extraction": result["needs_ai_extraction"],
            "extraction_request": result.get("extraction_request")
        })

    except FileNotFoundError as e:
        return jsonify({"ok": False, "error": str(e)}), 404
    except Exception as e:
        logger.error(f"Document upload error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/documents/<doc_id>", methods=["GET"])
def get_document_endpoint(doc_id):
    """Get a specific document by ID."""
    import document_manager

    doc = document_manager.get_document(doc_id)

    if not doc:
        return jsonify({"ok": False, "error": "Document not found"}), 404

    return jsonify({
        "ok": True,
        "document": doc
    })


@app.route("/api/documents/<doc_id>", methods=["PUT"])
def update_document_endpoint(doc_id):
    """
    Update document metadata.

    Request body:
        {
            "vendor": "Updated Vendor",
            "amount": 99.99,
            "category": "office",
            ...
        }
    """
    data = request.get_json() or {}

    if not data:
        return jsonify({"ok": False, "error": "No data provided"}), 400

    doc = db.update_document(doc_id, **data)

    if not doc:
        return jsonify({"ok": False, "error": "Document not found"}), 404

    return jsonify({
        "ok": True,
        "document": doc
    })


@app.route("/api/documents/<doc_id>", methods=["DELETE"])
def delete_document_endpoint(doc_id):
    """
    Delete a document.

    Query params:
        delete_file: true/false - also delete physical file
    """
    import document_manager

    delete_file = request.args.get("delete_file", "false").lower() == "true"

    success = document_manager.delete_document(doc_id, delete_file=delete_file)

    return jsonify({
        "ok": success,
        "message": "Document deleted" if success else "Document not found"
    })


@app.route("/api/documents/<doc_id>/extract", methods=["POST"])
def extract_document_metadata(doc_id):
    """
    Update document with AI-extracted metadata.

    Request body:
        {
            "metadata": {
                "vendor": "Amazon",
                "total": 29.99,
                "date": "2024-12-01",
                ...
            }
        }

    This endpoint is called after AI has processed the extraction request.
    """
    import document_manager

    data = request.get_json() or {}
    metadata = data.get("metadata", {})

    if not metadata:
        return jsonify({"ok": False, "error": "metadata required"}), 400

    doc = document_manager.update_document_metadata(doc_id, metadata)

    if not doc:
        return jsonify({"ok": False, "error": "Document not found"}), 404

    return jsonify({
        "ok": True,
        "document": doc
    })


@app.route("/api/documents/<doc_id>/question", methods=["POST"])
def ask_document_question(doc_id):
    """
    Build a request for asking a question about a document.

    Request body:
        {"question": "What items are on this receipt?"}

    Returns:
        Request structure for AI to process (includes doc context)
    """
    import document_manager

    data = request.get_json() or {}
    question = data.get("question")

    if not question:
        return jsonify({"ok": False, "error": "question required"}), 400

    request_data = document_manager.build_question_request(doc_id, question)

    if not request_data:
        return jsonify({"ok": False, "error": "Document not found"}), 404

    return jsonify({
        "ok": True,
        "request": request_data
    })


@app.route("/api/documents/stats", methods=["GET"])
def get_document_stats_endpoint():
    """Get document statistics."""
    import document_manager

    project_id = request.args.get("project_id")
    stats = document_manager.get_document_stats(project_id)

    return jsonify({
        "ok": True,
        "stats": stats
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 4000))
    logger.info(f"Starting Project Manager Agent on port {port}")
    logger.info(f"Context limit: {memory_manager.ACTIVE_CONTEXT_TOKENS:,} tokens")
    app.run(host="0.0.0.0", port=port, debug=False)
