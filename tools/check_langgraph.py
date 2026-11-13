"""Runs the agent's graph scenarios on the REAL LangGraph library (not the built-in stand-in).

    .\\venv\\Scripts\\python.exe tools\\check_langgraph.py

Prints "LANGGRAPH OK" when every scenario passes: tool-using investigation, memory recall (0 tokens),
engineer approval (interrupt + resume), denial, failed practice -> rethink, undo.
"""
import os
import sys
import unittest

os.environ["SHADOWRUN_GRAPH_ENGINE"] = "langgraph"  # must be set before the agent modules are imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.agentic.engine import ENGINE  # noqa: E402

if ENGINE != "LangGraph":
    print("LangGraph is NOT installed in this Python. Run:")
    print("  .\\venv\\Scripts\\python.exe -m pip install langgraph langchain-core")
    sys.exit(1)

from tests import test_agentic  # noqa: E402

suite = unittest.TestLoader().loadTestsFromTestCase(test_agentic.Graph)
result = unittest.TextTestRunner(verbosity=1).run(suite)
print("LANGGRAPH OK" if result.wasSuccessful() else "LANGGRAPH CHECK FAILED: send the output above to the team")
sys.exit(0 if result.wasSuccessful() else 1)
