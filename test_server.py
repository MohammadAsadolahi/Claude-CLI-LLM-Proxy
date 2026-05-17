"""
End-to-end tests for the Claude CLI OpenAI Proxy.
Start the server first:  python server.py
Then run:                 python test_server.py
"""

import json
import sys
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


if __name__ == "__main__":
    print("Claude CLI OpenAI Proxy — End-to-End Tests")
    print(f"Target: {PROXY_URL}\n")

    test("Chat completion", test_chat)
    test("Tool call (extraction pipeline format)", test_tool_call)
    test("Pronoun resolution (pipeline step 1)", test_pronoun_resolution)

    print(f"\n{'='*60}")
    print(f"Results: {passed} passed, {failed} failed")
    print(f"{'='*60}")

    sys.exit(1 if failed else 0)
