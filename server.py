"""
Claude CLI -> OpenAI-compatible API Proxy

Wraps the Claude Code CLI (`claude -p`) to serve OpenAI-format chat completions
and batch processing. Uses Claude Haiku 4.5 with low thinking effort by default.

No Anthropic API key needed — uses Claude CLI's built-in auth.

Chat completions support:
  * true token streaming (`"stream": true` → SSE chunks, `[DONE]`, optional
    usage chunk via `stream_options.include_usage`)
  * full multi-turn history (system / user / assistant / tool messages are
    flattened into a transcript; the CLI is stateless per call)
  * tools — both "auto" (model decides, replies with text or a JSON tool call
    that is converted to OpenAI `tool_calls`) and forced `tool_choice`
  * `response_format` (json_object / json_schema) via prompt instructions
  * `model` pass-through for haiku / sonnet / opus / claude-* ids

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
import shlex

from dotenv import load_dotenv
load_dotenv()
import random
import re
import subprocess
import tempfile
import threading
import time
import uuid
from typing import Any, AsyncIterator, Optional

from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse, Response, StreamingResponse
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
# Extra CLI flags. Default disables Claude Code's built-in tools (Bash, Read, …)
# — this turns a ~23k-token system prompt into ~150 tokens per call and stops
# the model from touching the local machine. (Do not add --bare: it skips the
# stored OAuth login and every call fails with "Not logged in".)
CLAUDE_EXTRA_ARGS = shlex.split(
    os.environ.get("CLAUDE_EXTRA_ARGS", '--tools ""')
)
# Used when a request carries no system message (see stream_claude).
DEFAULT_SYSTEM_PROMPT = os.environ.get(
    "DEFAULT_SYSTEM_PROMPT", "You are a helpful assistant."
)
# Model ids a client may request; anything else falls back to CLAUDE_MODEL.
_MODEL_ALIASES = {"haiku", "sonnet", "opus"}

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


# ═══════════════════════════════════════════════════════════════════════
#  Claude CLI wrapper (streaming)
# ═══════════════════════════════════════════════════════════════════════

def _resolve_model(requested: Any) -> str:
    if not requested:
        return MODEL
    r = str(requested).strip()
    if r in _MODEL_ALIASES or r.startswith("claude-"):
        return r
    return MODEL


# Effort names (OpenAI reasoning_effort / OpenRouter reasoning.effort) mapped
# to a Claude extended-thinking token budget. 0 disables thinking entirely.
_EFFORT_TO_THINKING = {
    "none": 0,
    "minimal": 0,
    "low": 3000,
    "medium": 8000,
    "high": 16000,
    "xhigh": 24000,
}
_THINKING_MIN = 1024   # Anthropic minimum budget when thinking is on
_THINKING_MAX = 32000  # keep well under max_tokens; >32k needs batch anyway


def _resolve_thinking_tokens(body: dict) -> Optional[int]:
    """Per-request thinking budget from OpenAI/OpenRouter/Anthropic-style fields.

    Accepted (first match wins):
      * ``reasoning_effort``: "none"|"minimal"|"low"|"medium"|"high"|"xhigh"   (OpenAI)
      * ``reasoning``: {"effort": ..., "max_tokens": N, "enabled": bool}       (OpenRouter)
      * ``thinking``:  {"type": "enabled"|"disabled", "budget_tokens": N}      (Anthropic)

    Returns a token budget (0 = disable thinking), or None to keep the
    server-side default (MAX_THINKING_TOKENS env, else CLI default).
    """
    def clamp(n: int) -> int:
        if n <= 0:
            return 0
        return max(_THINKING_MIN, min(int(n), _THINKING_MAX))

    effort = body.get("reasoning_effort")
    if isinstance(effort, str) and effort.strip().lower() in _EFFORT_TO_THINKING:
        return clamp(_EFFORT_TO_THINKING[effort.strip().lower()])

    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict):
        if reasoning.get("enabled") is False:
            return 0
        if isinstance(reasoning.get("max_tokens"), int):
            return clamp(reasoning["max_tokens"])
        r_effort = str(reasoning.get("effort", "")).strip().lower()
        if r_effort in _EFFORT_TO_THINKING:
            return clamp(_EFFORT_TO_THINKING[r_effort])

    thinking = body.get("thinking")
    if isinstance(thinking, dict):
        if thinking.get("type") == "disabled":
            return 0
        if isinstance(thinking.get("budget_tokens"), int):
            return clamp(thinking["budget_tokens"])
        if thinking.get("type") == "enabled":
            return clamp(_EFFORT_TO_THINKING["medium"])

    return None


def _usage_from_envelope(usage_raw: dict) -> dict:
    usage_raw = usage_raw or {}
    usage = {
        "prompt_tokens": usage_raw.get("input_tokens", 0)
        + usage_raw.get("cache_read_input_tokens", 0)
        + usage_raw.get("cache_creation_input_tokens", 0),
        "completion_tokens": usage_raw.get("output_tokens", 0),
    }
    usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
    return usage


def _cli_thread(cmd: list, prompt: str, timeout: int, loop, q: asyncio.Queue, holder: dict,
                env: Optional[dict] = None) -> None:
    """Run the CLI in a worker thread, forwarding stdout lines to an asyncio queue."""
    def put(item):
        loop.call_soon_threadsafe(q.put_nowait, item)

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,  # None → inherit; per-request thinking budget rides in here
        )
    except Exception as e:  # noqa: BLE001
        put(("exit", -1, f"Failed to start Claude CLI ({CLAUDE_PATH}): {e}"))
        return

    holder["proc"] = proc
    stderr_chunks: list = []

    def _drain_stderr():
        try:
            stderr_chunks.append(proc.stderr.read())
        except Exception:  # noqa: BLE001
            pass

    def _feed_stdin():
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass

    def _on_timeout():
        holder["timed_out"] = True
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_drain_stderr, daemon=True).start()
    threading.Thread(target=_feed_stdin, daemon=True).start()
    timer = threading.Timer(timeout, _on_timeout)
    timer.start()
    try:
        for line in proc.stdout:
            put(("line", line))
        proc.wait()
    finally:
        timer.cancel()
    put(("exit", proc.returncode, "".join(stderr_chunks).strip()))


async def stream_claude(
    system: str,
    prompt: str,
    model: str = MODEL,
    timeout: int = TIMEOUT,
    images: Optional[list] = None,
    thinking_tokens: Optional[int] = None,
) -> AsyncIterator[dict]:
    """Run one CLI call and yield events:

    ``images`` is a list of Anthropic image content blocks (see
    ``_image_block``).  When present the prompt is sent to the CLI as a
    ``stream-json`` user message so the model actually sees the pixels;
    otherwise the prompt goes to stdin as plain text exactly as before.

        {"type": "text", "text": "..."}                 — partial text as it is generated
        {"type": "result", "text": "...", "usage": {}}  — final envelope (always last)

    Raises CLIError, RateLimitError or TimeoutError.
    """
    cmd = [
        CLAUDE_PATH, "-p",
        "--no-session-persistence",
        "--model", model,
        "--effort", EFFORT,
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
        *CLAUDE_EXTRA_ARGS,
    ]

    # Thinking budget: per-request value wins, else the server-wide default.
    # Delivered via the MAX_THINKING_TOKENS environment variable (the
    # ``--max-thinking-tokens`` CLI flag does not exist in Claude Code 2.x).
    # 0 disables thinking for the call.
    env: Optional[dict] = None
    effective_thinking = thinking_tokens
    if effective_thinking is None and MAX_THINKING_TOKENS:
        try:
            effective_thinking = int(MAX_THINKING_TOKENS)
        except ValueError:
            effective_thinking = None
    if effective_thinking is not None:
        env = {**os.environ, "MAX_THINKING_TOKENS": str(effective_thinking)}

    # System prompt goes through a temp file and the prompt through stdin so
    # long conversations never hit the command-line length limit (32k on Windows).
    # Always pass one: without it the CLI injects its own ~7k-token Claude Code
    # system prompt into every call.
    system = system or DEFAULT_SYSTEM_PROMPT
    sys_file: Optional[str] = None
    if system:
        fd, sys_file = tempfile.mkstemp(prefix="claude-sys-", suffix=".txt", text=True)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(system)
        cmd.extend(["--system-prompt-file", sys_file])

    # Images can only be delivered as content blocks, which requires the
    # stream-json input format: one JSON line per user message on stdin.
    stdin_payload = prompt
    if images:
        cmd.extend(["--input-format", "stream-json"])
        stdin_payload = json.dumps({
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": prompt}, *images],
            },
        }, ensure_ascii=False) + "\n"

    logger.info(
        "Claude call: model=%s prompt=%d chars, system=%d chars, images=%d, thinking=%s",
        model, len(prompt), len(system or ""), len(images or []),
        "default" if effective_thinking is None else effective_thinking,
    )

    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()
    holder: dict = {}
    threading.Thread(
        target=_cli_thread, args=(cmd, stdin_payload, timeout, loop, q, holder),
        kwargs={"env": env}, daemon=True
    ).start()

    result_env: Optional[dict] = None
    saw_text = False
    rc = 0
    stderr = ""
    try:
        while True:
            item = await q.get()
            if item[0] == "exit":
                rc, stderr = item[1], item[2]
                break
            line = item[1].strip()
            if not line or not line.startswith("{"):
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            mtype = msg.get("type")
            if mtype == "stream_event":
                ev = msg.get("event") or {}
                if ev.get("type") == "content_block_delta":
                    delta = ev.get("delta") or {}
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        saw_text = True
                        yield {"type": "text", "text": delta["text"]}
            elif mtype == "result":
                result_env = msg
    finally:
        proc = holder.get("proc")
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        if sys_file:
            try:
                os.remove(sys_file)
            except OSError:
                pass

    if holder.get("timed_out"):
        raise TimeoutError(f"Claude CLI timed out after {timeout}s")

    if result_env is None:
        logger.error("Claude CLI exit %d without result: %s", rc, stderr[:500])
        low = stderr.lower()
        if "rate" in low or "429" in low or "overloaded" in low:
            raise RateLimitError(stderr[:500])
        raise CLIError(f"Exit code {rc}: {stderr[:500] or 'no result from Claude CLI'}")

    if result_env.get("is_error"):
        msg = str(result_env.get("result", "Unknown error"))
        if "rate" in msg.lower() or "overloaded" in msg.lower():
            raise RateLimitError(msg)
        raise CLIError(msg)

    usage = _usage_from_envelope(result_env.get("usage", {}))
    logger.info(
        "Claude response: $%.6f, %dms, %d tokens%s",
        result_env.get("total_cost_usd", 0) or 0,
        result_env.get("duration_ms", 0) or 0,
        usage["total_tokens"],
        "" if saw_text else " (no partial deltas — using final result)",
    )
    yield {
        "type": "result",
        "text": str(result_env.get("result", "") or ""),
        "usage": usage,
        "streamed": saw_text,
    }


# ═══════════════════════════════════════════════════════════════════════
#  OpenAI request → prompt
# ═══════════════════════════════════════════════════════════════════════

_DATA_URL_RE = re.compile(r"^data:([\w.+/-]+);base64,(.+)$", re.DOTALL)
_IMAGE_MEDIA_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


def _image_block(p: dict) -> Optional[dict]:
    """Convert an OpenAI-style image part into an Anthropic image content block.

    Accepts:
      {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
      {"type": "image_url", "image_url": "https://..."}
      {"type": "input_image", "image_url": "..."}          (Responses API)
      {"type": "image", "source": {...}}                    (Anthropic passthrough)

    Returns None when the part cannot be turned into something Claude accepts.
    """
    t = p.get("type")
    if t == "image" and isinstance(p.get("source"), dict):
        return {"type": "image", "source": p["source"]}

    url = p.get("image_url", p.get("url"))
    if isinstance(url, dict):
        url = url.get("url")
    if not isinstance(url, str) or not url:
        return None

    m = _DATA_URL_RE.match(url.strip())
    if m:
        media_type, data = m.group(1).lower(), re.sub(r"\s+", "", m.group(2))
        if media_type == "image/jpg":
            media_type = "image/jpeg"
        if media_type not in _IMAGE_MEDIA_TYPES:
            return None
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data},
        }
    if url.startswith(("http://", "https://")):
        return {"type": "image", "source": {"type": "url", "url": url}}
    return None


def _split_content(content: Any) -> tuple:
    """Split OpenAI message content into (text, [image blocks]).

    Text parts are joined with newlines; each image part that can be
    converted is returned as an Anthropic content block (in order) and
    removed from the text — callers add their own markers.
    Unconvertible images/audio become "[image omitted]" / "[audio omitted]".
    """
    if content is None:
        return "", []
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        return str(content), []

    parts: list = []
    images: list = []
    for p in content:
        if isinstance(p, str):
            parts.append(p)
        elif isinstance(p, dict):
            t = p.get("type")
            if t in ("text", "input_text", "output_text"):
                parts.append(str(p.get("text", "")))
            elif t in ("image_url", "input_image", "image"):
                block = _image_block(p)
                if block is not None:
                    images.append(block)
                else:
                    parts.append("[image omitted]")
            elif t in ("input_audio", "audio"):
                parts.append("[audio omitted]")
            elif "text" in p:
                parts.append(str(p["text"]))
    return "\n".join(x for x in parts if x), images


def _text_of(content: Any) -> str:
    """Flatten OpenAI message content (string or list of parts) to text.

    Images are dropped ("[image omitted]") — use ``_split_content`` where
    they must be preserved (user turns).
    """
    text, images = _split_content(content)
    if images:
        text = "\n".join(x for x in ([text] + ["[image omitted]"] * len(images)) if x)
    return text


def _maybe_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value
    return value


def _tool_instructions(specs: list, forced: Optional[str]) -> str:
    lines = [
        "TOOLS",
        "You can call the following tools. Each entry gives the tool name, a "
        "description, and the JSON Schema for its arguments:",
        json.dumps(specs, ensure_ascii=False, indent=2),
        "",
        "To call tools, respond with ONLY a JSON object of this exact shape and "
        "nothing else — no prose before or after it, no markdown code fences:",
        '{"tool_calls": [{"name": "<tool name>", "arguments": {<arguments matching that tool\'s schema>}}]}',
        "You may put several calls in the list.",
    ]
    if forced == "__any__":
        lines.append("You MUST call at least one tool in this reply.")
    elif forced:
        lines.append(f'You MUST call the tool "{forced}" in this reply.')
    else:
        lines.append(
            "If no tool is needed, reply normally in plain text and never mention "
            "tools or this JSON format."
        )
    return "\n".join(lines)


def _format_instructions(response_format: dict) -> str:
    rf_type = response_format.get("type")
    if rf_type == "json_schema":
        schema = (response_format.get("json_schema") or {}).get("schema") or {}
        return (
            "OUTPUT FORMAT\nRespond with ONLY a single JSON object that matches "
            "this JSON Schema — no prose, no markdown fences:\n"
            + json.dumps(schema, ensure_ascii=False, indent=2)
        )
    if rf_type == "json_object":
        return (
            "OUTPUT FORMAT\nRespond with ONLY a single valid JSON object — no "
            "prose, no markdown fences."
        )
    return ""


def _build_prompt(body: dict) -> tuple:
    """Return (system, prompt, mode, forced_tool, images).

    mode is "text" (plain reply), "json" (response_format requested) or
    "tools" (the model may answer with a JSON tool call).

    images is the list of Anthropic image blocks found in *user* messages
    (any turn, in order).  Each one is referenced in the prompt text as
    "[Image N attached]" so the model can tell which turn it belongs to;
    the blocks themselves are appended to the final user message by
    ``stream_claude``.
    """
    messages = body.get("messages") or []
    tools = body.get("tools") or []
    tool_choice = body.get("tool_choice")
    response_format = body.get("response_format")

    system_parts: list = []
    turns: list = []
    images: list = []
    for m in messages:
        role = m.get("role")
        if role in ("system", "developer"):
            system_parts.append(_text_of(m.get("content")))
        elif role == "user":
            text, imgs = _split_content(m.get("content"))
            if imgs:
                markers = []
                for block in imgs:
                    images.append(block)
                    markers.append(f"[Image {len(images)} attached]")
                text = "\n".join(x for x in [text, *markers] if x)
            turns.append(("User", text))
        elif role == "assistant":
            text = _text_of(m.get("content"))
            tcs = m.get("tool_calls") or []
            if tcs:
                calls = []
                for tc in tcs:
                    fn = tc.get("function") or {}
                    calls.append({
                        "name": fn.get("name"),
                        "arguments": _maybe_json(fn.get("arguments")),
                    })
                text = (text + "\n" if text else "") + json.dumps(
                    {"tool_calls": calls}, ensure_ascii=False
                )
            turns.append(("Assistant", text))
        elif role in ("tool", "function"):
            label = m.get("name") or m.get("tool_call_id") or "tool"
            turns.append((f"Tool result ({label})", _text_of(m.get("content"))))

    system = "\n\n".join(p for p in system_parts if p)
    mode = "text"
    forced: Optional[str] = None

    if tools and tool_choice != "none":
        specs = []
        for t in tools:
            fn = t.get("function", t)
            specs.append({
                "name": fn.get("name"),
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {}),
            })
        if isinstance(tool_choice, dict):
            forced = (tool_choice.get("function") or {}).get("name") or tool_choice.get("name")
        elif tool_choice == "required":
            forced = specs[0]["name"] if len(specs) == 1 else "__any__"
        mode = "tools"
        system = (system + "\n\n" if system else "") + _tool_instructions(specs, forced)
    elif isinstance(response_format, dict):
        instr = _format_instructions(response_format)
        if instr:
            mode = "json"
            system = (system + "\n\n" if system else "") + instr

    if len(turns) == 1 and turns[0][0] == "User":
        prompt = turns[0][1]
    elif not turns:
        prompt = "(no user message)"
    else:
        transcript = "\n\n".join(f"[{role}]\n{text}" for role, text in turns)
        prompt = (
            "Below is the conversation so far between the User and you (the "
            "Assistant), including any tool calls you made and their results.\n\n"
            f"{transcript}\n\n"
            "Write the Assistant's next reply to the latest message. Output only "
            "the reply itself."
        )
    if not prompt.strip():
        prompt = "(empty message)"
    return system, prompt, mode, forced, images


# ═══════════════════════════════════════════════════════════════════════
#  Model output → text / tool calls
# ═══════════════════════════════════════════════════════════════════════

def parse_json_from_text(text) -> Any:
    """Extract a JSON value from Claude's response (raises ValueError)."""
    if isinstance(text, (dict, list)):
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

    # Balanced-ARRAY scan first: a parallel tool-call reply is a JSON array,
    # possibly wrapped in tags/prose ("<function_calls>[{...},{...}]</...> ok").
    # The object scan below would only recover the FIRST call from an array.
    depth = 0
    start = None
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"' and depth > 0:
            in_str = True
        elif ch == "[":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "]":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    try:
                        val = json.loads(text[start: i + 1])
                        if isinstance(val, list) and val and all(
                                isinstance(v, dict) for v in val):
                            return val
                    except json.JSONDecodeError:
                        pass
                    start = None

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
                    return json.loads(text[start: i + 1])
                except json.JSONDecodeError:
                    start = None

    raise ValueError(f"Could not parse JSON from: {text[:300]}")


def _strip_fences(text: str) -> str:
    """Drop a surrounding ```json ... ``` fence (models add one despite instructions)."""
    m = re.match(r"^\s*```(?:json)?\s+(.*?)\s*```\s*$", text, re.DOTALL)
    return m.group(1).strip() if m else text


def _extract_tool_calls(text: str, forced: Optional[str]) -> Optional[list]:
    """Turn a JSON reply into [(name, arguments_dict_or_str), ...] or None."""
    try:
        obj = parse_json_from_text(text)
    except ValueError:
        return None

    if isinstance(obj, dict):
        if isinstance(obj.get("tool_calls"), list):
            calls = obj["tool_calls"]
        elif "name" in obj and ("arguments" in obj or "parameters" in obj):
            calls = [obj]
        elif forced and forced != "__any__":
            calls = [{"name": forced, "arguments": obj}]
        else:
            return None
    elif isinstance(obj, list):
        calls = obj
    else:
        return None

    out = []
    for c in calls:
        if not isinstance(c, dict):
            continue
        fn = c.get("function") or {}
        name = c.get("name") or fn.get("name")
        args = c.get("arguments", c.get("parameters", fn.get("arguments", {})))
        if name:
            out.append((name, _maybe_json(args)))
    return out or None


async def _chat_events(body: dict) -> AsyncIterator[tuple]:
    """Normalised chat pipeline used by both streaming and non-streaming paths.

    Yields ("text", str) as tokens arrive, optionally ("tool_calls", [...]),
    then always ("done", {"usage": {...}, "finish_reason": "stop"|"tool_calls"}).
    """
    system, prompt, mode, forced, images = _build_prompt(body)
    model = _resolve_model(body.get("model"))
    thinking = _resolve_thinking_tokens(body)

    buffer = ""          # tool-mode text held back until we know it is not JSON
    decided = None       # None → undecided, "text" → pass through, "json" → hold
    usage = None

    # OpenAI/OpenRouter semantics: an assistant reply may carry BOTH prose
    # content and tool_calls. Models often reason in prose, then emit the tool
    # JSON ("Let me check that. {\"tool_calls\": ...}"). In decided-"text"
    # state we therefore stream complete lines live, but from the first line
    # that looks like the start of a JSON/tag/fence block we hold the rest;
    # if the held tail parses as tool calls it is emitted as tool_calls
    # (finish_reason "tool_calls") after the prose — otherwise it is flushed
    # as text so nothing is ever lost.
    pending = ""         # decided-"text": partial line not yet streamed
    held = None          # decided-"text": suspected trailing tool-JSON block

    def _scan_text(pending: str, held, incoming: str):
        """Split streamed text into (lines_to_emit, pending, held)."""
        out = []
        if held is not None:
            return out, pending, held + incoming
        pending += incoming
        while "\n" in pending:
            line, rest = pending.split("\n", 1)
            ls = line.lstrip()
            if ls and ls[0] in "{[`<":
                return out, "", line + "\n" + rest
            out.append(line + "\n")
            pending = rest
        return out, pending, held

    async for ev in stream_claude(system, prompt, model=model, images=images,
                                  thinking_tokens=thinking):
        if ev["type"] == "text":
            chunk = ev["text"]
        elif ev["type"] == "result":
            usage = ev["usage"]
            if ev["streamed"] or not ev["text"]:
                continue
            chunk = ev["text"]  # no partial deltas came through: treat as one chunk
        else:
            continue

        if mode == "json":
            buffer += chunk      # held back so fences can be stripped at the end
            continue
        if mode != "tools":
            yield ("text", chunk)
            continue
        if decided == "text":
            outs, pending, held = _scan_text(pending, held, chunk)
            for o in outs:
                yield ("text", o)
            continue
        buffer += chunk
        if decided is None:
            stripped = buffer.lstrip()
            if not stripped:
                continue
            # "<" is held too: models sometimes wrap tool JSON in tags like
            # <function_calls>[...]</function_calls>. Rare text replies that
            # genuinely start with "<" lose incremental streaming but are
            # still emitted in full at the end (see the fallback below).
            if stripped[0] in "{[`<":
                decided = "json"
            else:
                decided = "text"
                outs, pending, held = _scan_text("", None, buffer)
                for o in outs:
                    yield ("text", o)
                buffer = ""

    finish = "stop"
    if mode == "json":
        yield ("text", _strip_fences(buffer))
    elif mode == "tools" and decided == "json":
        calls = _extract_tool_calls(buffer, forced)
        if calls:
            yield ("tool_calls", calls)
            finish = "tool_calls"
        else:
            yield ("text", buffer)
    elif mode == "tools" and decided == "text":
        # Trailing partial line may itself be a JSON block start
        if pending and held is None:
            ls = pending.lstrip()
            if ls and ls[0] in "{[`<":
                held, pending = pending, ""
        if pending:
            yield ("text", pending)
        if held:
            calls = _extract_tool_calls(held, forced)
            if calls:
                yield ("tool_calls", calls)
                finish = "tool_calls"
            else:
                yield ("text", held)
    elif buffer:
        yield ("text", buffer)

    yield ("done", {
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "finish_reason": finish,
    })


# ═══════════════════════════════════════════════════════════════════════
#  OpenAI response builders
# ═══════════════════════════════════════════════════════════════════════

def _tool_call_objs(calls: list) -> list:
    return [{
        "id": f"call_{uuid.uuid4().hex[:12]}",
        "type": "function",
        "function": {
            "name": name,
            "arguments": args if isinstance(args, str) else json.dumps(args, ensure_ascii=False),
        },
    } for name, args in calls]


async def _process_chat_request(body: dict) -> dict:
    """Process a single (non-streaming) chat request body → OpenAI response body.

    Raises CLIError, RateLimitError, TimeoutError, ValueError on failure.
    """
    text_parts: list = []
    tool_calls: Optional[list] = None
    done: dict = {}
    async for kind, payload in _chat_events(body):
        if kind == "text":
            text_parts.append(payload)
        elif kind == "tool_calls":
            tool_calls = _tool_call_objs(payload)
        else:
            done = payload

    message: dict = {"role": "assistant", "content": "".join(text_parts) or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": _resolve_model(body.get("model")),
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": done.get("finish_reason", "stop"),
        }],
        "usage": done.get("usage"),
    }


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


