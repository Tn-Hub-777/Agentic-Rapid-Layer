# Multi-Agent 3D Print Service

A multi-agent AI system that turns an uploaded 3D model into a priced, paid, scheduled print job.
Everything is **free / open source** and runs **offline out of the box** (scripted mock LLM, simulated printers, mock payments).

| Agent | Built with | Job |
|---|---|---|
| **Orchestrator** | LangGraph `StateGraph` + SQLite checkpointer + store | Runs the whole workflow, routes on conditions, pauses for human approval |
| **Quoting agent** | LangChain `create_agent` | Slices/estimates weight + time, calls the deterministic price engine, explains the quote, suggests configs |
| **Inventory agent** | LangChain `create_agent` | Checks stock, proposes substitutes, reserves filament, deducts after printing, reports low stock |
| **Print scheduler** | `deepagents.create_deep_agent` (+ `write_todos` planning) | Filters + ranks printers, assigns the best one, dispatches G-code |

**Golden rule:** the LLM chooses and explains; deterministic code decides. Prices, stock changes, printer eligibility,
payment verification and state transitions are never LLM decisions.

## Quick start (VS Code)

```bash
# 1. open this folder in VS Code, open a terminal
python -m venv .venv
# Windows PowerShell (only needed if activation is blocked by the execution policy)
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

# 2. run the tests (offline, ~6 s)
python -m pytest -q

# 3. command-line demo of the full flow
python scripts/run_demo.py

# 4. the web UI
streamlit run ui/streamlit_app.py

# 5. (optional) REST API  ->  http://localhost:8000/docs
uvicorn app.api.main:app --reload
```

VS Code: select the `.venv` interpreter (Ctrl+Shift+P → *Python: Select Interpreter*). The **Run and Debug** panel
already has launch configs for the UI, API, CLI demo and eval. `examples/` holds six sample STL files
(`too_big.stl` is rejected, `large_bracket.stl` only fits one printer).

Copy `.env.example` to `.env` to change the LLM, slicer or payment provider.

## What happens to an order

```
upload -> validate+analyse (trimesh) -> QUOTING AGENT -> INVENTORY AGENT (stock check)
   short on stock? -> propose substitute -> re-quote
   -> [customer approval ⏸] -> create payment -> [payment ⏸, signature verified server-side]
   -> INVENTORY AGENT (reserve) -> PRINT SCHEDULER (rank printers, assign, send G-code)
   -> [print finished ⏸] -> INVENTORY AGENT (deduct, low-stock alert) -> long-term memory updated
```
Every ⏸ is a LangGraph `interrupt()`: the order is saved to the checkpoint DB and can be resumed later, even after a restart.
Failures at any step release reserved filament and flag refunds.

## Where each course topic is demonstrated

* **Agentic AI:** StateGraph orchestration, conditional edges, `create_agent` + `create_deep_agent`, tools, system prompts,
  message history, `interrupt()` human-in-the-loop, SQLite checkpointer (short-term memory), SqliteStore (long-term memory),
  agent handoff through shared state, retry when an agent skips a tool.
* **LLM engineering:** versioned system prompts (`app/llm/prompts.py`), tool schemas, provider abstraction
  (mock / Ollama / Groq / Gemini via env vars), model fallback middleware, argument validation, token + latency tracking.
* **AI engineering:** deterministic/probabilistic boundary, guardrails (path sandbox, no shell tool, validated tool args,
  HMAC-verified payments), observability tables, evaluation dataset + runner, 52 automated tests.

More detail: `docs/architecture.md`, `docs/agents.md`, `docs/setup-real-llm-and-hardware.md`.

## Project layout

```
app/
  agents/factory.py        build the 3 agents
  graph/orchestrator.py    LangGraph workflow (nodes, routing, interrupts)   <- integration lives here
  graph/state.py           shared PrintJobState
  graph/order_status.py    order state machine (rejects QUOTE_READY -> PRINTING)
  tools/                   geometry, pricing, config rules, inventory, scheduler ops, payments, @tool wrappers
  llm/                     provider.py (abstraction), mock.py (offline model), prompts.py
  memory/stores.py         checkpointer + long-term store
  slicer/service.py        PrusaSlicer CLI + estimator fallback
  drivers/printers.py      Simulated + OctoPrint drivers
  observability/tracing.py callback -> trace_events table (+ optional LangSmith)
  service.py               entry point used by UI/API/tests
  api/main.py              FastAPI
ui/streamlit_app.py        web UI
tests/                     units, agents, orchestrator, API, eval dataset
scripts/                   run_demo.py, run_eval.py, make_examples.py
```

## Honest limitations

* The **mock LLM is a script**, not a model. It exercises the integration (graph, tools, state, checkpoints). Its eval score
  (7/7) only proves the plumbing; run `scripts/run_eval.py` with a real model to measure language understanding.
* Real LLM providers, PrusaSlicer, OctoPrint and Razorpay code paths are written but **were not run** where this was built
  (offline sandbox). Expect to debug small things the first time, especially small local models and tool calling.
* Slicing without PrusaSlicer uses an estimator (weight within a few % of reality for simple parts; time is rougher).
* Simulated printers do not tick down on their own: "print finished" is a button/endpoint (a real setup would receive a
  printer/OctoPrint webhook).
* No user accounts, no admin dashboard, no object storage: deliberately out of scope for the multi-agent focus.
