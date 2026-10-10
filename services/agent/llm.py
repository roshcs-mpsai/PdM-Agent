"""The language model behind the agent (T16; AGT-11).

``AnthropicModel`` calls the Messages API over HTTPS with httpx (the anthropic SDK is not a
dependency). What the pinned model, claude-opus-5-5, requires and what this client does:

* no ``temperature``, ``top_p`` or ``top_k``: the model refuses non-default values (400). The
  FRD's temperature 0 cannot be set; runs are audited from their stored transcript instead;
* ``tool_choice`` is ``auto``: forcing a tool is refused, so the system prompt says when to call
  submit_brief and the runtime nudges once the model stops without calling it;
* thinking is always on (adaptive). Its blocks arrive with an empty text and a signature and are
  echoed back exactly as received; the runner never edits, reorders or drops a block;
* ``output_config.effort`` sets how hard the model thinks; ``max_tokens`` covers thinking and text;
* strict tool use (``strict: true``) when configured: ``strict_schema`` drops what strict mode
  does not accept (length and number limits, maxItems) into the description, and closes every
  object (``additionalProperties: false``). The runtime still validates every input;
* prompt caching: the tools and system prompt are cached, and the latest message carries the
  breakpoint, so each turn pays for its new tokens only.

A failed call (no connection, timeout, 408, 429, 5xx, 529) is retried ``retries`` times with
backoff, then raised as ``ModelUnavailable``: the runner hands the incident back so it stays
visible as a raw alert. Any other 4xx is ``ModelError`` (a request the API will never accept).

``ScriptedModel`` returns canned turns, for tests and dry runs without an API key.
"""
from __future__ import annotations

import copy
import json
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence, Union

import httpx

API_URL = "https://api.anthropic.com"
API_VERSION = "2023-06-01"
RETRY_STATUS = {408, 429, 529}
EFFORTS = ("low", "medium", "high", "xhigh", "max")


class ModelUnavailable(RuntimeError):
    """The model could not be reached after the retries (an outage, not a bad request)."""


class ModelError(RuntimeError):
    """The API refused the request itself (a 4xx other than 408 and 429)."""


@dataclass
class Turn:
    """One model answer. ``content`` is kept exactly as received."""
    content: list
    stop_reason: str
    usage: dict
    model: str
    stop_details: Optional[dict] = None
    request_id: Optional[str] = None
    seconds: float = 0.0

    @property
    def tool_uses(self) -> list[dict]:
        return [b for b in self.content if b.get("type") == "tool_use"]

    @property
    def text(self) -> str:
        return "\n".join(b.get("text", "") for b in self.content if b.get("type") == "text")

    def record(self) -> dict:
        """What the trace keeps about the turn besides its content."""
        return {"stop_reason": self.stop_reason, "stop_details": self.stop_details, "usage": self.usage,
                "model": self.model, "request_id": self.request_id, "seconds": round(self.seconds, 3)}


def counted_tokens(usage: dict) -> int:
    """Tokens charged to the budget: input not read from cache, cache writes and output (thinking
    included). Cache reads are recorded but not counted, so the system prompt counts once."""
    return int(usage.get("input_tokens") or 0) + int(usage.get("cache_creation_input_tokens") or 0) + \
        int(usage.get("output_tokens") or 0)


# --- tool schemas ---------------------------------------------------------------------------------

_DROP = ("title", "$schema", "$id", "examples")
_LIMITS = {"minLength": "at least {} characters", "maxLength": "at most {} characters", "minimum": "at least {}",
           "maximum": "at most {}", "exclusiveMinimum": "more than {}", "exclusiveMaximum": "less than {}",
           "multipleOf": "a multiple of {}", "maxItems": "at most {} items"}


def strict_schema(schema: dict) -> dict:
    """A JSON Schema as strict tool use accepts it: limits strict mode does not support move into
    the description (the runtime still enforces them), minItems above 1 becomes 1, annotations
    go, and every object gets ``additionalProperties: false``."""
    if not isinstance(schema, dict):
        return schema
    out: dict = {}
    notes: list[str] = []
    for key, value in schema.items():
        if key in _DROP:
            continue
        if key in _LIMITS:
            notes.append(_LIMITS[key].format(value))
        elif key == "minItems" and isinstance(value, int) and value > 1:
            notes.append(f"at least {value} items")
            out[key] = 1
        elif key in ("properties", "$defs", "definitions"):
            out[key] = {name: strict_schema(sub) for name, sub in value.items()}
        elif key in ("items", "not"):
            out[key] = strict_schema(value)
        elif key in ("anyOf", "allOf", "oneOf"):
            out[key] = [strict_schema(sub) for sub in value]
        else:
            out[key] = value
    if out.get("type") == "object" or "properties" in out:
        out["additionalProperties"] = False
    if notes:
        out["description"] = (f"{out['description']} " if out.get("description") else "") + f"({'; '.join(notes)})"
    return out


