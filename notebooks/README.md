# Understand the five failure types

Open these notebooks in order and select the repository's `.venv/bin/python` as your Python kernel. Use **Run All** or run one cell at a time. A Jupyter-capable editor and `ipykernel` in the selected environment are needed for interactive execution.

If the kernel is unavailable, run this once from the repository root, then select `.venv/bin/python` in VS Code's notebook kernel picker:

```bash
uv pip install --python .venv/bin/python ipykernel
```

That setup downloads a Python package; executing the examples afterward requires no network access. Every notebook code cell was verified with network connections disabled. `ipykernel` is installed in this workspace’s project environment. Select **Research Finder (.venv)** or the repository’s `.venv/bin/python`, then restart the kernel.

| Notebook | What you will see |
| --- | --- |
| [01_invalid_json.ipynb](01_invalid_json.ipynb) | A saved response with an extra closing brace fails the actual JSON parser. |
| [02_schema_violations.ipynb](02_schema_violations.ipynb) | A saved response parses as JSON but fails the required response schema. |
| [03_invalid_passage_references.ipynb](03_invalid_passage_references.ipynb) | A saved response passes the schema but cites unavailable passage IDs. |
| [04_unsupported_direct_evidence.ipynb](04_unsupported_direct_evidence.ipynb) | A reconstructed direct-coverage declaration fails the proposed-component mapping check. |
| [05_unresolved_evidence_review.ipynb](05_unresolved_evidence_review.ipynb) | A saved final review leaves targets ineligible; inspect the blocking claims and passages. |

Each notebook contains one example, the relevant input or schema, a runnable production validation check, and an explanation of the result. Expected failures are caught and printed, so **Run All should finish successfully**. Full responses and inputs remain available in the `example` variable and the linked local `examples/` JSON files; long responses are only excerpted for display.

The saved-example cells run **offline with zero model calls**. Notebook 01 also has an explicit live DeepSeek call, disabled by default; set `RUN_MODEL_CALL = True` to make that paid request. They use the existing project dependencies. They do not rerun the research pipeline or change its validators. Notebook 01 displays the full prompt and user input, saves fresh responses under `local_results/`, and validates them. Importing the validator modules does not invoke a model.

## Where the examples came from

Fixtures were extracted from `cost_aware_agent_rl_20261011T034213Z/comparison.json`. Each fixture includes the checkpoint SHA-256, paper and direction IDs, and a JSON pointer locating the original record. Fixtures are bundled so you do not need the original run directory to execute the notebooks.

Notebooks 1–3 contain actual retained failed review attempts, which may have received subsequent repair attempts. They should not be interpreted as separate final failed papers. Notebook 5 uses the final saved evidence review for its paper.

Notebook 4 is explicitly **reconstructed**: the run saved its rejection diagnostic and target, but not the rejected extraction response. Its illustrative claim must not be read as a scientific result from the paper.

## How the checks differ

The processing order is JSON parsing → schema validation → reference/relationship validation → evidence review and eligibility. Passing an earlier check does not imply passing the next. “Unresolved” means an accepted comparison judgment is unavailable; it does not mean a hypothesis is false. Model review also does not replace your team's experiments.

For developers, `build_examples.py CHECKPOINT` regenerates these artifacts from the saved checkpoint. This is an explicit offline build step; it is not needed to use the notebooks.

To regenerate notebook 01’s live section after rebuilding the notebooks, run `build_json_request.py RUN_DIRECTORY`, then `json_call_cells.py`. The full request is reconstructed from saved records and hash-checked local evidence; the original wire request was not logged.
