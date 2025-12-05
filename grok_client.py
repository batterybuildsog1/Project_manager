#!/usr/bin/env python3
"""
Grok API client for Project Manager Agent.
Supports tool/function calling for memory retrieval.
"""

import os
import json
import urllib.request
import urllib.error
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

# API config
API_URL = "https://api.x.ai/v1/chat/completions"
API_KEY = os.environ.get("XAI_API_KEY_FAST", os.environ.get("XAI_API_KEY"))
MODEL = "grok-4-latest"

SYSTEM_PROMPT = """You are a helpful project manager assistant. You help the user manage tasks, track progress, and stay organized.

Your capabilities:
- Track tasks and deadlines
- Answer questions about projects
- Help prioritize work
- Remember context from our conversation

HISTORY TOOLS (use when needed):
- search_history(query) - Find past messages by keyword
- get_messages_by_date(date) - Get messages from a specific date
- get_extended_context(count) - Load more recent messages

When user asks about something not in visible context, USE THE TOOLS.
Don't guess or make up past conversations.

Be concise and actionable."""


def chat(
    messages: List[Dict[str, str]],
    system_prompt: str = None,
    tools: List[dict] = None,
    temperature: float = 0.7
) -> Dict[str, Any]:
    """
    Send messages to Grok and get a response.

    Args:
        messages: List of {"role": "user"|"assistant", "content": "..."}
        system_prompt: System instructions (default: SYSTEM_PROMPT)
        tools: Tool definitions for function calling
        temperature: Creativity (0-1)

    Returns:
        {"content": str, "tool_calls": list or None}
    """
    if not API_KEY:
        raise ValueError("XAI_API_KEY or XAI_API_KEY_FAST not set")

    if system_prompt is None:
        system_prompt = SYSTEM_PROMPT

    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            *messages
        ],
        "temperature": temperature,
        "stream": False
    }

    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "application/json"
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(API_URL, data=data, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
            message = result["choices"][0]["message"]

            return {
                "content": message.get("content", ""),
                "tool_calls": message.get("tool_calls")
            }
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8") if e.fp else ""
        raise RuntimeError(f"Grok API error {e.code}: {error_body}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Connection error: {e.reason}")


def chat_with_tools(
    messages: List[Dict[str, str]],
    tools: List[dict],
    tool_executor,
    chat_id: str,
    system_prompt: str = None,
    max_iterations: int = 3
) -> str:
    """
    Chat with automatic tool execution.

    Args:
        messages: Conversation messages
        tools: Tool definitions
        tool_executor: Function to execute tools (name, args, chat_id) -> result
        chat_id: Chat ID for tool execution
        system_prompt: Optional system prompt
        max_iterations: Max tool call iterations

    Returns:
        Final response text
    """
    current_messages = messages.copy()

    for _ in range(max_iterations):
        response = chat(current_messages, system_prompt=system_prompt, tools=tools)

        if not response["tool_calls"]:
            return response["content"]

        # Add assistant message with tool calls
        current_messages.append({
            "role": "assistant",
            "content": response["content"] or "",
            "tool_calls": response["tool_calls"]
        })

        # Execute each tool and add results
        for tool_call in response["tool_calls"]:
            func_name = tool_call["function"]["name"]
            args = json.loads(tool_call["function"]["arguments"])

            logger.info(f"Executing tool: {func_name}({args})")
            result = tool_executor(func_name, args, chat_id)

            current_messages.append({
                "role": "tool",
                "tool_call_id": tool_call["id"],
                "content": json.dumps(result)
            })

    # Final call without tools
    response = chat(current_messages, system_prompt=system_prompt)
    return response["content"]


def quick_response(user_message: str, context: List[Dict[str, str]] = None) -> str:
    """Get a quick response (no tools)."""
    messages = context or []
    messages.append({"role": "user", "content": user_message})
    response = chat(messages)
    return response["content"]


if __name__ == "__main__":
    response = quick_response("Hello! What can you help me with?")
    print(response)
