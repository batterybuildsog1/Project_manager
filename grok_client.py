#!/usr/bin/env python3
"""
Grok 4.1 API client for Project Manager Agent.
Uses the fast reasoning model for cost-effective responses.
"""

import os
import json
import urllib.request
import urllib.error
from typing import List, Dict, Any, Optional

# Grok 4.1 API config
API_URL = "https://api.x.ai/v1/chat/completions"
API_KEY = os.environ.get("XAI_API_KEY_FAST", os.environ.get("XAI_API_KEY"))
MODEL = "grok-4-latest"

SYSTEM_PROMPT = """You are a helpful project manager assistant. You help the user manage tasks, track progress, and stay organized.

Your capabilities:
- Track tasks and deadlines
- Answer questions about projects
- Help prioritize work
- Remember context from our conversation

Be concise and actionable. When the user asks about tasks, help them break things down into next steps."""


def chat(
    messages: List[Dict[str, str]],
    system_prompt: str = SYSTEM_PROMPT,
    temperature: float = 0.7
) -> str:
    """
    Send messages to Grok and get a response.

    Args:
        messages: List of {"role": "user"|"assistant", "content": "..."}
        system_prompt: System instructions
        temperature: Creativity (0-1)

    Returns:
        Assistant's response text
    """
    if not API_KEY:
        raise ValueError("XAI_API_KEY or XAI_API_KEY_FAST not set")

    # Build request
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            *messages
        ],
        "temperature": temperature,
        "stream": False
    }

    # Browser-like headers to avoid Cloudflare bot detection (error 1010)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://x.ai",
        "Referer": "https://x.ai/"
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(API_URL, data=data, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
            return result["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8") if e.fp else ""
        raise RuntimeError(f"Grok API error {e.code}: {error_body}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Connection error: {e.reason}")


def quick_response(user_message: str, context: List[Dict[str, str]] = None) -> str:
    """
    Get a quick response with optional context.

    Args:
        user_message: The user's message
        context: Previous messages for context

    Returns:
        Assistant's response
    """
    messages = context or []
    messages.append({"role": "user", "content": user_message})
    return chat(messages)


# Test
if __name__ == "__main__":
    response = quick_response("Hello! What can you help me with?")
    print(response)
