# How to Use the Claude CLI → OpenAI Proxy (Integration Guide)

> **Audience:** This file is meant to be dropped into *another* project (or handed to a
> Claude Code agent working in another repo) so it knows how to route LLM calls through
> this proxy. It describes how to use the proxy **from the outside**, as a service.

---

## 1. What this is

A local FastAPI server that exposes an **OpenAI-compatible API** but answers every request
by shelling out to the local `claude.exe` (Claude CLI). It uses Claude CLI's built-in
authentication, so **no Anthropic / OpenAI API key is required**.

Any OpenAI SDK or OpenAI-compatible client works against it — you only change the
`base_url` and pass a dummy `api_key`.

```
Your app  ──(OpenAI format)──>  Proxy (port 8082)  ──>  claude.exe  ──>  Claude
```

- Default model: **`haiku`** (server-side; see [§6](#6-important-behaviors--limitations))
- Default port: **`8082`**
- Default base URL: **`http://localhost:8082/v1`**
- API key: **any non-empty string** (e.g. `"not-needed"`) — it is ignored

---

## 2. Before you call: make sure the server is running

The proxy is a *separate process*. Your application does not start it; it must already be
listening. To verify it is up:

```bash
curl http://localhost:8082/health
```

Expected:

```json
{ "status": "ok", "model": "haiku", "effort": "low", "claude_path": "...", "batch_max_concurrent": 3, "active_batches": 0 }
```

If this fails with a connection error, the proxy is not running. Start it from the proxy
repo (`c:\Users\AG\Documents\GIT_PERSONAL\CaludeCLI LLM Proxy`) — either natively:

```powershell
python server.py
```

or as a container on Docker Desktop (recommended — survives reboots, nothing to babysit):

```powershell
docker compose up -d --build     # first time; afterwards just: docker compose up -d
docker logs -f claude-cli-proxy  # watch calls
```

The container bind-mounts your `~/.claude` (OAuth login) and copies `~/.claude.json` at
start-up, so it uses the same Claude Code login as the host — no API key. If you re-login
on the host, restart the container. The image pins the Claude Code CLI version
(`CLAUDE_CODE_VERSION` in `docker-compose.yml`); bump it when you upgrade the host CLI.

> If you are an agent in a **different** repo and `/health` is unreachable, do **not** try to
> guess — tell the user the proxy needs to be started, or start it from the proxy directory.

---

## 3. Quickest possible call

### Python (OpenAI SDK — recommended)

```python
from openai import OpenAI

client = OpenAI(
    api_key="not-needed",                 # ignored, but the SDK requires something
    base_url="http://localhost:8082/v1",  # the only thing that changes vs. real OpenAI
)

resp = client.chat.completions.create(
    model="haiku",                        # value is ignored; model is fixed server-side
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "What is the capital of France?"},
    ],
)
print(resp.choices[0].message.content)
```

### JavaScript / TypeScript (OpenAI SDK)

```javascript
import OpenAI from "openai";

const client = new OpenAI({
  apiKey: "not-needed",
  baseURL: "http://localhost:8082/v1",
});

const resp = await client.chat.completions.create({
  model: "haiku",
  messages: [{ role: "user", content: "Hello!" }],
});
console.log(resp.choices[0].message.content);
```

### Raw HTTP (curl)

```bash
curl http://localhost:8082/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Hello!"}]}'
```

---

## 4. Structured output / tool calling (forced JSON)

To get **guaranteed structured JSON** back, define a single tool/function and force it with
`tool_choice`. The proxy injects your JSON schema into the system prompt, then parses
Claude's output back into a normal OpenAI `tool_calls` response.

```python
resp = client.chat.completions.create(
    model="haiku",
    messages=[
        {"role": "system", "content": "Extract medical relationships."},
        {"role": "user", "content": "Aspirin reduces cardiovascular disease risk."},
    ],
    tools=[{
        "type": "function",
        "function": {
            "name": "store_relations",
            "parameters": {
                "type": "object",
                "properties": {
                    "triplets": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "entity1": {"type": "string"},
                                "relation": {"type": "string"},
                                "entity2": {"type": "string"},
                            },
                        },
                    }
                },
            },
        },
    }],
    tool_choice={"type": "function", "function": {"name": "store_relations"}},
)

import json
args = json.loads(resp.choices[0].message.tool_calls[0].function.arguments)
print(args["triplets"])
```

**Rules for tool calls (how the proxy actually behaves):**
- All tools in `tools` are described to the model (name, description, parameters).
- Without `tool_choice` (or with `"auto"`) the model chooses between a plain-text answer
  and a tool call — the normal agent loop (call → `role: "tool"` result → next turn) works.
  `tool_choice: {"function": …}` / `"required"` forces a call; `"none"` disables tools.
- Tool calls are prompt-based: the model is asked for raw JSON and the proxy is tolerant —
  it strips markdown fences and extracts the first balanced `{...}` if needed.
- Response comes back with `finish_reason: "tool_calls"` and the JSON in
  `message.tool_calls[i].function.arguments` (a JSON **string**, per OpenAI convention).
  In streaming mode the call arrives as a single `delta.tool_calls` chunk.

---

## 5. Endpoint reference

| Method | Endpoint | Purpose |
|:--|:--|:--|
| `POST` | `/v1/chat/completions` | Chat completion or forced tool call |
| `GET`  | `/v1/models` | Lists the one configured model |
| `GET`  | `/health` | Liveness + current config |
| `POST` | `/v1/files` | Upload a JSONL file (for batch) |
| `GET`  | `/v1/files` / `/v1/files/{id}` | List / get file metadata |
| `GET`  | `/v1/files/{id}/content` | Download file bytes (batch results) |
| `DELETE` | `/v1/files/{id}` | Delete a file |
| `POST` | `/v1/batches` | Create a batch job from an uploaded file |
| `GET`  | `/v1/batches` / `/v1/batches/{id}` | List / poll batch status |
| `POST` | `/v1/batches/{id}/cancel` | Cancel a running batch |

---

## 6. Important behaviors & limitations

**Read this before integrating — the proxy is not a full OpenAI clone.**

1. **Model.** `model` is honoured when it is `haiku`, `sonnet`, `opus` or a full
   `claude-*` id; anything else (e.g. `gpt-4o-mini`) silently falls back to the proxy's
   `CLAUDE_MODEL`. Check `/health` for the live defaults.

1b. **Thinking (per request).** Extended thinking can be controlled per request with any
   of these OpenAI/OpenRouter/Anthropic-compatible body fields (first match wins):

   ```jsonc
   {"reasoning_effort": "medium"}                          // OpenAI style:
                                                           // none|minimal → off,
                                                           // low → 3000, medium → 8000,
                                                           // high → 16000, xhigh → 24000
   {"reasoning": {"effort": "low"}}                        // OpenRouter style
   {"reasoning": {"max_tokens": 4096}}                     // explicit budget (min 1024, max 32000)
   {"reasoning": {"enabled": false}}                       // off
   {"thinking": {"type": "enabled", "budget_tokens": 4096}}// Anthropic style
   ```

   The budget is delivered to the CLI via the `MAX_THINKING_TOKENS` environment
   variable for that call only; `0` disables thinking. Requests without any of these
   fields use the server-wide `MAX_THINKING_TOKENS` env default (or the CLI default
   when unset). Thinking deltas are not forwarded to the client — only the final
   answer text — but thinking tokens do count toward `completion_tokens`.
   Note `CLAUDE_EFFORT`/`--effort` maps to the API effort parameter, which Haiku
   does not support; the thinking budget above is the lever that actually works
   on Haiku.

2. **Multi-turn history is supported.** All `system`/`developer` messages are joined into
   the system prompt; `user`, `assistant` (including their `tool_calls`) and `tool`
   messages are flattened into a transcript, because the CLI is stateless per call. Long
   histories therefore cost input tokens on every turn — trim on the client side if needed.

3. **Streaming is supported.** `stream: true` returns standard SSE chunks
   (`chat.completion.chunk` … `data: [DONE]`); `stream_options.include_usage` adds the final
   usage chunk. Chunks arrive as the CLI emits them (roughly every 100–200 characters,
   not per token). A failure mid-stream arrives as a `data: {"error": …}` event.

4. **Tools.** With `tools` and no `tool_choice` (or `"auto"`) the model decides: it either
   answers in text or emits a JSON tool call that the proxy converts into OpenAI
   `tool_calls` with `finish_reason: "tool_calls"`. `tool_choice: {"function": …}` or
   `"required"` forces a call; `"none"` disables tools. This is prompt-based, so a model
   may occasionally answer in text where a call was expected — handle both branches.

5. **`response_format`** (`json_object` / `json_schema`) is honoured via prompt
   instructions; the reply is buffered and returned as clean JSON (fences stripped).

6. **Sampling params are ignored.** `temperature`, `top_p`, `max_tokens`, `stop`, etc. have
   no effect — generation is controlled by the CLI's model/effort settings.

7. **`n > 1` not supported.** Exactly one choice is returned.

8. **Content parts.** `content` may be a string or the OpenAI parts array; text parts are
   concatenated. **Images are supported**: `image_url` parts with a `data:image/...;base64,`
   URL (jpeg/png/gif/webp) or an `http(s)` URL are forwarded to Claude as real image
   blocks (via the CLI's `--input-format stream-json`), from any user turn; each is
   referenced in the transcript as `[Image N attached]`. Audio parts still become
   `[audio omitted]`.

9. **Usage numbers** are mapped from the CLI: `prompt_tokens` includes cache read/creation
   input tokens; `completion_tokens` is output tokens. Cost is logged server-side, not
   returned in the response body. Claude Code's built-in tools are disabled per call
   (`--tools ""`), so the fixed overhead is ~150 input tokens instead of ~23k.

---

## 7. Error handling

The proxy returns OpenAI-style error envelopes:

```json
{ "error": { "message": "...", "type": "rate_limit_error" } }
```

| Status | `type` | Meaning | What to do |
|:--|:--|:--|:--|
| `200` | — | Success | — |
| `429` | `rate_limit_error` | CLI is rate-limited / overloaded | Back off and retry with jitter |
| `500` | `internal_error` | CLI crashed, bad JSON, or parse failure | Inspect proxy logs; retry once |
| `504` | `timeout_error` | Exceeded `CLAUDE_TIMEOUT` (default 300 s) | Shorten prompt or raise timeout |

For latency-sensitive callers, treat `429` and `504` as retryable; treat `500` as likely a
bad request or environment problem.

---

## 8. Batch processing (many requests at once)

Mirrors OpenAI's Files + Batches workflow. Useful for processing N prompts without firing N
concurrent HTTP calls. The proxy runs them with bounded concurrency
(`BATCH_MAX_CONCURRENT`, default 3) and automatic retry with exponential backoff
(`BATCH_MAX_RETRIES`, default 5).

> **Note:** Files and batches are stored **in memory** — they are lost if the proxy
> restarts. Download results before stopping the server. Only the
> `/v1/chat/completions` endpoint is supported as a batch target.

### Input file format (JSONL)

One request per line. Each line's `body` is exactly what you'd POST to
`/v1/chat/completions`:

```jsonl
{"custom_id": "req-1", "method": "POST", "url": "/v1/chat/completions", "body": {"messages": [{"role": "user", "content": "Define hypertension."}]}}
{"custom_id": "req-2", "method": "POST", "url": "/v1/chat/completions", "body": {"messages": [{"role": "user", "content": "Define diabetes."}]}}
```

### End-to-end via the OpenAI SDK

```python
from openai import OpenAI
import json, time

client = OpenAI(api_key="not-needed", base_url="http://localhost:8082/v1")

# 1. Upload the JSONL file
with open("requests.jsonl", "rb") as f:
    upload = client.files.create(file=f, purpose="batch")

# 2. Create the batch
batch = client.batches.create(
    input_file_id=upload.id,
    endpoint="/v1/chat/completions",
    completion_window="24h",
)

# 3. Poll until done
while True:
    batch = client.batches.retrieve(batch.id)
    if batch.status in ("completed", "failed", "cancelled", "expired"):
        break
    print(batch.status, batch.request_counts)
    time.sleep(3)

# 4. Download results (one JSON object per line)
if batch.output_file_id:
    text = client.files.content(batch.output_file_id).text
    for line in text.splitlines():
        row = json.loads(line)
        print(row["custom_id"], row["response"]["body"]["choices"][0]["message"]["content"])

# Failed items (if any) land in a separate error file
if batch.error_file_id:
    print(client.files.content(batch.error_file_id).text)
```

Batch statuses progress: `validating → in_progress → finalizing → completed`
(or `failed` / `cancelled`). Each output line has `custom_id`, a `response` (with the full
chat-completion body), and an `error` field that is `null` on success.

---

## 9. Configuration cheat-sheet (controlled on the proxy host)

These are set where the **proxy** runs (env vars or its `.env`), not by the client:

| Variable | Default | Notes |
|:--|:--|:--|
| `CLAUDE_PATH` | `C:\Users\AG\.local\bin\claude.exe` | Path to the CLI binary |
| `CLAUDE_MODEL` | `haiku` | `haiku` · `sonnet` · `opus` |
| `CLAUDE_EFFORT` | `low` | `min` · `low` · `balanced` · `high` · `max` |
| `PORT` | `8082` | Listen port |
| `CLAUDE_TIMEOUT` | `300` | Per-request seconds |
| `MAX_THINKING_TOKENS` | _(unset)_ | Server-wide default thinking budget; per-request `reasoning_effort`/`reasoning`/`thinking` fields override it (see §1b) |
| `BATCH_MAX_CONCURRENT` | `3` | Parallel workers per batch |
| `BATCH_MAX_RETRIES` | `5` | Retries per batch item on rate-limit/timeout |
| `CLAUDE_EXTRA_ARGS` | `--tools ""` | Extra CLI flags (disables built-in tools). Never add `--bare` — it skips the stored login |
| `DEFAULT_SYSTEM_PROMPT` | `You are a helpful assistant.` | Used when a request has no system message (otherwise the CLI injects its own ~7k-token one) |
| `CLAUDE_CODE_VERSION` | _(see docker-compose.yml)_ | Docker only — CLI version baked into the image |

---

## 10. TL;DR for an agent integrating this

1. Point the OpenAI client at `base_url="http://localhost:8082/v1"`, `api_key="not-needed"`.
2. `GET /health` first — if it fails, the proxy isn't running.
3. Full message history, `stream: true` and `tools` (auto or forced) all work like OpenAI;
   history is re-sent on every turn, so keep it trimmed.
4. Pass `model` as `haiku` / `sonnet` / `opus`; anything else falls back to the proxy's env.
5. For structured output, force a tool via `tools` + `tool_choice` (or use
   `response_format`) and read `tool_calls[0].function.arguments`.
6. Retry on `429`/`504`; surface `500` to the user.
7. For bulk work, use the Files + Batches flow (results are in-memory — download before the
   proxy stops).
