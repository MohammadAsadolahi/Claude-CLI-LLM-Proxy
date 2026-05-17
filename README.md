<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://img.shields.io/badge/%E2%9C%A6_Claude_CLI_%E2%86%92_OpenAI_Proxy-6366f1?style=for-the-badge&labelColor=1e1b4b">
    <img src="https://img.shields.io/badge/%E2%9C%A6_Claude_CLI_%E2%86%92_OpenAI_Proxy-6366f1?style=for-the-badge&labelColor=1e1b4b" alt="Claude CLI to OpenAI Proxy" height="40"/>
  </picture>
</p>

<p align="center">
  <strong>A lightweight FastAPI bridge that lets any OpenAI-compatible client<br>talk to your local Claude CLI — zero API keys required.</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.9+-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python"/>
  <img src="https://img.shields.io/badge/FastAPI-0.115+-009688?style=flat-square&logo=fastapi&logoColor=white" alt="FastAPI"/>
  <img src="https://img.shields.io/badge/OpenAI_Compatible-412991?style=flat-square&logo=openai&logoColor=white" alt="OpenAI"/>
  <img src="https://img.shields.io/badge/Claude_CLI-Local-D97706?style=flat-square&logo=anthropic&logoColor=white" alt="Claude"/>
  <img src="https://img.shields.io/badge/License-MIT-22c55e?style=flat-square" alt="License"/>
</p>

<p align="center">
  <a href="#why-this-exists">Why</a>&ensp;&bull;&ensp;<a href="#architecture">Architecture</a>&ensp;&bull;&ensp;<a href="#quick-start">Quick Start</a>&ensp;&bull;&ensp;<a href="#api-reference">API</a>&ensp;&bull;&ensp;<a href="#client-examples">Clients</a>&ensp;&bull;&ensp;<a href="#configuration">Config</a>
</p>

---

## Why This Exists

<table>
<tr>
<td width="50%" valign="top">

**The problem**

Your tools, SDKs, and IDE extensions speak **OpenAI**. Claude CLI speaks its own protocol. Without a translation layer, they can't communicate.

```mermaid
graph LR
    A["Your App<br><i>OpenAI format</i>"] -. "incompatible" .-> B["claude.exe"]
    style A fill:#1e293b,stroke:#334155,color:#e2e8f0
    style B fill:#1e293b,stroke:#334155,color:#e2e8f0
    linkStyle 0 stroke:#ef4444,stroke-dasharray:5
```

</td>
<td width="50%" valign="top">

**The solution**

This proxy sits between them — accepts OpenAI-format requests, calls your local `claude.exe`, and returns OpenAI-format responses. Drop-in, transparent.

```mermaid
graph LR
    A["Your App"] --> B["Proxy :8082"] --> C["claude.exe"]
    C --> B --> A
    style A fill:#1e293b,stroke:#6366f1,color:#e2e8f0
    style B fill:#6366f1,stroke:#4f46e5,color:#fff
    style C fill:#d97706,stroke:#b45309,color:#fff
```

</td>
</tr>
</table>

---

## Architecture

```mermaid
graph TB
    subgraph Clients
        C1["OpenAI SDKs"]
        C2["IDE Extensions"]
        C3["AI Agent Frameworks"]
        C4["Custom Applications"]
    end

    subgraph Proxy["FastAPI Proxy &ensp; :8082"]
        direction TB
        P1["Request Translator"]
        P2["Response Builder"]
        P3["Error Handler"]
    end

    subgraph Backend
        B1["claude.exe"]
        B2["Built-in Auth"]
    end

    C1 & C2 & C3 & C4 --> P1
    P1 --> B1
    B1 --> P2
    P2 --> C1 & C2 & C3 & C4
    P3 -. "fallback" .-> P2
    B1 --- B2

    style P1 fill:#6366f1,stroke:#4f46e5,color:#fff
    style P2 fill:#0ea5e9,stroke:#0284c7,color:#fff
    style P3 fill:#64748b,stroke:#475569,color:#fff
    style B1 fill:#d97706,stroke:#b45309,color:#fff
    style B2 fill:#92400e,stroke:#78350f,color:#fff
    style C1 fill:#1e293b,stroke:#334155,color:#e2e8f0
    style C2 fill:#1e293b,stroke:#334155,color:#e2e8f0
    style C3 fill:#1e293b,stroke:#334155,color:#e2e8f0
    style C4 fill:#1e293b,stroke:#334155,color:#e2e8f0
```

