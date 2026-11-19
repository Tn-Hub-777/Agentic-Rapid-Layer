# Agents, prompts and tools

| Agent | Tools (the only things it can do) | Cannot |
|---|---|---|
| Quoting | `recommend_configuration`, `get_material_prices`, `estimate_print`, `calculate_price` | touch stock, printers, payments; invent prices |
| Inventory | `check_stock`, `suggest_alternative_material`, `reserve_material`, `deduct_material`, `release_reservation`, `low_stock_report` | change prices, dispatch prints |
| Scheduler | `list_printers`, `score_printers`, `assign_job`, `dispatch_gcode` + planning (`write_todos`) + read-only `ls`/`read_file` | run shell commands, write files |

System prompts live in `app/llm/prompts.py` (`PROMPT_VERSION`). Each begins with an `[AGENT:name]` tag used for logging and by the mock model.

## Guardrails (all tested in `tests/`)
* Tool arguments are validated (enums, ranges, allowed materials); invalid input returns an error instead of acting.
* File tools only accept paths inside `DATA_DIR`.
* `deepagents` ships a shell `execute` tool and file-writing tools by default. `build_scheduler_agent` replaces the default filesystem
  tools with a read-only subset so the model can never run commands or write files.
* AI configuration suggestions are never applied silently; the customer must choose "Apply recommendation" or "Approve".
* Dispatching to a printer is only reachable after approval -> verified payment -> reservation (graph structure, not a prompt).
* Payment signatures are verified with HMAC (constant-time compare) inside the workflow; webhook bodies are verified separately.
* The slicer runs as a subprocess with a fixed argv list, no shell and a timeout. In production run it in a container.

## Observability
`TraceLogger` writes every LLM call and tool call (agent, order, duration, tokens, error) to `trace_events`; the UI's
"Agent traces" tab and `GET /api/admin/traces` summarise it. For LangSmith set `LANGSMITH_TRACING=true` and `LANGSMITH_API_KEY`
in `.env`; no code changes needed.

## Evaluation
`tests/evals/config_cases.json` maps free-text requests to the expected goal/config. `pytest` runs it against the mock;
`python scripts/run_eval.py` runs it against your configured model and reports tool-selection accuracy, structured-output
validity, hallucinated goals, latency and tokens. Add cases to the JSON to extend it.
