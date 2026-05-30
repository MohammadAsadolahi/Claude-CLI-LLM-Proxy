"""
End-to-end tests for the Claude CLI OpenAI Proxy.
Start the server first:  python server.py
Then run:                 python test_server.py
"""

import io
import json
import sys
import time
from openai import OpenAI

PROXY_URL = "http://localhost:8082/v1"

client = OpenAI(api_key="dummy-key", base_url=PROXY_URL)

passed = 0
failed = 0


def test(name, fn):
    global passed, failed
    print(f"\n{'='*60}")
    print(f"TEST: {name}")
    print(f"{'='*60}")
    try:
        fn()
        passed += 1
        print("PASSED")
    except Exception as e:
        failed += 1
        print(f"FAILED: {e}")


# ── Individual chat completion tests ────────────────────────────────────

def test_chat():
    resp = client.chat.completions.create(
        model="haiku",
        messages=[
            {"role": "system", "content": "Reply in exactly one word."},
            {"role": "user", "content": "Capital of France?"},
        ],
        reasoning_effort="high",
    )
    content = resp.choices[0].message.content
    print(f"  Response: {content}")
    assert content and len(content.strip()) > 0, "Empty response"


def test_tool_call():
    tool = {
        "type": "function",
        "function": {
            "name": "store_extracted_relations",
            "description": "Store extracted entity-relationship-entity triplets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "triplets": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "e1": {"type": "string"},
                                "e1_type": {
                                    "type": "string",
                                    "enum": [
                                        "ACTIVITY", "BODY_PART", "CHEMICALS",
                                        "DISEASE_DISORDER", "DRUGS", "BIO_MARKER",
                                        "FOOD", "SIGN_OR_SYMPTOM", "RISK_FACTOR",
                                        "HEALTH_PROCEDURES", "GENES",
                                        "DISCRIMINATIVES", "NONE",
                                    ],
                                },
                                "verb": {"type": "string"},
                                "e2": {"type": "string"},
                                "e2_type": {
                                    "type": "string",
                                    "enum": [
                                        "ACTIVITY", "BODY_PART", "CHEMICALS",
                                        "DISEASE_DISORDER", "DRUGS", "BIO_MARKER",
                                        "FOOD", "SIGN_OR_SYMPTOM", "RISK_FACTOR",
                                        "HEALTH_PROCEDURES", "GENES",
                                        "DISCRIMINATIVES", "NONE",
                                    ],
                                },
                            },
                            "required": ["e1", "e1_type", "verb", "e2", "e2_type"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["triplets"],
                "additionalProperties": False,
            },
        },
    }

    resp = client.chat.completions.create(
        model="haiku",
        messages=[
            {"role": "system", "content": "Extract medical relationships as triplets."},
            {
                "role": "user",
                "content": (
                    "Aspirin reduces the risk of cardiovascular disease. "
                    "Smoking causes lung cancer."
                ),
            },
        ],
        tools=[tool],
        tool_choice={
            "type": "function",
            "function": {"name": "store_extracted_relations"},
        },
        reasoning_effort="high",
        max_completion_tokens=16000,
    )

    tc = resp.choices[0].message.tool_calls
    assert tc, "No tool calls returned"
    assert tc[0].function.name == "store_extracted_relations"
    assert resp.choices[0].finish_reason == "tool_calls"

    args = json.loads(tc[0].function.arguments)
    print(f"  Tool: {tc[0].function.name}")
    print(f"  Triplets ({len(args['triplets'])}):")
    for t in args["triplets"]:
        print(
            f"    ({t['e1']}:{t['e1_type']}) --[{t['verb']}]--> "
            f"({t['e2']}:{t['e2_type']})"
        )

    assert "triplets" in args, "Missing 'triplets' key"
    assert len(args["triplets"]) >= 2, (
        f"Expected >= 2 triplets, got {len(args['triplets'])}"
    )


def test_pronoun_resolution():
    system = (
        "Replace every pronoun (he, she, it, they, them, their, his, her, its) "
        "with the specific entity it refers to. Preserve original meaning and "
        "structure. Output ONLY the final text."
    )
    user = (
        "Diabetes mellitus is a chronic disease. It causes frequent urination. "
        "Patients should monitor their blood sugar levels. "
        "Metformin treats it effectively."
    )

    resp = client.chat.completions.create(
        model="haiku",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        reasoning_effort="high",
    )

    content = resp.choices[0].message.content
    print(f"  Input:  {user}")
    print(f"  Output: {content}")
    assert content and len(content) > 20, "Response too short"


# ── Batch processing tests ─────────────────────────────────────────────