def tool_definitions(specs: Sequence[dict], strict: bool) -> list[dict]:
    """{name, description, input_schema} specs as the Messages API takes them."""
    out = []
    for s in specs:
        d = {"name": s["name"], "description": s["description"],
             "input_schema": strict_schema(s["input_schema"]) if strict else s["input_schema"]}
        if strict:
            d["strict"] = True
        out.append(d)
    return out


def strict_counts(definitions: Sequence[dict]) -> dict:
    """Optional parameters and union-typed parameters across strict tools (the API allows 24 and 16)."""
    optional = unions = 0

    def walk(node):
        nonlocal optional, unions
        if isinstance(node, dict):
            props = node.get("properties")
            if isinstance(props, dict):
                required = set(node.get("required") or [])
                for name, sub in props.items():
                    optional += name not in required
                    if isinstance(sub, dict) and ("anyOf" in sub or isinstance(sub.get("type"), list)):
                        unions += 1
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for d in definitions:
        if d.get("strict"):
            walk(d["input_schema"])
    return {"optional": optional, "unions": unions}


def with_cache_breakpoint(messages: list) -> list:
    """A copy of the conversation whose last message carries the cache breakpoint; the stored
    conversation itself is never changed."""
    out = copy.deepcopy(messages)
    if not out:
        return out
    last = out[-1]
    if isinstance(last.get("content"), str):
        last["content"] = [{"type": "text", "text": last["content"]}]
    for block in reversed(last["content"]):
        if block.get("type") in ("text", "tool_result", "image", "document"):
            block["cache_control"] = {"type": "ephemeral"}
            break
    return out


# --- the API ----------------------------------------------------------------------------------------

