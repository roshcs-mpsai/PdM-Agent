"""The agent's loop as a LangGraph graph (AGT-07), for a deployment that wants LangGraph.

The runner's steps (runner.NODES) are the graph's nodes, and the step each one returns is the
only edge condition, so the graph cannot behave differently from ``Runner.run``; it adds
LangGraph's checkpointers (``build(runner, checkpointer=PostgresSaver(...))``) in place of the
runner's own agent_checkpoints table.

LangGraph is not installed in the environment this was built in (no package index), so this
module is not exercised there. tests/test_agent.py runs a scripted incident both ways and
compares the outcome and tool sequence wherever langgraph is installed:

    pip install langgraph && python -m pytest tests/test_agent.py -k langgraph
"""
from __future__ import annotations

from typing import Optional, TypedDict

from runner import NODES, Runner


class GraphState(TypedDict, total=False):
    state: dict


def _step(runner: Runner, node: str):
    def step(s: GraphState) -> GraphState:
        state = dict(s["state"])
        state["node"] = getattr(runner, f"_{node}")(state)
        return {"state": state}
    step.__name__ = node
    return step


def build(runner: Runner, checkpointer=None):
    """The compiled graph: START -> start, then each node to the node it names, 'end' to END."""
    from langgraph.graph import END, START, StateGraph

    graph = StateGraph(GraphState)
    for node in NODES:
        graph.add_node(node, _step(runner, node))
    graph.add_edge(START, "start")
    routes = {n: n for n in NODES}
    routes["end"] = END
    for node in NODES:
        graph.add_conditional_edges(node, lambda s: s["state"]["node"], routes)
    return graph.compile(checkpointer=checkpointer)


def run(runner: Runner, incident_id: str, trace_id: Optional[str] = None, checkpointer=None) -> dict:
    """One incident through the graph; returns the final run state (as Runner.run does)."""
    state = runner.initial_state(incident_id, trace_id)
    runner._began = runner.clock()
    config = {"recursion_limit": 4 * runner.budget.turns + 20,
              "configurable": {"thread_id": state["trace_id"]}}
    out = build(runner, checkpointer).invoke({"state": state}, config=config)
    return out["state"]
