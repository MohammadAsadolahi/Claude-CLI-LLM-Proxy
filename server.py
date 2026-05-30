"""
Claude CLI -> OpenAI-compatible API Proxy

Wraps claude.exe to serve OpenAI-format chat completions and batch processing.
Uses Claude Haiku 4.5 with low thinking effort by default.

No Anthropic API key needed — uses Claude CLI's built-in auth.

Batch API mirrors OpenAI's /v1/files + /v1/batches workflow:
  1. Upload JSONL  →  POST /v1/files
  2. Create batch  →  POST /v1/batches
  3. Poll status   →  GET  /v1/batches/{id}
  4. Get results   →  GET  /v1/files/{id}/content
"""

import asyncio
import json
import logging
import os

from dotenv import load_dotenv
load_dotenv()
import random
import re
import subprocess
import time
import uuid

from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse, Response
import uvicorn

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── Configuration ───────────────────────────────────────────────────────
CLAUDE_PATH = os.environ.get(
    "CLAUDE_PATH", r"C:\Users\AG\.local\bin\claude.exe"
)
MODEL = os.environ.get("CLAUDE_MODEL", "haiku")
EFFORT = os.environ.get("CLAUDE_EFFORT", "low")
PORT = int(os.environ.get("PORT", "8082"))
MAX_THINKING_TOKENS = os.environ.get("MAX_THINKING_TOKENS", "")
TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "300"))
BATCH_MAX_CONCURRENT = int(os.environ.get("BATCH_MAX_CONCURRENT", "3"))
BATCH_MAX_RETRIES = int(os.environ.get("BATCH_MAX_RETRIES", "5"))

app = FastAPI(title="Claude CLI OpenAI Proxy")


# ── Exceptions ──────────────────────────────────────────────────────────

class CLIError(Exception):
    pass


class RateLimitError(Exception):
    pass


# ── In-memory stores ───────────────────────────────────────────────────
file_store: dict = {}
batch_store: dict = {}
batch_cancel_events: dict = {}


# ── Claude CLI wrapper ─────────────────────────────────────────────────

