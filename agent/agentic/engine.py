"""Picks the graph engine: the real LangGraph when it is installed, else the built-in stand-in.

Force the stand-in (e.g. in tests) with SHADOWRUN_GRAPH_ENGINE=builtin.
"""
import os

ENGINE = "built-in"
if os.getenv("SHADOWRUN_GRAPH_ENGINE", "").lower() != "builtin":
    try:
        from langgraph.graph import END, START, StateGraph  # noqa: F401
        from langgraph.types import Command, interrupt  # noqa: F401
        try:
            from langgraph.checkpoint.memory import InMemorySaver as MemorySaver  # noqa: F401
        except ImportError:
            from langgraph.checkpoint.memory import MemorySaver  # noqa: F401
        ENGINE = "LangGraph"
    except Exception:  # not installed or an incompatible version: fall back, the demo keeps working
        ENGINE = "built-in"

if ENGINE == "built-in":
    from agent.agentic.minigraph import END, START, Command, MemorySaver, StateGraph, interrupt  # noqa: F401
