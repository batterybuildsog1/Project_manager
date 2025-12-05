#!/usr/bin/env python3
"""
Telegram client for Project Manager Agent.
Reuses existing telegram_lib pattern.
"""

import os
import json
import urllib.request
import urllib.error
from typing import Dict, Any

# Telegram config
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
API_BASE = "https://api.telegram.org/bot"


def send_message(text: str, chat_id: int = None, parse_mode: str = None) -> Dict[str, Any]:
    """
    Send a message to Telegram.

    Args:
        text: Message text
        chat_id: Target chat (defaults to TELEGRAM_CHAT_ID)
        parse_mode: "Markdown" or "HTML" for formatting

    Returns:
        API response
    """
    if not BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN not set")

    target_chat = chat_id or CHAT_ID
    if not target_chat:
        raise ValueError("No chat_id specified")

    url = f"{API_BASE}{BOT_TOKEN}/sendMessage"

    payload = {
        "chat_id": int(target_chat),
        "text": text
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
            if not result.get("ok"):
                raise RuntimeError(f"Telegram API error: {result}")
            return result.get("result", {})
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8") if e.fp else ""
        raise RuntimeError(f"Telegram API error {e.code}: {error_body}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Connection error: {e.reason}")


def set_webhook(url: str) -> Dict[str, Any]:
    """
    Set the Telegram webhook URL.

    Args:
        url: Public URL for webhook (e.g., ngrok URL + /webhook)

    Returns:
        API response
    """
    if not BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN not set")

    api_url = f"{API_BASE}{BOT_TOKEN}/setWebhook"

    payload = {"url": url}
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(api_url, data=data, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
            return result
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8") if e.fp else ""
        raise RuntimeError(f"Telegram API error {e.code}: {error_body}")


def delete_webhook() -> Dict[str, Any]:
    """Delete the current webhook (for switching to polling)."""
    if not BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN not set")

    api_url = f"{API_BASE}{BOT_TOKEN}/deleteWebhook"
    req = urllib.request.Request(api_url)

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
            return result
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8") if e.fp else ""
        raise RuntimeError(f"Telegram API error {e.code}: {error_body}")


def get_webhook_info() -> Dict[str, Any]:
    """Get current webhook info."""
    if not BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN not set")

    api_url = f"{API_BASE}{BOT_TOKEN}/getWebhookInfo"
    req = urllib.request.Request(api_url)

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
            return result.get("result", {})
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8") if e.fp else ""
        raise RuntimeError(f"Telegram API error {e.code}: {error_body}")


def parse_update(update: Dict[str, Any]) -> Dict[str, Any]:
    """
    Parse a Telegram update into a simpler format.

    Args:
        update: Raw Telegram update object

    Returns:
        Parsed message with text, chat_id, from_user, etc.
    """
    message = update.get("message", {})

    return {
        "update_id": update.get("update_id"),
        "message_id": message.get("message_id"),
        "text": message.get("text", ""),
        "chat_id": message.get("chat", {}).get("id"),
        "from_user": message.get("from", {}).get("first_name", "Unknown"),
        "from_id": message.get("from", {}).get("id"),
        "date": message.get("date")
    }


# Test
if __name__ == "__main__":
    info = get_webhook_info()
    print(f"Current webhook: {info.get('url', 'None')}")