async def _stream_chat_response(body: dict) -> AsyncIterator[str]:
    resp_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    model = _resolve_model(body.get("model"))
    include_usage = bool((body.get("stream_options") or {}).get("include_usage"))

    def chunk(delta: dict, finish: Optional[str] = None) -> str:
        return _sse({
            "id": resp_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        })

    yield chunk({"role": "assistant", "content": ""})
    try:
        async for kind, payload in _chat_events(body):
            if kind == "text":
                if payload:
                    yield chunk({"content": payload})
            elif kind == "tool_calls":
                deltas = []
                for i, tc in enumerate(_tool_call_objs(payload)):
                    deltas.append({"index": i, **tc})
                yield chunk({"tool_calls": deltas})
            else:
                yield chunk({}, payload["finish_reason"])
                if include_usage:
                    yield _sse({
                        "id": resp_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [],
                        "usage": payload["usage"],
                    })
    except RateLimitError as e:
        yield _sse({"error": {"message": str(e), "type": "rate_limit_error", "code": 429}})
    except (CLIError, ValueError) as e:
        yield _sse({"error": {"message": str(e), "type": "internal_error", "code": 500}})
    except TimeoutError as e:
        yield _sse({"error": {"message": str(e), "type": "timeout_error", "code": 504}})
    except Exception as e:  # noqa: BLE001
        logger.error("Unexpected streaming error: %s", e, exc_info=True)
        yield _sse({"error": {"message": str(e), "type": "internal_error", "code": 500}})
    yield "data: [DONE]\n\n"


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
    if body.get("stream"):
        return StreamingResponse(
            _stream_chat_response(body),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    try:
        return JSONResponse(content=await _process_chat_request(body))
    except RateLimitError as e:
        return _openai_error(429, str(e), "rate_limit_error")
    except (CLIError, ValueError) as e:
        return _openai_error(500, str(e), "internal_error")
    except TimeoutError as e:
        return _openai_error(504, str(e), "timeout_error")
    except Exception as e:  # noqa: BLE001
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

        except Exception as e:  # noqa: BLE001
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
        "per_request_thinking": "reasoning_effort | reasoning{effort,max_tokens,enabled} | thinking{type,budget_tokens}",
        "claude_path": CLAUDE_PATH,
        "claude_extra_args": CLAUDE_EXTRA_ARGS,
        "streaming": True,
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
    logger.info("Claude path: %s | extra args: %s", CLAUDE_PATH, CLAUDE_EXTRA_ARGS)
    uvicorn.run(app, host="0.0.0.0", port=PORT)