def test_batch_chat():
    """Batch of plain chat completions — OpenAI SDK workflow."""
    requests = [
        {
            "custom_id": "capital-france",
            "method": "POST",
            "url": "/v1/chat/completions",
            "body": {
                "model": "haiku",
                "messages": [
                    {"role": "system", "content": "Reply in exactly one word."},
                    {"role": "user", "content": "Capital of France?"},
                ],
            },
        },
        {
            "custom_id": "capital-japan",
            "method": "POST",
            "url": "/v1/chat/completions",
            "body": {
                "model": "haiku",
                "messages": [
                    {"role": "system", "content": "Reply in exactly one word."},
                    {"role": "user", "content": "Capital of Japan?"},
                ],
            },
        },
    ]

    jsonl = "\n".join(json.dumps(r) for r in requests)

    # 1. Upload file
    batch_file = client.files.create(
        file=("batch_input.jsonl", jsonl.encode("utf-8")),
        purpose="batch",
    )
    print(f"  Uploaded file: {batch_file.id}")
    assert batch_file.id.startswith("file-")

    # 2. Create batch
    batch = client.batches.create(
        input_file_id=batch_file.id,
        endpoint="/v1/chat/completions",
        completion_window="24h",
    )
    print(f"  Created batch: {batch.id} (status={batch.status})")
    assert batch.id.startswith("batch_")

    # 3. Poll for completion
    deadline = time.time() + 600
    while time.time() < deadline:
        batch = client.batches.retrieve(batch.id)
        counts = batch.request_counts
        print(
            f"  Poll: status={batch.status}  "
            f"completed={counts.completed}/{counts.total}  "
            f"failed={counts.failed}"
        )
        if batch.status in ("completed", "failed", "cancelled", "expired"):
            break
        time.sleep(5)

    assert batch.status == "completed", f"Batch ended with status '{batch.status}'"
    assert batch.output_file_id, "No output file produced"

    # 4. Download results
    result_content = client.files.content(batch.output_file_id)
    result_text = result_content.text
    result_lines = [json.loads(ln) for ln in result_text.strip().split("\n") if ln.strip()]

    print(f"  Results ({len(result_lines)}):")
    for r in result_lines:
        body = r["response"]["body"]
        answer = body["choices"][0]["message"]["content"]
        print(f"    {r['custom_id']}: {answer}")

    assert len(result_lines) == 2, f"Expected 2 results, got {len(result_lines)}"
    for r in result_lines:
        assert r["error"] is None, f"Request {r['custom_id']} had error: {r['error']}"
        assert r["response"]["status_code"] == 200


def test_batch_tool_call():
    """Batch of tool-call requests — same format as the extraction pipeline."""
    tool = {
        "type": "function",
        "function": {
            "name": "store_extracted_relations",
            "description": "Store extracted entity-relationship-entity triplets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "triplets": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "e1": {"type": "string"},
                                "e1_type": {"type": "string"},
                                "verb": {"type": "string"},
                                "e2": {"type": "string"},
                                "e2_type": {"type": "string"},
                            },
                            "required": ["e1", "e1_type", "verb", "e2", "e2_type"],
                        },
                    }
                },
                "required": ["triplets"],
            },
        },
    }

    requests = [
        {
            "custom_id": "doc-aspirin",
            "method": "POST",
            "url": "/v1/chat/completions",
            "body": {
                "model": "haiku",
                "messages": [
                    {"role": "system", "content": "Extract medical relationships as triplets."},
                    {"role": "user", "content": "Aspirin reduces the risk of cardiovascular disease."},
                ],
                "tools": [tool],
                "tool_choice": {
                    "type": "function",
                    "function": {"name": "store_extracted_relations"},
                },
            },
        },
        {
            "custom_id": "doc-smoking",
            "method": "POST",
            "url": "/v1/chat/completions",
            "body": {
                "model": "haiku",
                "messages": [
                    {"role": "system", "content": "Extract medical relationships as triplets."},
                    {"role": "user", "content": "Smoking causes lung cancer and chronic bronchitis."},
                ],
                "tools": [tool],
                "tool_choice": {
                    "type": "function",
                    "function": {"name": "store_extracted_relations"},
                },
            },
        },
    ]

    jsonl = "\n".join(json.dumps(r) for r in requests)

    batch_file = client.files.create(
        file=("batch_tool.jsonl", jsonl.encode("utf-8")),
        purpose="batch",
    )
    print(f"  Uploaded file: {batch_file.id}")

    batch = client.batches.create(
        input_file_id=batch_file.id,
        endpoint="/v1/chat/completions",
        completion_window="24h",
    )
    print(f"  Created batch: {batch.id}")

    deadline = time.time() + 600
    while time.time() < deadline:
        batch = client.batches.retrieve(batch.id)
        counts = batch.request_counts
        print(
            f"  Poll: status={batch.status}  "
            f"completed={counts.completed}/{counts.total}  "
            f"failed={counts.failed}"
        )
        if batch.status in ("completed", "failed", "cancelled", "expired"):
            break
        time.sleep(5)

    assert batch.status == "completed", f"Batch ended with status '{batch.status}'"
    assert batch.output_file_id

    result_content = client.files.content(batch.output_file_id)
    result_lines = [json.loads(ln) for ln in result_content.text.strip().split("\n") if ln.strip()]

    print(f"  Results ({len(result_lines)}):")
    for r in result_lines:
        body = r["response"]["body"]
        tc = body["choices"][0]["message"].get("tool_calls", [])
        assert tc, f"No tool calls for {r['custom_id']}"
        args = json.loads(tc[0]["function"]["arguments"])
        triplets = args.get("triplets", [])
        print(f"    {r['custom_id']}: {len(triplets)} triplets extracted")
        for t in triplets:
            print(f"      ({t['e1']}) --[{t['verb']}]--> ({t['e2']})")
        assert len(triplets) >= 1, f"Expected >= 1 triplet for {r['custom_id']}"

    assert len(result_lines) == 2


# ── Run all tests ──────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Claude CLI OpenAI Proxy — End-to-End Tests")
    print(f"Target: {PROXY_URL}\n")

    test("Chat completion", test_chat)
    test("Tool call (extraction pipeline format)", test_tool_call)
    test("Pronoun resolution (pipeline step 1)", test_pronoun_resolution)
    test("Batch: chat completions", test_batch_chat)
    test("Batch: tool calls (extraction format)", test_batch_tool_call)

    print(f"\n{'='*60}")
    print(f"Results: {passed} passed, {failed} failed")
    print(f"{'='*60}")

    sys.exit(1 if failed else 0)
