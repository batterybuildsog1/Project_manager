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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 4000))
    logger.info(f"Starting Project Manager Agent on port {port}")
    logger.info(f"Context limit: {memory_manager.ACTIVE_CONTEXT_TOKENS:,} tokens")
    app.run(host="0.0.0.0", port=port, debug=False)
