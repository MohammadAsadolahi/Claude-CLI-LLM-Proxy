<h1 align="center">Claude CLI &rarr; OpenAI Proxy</h1>

<p align="center">
  A lightweight FastAPI bridge that lets any OpenAI-compatible client<br>
  talk to your local Claude CLI — zero API keys required.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.9+-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.9+"/>
  &nbsp;
  <img src="https://img.shields.io/badge/FastAPI-0.115+-009688?style=flat-square&logo=fastapi&logoColor=white" alt="FastAPI"/>
  &nbsp;
  <img src="https://img.shields.io/badge/OpenAI_Compatible-412991?style=flat-square&logo=openai&logoColor=white" alt="OpenAI Compatible"/>
  &nbsp;
  <img src="https://img.shields.io/badge/License-MIT-22c55e?style=flat-square" alt="MIT License"/>
</p>

<p align="center">
  <a href="#architecture">Architecture</a> &middot;
  <a href="#quick-start">Quick Start</a> &middot;
  <a href="#configuration">Configuration</a> &middot;
  <a href="#api-reference">API Reference</a> &middot;
  <a href="#client-examples">Client Examples</a>
</p>

---

### The Problem

Your tools speak OpenAI. Claude CLI speaks its own protocol. Without a translation layer they can't communicate. This proxy sits in the middle — it accepts standard OpenAI-format requests, invokes your local `claude.exe`, and returns standard OpenAI-format responses. No API key. No cloud dependency. Drop-in compatible.

---

## Architecture

```mermaid
graph TB
    subgraph Clients
        C1["OpenAI SDKs"]
        C2["IDE Extensions"]
        C3["Agent Frameworks"]
        C4["Custom Apps"]
    end

    subgraph Proxy["FastAPI Proxy · port 8082"]
        direction TB
        P1["Request Translator"]
        P2["Response Builder"]
        P3["Error Handler"]
    end

    subgraph Backend["Local Backend"]
        B1["claude.exe"]
        B2["Built-in Auth"]
    end

    C1 & C2 & C3 & C4 --> P1
    P1 --> B1
    B1 --> P2
    P2 --> C1 & C2 & C3 & C4
    P3 -. fallback .-> P2
    B1 --- B2

    style P1 fill:#6366f1,stroke:#4f46e5,color:#fff
    style P2 fill:#6366f1,stroke:#4f46e5,color:#fff
    style P3 fill:#6366f1,stroke:#4f46e5,color:#fff
    style B1 fill:#d97706,stroke:#b45309,color:#fff
    style B2 fill:#d97706,stroke:#b45309,color:#fff
    style C1 fill:#1e293b,stroke:#334155,color:#e2e8f0
    style C2 fill:#1e293b,stroke:#334155,color:#e2e8f0
    style C3 fill:#1e293b,stroke:#334155,color:#e2e8f0
    style C4 fill:#1e293b,stroke:#334155,color:#e2e8f0
```

---

## Request Lifecycle

```mermaid
sequenceDiagram
    participant C as Client
    participant P as Proxy
    participant CLI as claude.exe

    C->>+P: POST /v1/chat/completions
    Note over P: Parse messages, detect tools

    alt Has tool definitions
        Note over P: Augment system prompt with JSON schema
    end

    P->>+CLI: subprocess · --output-format json
    Note over CLI: Local auth · no API key
    CLI-->>-P: JSON envelope {result, usage, cost}

    alt Tool call response
        Note over P: Extract JSON args, build tool_calls
        P-->>C: finish_reason: tool_calls
    else Text response
        P-->>-C: finish_reason: stop
    end
```

---

## Processing Pipeline

```mermaid
flowchart LR
    A["Request"] --> B{"Tools?"}

    B -- yes --> C["Augment prompt\nwith schema"]
    B -- no --> D["Pass through\nmessages"]

    C & D --> E["claude.exe"]

    E --> F{"Exit\ncode"}

    F -- 0 --> G["Parse\nenvelope"]
    F -- fail --> H{"Rate\nlimited?"}

    H -- yes --> I["429"]
    H -- no --> J["500"]

    G --> K{"Tool\nresponse?"}

    K -- yes --> L["Build\ntool_calls"]
    K -- no --> M["Build text\nresponse"]

    L & M --> N["OpenAI-format\nresponse"]

    style A fill:#6366f1,stroke:#4f46e5,color:#fff
    style E fill:#d97706,stroke:#b45309,color:#fff
    style N fill:#10b981,stroke:#059669,color:#fff
    style I fill:#f59e0b,stroke:#d97706,color:#fff
    style J fill:#ef4444,stroke:#dc2626,color:#fff
    style B fill:#1e293b,stroke:#6366f1,color:#e2e8f0
    style F fill:#1e293b,stroke:#6366f1,color:#e2e8f0
    style H fill:#1e293b,stroke:#6366f1,color:#e2e8f0
    style K fill:#1e293b,stroke:#6366f1,color:#e2e8f0
    style C fill:#1e293b,stroke:#334155,color:#e2e8f0
    style D fill:#1e293b,stroke:#334155,color:#e2e8f0
    style G fill:#1e293b,stroke:#334155,color:#e2e8f0
    style L fill:#1e293b,stroke:#334155,color:#e2e8f0
    style M fill:#1e293b,stroke:#334155,color:#e2e8f0
```