def _run_claude(cmd: list, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
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
    if MAX_THINKING_TOKENS:
        cmd.extend(["--max-thinking-tokens", MAX_THINKING_TOKENS])
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


# ── JSON / tool helpers ─────────────────────────────────────────────────

def parse_json_from_text(text) -> dict:
    """Extract a JSON object from Claude's response."""
    if isinstance(text, dict):
        return text

    text = str(text).strip()

    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass

    m = re.search(r"```(?:json)?\s*\n(.*?)\n\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass

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


# ── Shared request processing ──────────────────────────────────────────

def _build_chat_response(result_text: str, usage: dict) -> dict:
    return {
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
    }


def _build_tool_response(tool_name: str, tool_args: dict, usage: dict) -> dict:
    return {
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
                        "arguments": json.dumps(tool_args, ensure_ascii=False),
                    },
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": usage,
    }


async def _process_chat_request(body: dict) -> dict:
    """Process a single chat completion request body → OpenAI response body.

    Raises CLIError, RateLimitError, TimeoutError, ValueError on failure.
    """
    system_msg = ""
    user_msg = ""
    for m in body.get("messages", []):
        if m["role"] == "system":
            system_msg = m["content"]
        elif m["role"] == "user":
            user_msg = m["content"]

    tools = body.get("tools")
    tool_choice = body.get("tool_choice")

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
        return _build_tool_response(tool_name, tool_args, usage)
    else:
        result_text, usage = await call_claude(system_msg, user_msg)
        return _build_chat_response(result_text, usage)


def _openai_error(status_code: int, message: str, error_type: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": error_type}},
    )


# ═══════════════════════════════════════════════════════════════════════
#  Chat Completions
# ═══════════════════════════════════════════════════════════════════════

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    try:
        return JSONResponse(content=await _process_chat_request(body))
    except RateLimitError as e:
        return _openai_error(429, str(e), "rate_limit_error")
    except (CLIError, ValueError) as e:
        return _openai_error(500, str(e), "internal_error")
    except TimeoutError as e:
        return _openai_error(504, str(e), "timeout_error")
    except Exception as e:
        logger.error("Unexpected error: %s", e, exc_info=True)
        return _openai_error(500, str(e), "internal_error")


# ═══════════════════════════════════════════════════════════════════════
#  Files API
# ═══════════════════════════════════════════════════════════════════════

@app.post("/v1/files")
async def upload_file(file: UploadFile = File(...), purpose: str = Form(...)):
    content = await file.read()
    file_id = f"file-{uuid.uuid4().hex[:24]}"
    obj = {
        "id": file_id,
        "object": "file",
        "bytes": len(content),
        "created_at": int(time.time()),
        "filename": file.filename or "upload.jsonl",
        "purpose": purpose,
        "status": "processed",
        "_content": content,
    }
    file_store[file_id] = obj
    logger.info("File uploaded: %s (%d bytes, purpose=%s)", file_id, len(content), purpose)
    return JSONResponse(content={k: v for k, v in obj.items() if not k.startswith("_")})


@app.get("/v1/files")
async def list_files():
    data = [{k: v for k, v in f.items() if not k.startswith("_")} for f in file_store.values()]
    return JSONResponse(content={"object": "list", "data": data})


@app.get("/v1/files/{file_id}")
async def get_file(file_id: str):
    if file_id not in file_store:
        return _openai_error(404, f"No such File object: {file_id}", "invalid_request_error")
    return JSONResponse(content={k: v for k, v in file_store[file_id].items() if not k.startswith("_")})


@app.get("/v1/files/{file_id}/content")
async def get_file_content(file_id: str):
    if file_id not in file_store:
        return _openai_error(404, f"No such File object: {file_id}", "invalid_request_error")
    content = file_store[file_id]["_content"]
    if isinstance(content, str):
        content = content.encode("utf-8")
    return Response(content=content, media_type="application/octet-stream")


@app.delete("/v1/files/{file_id}")
async def delete_file(file_id: str):
    if file_id not in file_store:
        return _openai_error(404, f"No such File object: {file_id}", "invalid_request_error")
    del file_store[file_id]
    return JSONResponse(content={"id": file_id, "object": "file", "deleted": True})


# ═══════════════════════════════════════════════════════════════════════
#  Batches API
# ═══════════════════════════════════════════════════════════════════════

def _new_batch_object(batch_id, input_file_id, endpoint, completion_window, metadata=None):
    now = int(time.time())
    return {
        "id": batch_id,
        "object": "batch",
        "endpoint": endpoint,
        "errors": None,
        "input_file_id": input_file_id,
        "completion_window": completion_window,
        "status": "validating",
        "output_file_id": None,
        "error_file_id": None,
        "created_at": now,
        "in_progress_at": None,
        "expires_at": now + 86400,
        "finalizing_at": None,
        "completed_at": None,
        "failed_at": None,
        "expired_at": None,
        "cancelling_at": None,
        "cancelled_at": None,
        "request_counts": {"total": 0, "completed": 0, "failed": 0},
        "metadata": metadata,
    }


@app.post("/v1/batches")
async def create_batch(request: Request):
    body = await request.json()
    input_file_id = body.get("input_file_id")
    endpoint = body.get("endpoint", "/v1/chat/completions")
    completion_window = body.get("completion_window", "24h")
    metadata = body.get("metadata")

    if not input_file_id or input_file_id not in file_store:
        return _openai_error(400, f"Invalid file id: {input_file_id}", "invalid_request_error")
    if endpoint != "/v1/chat/completions":
        return _openai_error(
            400,
            f"Unsupported endpoint: {endpoint}. Only /v1/chat/completions is supported.",
            "invalid_request_error",
        )

    batch_id = f"batch_{uuid.uuid4().hex[:24]}"
    batch_obj = _new_batch_object(batch_id, input_file_id, endpoint, completion_window, metadata)
    batch_store[batch_id] = batch_obj

    cancel_event = asyncio.Event()
    batch_cancel_events[batch_id] = cancel_event
    asyncio.create_task(_run_batch(batch_id, cancel_event))

    logger.info("Batch created: %s (input=%s)", batch_id, input_file_id)
    return JSONResponse(content=batch_obj)


@app.get("/v1/batches/{batch_id}")
async def get_batch(batch_id: str):
    if batch_id not in batch_store:
        return _openai_error(404, f"No such Batch: {batch_id}", "invalid_request_error")
    return JSONResponse(content=batch_store[batch_id])


@app.post("/v1/batches/{batch_id}/cancel")
async def cancel_batch(batch_id: str):
    if batch_id not in batch_store:
        return _openai_error(404, f"No such Batch: {batch_id}", "invalid_request_error")
    batch = batch_store[batch_id]
    if batch["status"] in ("completed", "failed", "cancelled", "expired"):
        return _openai_error(
            400, f"Cannot cancel batch with status '{batch['status']}'", "invalid_request_error"
        )
    batch["status"] = "cancelling"
    batch["cancelling_at"] = int(time.time())
    if batch_id in batch_cancel_events:
        batch_cancel_events[batch_id].set()
    return JSONResponse(content=batch)


@app.get("/v1/batches")
async def list_batches(limit: int = 20, after: str = None):
    batches = sorted(batch_store.values(), key=lambda b: b["created_at"], reverse=True)
    if after:
        idx = next((i for i, b in enumerate(batches) if b["id"] == after), -1)
        if idx >= 0:
            batches = batches[idx + 1:]
    batches = batches[:limit]
    return JSONResponse(content={
        "object": "list",
        "data": batches,
        "first_id": batches[0]["id"] if batches else None,
        "last_id": batches[-1]["id"] if batches else None,
        "has_more": len(batches) == limit,
    })


# ── Batch background processor ─────────────────────────────────────────

async def _process_batch_item(
    req: dict,
    semaphore: asyncio.Semaphore,
    cancel_event: asyncio.Event,
    batch_id: str,
) -> tuple:
    """Process one JSONL line from the batch input.

    Returns (result_dict, is_success).
    The semaphore is acquired only for the actual Claude call, released during
    backoff so other items can proceed.
    """
    custom_id = req.get("custom_id", "")
    body = req.get("body", {})
    req_id = f"batch_req_{uuid.uuid4().hex[:24]}"

    for attempt in range(BATCH_MAX_RETRIES):
        if cancel_event.is_set():
            return {
                "id": req_id,
                "custom_id": custom_id,
                "response": None,
                "error": {"code": "cancelled", "message": "Batch was cancelled"},
            }, False

        try:
            async with semaphore:
                response_body = await _process_chat_request(body)

            batch_store[batch_id]["request_counts"]["completed"] += 1
            return {
                "id": req_id,
                "custom_id": custom_id,
                "response": {
                    "status_code": 200,
                    "request_id": f"req_{uuid.uuid4().hex[:24]}",
                    "body": response_body,
                },
                "error": None,
            }, True

        except RateLimitError as e:
            if attempt < BATCH_MAX_RETRIES - 1:
                delay = min(2 * (2 ** attempt), 120)
                delay += delay * 0.5 * (2 * random.random() - 1)
                delay = max(1, delay)
                logger.warning(
                    "Batch %s item %s: rate limit — retry in %.1fs (%d/%d)",
                    batch_id, custom_id, delay, attempt + 1, BATCH_MAX_RETRIES,
                )
                await asyncio.sleep(delay)
                continue

            batch_store[batch_id]["request_counts"]["failed"] += 1
            return {
                "id": req_id,
                "custom_id": custom_id,
                "response": {
                    "status_code": 429,
                    "request_id": f"req_{uuid.uuid4().hex[:24]}",
                    "body": {"error": {"message": str(e), "type": "rate_limit_error"}},
                },
                "error": {"code": "rate_limit_exceeded", "message": str(e)},
            }, False

        except TimeoutError as e:
            if attempt < BATCH_MAX_RETRIES - 1:
                delay = min(2 * (2 ** attempt), 120)
                logger.warning(
                    "Batch %s item %s: timeout — retry in %.1fs (%d/%d)",
                    batch_id, custom_id, delay, attempt + 1, BATCH_MAX_RETRIES,
                )
                await asyncio.sleep(delay)
                continue

            batch_store[batch_id]["request_counts"]["failed"] += 1
            return {
                "id": req_id,
                "custom_id": custom_id,
                "response": {
                    "status_code": 504,
                    "request_id": f"req_{uuid.uuid4().hex[:24]}",
                    "body": {"error": {"message": str(e), "type": "timeout_error"}},
                },
                "error": {"code": "timeout", "message": str(e)},
            }, False

        except (CLIError, ValueError) as e:
            batch_store[batch_id]["request_counts"]["failed"] += 1
            return {
                "id": req_id,
                "custom_id": custom_id,
                "response": {
                    "status_code": 500,
                    "request_id": f"req_{uuid.uuid4().hex[:24]}",
                    "body": {"error": {"message": str(e), "type": "internal_error"}},
                },
                "error": {"code": "internal_error", "message": str(e)},
            }, False

        except Exception as e:
            logger.error("Batch %s item %s: unexpected: %s", batch_id, custom_id, e, exc_info=True)
            batch_store[batch_id]["request_counts"]["failed"] += 1
            return {
                "id": req_id,
                "custom_id": custom_id,
                "response": {
                    "status_code": 500,
                    "request_id": f"req_{uuid.uuid4().hex[:24]}",
                    "body": {"error": {"message": str(e), "type": "internal_error"}},
                },
                "error": {"code": "internal_error", "message": str(e)},
            }, False

    batch_store[batch_id]["request_counts"]["failed"] += 1
    return {
        "id": req_id,
        "custom_id": custom_id,
        "response": None,
        "error": {"code": "max_retries_exceeded", "message": "All retry attempts exhausted"},
    }, False


async def _run_batch(batch_id: str, cancel_event: asyncio.Event):
    """Background task that processes every request in a batch."""
    batch = batch_store[batch_id]

    # ── Validate input file ──
    input_file = file_store.get(batch["input_file_id"])
    if not input_file:
        batch["status"] = "failed"
        batch["failed_at"] = int(time.time())
        batch["errors"] = {
            "object": "list",
            "data": [{"code": "invalid_file", "message": "Input file not found"}],
        }
        return

    content = input_file["_content"]
    if isinstance(content, bytes):
        content = content.decode("utf-8")

    # ── Parse JSONL ──
    lines = [ln.strip() for ln in content.strip().split("\n") if ln.strip()]
    requests_list: list[dict] = []
    for i, line in enumerate(lines):
        try:
            requests_list.append(json.loads(line))
        except json.JSONDecodeError as e:
            batch["status"] = "failed"
            batch["failed_at"] = int(time.time())
            batch["errors"] = {
                "object": "list",
                "data": [{"code": "invalid_json", "message": f"Line {i + 1}: {e}", "line": i + 1}],
            }
            logger.error("Batch %s: invalid JSON on line %d", batch_id, i + 1)
            return

    if not requests_list:
        batch["request_counts"]["total"] = 0
        batch["status"] = "completed"
        batch["completed_at"] = int(time.time())
        logger.info("Batch %s: empty input — nothing to process", batch_id)
        return

    batch["request_counts"]["total"] = len(requests_list)
    batch["status"] = "in_progress"
    batch["in_progress_at"] = int(time.time())
    logger.info(
        "Batch %s: processing %d requests (max_concurrent=%d, max_retries=%d)",
        batch_id, len(requests_list), BATCH_MAX_CONCURRENT, BATCH_MAX_RETRIES,
    )

    # ── Process concurrently ──
    semaphore = asyncio.Semaphore(BATCH_MAX_CONCURRENT)
    tasks = [
        _process_batch_item(req, semaphore, cancel_event, batch_id)
        for req in requests_list
    ]
    results = await asyncio.gather(*tasks)

    # ── Build output / error files ──
    output_lines: list[str] = []
    error_lines: list[str] = []
    for result_line, is_success in results:
        line_json = json.dumps(result_line, ensure_ascii=False)
        if is_success:
            output_lines.append(line_json)
        else:
            error_lines.append(line_json)

    if output_lines:
        out_id = f"file-{uuid.uuid4().hex[:24]}"
        out_bytes = "\n".join(output_lines).encode("utf-8")
        file_store[out_id] = {
            "id": out_id,
            "object": "file",
            "bytes": len(out_bytes),
            "created_at": int(time.time()),
            "filename": f"batch_{batch_id}_output.jsonl",
            "purpose": "batch_output",
            "status": "processed",
            "_content": out_bytes,
        }
        batch["output_file_id"] = out_id

    if error_lines:
        err_id = f"file-{uuid.uuid4().hex[:24]}"
        err_bytes = "\n".join(error_lines).encode("utf-8")
        file_store[err_id] = {
            "id": err_id,
            "object": "file",
            "bytes": len(err_bytes),
            "created_at": int(time.time()),
            "filename": f"batch_{batch_id}_errors.jsonl",
            "purpose": "batch_output",
            "status": "processed",
            "_content": err_bytes,
        }
        batch["error_file_id"] = err_id

    # ── Final status ──
    batch["finalizing_at"] = int(time.time())
    if cancel_event.is_set():
        batch["status"] = "cancelled"
        batch["cancelled_at"] = int(time.time())
    elif batch["request_counts"]["failed"] == batch["request_counts"]["total"]:
        batch["status"] = "failed"
        batch["failed_at"] = int(time.time())
    else:
        batch["status"] = "completed"
        batch["completed_at"] = int(time.time())

    logger.info(
        "Batch %s: %s — %d/%d completed, %d failed",
        batch_id, batch["status"],
        batch["request_counts"]["completed"],
        batch["request_counts"]["total"],
        batch["request_counts"]["failed"],
    )


# ═══════════════════════════════════════════════════════════════════════
#  Models & Health
# ═══════════════════════════════════════════════════════════════════════

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
        "max_thinking_tokens": MAX_THINKING_TOKENS or None,
        "claude_path": CLAUDE_PATH,
        "batch_max_concurrent": BATCH_MAX_CONCURRENT,
        "active_batches": sum(
            1 for b in batch_store.values()
            if b["status"] in ("validating", "in_progress", "finalizing")
        ),
    }


if __name__ == "__main__":
    logger.info(
        "Claude CLI -> OpenAI proxy | port=%d | model=%s | effort=%s | batch_concurrent=%d",
        PORT, MODEL, EFFORT, BATCH_MAX_CONCURRENT,
    )
    logger.info("Claude path: %s", CLAUDE_PATH)
    uvicorn.run(app, host="0.0.0.0", port=PORT)