---

## Request Lifecycle

A complete trace of how a single request flows through the system:

```mermaid
sequenceDiagram
    participant C as Client
    participant P as Proxy
    participant CLI as claude.exe

    C->>+P: POST /v1/chat/completions
    Note over P: Parse messages, detect tools

    alt Has tool definitions
        Note over P: Augment system prompt<br>with JSON schema
    end

    P->>+CLI: subprocess: claude.exe -p --output-format json
    Note over CLI: Local auth, no API key
    CLI-->>-P: JSON envelope<br>{result, usage, cost}

    alt Tool call response
        Note over P: Extract JSON args,<br>build tool_calls array
        P-->>C: finish_reason: tool_calls
    else Text response
        P-->>-C: finish_reason: stop
    end
```

---

## Processing Pipeline

```mermaid
flowchart LR
    A["Incoming<br>Request"] --> B{"Tools<br>defined?"}

    B -- "yes" --> C["Augment system prompt<br>with JSON schema"]
    B -- "no" --> D["Pass through<br>messages"]

    C & D --> E["Execute<br>claude.exe"]

    E --> F{"Exit<br>code"}

    F -- "0" --> G["Parse JSON<br>envelope"]
    F -- "!= 0" --> H{"Rate<br>limited?"}

    H -- "yes" --> I["429"]
    H -- "no" --> J["500"]

    G --> K{"Tool<br>response?"}

    K -- "yes" --> L["Build<br>tool_calls"]
    K -- "no" --> M["Build text<br>response"]

    L & M --> N["Return OpenAI-<br>format JSON"]

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
<td align="center" width="25%">

**OpenAI Compatible**

Drop-in replacement for any client that speaks the OpenAI API format — SDKs, agents, IDE plugins.

</td>
<td align="center" width="25%">

**Tool Calling**

Full function-calling support via schema-augmented system prompts. Returns proper `tool_calls` responses.

</td>
<td align="center" width="25%">

**Zero API Keys**

Uses Claude CLI's built-in auth. No Anthropic API key needed for the proxy.

</td>
<td align="center" width="25%">

**Production Ready**

Async FastAPI, configurable timeouts, rate-limit detection, and structured error handling.

</td>
</tr>
</table>

---

## Quick Start

### Prerequisites

| Requirement | Version | Purpose |
|:--|:--|:--|
| Python | `3.9+` (recommended `3.11`) | Runtime |
| Claude CLI | Latest | AI backend — [`claude.exe`](https://docs.anthropic.com/en/docs/claude-cli) |
| pip | Any | Dependency installation |

### Install

```bash
git clone https://github.com/MohammadAsadolahi/Claude-CLI-OpenAI-Proxy.git
cd Claude-CLI-OpenAI-Proxy