---

## Key Features

<table>
<tr>
<td align="center" width="25%" valign="top">
<br>
<strong>OpenAI Compatible</strong><br><br>
Drop-in replacement for any client that speaks the OpenAI API — SDKs, agents, IDE plugins.
<br><br>
</td>
<td align="center" width="25%" valign="top">
<br>
<strong>Tool Calling</strong><br><br>
Function-calling support via schema-augmented system prompts. Returns proper <code>tool_calls</code> responses.
<br><br>
</td>
<td align="center" width="25%" valign="top">
<br>
<strong>Zero API Keys</strong><br><br>
Uses Claude CLI's built-in auth. No Anthropic API key needed for the proxy itself.
<br><br>
</td>
<td align="center" width="25%" valign="top">
<br>
<strong>Production Ready</strong><br><br>
Async FastAPI, configurable timeouts, rate-limit detection, and structured error responses.
<br><br>
</td>
</tr>
</table>

---

## Quick Start

**Prerequisites** — Python 3.9+ &nbsp;&middot;&nbsp; A working [`claude.exe`](https://docs.anthropic.com/en/docs/claude-cli) &nbsp;&middot;&nbsp; pip

```bash
# clone and install
git clone https://github.com/MohammadAsadolahi/Claude-CLI-OpenAI-Proxy.git
cd Claude-CLI-OpenAI-Proxy
python -m venv .venv
.venv\Scripts\activate              # Linux/Mac: source .venv/bin/activate
pip install -r requirements.txt

# run
python server.py

# verify
curl http://127.0.0.1:8082/health
# → { "status": "ok", "model": "haiku", "effort": "max" }
```

---

## Configuration

All settings use environment variables with sensible defaults.

| Variable | Default | Description |
|:--|:--|:--|
| `CLAUDE_PATH` | `C:\Users\AG\.local\bin\claude.exe` | Path to Claude CLI binary |
| `CLAUDE_MODEL` | `haiku` | Model alias — `haiku` · `sonnet` · `opus` |
| `CLAUDE_EFFORT` | `max` | Thinking effort — `min` · `low` · `balanced` · `high` · `max` |
| `PORT` | `8082` | HTTP listen port |
| `CLAUDE_TIMEOUT` | `300` | Per-request timeout in seconds |

<details>
<summary><strong>PowerShell example</strong></summary>

```powershell
$env:CLAUDE_PATH   = 'C:\Users\AG\.local\bin\claude.exe'
$env:CLAUDE_MODEL  = 'sonnet'
$env:CLAUDE_EFFORT = 'max'
$env:PORT          = '8082'
python server.py
```

</details>

<details>
<summary><strong>Bash example</strong></summary>

```bash
export CLAUDE_PATH="/usr/local/bin/claude"
export CLAUDE_MODEL="sonnet"
export CLAUDE_EFFORT="max"
export PORT="8082"
python server.py
```

</details>

---

## API Reference

| Method | Endpoint | Description |
|:--|:--|:--|
| `POST` | `/v1/chat/completions` | Chat completions and tool calls |
| `GET` | `/v1/models` | List available models |
| `GET` | `/health` | Health check with config info |

### POST `/v1/chat/completions`

<details open>
<summary><strong>Text completion</strong></summary>

```bash
curl -X POST http://127.0.0.1:8082/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "messages": [
      {"role": "system", "content": "You are a helpful assistant."},
      {"role": "user", "content": "What is the capital of France?"}
    ]
  }'
```

```json
{
  "id": "chatcmpl-a1b2c3d4e5f6",
  "object": "chat.completion",
  "created": 1700000000,
  "model": "haiku",
  "choices": [{
    "index": 0,
    "message": { "role": "assistant", "content": "The capital of France is Paris." },
    "finish_reason": "stop"
  }],
  "usage": { "prompt_tokens": 25, "completion_tokens": 8, "total_tokens": 33 }
}
```

</details>

<details>
<summary><strong>Tool call (function calling)</strong></summary>

```bash
curl -X POST http://127.0.0.1:8082/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "messages": [
      {"role": "system", "content": "Extract medical relationships."},
      {"role": "user", "content": "Aspirin reduces cardiovascular disease risk."}
    ],
    "tools": [{
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
                  "entity2": {"type": "string"}
                }
              }
            }
          }
        }
      }
    }],
    "tool_choice": { "type": "function", "function": {"name": "store_relations"} }
  }'
```

```json
{
  "id": "chatcmpl-x7y8z9w0v1u2",
  "object": "chat.completion",
  "model": "haiku",
  "choices": [{
    "index": 0,
    "message": {
      "role": "assistant",
      "content": null,
      "tool_calls": [{
        "id": "call_a1b2c3d4e5f6",
        "type": "function",
        "function": {
          "name": "store_relations",
          "arguments": "{\"triplets\":[{\"entity1\":\"Aspirin\",\"relation\":\"reduces risk of\",\"entity2\":\"cardiovascular disease\"}]}"
        }
      }]
    },
    "finish_reason": "tool_calls"
  }]
}
```

</details>

### GET `/v1/models`

```json
{
  "object": "list",
  "data": [{ "id": "haiku", "object": "model", "created": 1700000000, "owned_by": "anthropic" }]
}
```

---

## Client Examples

<table>
<tr>
<td width="50%" valign="top">

**Python**

```python
from openai import OpenAI

client = OpenAI(
    api_key="not-needed",
    base_url="http://localhost:8082/v1",
)

resp = client.chat.completions.create(
    model="haiku",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(resp.choices[0].message.content)
```

</td>
<td width="50%" valign="top">

**JavaScript**

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

</td>
</tr>
<tr>
<td width="50%" valign="top">

**cURL**

```bash
curl http://localhost:8082/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Hello!"}]}'
```

</td>
<td width="50%" valign="top">

**Any OpenAI-compatible tool**

```yaml
API_BASE: http://localhost:8082/v1
API_KEY:  not-needed
MODEL:    haiku
```

Works with **Continue**, **Cursor**, **LangChain**, **LlamaIndex**, **AutoGen**, and more.

</td>
</tr>
</table>

---

## Error Handling

| Status | Type | Cause |
|:--|:--|:--|
| **200** | Success | Valid response returned |
| **429** | `rate_limit_error` | CLI is rate-limited or overloaded |
| **500** | `internal_error` | CLI crash, invalid JSON, or parse failure |
| **504** | `timeout_error` | CLI exceeded `CLAUDE_TIMEOUT` seconds |

---

## Project Structure

```
.
├── server.py           FastAPI app — proxy logic, endpoints, CLI invocation
├── test_server.py      End-to-end tests (chat, tool calls, NLP pipeline)
├── requirements.txt    Dependencies: fastapi, uvicorn, openai
├── .env.example        Environment variable template
└── README.md
```

---

## Testing

Start the server in one terminal, then run the test suite in another:

```bash
python server.py          # terminal 1
python test_server.py     # terminal 2
```

| Test | Validates |
|:--|:--|
| `test_chat` | Basic completion returns non-empty content |
| `test_tool_call` | Function calling extracts structured JSON matching the schema |
| `test_pronoun_resolution` | NLP pipeline step produces meaningful resolved text |

---

## Security

| Aspect | Detail |
|:--|:--|
| **Local execution** | Shells out to a local `claude.exe` binary. No requests leave your machine via the proxy. |
| **No credentials** | Uses Claude CLI's built-in authentication. The proxy itself requires no API key. |
| **Network exposure** | Binds to `0.0.0.0` by default. For production, deploy behind an authenticated gateway with TLS. |

---

## Troubleshooting

<details>
<summary><strong>500 — CLI errors</strong></summary>

Check server logs for `claude.exe` stderr output. Common causes: wrong `CLAUDE_PATH`, incompatible CLI version, or malformed JSON returned by the CLI.

</details>

<details>
<summary><strong>429 — Rate limiting</strong></summary>

The proxy detects rate-limit signals from CLI output (keywords: "rate", "429", "overloaded") and surfaces them as HTTP 429. Wait and retry.

</details>

<details>
<summary><strong>504 — Timeouts</strong></summary>

Increase `CLAUDE_TIMEOUT` (default: 300s) or switch to a faster model like `haiku`.

</details>

---

## Contributing

1. Fork the repository
2. Create a feature branch — `git checkout -b feature/my-feature`
3. Commit your changes — `git commit -m 'Add my feature'`
4. Push — `git push origin feature/my-feature`
5. Open a Pull Request

---

<p align="center">
  <img src="https://img.shields.io/badge/FastAPI-009688?style=flat-square&logo=fastapi&logoColor=white" alt="FastAPI"/>
  &nbsp;
  <img src="https://img.shields.io/badge/Python-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python"/>
  &nbsp;
  <img src="https://img.shields.io/badge/Anthropic-191919?style=flat-square&logo=anthropic&logoColor=white" alt="Anthropic"/>
</p>

<p align="center">
  <sub>Built by <a href="https://github.com/MohammadAsadolahi">Mohammad Asadolahi</a></sub>
</p>
