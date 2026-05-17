"""
Claude CLI -> OpenAI-compatible API Proxy

Wraps claude.exe to serve OpenAI-format chat completions.
Uses Claude Haiku 4.5 with max thinking effort.

No Anthropic API key needed — uses Claude CLI's built-in auth.
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

CLAUDE_PATH = os.environ.get(
    "CLAUDE_PATH", r"C:\Users\AG\.local\bin\claude.exe"
)
MODEL = os.environ.get("CLAUDE_MODEL", "haiku")
EFFORT = os.environ.get("CLAUDE_EFFORT", "max")
PORT = int(os.environ.get("PORT", "8082"))
TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "300"))

app = FastAPI(title="Claude CLI OpenAI Proxy")


class CLIError(Exception):
    pass


class RateLimitError(Exception):
    pass


def _run_claude(cmd: list, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )


async def call_claude(
    system: str,
    prompt: str,
    timeout: int = TIMEOUT,
) -> tuple:
    """Call claude.exe and return (response_text, usage_dict)."""
    cmd = [
        CLAUDE_PATH, "-p",
        "--no-session-persistence",
        "--model", MODEL,
        "--effort", EFFORT,
        "--output-format", "json",
    ]
    if system:
        cmd.extend(["--system-prompt", system])
    cmd.append(prompt)

    logger.info(
        "Claude call: prompt=%d chars, system=%d chars",
        len(prompt), len(system),
    )

    try:
        result = await asyncio.to_thread(_run_claude, cmd, timeout)
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"Claude CLI timed out after {timeout}s")

    stdout = result.stdout.strip()
    stderr = result.stderr.strip()

    if result.returncode != 0:
        logger.error("Claude CLI exit %d: %s", result.returncode, stderr[:500])
        low = (stderr + stdout).lower()
        if "rate" in low or "429" in low or "overloaded" in low:
            raise RateLimitError(stderr[:500])
        raise CLIError(f"Exit code {result.returncode}: {stderr[:500]}")

    if not stdout:
        raise CLIError("Empty response from Claude CLI")

    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        m = re.search(r'\{.*"type"\s*:\s*"result".*\}', stdout, re.DOTALL)
        if m:
            envelope = json.loads(m.group(0))
        else:
            raise CLIError(f"Invalid JSON from CLI: {stdout[:300]}")

    if envelope.get("is_error"):
        msg = str(envelope.get("result", "Unknown error"))
        if "rate" in msg.lower() or "overloaded" in msg.lower():
            raise RateLimitError(msg)
        raise CLIError(msg)

    response_text = envelope.get("result", "")

    usage_raw = envelope.get("usage", {})
    usage = {
        "prompt_tokens": usage_raw.get("input_tokens", 0)
        + usage_raw.get("cache_read_input_tokens", 0)
        + usage_raw.get("cache_creation_input_tokens", 0),
        "completion_tokens": usage_raw.get("output_tokens", 0),
    }
    usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]

    cost = envelope.get("total_cost_usd", 0)
    dur = envelope.get("duration_ms", 0)
    logger.info("Claude response: $%.6f, %dms, %d tokens", cost, dur, usage["total_tokens"])

    return response_text, usage


def parse_json_from_text(text) -> dict:
    """Extract a JSON object from Claude's response."""
    if isinstance(text, dict):
        return text

    text = str(text).strip()

    # Try direct parse
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass

    # Strip markdown code blocks
    m = re.search(r"```(?:json)?\s*\n(.*?)\n\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Find outermost JSON object
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    start = None

    raise ValueError(f"Could not parse JSON from: {text[:300]}")


def build_tool_system_prompt(system: str, tool_schema: dict) -> str:
    """Augment system prompt with JSON output instructions for tool calls."""
    schema_str = json.dumps(tool_schema, indent=2)
    return (
        f"{system}\n\n"
        "CRITICAL OUTPUT REQUIREMENT:\n"
        "You MUST respond with ONLY a valid JSON object matching this schema:\n"
        f"```json\n{schema_str}\n```\n"
        "Output the raw JSON only. No markdown fences, no explanation, "
        "no extra text before or after the JSON."
    )


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()

    system_msg = ""
    user_msg = ""
    for m in body.get("messages", []):
        if m["role"] == "system":
            system_msg = m["content"]
        elif m["role"] == "user":
            user_msg = m["content"]

    tools = body.get("tools")
    tool_choice = body.get("tool_choice")

    try:
        if tools and tool_choice:
            tool = tools[0]
            func = tool.get("function", tool)
            tool_name = func.get("name", "")
            if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
                tool_name = tool_choice["function"]["name"]

            schema = func.get("parameters", {})

            augmented_system = build_tool_system_prompt(system_msg, schema)
            result_text, usage = await call_claude(augmented_system, user_msg)
            tool_args = parse_json_from_text(result_text)

            return JSONResponse(content={
                "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": MODEL,
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": f"call_{uuid.uuid4().hex[:12]}",
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(
                                    tool_args, ensure_ascii=False
                                ),
                            },
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": usage,
            })
        else:
            result_text, usage = await call_claude(system_msg, user_msg)
            return JSONResponse(content={
                "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": MODEL,
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": result_text,
                    },
                    "finish_reason": "stop",
                }],
                "usage": usage,
            })

    except RateLimitError as e:
        return JSONResponse(
            status_code=429,
            content={"error": {"message": str(e), "type": "rate_limit_error"}},
        )
    except (CLIError, ValueError) as e:
        return JSONResponse(
            status_code=500,
            content={"error": {"message": str(e), "type": "internal_error"}},
        )
    except TimeoutError as e:
        return JSONResponse(
            status_code=504,
            content={"error": {"message": str(e), "type": "timeout_error"}},
        )
    except Exception as e:
        logger.error("Unexpected error: %s", e, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"error": {"message": str(e), "type": "internal_error"}},
        )


@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [{
            "id": MODEL,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "anthropic",
        }],
    }


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "model": MODEL,
        "effort": EFFORT,
        "claude_path": CLAUDE_PATH,
    }


if __name__ == "__main__":
    logger.info(
        "Claude CLI -> OpenAI proxy | port=%d | model=%s | effort=%s",
        PORT, MODEL, EFFORT,
    )
    logger.info("Claude path: %s", CLAUDE_PATH)
    uvicorn.run(app, host="0.0.0.0", port=PORT)
