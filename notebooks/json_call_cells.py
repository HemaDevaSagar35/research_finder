"""Add an explicit live-request section to notebook 01. No calls are executed."""
import json
from pathlib import Path

def add_call_cells(directory):
    path = directory / '01_invalid_json.ipynb'
    nb = json.loads(path.read_text())
    nb['cells'] = [c for c in nb['cells'] if not c['id'].startswith('live-')]
    sections = [
        ('markdown', """## Reproduce this specific model request
[Open the full input and prompt](examples/01_model_request.json).

This is the **evidence-review** call that produced the malformed response. The original wire request was not logged. Input is reconstructed from the exact saved record and targets, hash-verified source pages, and current production prompt/schema. Historical environment-dependent temperature and output caps were not saved, so this is not a guaranteed byte-for-byte historical request.

Inspect or edit the prompt/input below, run the model explicitly, then validate its new response. A fresh response may succeed or fail differently."""),
        ('code', '''request = load_example(root, "01_model_request")
system_prompt = request["system_prompt"]
user_input = request["user_input"]
pprint(request["provenance"])
print("Provider:", request["provider"], "Model:", request["model"])
print("Task:", user_input["task"])
print("Targets:", [t["target_id"] for t in user_input["payload"]["targets"]])
print("Claims:", len(user_input["record"]["claims"]))
print("Prior passages:", len(user_input["payload"]["prior_passages"]))
print("Candidate-source passages:", len(user_input["payload"]["candidate_source_passages"]))'''),
        ('markdown', '## Full system prompt\nEdit system_prompt to test a prompt change.'),
        ('code', 'print(system_prompt)'),
        ('markdown', '## Full user input\nIncludes the response schema, target, paper passages and claims under review. This output is long; the same input is in the linked JSON.'),
        ('code', 'print(json.dumps(user_input, indent=2, ensure_ascii=False))'),
        ('markdown', """## Explicit live model call
Set RUN_MODEL_CALL = True and execute this cell when ready. It uses the repository DeepSeek configuration/API key and JSON response mode. Run All is free with the default False.

There is no pipeline or evidence-repair loop. SDK retries are disabled; the shared client can still retry once with a smaller output allowance after a provider context rejection. The saved requested output allowance is adjusted by the same client/provider limits as the pipeline. Current environment settings apply. No API keys are printed or saved.

New requests and responses are saved under notebooks/local_results/, separately from the historical fixture."""),
        ('code', '''RUN_MODEL_CALL = False
MODEL = request["model"]
MAX_OUTPUT_TOKENS = request["max_tokens"]
messages = [
    {"role": "system", "content": system_prompt},
    {"role": "user", "content": json.dumps(user_input, ensure_ascii=False, separators=(",", ":"))},
]
fresh_result = None
if RUN_MODEL_CALL:
    from llm_client import AsyncLLMClient
    from datetime import datetime, timezone
    client = AsyncLLMClient(request["provider"], concurrency=1, max_retries=0)
    try:
        fresh_result = await client.chat_result(
            messages=messages, model=MODEL, max_tokens=MAX_OUTPUT_TOKENS,
            response_format=request["response_format"],
        )
    finally:
        await client._client.close()
    output_dir = root / "notebooks" / "local_results"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / ("01_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    output_path.write_text(json.dumps({"messages": messages, "requested_model": MODEL,
        "requested_max_tokens": MAX_OUTPUT_TOKENS, "response_format": request["response_format"],
        "text": fresh_result.text, "model": fresh_result.model,
        "finish_reason": fresh_result.finish_reason}, indent=2, ensure_ascii=False))
    print("Saved request and response:", output_path)
    print("Finish reason:", fresh_result.finish_reason)
else:
    print("Call skipped. Set RUN_MODEL_CALL = True to run it.")'''),
        ('markdown', '## Validate the fresh response\nIdentify the first failure without triggering another paid call. Passing these checks does not establish scientific correctness or final approval.'),
        ('code', '''if fresh_result is not None:
    step = "output completeness"
    try:
        if fresh_result.truncated:
            raise ValueError("truncated model response")
        step = "JSON parsing"
        parsed_fresh = _parse_json(fresh_result.text)
        print("JSON parsing: PASS")
        step = "schema validation"
        report_fresh = EvidenceReport.model_validate(parsed_fresh)
        print("Schema validation: PASS")
        step = "evidence-reference validation"
        saved_record = EvidenceRecord.model_validate(user_input["record"])
        validate_evidence_report(report_fresh, saved_record,
            [Passage.model_validate(p) for p in user_input["payload"]["prior_passages"]], require_scope=True)
        print("Evidence-reference validation: PASS")
        print("Scope citation gaps:", scope_citation_gaps(report_fresh, saved_record))
    except ValueError as error:
        print(step + ": FAIL")
        print(error)
else:
    print("No fresh response yet. Run the model-call cell when ready.")'''),
    ]
    for i, (kind, source) in enumerate(sections):
        cell = {'id': f'live-{i}', 'cell_type': kind, 'metadata': {}, 'source': source.splitlines(keepends=True)}
        if kind == 'code':
            cell.update(execution_count=None, outputs=[])
        nb['cells'].append(cell)
    nb['cells'][0]['source'] = ['# 1. Invalid JSON\n\nReplay the saved parsing failure, inspect the reconstructed request, and optionally make a fresh model call. Offline cells are free. The live call is disabled until you set RUN_MODEL_CALL = True.\n']
    path.write_text(json.dumps(nb, indent=2) + '\n')

if __name__ == '__main__':
    add_call_cells(Path(__file__).resolve().parent)
