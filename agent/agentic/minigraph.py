"""A tiny stand-in for LangGraph, used ONLY when the langgraph package is not installed (and in unit tests).

It implements the small part of LangGraph's API that ShadowRun-Ops uses, with the same behaviour:
StateGraph(schema) with add_node / add_edge / add_conditional_edges / compile(checkpointer),
START / END, reducers via Annotated[list, operator.add], interrupt() + Command(resume=...) for
human-in-the-loop, and ainvoke / aget_state. With langgraph installed, the real library is used instead.
"""
import contextvars
import inspect
import typing
from types import SimpleNamespace

START = "__start__"
END = "__end__"

_MISSING = object()
_resume = contextvars.ContextVar("minigraph_resume", default=_MISSING)


class GraphInterrupt(Exception):
    def __init__(self, value):
        super().__init__("graph interrupted")
        self.value = value


class Interrupt:
    def __init__(self, value):
        self.value = value


class Command:
    def __init__(self, resume=None):
        self.resume = resume


def interrupt(value):
    """First run: pause the graph here. After Command(resume=x): returns x."""
    answer = _resume.get()
    if answer is _MISSING:
        raise GraphInterrupt(value)
    _resume.set(_MISSING)
    return answer


class MemorySaver:
    def __init__(self):
        self.threads = {}


class StateGraph:
    def __init__(self, schema):
        hints = typing.get_type_hints(schema, include_extras=True)
        self.keys = set(hints)
        self.reducers = {}
        for key, hint in hints.items():
            if typing.get_origin(hint) is typing.Annotated:
                self.reducers[key] = typing.get_args(hint)[1]
        self.nodes, self.edges, self.conditional = {}, {}, {}

    def add_node(self, name, fn):
        self.nodes[name] = fn

    def add_edge(self, source, target):
        self.edges[source] = target

    def add_conditional_edges(self, source, path, path_map=None):
        self.conditional[source] = (path, path_map)

    def compile(self, checkpointer=None):
        return CompiledGraph(self, checkpointer or MemorySaver())


class CompiledGraph:
    def __init__(self, graph, saver):
        self.g, self.saver = graph, saver

    def _apply(self, state, update):
        if update is None:
            return state
        if not isinstance(update, dict):
            raise TypeError(f"a node must return a dict, got {type(update).__name__}")
        new = dict(state)
        for key, value in update.items():
            if key not in self.g.keys:
                raise KeyError(f"node returned unknown state key '{key}'")
            reducer = self.g.reducers.get(key)
            new[key] = reducer(new.get(key, []), value) if reducer and key in new else value
        return new

    def _next(self, node, state):
        if node in self.g.conditional:
            path, path_map = self.g.conditional[node]
            key = path(state)
            return path_map[key] if path_map else key
        return self.g.edges.get(node, END)

    async def ainvoke(self, inp, config=None):
        config = config or {}
        thread = config.get("configurable", {}).get("thread_id", "default")
        limit = config.get("recursion_limit", 25)
        token = None
        if isinstance(inp, Command):
            saved = self.saver.threads[thread]
            state, node = saved["state"], saved["next"]
            token = _resume.set(inp.resume)
        else:
            state = self._apply({}, inp)
            node = self._next(START, state)
        steps = 0
        try:
            while node != END:
                steps += 1
                if steps > limit:
                    raise RecursionError(f"graph ran more than {limit} steps")
                try:
                    update = self.g.nodes[node](state)
                    if inspect.isawaitable(update):
                        update = await update
                except GraphInterrupt as gi:
                    self.saver.threads[thread] = {"state": state, "next": node}
                    return {**state, "__interrupt__": [Interrupt(gi.value)]}
                state = self._apply(state, update)
                node = self._next(node, state)
            self.saver.threads[thread] = {"state": state, "next": None}
            return state
        finally:
            if token is not None:
                _resume.reset(token)

    async def aget_state(self, config):
        thread = config.get("configurable", {}).get("thread_id", "default")
        saved = self.saver.threads.get(thread, {"state": {}, "next": None})
        return SimpleNamespace(values=saved["state"], next=(saved["next"],) if saved["next"] else ())
