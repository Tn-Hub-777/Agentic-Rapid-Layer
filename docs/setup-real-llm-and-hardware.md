# Switching from the demo setup to real components

Edit `.env` (copy from `.env.example`). Nothing else changes in the code.

## Real LLM (free options)
| Option | `.env` | Notes |
|---|---|---|
| Ollama (local) | `LLM_PROVIDER=ollama`, `LLM_MODEL=qwen3:8b` | Install Ollama, `ollama pull qwen3:8b`. Needs roughly 8 GB free RAM; a GPU helps a lot. Models of 3-4B parameters are much less reliable at tool calling. |
| Groq | `LLM_PROVIDER=groq`, `LLM_MODEL=<model>`, `GROQ_API_KEY=...` | Free tier with rate limits; limits and model names change, check their console. |
| Gemini (recommended) | `LLM_PROVIDER=gemini`, `LLM_MODEL=gemini-3.8-flash`, `GOOGLE_API_KEY=...` | Best current free-tier choice here for reliable multi-step tool use. Create the key in Google AI Studio. Free-tier quotas and availability can change. |
| Fallback | `FALLBACK_PROVIDER=groq`, `FALLBACK_MODEL=...` | Agents switch to it automatically if the primary model errors. |

Then run `python scripts/run_eval.py` and `streamlit run ui/streamlit_app.py`. If a small model skips tools, the orchestrator
retries once and then fails the order cleanly; try a larger model or Groq/Gemini.

## Real slicing
Install PrusaSlicer (free) and put its console binary on PATH (or set `PRUSA_SLICER_PATH`), keep `SLICER=auto`.
The quote then uses PrusaSlicer's own filament grams and print time; the quote card shows which slicer produced the numbers.

## Real printers
Per printer: set `driver='octoprint'` and `url` in the `printers` table, and put its key in env var `OCTOPRINT_KEY_<PRINTERID>`
(e.g. `OCTOPRINT_KEY_P1`). Replace the "print finished" button by an OctoPrint event webhook calling
`POST /api/orders/{id}/print-finished` (needs `X-Admin-Token`).

## Real payments
`PAYMENT_PROVIDER=razorpay` with Razorpay **test-mode** keys (free). The gateway result must reach
`POST /api/orders/{id}/payment` or `POST /api/payments/webhook`; the workflow re-verifies the signature server-side.

## Moving to PostgreSQL / Docker later
All SQL is in `app/db/database.py` and `app/tools/*_ops.py`; swap the connection layer and use `PostgresSaver` / `PostgresStore`
from LangGraph in `app/memory/stores.py`.