class AnthropicModel:
    """The Messages API. ``client``: an httpx.Client (tests pass one with a mock transport)."""

    def __init__(self, name: str, *, max_tokens: int = 8000, effort: Optional[str] = "medium",
                 timeout_s: float = 60, retries: int = 2, api_key: Optional[str] = None,
                 base_url: Optional[str] = None, client: Optional[httpx.Client] = None,
                 sleep: Callable[[float], None] = time.sleep):
        if effort is not None and effort not in EFFORTS:
            raise ValueError(f"effort is one of {', '.join(EFFORTS)}, not {effort!r}")
        self.name = name
        self.max_tokens = int(max_tokens)
        self.effort = effort
        self.timeout_s = float(timeout_s)
        self.retries = int(retries)
        self._key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self._key:
            raise ModelError("ANTHROPIC_API_KEY is not set")
        self.base_url = (base_url or os.environ.get("ANTHROPIC_BASE_URL") or API_URL).rstrip("/")
        self.client = client or httpx.Client(timeout=self.timeout_s)
        self.sleep = sleep

    def request(self, system: str, tools: list, messages: list) -> dict:
        """The request body (no sampling parameters, tool_choice auto, cache breakpoints)."""
        body = {"model": self.name, "max_tokens": self.max_tokens,
                "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                "tools": tools, "tool_choice": {"type": "auto"}, "messages": with_cache_breakpoint(messages)}
        if self.effort:
            body["output_config"] = {"effort": self.effort}
        return body

    def create(self, system: str, tools: list, messages: list) -> Turn:
        body = self.request(system, tools, messages)
        headers = {"x-api-key": self._key, "anthropic-version": API_VERSION, "content-type": "application/json"}
        last = "no attempt made"
        for attempt in range(self.retries + 1):
            started = time.monotonic()
            try:
                r = self.client.post(f"{self.base_url}/v1/messages", json=body, headers=headers,
                                     timeout=self.timeout_s)
            except httpx.HTTPError as exc:            # no connection, timeout, broken answer
                last = f"{type(exc).__name__}: {exc}"
                wait = 2.0 * 4 ** attempt
            else:
                if r.status_code == 200:
                    doc = r.json()
                    return Turn(content=doc.get("content") or [], stop_reason=doc.get("stop_reason") or "",
                                usage=doc.get("usage") or {}, model=doc.get("model") or self.name,
                                stop_details=doc.get("stop_details"), request_id=r.headers.get("request-id"),
                                seconds=time.monotonic() - started)
                message = _error_text(r)
                if r.status_code in RETRY_STATUS or r.status_code >= 500:
                    last = f"HTTP {r.status_code}: {message}"
                    wait = _retry_after(r, default=2.0 * 4 ** attempt)
                else:
                    raise ModelError(f"HTTP {r.status_code}: {message}")
            if attempt < self.retries:
                self.sleep(min(wait, 30.0))
        raise ModelUnavailable(f"{self.name} unreachable after {self.retries + 1} attempts ({last})")


def _error_text(r: httpx.Response) -> str:
    try:
        doc = r.json()
        err = doc.get("error") if isinstance(doc, dict) else None
        if isinstance(err, dict):
            return f"{err.get('type', 'error')}: {err.get('message', '')}"[:300]
    except ValueError:
        pass
    return (r.text or "")[:300]


def _retry_after(r: httpx.Response, default: float) -> float:
    try:
        return max(0.0, float(r.headers.get("retry-after", default)))
    except ValueError:
        return default


# --- scripted turns ---------------------------------------------------------------------------------

Step = Union[list, Callable[[list], list]]


def call(name: str, **arguments) -> dict:
    """A tool_use block for a script."""
    return {"type": "tool_use", "name": name, "input": arguments}


def say(text: str) -> dict:
    return {"type": "text", "text": text}


def thinking() -> dict:
    """A thinking block as claude-opus-5-5 sends it by default: no text, a signature."""
    return {"type": "thinking", "thinking": "", "signature": "c2NyaXB0ZWQ="}


@dataclass
class ScriptedModel:
    """Canned answers: each step is a list of blocks, or a function of the conversation so far
    that returns them. ``then`` once the steps run out: 'repeat' the last step or 'fail'.
    ``outage``: step numbers (from 0) that raise ModelUnavailable instead.

    In a step written as data (a JSON script), the string ``"{R:<tool>}"`` becomes the id of the
    latest result of that tool in the conversation, and ``"{R:<tool>*}"`` inside a list becomes
    all of them, so a fixed script can cite results whose ids depend on the run."""
    steps: list
    name: str = "scripted"
    then: str = "fail"
    outage: Sequence[int] = ()
    calls: list = field(default_factory=list)
    _i: int = 0

    def create(self, system: str, tools: list, messages: list) -> Turn:
        i = self._i
        self._i += 1
        self.calls.append({"system": system, "tools": [t["name"] for t in tools],
                           "messages": copy.deepcopy(messages)})
        if i in self.outage:
            raise ModelUnavailable(f"scripted outage at step {i}")
        if i >= len(self.steps):
            if self.then != "repeat" or not self.steps:
                raise AssertionError(f"the script has {len(self.steps)} steps; the runner asked for step {i + 1}")
            i = len(self.steps) - 1
        step = self.steps[i]
        blocks = copy.deepcopy(step(messages) if callable(step) else step)
        if not callable(step):
            blocks = _fill(blocks, results_seen(messages))
        for k, b in enumerate(blocks):
            if b.get("type") == "tool_use":
                b.setdefault("id", f"toolu_{self._i:02d}_{k}")
        stop = "tool_use" if any(b.get("type") == "tool_use" for b in blocks) else "end_turn"
        first = self._i == 1                     # the system prompt is written to the cache once
        usage = {"input_tokens": len(json.dumps(messages[-1:])) // 4, "output_tokens": len(json.dumps(blocks)) // 4,
                 "cache_read_input_tokens": 0 if first else len(system) // 4,
                 "cache_creation_input_tokens": len(system) // 4 if first else 0}
        return Turn(content=blocks, stop_reason=stop, usage=usage, model=self.name)


def _fill(node, seen: dict):
    """Replace {R:tool} and {R:tool*} placeholders with result ids seen in the conversation."""
    if isinstance(node, str) and node.startswith("{R:") and node.endswith("}") and not node.endswith("*}"):
        ids = seen.get(node[3:-1]) or []
        return ids[-1] if ids else node
    if isinstance(node, list):
        out = []
        for v in node:
            if isinstance(v, str) and v.startswith("{R:") and v.endswith("*}"):
                out.extend(seen.get(v[3:-2]) or [])
            else:
                out.append(_fill(v, seen))
        return out
    if isinstance(node, dict):
        return {k: _fill(v, seen) for k, v in node.items()}
    return node


def results_seen(messages: list) -> dict:
    """{tool: [result ids]} from the tool results and the first message of a conversation: what a
    script can cite."""
    out: dict = {}
    for m in messages:
        if m.get("role") != "user" or not isinstance(m.get("content"), list):
            continue
        for b in m["content"]:
            text = b.get("content") if b.get("type") == "tool_result" else b.get("text")
            if not isinstance(text, str):
                continue
            try:
                doc = json.loads(text)
            except ValueError:
                doc = None
            if isinstance(doc, dict) and "result_id" in doc and "tool" in doc:
                out.setdefault(doc["tool"], []).append(doc["result_id"])
    return out
