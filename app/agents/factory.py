"""Agent factories.

* Quoting agent   -> langchain `create_agent`          (tool-calling ReAct agent)
* Inventory agent -> langchain `create_agent`          (tool-calling ReAct agent)
* Scheduler agent -> deepagents `create_deep_agent`    (planning via write_todos, built-in middleware)

Agents have no checkpointer of their own: they run as nodes inside the orchestrator graph, whose
checkpointer persists the whole workflow (thread_id = order id).
"""
from __future__ import annotations

from deepagents import create_deep_agent
from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain.agents import create_agent
from langchain.agents.middleware import TodoListMiddleware

from app.llm import prompts
from app.llm.provider import get_chat_model, get_fallback_middleware
from app.tools.agent_tools import INVENTORY_TOOLS, QUOTING_TOOLS, SCHEDULER_TOOLS


def build_quoting_agent():
    return create_agent(get_chat_model(), tools=QUOTING_TOOLS, system_prompt=prompts.QUOTING_SYSTEM,
                        middleware=get_fallback_middleware(), name="quoting_agent")


def build_inventory_agent():
    return create_agent(get_chat_model(), tools=INVENTORY_TOOLS, system_prompt=prompts.INVENTORY_SYSTEM,
                        middleware=get_fallback_middleware(), name="inventory_agent")


def build_scheduler_agent():
    """Deep agent with planning (write_todos) and read-only scratch files.

    SECURITY: deepagents ships `execute` (shell) and file-writing tools by default. This service must never let an
    LLM run shell commands, so we replace the default filesystem tools with a read-only subset (ls, read_file).
    """
    middleware = [TodoListMiddleware(), FilesystemMiddleware(tools=["ls", "read_file"]), *get_fallback_middleware()]
    return create_deep_agent(model=get_chat_model(), tools=SCHEDULER_TOOLS, system_prompt=prompts.SCHEDULER_SYSTEM,
                             middleware=middleware, name="scheduler_agent")
