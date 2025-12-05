#!/usr/bin/env python3
"""
Project Manager Agent - Flask Server

Runs on port 4000, receives Telegram webhooks, responds via Grok 4.1.
"""

import os
import sys
import json
import logging
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from flask import Flask, request, jsonify, Response
import db
import grok_client
import telegram_client
import task_manager
import toc_engine
import memory_manager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Context window for conversation
CONTEXT_MESSAGES = 10


@app.route("/", methods=["GET"])
def health():
    """Health check endpoint."""
    return jsonify({
        "status": "ok",
        "service": "project-manager-agent",
        "port": 4000
    })


@app.route("/webhook", methods=["POST"])
def webhook():
    """
    Telegram webhook endpoint.
    Receives updates, processes with Grok, responds.
    Uses memory_manager for unlimited conversation history.
    """
    try:
        update = request.get_json()
        logger.info(f"Received update: {json.dumps(update, indent=2)}")

        # Parse the update
        parsed = telegram_client.parse_update(update)
        text = parsed.get("text", "").strip()
        chat_id = parsed.get("chat_id")
        from_user = parsed.get("from_user")

        if not text or not chat_id:
            logger.info("No text or chat_id, ignoring")
            return jsonify({"ok": True})

        logger.info(f"Message from {from_user}: {text}")

        # Save user message to memory manager (handles both new and legacy tables)
        memory_manager.add_message_to_conversation(
            chat_id=str(chat_id),
            role="user",
            content=text,
            sender_name=from_user
        )

        # Build context with auto-compaction (handles large conversations)
        context, compaction_result = memory_manager.build_context_with_auto_compact(
            chat_id=str(chat_id)
        )

        if compaction_result and compaction_result.get("compacted", 0) > 0:
            logger.info(f"Auto-compacted {compaction_result['compacted']} messages, saved {compaction_result['tokens_saved']} tokens")

        # Get response from Grok with full context
        try:
            # context[0] is system message, rest are conversation
            response = grok_client.chat(
                messages=context[1:],  # Skip system, it's handled by chat()
                system_prompt=context[0]["content"]  # Use our built system prompt with summaries
            )
            logger.info(f"Grok response: {response[:100]}...")
        except Exception as e:
            logger.error(f"Grok error: {e}")
            response = f"Sorry, I encountered an error: {str(e)}"

        # Save assistant response to memory manager
        memory_manager.add_message_to_conversation(
            chat_id=str(chat_id),
            role="assistant",
            content=response
        )

        # Send response to Telegram
        try:
            telegram_client.send_message(response, chat_id)
            logger.info("Response sent to Telegram")
        except Exception as e:
            logger.error(f"Telegram send error: {e}")

        return jsonify({"ok": True})

    except Exception as e:
        logger.error(f"Webhook error: {e}", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/send", methods=["POST"])
def send():
    """
    Manual send endpoint (for testing).
    POST {"message": "text", "chat_id": optional}
    """
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
    limit = request.args.get("limit", 20, type=int)
    chat_id = request.args.get("chat_id")

    if chat_id:
        # Use memory manager for chat-specific history
        messages = memory_manager.get_recent_conversation_messages(str(chat_id), limit=limit)
    else:
        # Fall back to legacy for all messages
        messages = db.get_recent_messages(limit=limit)

    return jsonify({"messages": messages})


@app.route("/memory/stats", methods=["GET"])
def memory_stats():
    """Get memory usage statistics for a conversation."""
    chat_id = request.args.get("chat_id")

    if not chat_id:
        return jsonify({"error": "chat_id required"}), 400

    stats = memory_manager.get_memory_stats(str(chat_id))
    return jsonify(stats)


@app.route("/memory/compact", methods=["POST"])
def memory_compact():
    """Force compaction of a conversation."""
    try:
        data = request.get_json() or {}
        chat_id = data.get("chat_id")

        if not chat_id:
            return jsonify({"error": "chat_id required"}), 400

        result = memory_manager.compact_conversation(str(chat_id))
        return jsonify({"ok": True, "result": result})

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/memory/search", methods=["GET"])
def memory_search():
    """Search conversation history."""
    chat_id = request.args.get("chat_id")
    query = request.args.get("q")
    limit = request.args.get("limit", 5, type=int)

    if not chat_id or not query:
        return jsonify({"error": "chat_id and q required"}), 400

    messages = memory_manager.search_history(query, str(chat_id), limit=limit)
    summaries = memory_manager.search_summaries(query, str(chat_id), limit=3)

    return jsonify({
        "messages": messages,
        "summaries": summaries
    })


@app.route("/clear", methods=["POST"])
def clear():
    """Clear conversation history."""
    db.clear_messages()
    return jsonify({"ok": True, "message": "History cleared"})


@app.route("/webhook/set", methods=["POST"])
def set_webhook():
    """
    Set Telegram webhook URL.
    POST {"url": "https://your-ngrok-url.ngrok.io/webhook"}
    """
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
        result = telegram_client.get_webhook_info()
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/webhook/delete", methods=["POST"])
def delete_webhook():
    """Delete current webhook."""
    try:
        result = telegram_client.delete_webhook()
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 4000))
    logger.info(f"Starting Project Manager Agent on port {port}")
    logger.info("Endpoints:")
    logger.info("  GET  /              - Health check")
    logger.info("  POST /webhook       - Telegram webhook")
    logger.info("  POST /send          - Manual send")
    logger.info("  GET  /history       - View history (?chat_id=&limit=)")
    logger.info("  POST /clear         - Clear history")
    logger.info("  POST /webhook/set   - Set webhook URL")
    logger.info("  GET  /webhook/info  - Get webhook info")
    logger.info("Memory Management:")
    logger.info("  GET  /memory/stats  - Memory usage stats (?chat_id=)")
    logger.info("  POST /memory/compact - Force compaction")
    logger.info("  GET  /memory/search - Search history (?chat_id=&q=)")
    app.run(host="0.0.0.0", port=port, debug=False)
