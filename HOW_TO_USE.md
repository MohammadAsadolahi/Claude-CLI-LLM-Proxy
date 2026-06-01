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
repo (`c:\Users\AG\Documents\GIT_PERSONAL\CaludeCLI LLM Proxy`):

```powershell
python server.py
```

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
- You **must** set both `tools` and `tool_choice` to trigger tool mode.
- Only the **first** tool in the `tools` list is used. The forced name comes from
  `tool_choice.function.name` if given, otherwise the first tool's name.
- The model is instructed to return raw JSON matching `function.parameters`. The proxy is
  tolerant — it strips markdown fences and extracts the first balanced `{...}` if needed.
- Response comes back with `finish_reason: "tool_calls"` and the JSON in
  `message.tool_calls[0].function.arguments` (a JSON **string**, per OpenAI convention).

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

1. **Model is fixed server-side.** The `model` field in your request is **ignored**. The
   actual model (`haiku` / `sonnet` / `opus`) and thinking effort are set by environment
   variables on the proxy (`CLAUDE_MODEL`, `CLAUDE_EFFORT`). To use a different model, the
   proxy must be restarted with different env vars. Check `/health` for the live model.

2. **Only one system + one user message are used.** The proxy scans `messages` and keeps
   the **last** `system` message and the **last** `user` message. It does **not** support
   multi-turn conversation history, and it ignores `assistant` messages. If you need
   context from prior turns, fold it into a single user (or system) message yourself.

3. **No streaming.** `stream: true` is not supported. Always read the full response.

4. **Sampling params are ignored.** `temperature`, `top_p`, `max_tokens`, `stop`, etc. have
   no effect — generation is controlled by the CLI's model/effort settings.

5. **`n > 1` not supported.** Exactly one choice is returned.

6. **Message `content` must be a plain string.** Send `"content": "text"`, not the
   OpenAI content-parts array. No image/multimodal input.

7. **Usage numbers** are mapped from the CLI: `prompt_tokens` includes cache read/creation
   input tokens; `completion_tokens` is output tokens. Cost is logged server-side, not
   returned in the response body.

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
| `MAX_THINKING_TOKENS` | _(unset)_ | Optional cap to reduce usage |
| `BATCH_MAX_CONCURRENT` | `3` | Parallel workers per batch |
| `BATCH_MAX_RETRIES` | `5` | Retries per batch item on rate-limit/timeout |

---

## 10. TL;DR for an agent integrating this

1. Point the OpenAI client at `base_url="http://localhost:8082/v1"`, `api_key="not-needed"`.
2. `GET /health` first — if it fails, the proxy isn't running.
3. Use one `system` + one `user` message; don't rely on multi-turn history or streaming.
4. The `model` you pass is cosmetic; the real model lives in the proxy's env.
5. For structured output, force a single tool via `tools` + `tool_choice` and read
   `tool_calls[0].function.arguments`.
6. Retry on `429`/`504`; surface `500` to the user.
7. For bulk work, use the Files + Batches flow (results are in-memory — download before the
   proxy stops).