python -m venv .venv
.venv\Scripts\activate            # Linux/Mac: source .venv/bin/activate
pip install -r requirements.txt
```

### Run

```bash
python server.py
```

### Verify

```bash
curl http://127.0.0.1:8082/health
```

```json
{ "status": "ok", "model": "haiku", "effort": "max" }
```

---

## Configuration

All settings use environment variables with sensible defaults.

| Variable | Default | Description |
|:--|:--|:--|
| `CLAUDE_PATH` | `C:\Users\AG\.local\bin\claude.exe` | Path to Claude CLI binary |
| `CLAUDE_MODEL` | `haiku` | Model alias — `haiku`, `sonnet`, or `opus` |
| `CLAUDE_EFFORT` | `max` | Thinking effort — `min` `low` `balanced` `high` `max` |
| `PORT` | `8082` | HTTP listen port |
| `CLAUDE_TIMEOUT` | `300` | Per-request timeout in seconds |

<details>
<summary>PowerShell example</summary>

```powershell
$env:CLAUDE_PATH   = 'C:\Users\AG\.local\bin\claude.exe'
$env:CLAUDE_MODEL  = 'sonnet'
$env:CLAUDE_EFFORT = 'max'
$env:PORT          = '8082'
python server.py
```

</details>

<details>
<summary>Bash example</summary>

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

### `POST /v1/chat/completions`

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

### `GET /v1/models`

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
<td width="50%">

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
<td width="50%">

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
<td width="50%">

**cURL**

```bash
curl http://localhost:8082/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Hello!"}]}'
```

</td>
<td width="50%">

**Any OpenAI-compatible tool**

```yaml
API_BASE: http://localhost:8082/v1
API_KEY:  not-needed
MODEL:    haiku
```

Works with **Continue**, **Cursor**, **LangChain**,
**LlamaIndex**, **AutoGen**, and more.

</td>
</tr>
</table>

---

## Error Handling

```mermaid
graph LR
    A["Request"] --> B{"Process"}
    B -- "ok" --> C["200"]
    B -- "rate limit" --> D["429"]
    B -- "cli error" --> E["500"]
    B -- "timeout" --> F["504"]

    style C fill:#10b981,stroke:#059669,color:#fff
    style D fill:#f59e0b,stroke:#d97706,color:#fff
    style E fill:#ef4444,stroke:#dc2626,color:#fff
    style F fill:#6366f1,stroke:#4f46e5,color:#fff
    style A fill:#1e293b,stroke:#334155,color:#e2e8f0
    style B fill:#1e293b,stroke:#6366f1,color:#e2e8f0
```

| Status | Type | Cause |
|:--:|:--|:--|
| **200** | Success | Valid response returned |
| **429** | `rate_limit_error` | CLI rate-limited or overloaded |
| **500** | `internal_error` | CLI crash, bad JSON, or parse failure |
| **504** | `timeout_error` | CLI exceeded `CLAUDE_TIMEOUT` |

---

## Project Structure

```
.
├── server.py             FastAPI app — proxy logic, endpoints, CLI invocation
├── test_server.py        End-to-end tests (chat, tool calls, NLP pipeline)
├── requirements.txt      Dependencies: fastapi, uvicorn, openai
├── .env.example          Environment variable template
└── README.md
```

---

## Testing

Start the server, then run the test suite in a second terminal:

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

| | |
|:--|:--|
| **Local execution** | Shells out to a local `claude.exe` binary. No requests leave your machine via the proxy. |
| **No credentials needed** | Uses Claude CLI's built-in authentication. The proxy itself requires no API key. |
| **Network exposure** | Binds to `0.0.0.0` by default. For production, deploy behind an authenticated gateway with TLS. |

---

## Troubleshooting

<details>
<summary><strong>500 — CLI errors</strong></summary>

Check server logs for `claude.exe` stderr. Common causes: wrong `CLAUDE_PATH`, incompatible CLI version, or malformed JSON output.

</details>

<details>
<summary><strong>429 — Rate limiting</strong></summary>

The proxy detects rate-limit signals ("rate", "429", "overloaded") from CLI output and returns HTTP 429. Wait and retry.

</details>

<details>
<summary><strong>504 — Timeouts</strong></summary>

Increase `CLAUDE_TIMEOUT` (default: 300s) or use a faster model like `haiku`.

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
  <img src="https://img.shields.io/badge/Python-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python"/>
  <img src="https://img.shields.io/badge/Anthropic-191919?style=flat-square&logo=anthropic&logoColor=white" alt="Anthropic"/>
</p>

<p align="center">
  <sub>Built by <a href="https://github.com/MohammadAsadolahi">Mohammad Asadolahi</a></sub>
</p>
