"""T7 — the OpenAI **Responses** wire API (``client.responses.create``).

Copilot serves ``gpt-5*``/``grok-*``/``oswe*``/``mai-*`` models over the
Responses API, not ``chat/completions`` (PLAN.md §2.3), and the rest of this
codebase speaks only completions. This module is the translation layer on both
sides of that gap:

* request  — the internal chat-style history that session files already hold
  becomes Responses ``input`` items (:func:`to_responses_input`), system text
  becomes ``instructions``, chat tools become flat Responses tools
  (:func:`to_responses_tools`), and ``max_tokens`` becomes
  ``max_output_tokens``.
* reply    — streamed events (or a non-streamed ``output`` list) are
  reassembled into *exactly* the object shape the completions path returns, so
  ``agent._send_with_retry`` and every caller of ``response.choices[0].message``
  need no changes.

Reasoning items are the one piece of state the wire API carries that chat-style
history has no slot for. A Responses reply names each ``reasoning`` item by id,
and the next turn has to replay it or the model loses its own train of thought.
So ``send`` returns the ids on ``result.reasoning_item_ids`` (and on the message,
which ``chat.py`` appends to its history directly), ``agent`` persists them into
the assistant dict as ``reasoning_item_ids``, and :func:`to_responses_input`
re-emits ``{"type": "reasoning", "id": ..., "summary": []}`` for each. Replaying
a bare id is what the brief specifies; a server that has evicted that item
answers with a 400, which ``_send_with_retry`` re-raises rather than retrying —
loud, not silent.

Only the Responses path lives here. Completions-served models and non-Copilot
providers keep going through ``client.chat.completions.create`` untouched; the
one thing they now share is the console printer (:class:`ConsolePrinter`), so
both wire APIs stream to the terminal identically.
"""

import json
from types import SimpleNamespace
from typing import Any, Optional

import typer

from ..utils import global_logger

SYSTEM_ROLE = "system"
USER_ROLE = "user"
ASSISTANT_ROLE = "assistant"
TOOL_ROLE = "tool"

# ``text`` is what chat-style histories use, ``input_text``/``output_text`` what
# Responses uses; a part carrying any of them contributes text.
TEXT_PART_TYPES = ("text", "input_text", "output_text")
# This codebase emits ``{"type": "image", "image_url": {...}}`` (see
# copilot.IMAGE_CONTENT_TYPE); chat-style sessions also carry ``image_url``.
IMAGE_PART_TYPES = ("image", "image_url")

FUNCTION_CALL_TYPE = "function_call"
REASONING_TYPE = "reasoning"
MESSAGE_TYPE = "message"

ITEM_ADDED_EVENT = "response.output_item.added"
ITEM_DONE_EVENTS = ("response.output_item.done", "response.output_item.completed")
ARGS_DELTA_EVENT = "response.function_call_arguments.delta"
ARGS_DONE_EVENT = "response.function_call_arguments.done"
TEXT_DELTA_EVENTS = ("response.output_text.delta", "response.refusal.delta")
REASONING_SUMMARY_DELTA_EVENT = "response.reasoning_summary_text.delta"
FAILED_EVENTS = ("response.failed", "response.error")


class ResponsesError(Exception):
    """The Responses API reported a failed/errored response."""


# --- reading a history that is dicts *and* SDK objects ---------------------

def _field(source: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from a dict *or* an SDK object.

    ``chat.py``/``agent.py`` append the SDK's own message objects to the
    history, and a fake/test history is plain dicts; neither may be assumed.
    """
    if isinstance(source, dict):
        return source.get(name, default)
    return getattr(source, name, default)


def _text_of(content: Any) -> str:
    """Flatten message content (str, part list, or None) to text."""
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if isinstance(content, list):
        parts = []
        for part in content:
            text = _field(part, "text")
            if isinstance(text, str) and (_field(part, "type") in TEXT_PART_TYPES
                                         or _field(part, "type") is None):
                parts.append(text)
        return "".join(parts)
    return str(content)


def _image_url(value: Any) -> Optional[str]:
    """The url of an image part, spelled ``{"image_url": {"url": ...}}`` or bare."""
    if isinstance(value, str):
        return value
    url = _field(value, "url")
    return url if isinstance(url, str) and url else None


def _arguments(source: Any) -> str:
    """Tool-call arguments as a JSON string; a parsed dict gets re-encoded."""
    raw = _field(source, "arguments")
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    try:
        return json.dumps(raw)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return str(raw)


def _tool_calls_of(message: Any) -> list[dict]:
    """Normalize a message's tool_calls to ``{id, name, arguments}``."""
    calls = []
    for call in (_field(message, "tool_calls") or []):
        function = _field(call, "function")
        holder = function if function is not None else call
        calls.append({
            "id": _field(call, "id") or _field(call, "call_id"),
            "name": _field(holder, "name"),
            "arguments": _arguments(holder),
        })
    return calls


def _reasoning_ids(source: Any) -> list[str]:
    """Stored reasoning item ids, ignoring anything that is not a string."""
    ids = _field(source, "reasoning_item_ids")
    if not isinstance(ids, (list, tuple)):
        return []
    return [item for item in ids if isinstance(item, str) and item]


# --- request translation ---------------------------------------------------

def _input_parts(content: Any) -> list[dict]:
    """Content parts as Responses ``input_*`` parts; text-only stays one part."""
    if isinstance(content, list):
        parts = []
        for part in content:
            part_type = _field(part, "type")
            if part_type in IMAGE_PART_TYPES:
                url = _image_url(_field(part, "image_url"))
                if url:
                    parts.append({"type": "input_image", "image_url": url})
                else:
                    global_logger.warning(
                        f"Ignoring an image message part with no usable url: {part!r}")
                continue
            text = _field(part, "text")
            if isinstance(text, str) and text:
                parts.append({"type": "input_text", "text": text})
        if parts:
            return parts
    return [{"type": "input_text", "text": _text_of(content)}]


def instructions_from(messages: Any) -> Optional[str]:
    """The system text of a history, joined. ``None`` when it has none.

    The Responses API has no system *input item*: system text is the
    ``instructions`` parameter. Every system message is folded in, in order,
    because ``agent.py`` keeps exactly one at the head but a resumed session may
    not.
    """
    if not isinstance(messages, list):
        return None
    blocks = [_text_of(_field(message, "content")) for message in messages
              if _field(message, "role") == SYSTEM_ROLE]
    blocks = [block for block in blocks if block]
    return "\n\n".join(blocks) if blocks else None


def to_responses_input(messages: Any) -> list[dict]:
    """Translate the internal chat-style history into Responses input items.

    ``system`` messages are not input items — they are ``instructions`` (see
    :func:`instructions_from`). ``tool`` results become ``function_call_output``,
    an assistant turn becomes its replayed reasoning items, then one
    ``function_call`` per tool call, then its ``output_text`` message. An
    assistant turn with neither content nor tool calls is dropped: the API
    rejects an empty message, and it carries nothing.
    """
    items: list[dict] = []
    if not isinstance(messages, list):
        return items
    for message in messages:
        role = _field(message, "role")
        content = _field(message, "content")
        if role == SYSTEM_ROLE:
            continue
        if role == TOOL_ROLE:
            items.append({"type": "function_call_output",
                          "call_id": _field(message, "tool_call_id"),
                          "output": _text_of(content)})
            continue
        if role == ASSISTANT_ROLE:
            items.extend(_assistant_items(message, content))
            continue
        if role == USER_ROLE:
            items.append({"role": USER_ROLE, "content": _input_parts(content)})
            continue
        global_logger.warning(f"Skipping message with unsupported role "
                              f"for the Responses API: {role!r}")
    return items


def _assistant_items(message: Any, content: Any) -> list[dict]:
    items: list[dict] = []
    for reasoning_id in _reasoning_ids(message):
        items.append({"type": REASONING_TYPE, "id": reasoning_id, "summary": []})
    for call in _tool_calls_of(message):
        items.append({"type": FUNCTION_CALL_TYPE, "call_id": call["id"],
                      "name": call["name"], "arguments": call["arguments"]})
    text = _text_of(content)
    if text:
        items.append({"role": ASSISTANT_ROLE,
                      "content": [{"type": "output_text", "text": text}]})
    return items


def to_responses_tools(tools: Any) -> list[dict]:
    """Unwrap chat tools (``{"type": "function", "function": {...}}``).

    Responses takes a flat tool with ``name``/``description``/``parameters`` at
    the top level. ``strict`` is pinned to ``False``: structured-output strict
    mode rejects the ``additionalProperties``-free-but-loose schemas this repo
    hand-writes in ``agent._get_tools_schema``. Anything already flat (or not a
    function tool at all) is passed through, copied so the caller's list is
    never mutated.
    """
    converted: list[dict] = []
    for tool in (tools or []):
        if not isinstance(tool, dict):
            global_logger.warning(f"Skipping tool the Responses API cannot take: {tool!r}")
            continue
        function = tool.get("function")
        if isinstance(function, dict):
            flat = {"type": "function", "name": function.get("name"), "strict": False}
            for key in ("description", "parameters"):
                if key in function:
                    flat[key] = function[key]
            converted.append(flat)
            continue
        flat = dict(tool)
        if flat.get("type") == "function":
            flat.setdefault("strict", False)
        converted.append(flat)
    return converted


def request_payload(model: str, messages: list, tools: Any = None, temperature: Any = None,
                    max_tokens: Any = None, extra_headers: Any = None,
                    stream: bool = True) -> dict:
    """The kwargs for one ``responses.create`` call, from internal arguments.

    ``store`` is deliberately left at the SDK's default (true): the next turn
    replays reasoning items by id alone, and a server that does not keep the
    item cannot resolve it. (pi, which replays the whole item including its
    encrypted content, is the ``store: false`` variant of the same trick.)

    ``temperature`` is accepted and never sent. Verified live against Copilot:
    *every* model it serves over the Responses API answers an explicit
    ``temperature`` with ``400 "Unsupported parameter: 'temperature' is not
    supported with this model."`` — ``gpt-5-mini``, ``gpt-5.6-luna``,
    ``gpt-6-luna`` and ``mai-code-1.1-flash`` all do it, and the reference
    implementation (pi) never sends temperature through Copilot at all.

    The rule is scoped to the wire API, not to model ids, on purpose: the first
    version gated on a ``gpt-5`` prefix, ``gpt-6-luna`` slipped through, and the
    400 killed a live dreamer run. The Responses API itself does support
    temperature, but Copilot is the only provider this codebase routes here
    (PLAN.md §2.3), and every model it serves over this API refuses the
    parameter. Should a future provider here genuinely want it, that belongs on
    this signature as a compat flag — not as a name pattern to keep chasing.
    The argument stays either way so both wire APIs keep one shared caller
    signature; the completions path still passes it through.
    """
    kwargs: dict[str, Any] = {"model": model, "input": to_responses_input(messages),
                              "stream": bool(stream)}
    instructions = instructions_from(messages)
    if instructions:
        kwargs["instructions"] = instructions
    converted_tools = to_responses_tools(tools)
    if converted_tools:
        kwargs["tools"] = converted_tools
    if max_tokens:
        kwargs["max_output_tokens"] = int(max_tokens)
    # ``temperature`` is dropped here on purpose, for every model on this path;
    # the docstring above carries the verified reason. Nothing is sent in its
    # place: the model's own default sampling applies.
    kwargs["extra_headers"] = extra_headers
    return kwargs


# --- console streaming (shared by both wire APIs) --------------------------

class ConsolePrinter:
    """Live streaming output for one reply, in the completions path's format.

    Extracted so the Responses path prints exactly what the completions path
    has always printed: dim reasoning behind a ``💭 [Thinking]`` marker, cyan
    answer text with consecutive newlines collapsed, and a closing newline.
    ``enabled=False`` (a non-streamed reply) prints nothing and keeps no state.
    """

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._in_reasoning = False
        self._saw_text = False
        self._prev_char_was_newline = True

    def reasoning(self, chunk: Any) -> None:
        if not self.enabled or not chunk:
            return
        if not self._in_reasoning:
            self._in_reasoning = True
            typer.secho("💭 [Thinking] ", fg=typer.colors.BRIGHT_BLACK, dim=True, nl=False)
        typer.secho(chunk, nl=False, fg=typer.colors.BRIGHT_BLACK, dim=True)

    def text(self, chunk: Any) -> None:
        if not self.enabled or not chunk:
            return
        self.close_reasoning()
        self._saw_text = True
        filtered = ""
        for char in chunk:
            if char == "\n":
                if not self._prev_char_was_newline:
                    filtered += char
                self._prev_char_was_newline = True
            else:
                filtered += char
                self._prev_char_was_newline = False
        if filtered:
            typer.secho(filtered, nl=False, fg=typer.colors.CYAN)

    def close_reasoning(self) -> None:
        """End the thinking block: the answer (or a tool call) starts here."""
        if self.enabled and self._in_reasoning:
            self._in_reasoning = False
            typer.secho("\n", nl=False)
            self._prev_char_was_newline = True

    def finish(self) -> None:
        if not self.enabled:
            return
        self.close_reasoning()
        if self._saw_text and not self._prev_char_was_newline:
            typer.secho("")


# --- reply reassembly ------------------------------------------------------

def _result(text: str, reasoning: str, calls: list[dict], reasoning_ids: list[str]):
    """The completions path's return shape, from reassembled pieces."""
    tool_calls = [SimpleNamespace(id=call["id"], type="function",
                                  function=SimpleNamespace(name=call["name"],
                                                           arguments=call["arguments"]))
                  for call in calls]
    ids = list(reasoning_ids)
    message = SimpleNamespace(
        role=ASSISTANT_ROLE,
        content=text,
        tool_calls=tool_calls if tool_calls else None,
        reasoning_content=reasoning if reasoning else None,
        # A copy, not an alias: chat.py appends this message to its history, and
        # a shared list would let one holder's edit show up in the other's.
        reasoning_item_ids=list(ids),
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=message)],
                           reasoning_item_ids=ids)


class _CallSlots:
    """Tool calls under construction, keyed by output index and/or item id.

    Parallel calls interleave their argument deltas, so ``output_index`` (the
    order the model produced them in) is the identity that survives; ``item_id``
    is the fallback for events that carry one and not the other.
    """

    def __init__(self):
        self.slots: list[dict] = []
        self._by_index: dict[Any, dict] = {}
        self._by_item: dict[Any, dict] = {}

    def slot(self, output_index: Any = None, item_id: Any = None) -> dict:
        for table, key in ((self._by_item, item_id), (self._by_index, output_index)):
            if key is not None and key in table:
                slot = table[key]
                if output_index is not None and output_index not in self._by_index:
                    self._by_index[output_index] = slot
                if item_id is not None and item_id not in self._by_item:
                    self._by_item[item_id] = slot
                return slot
        slot = {"id": None, "name": None, "arguments": "", "order": len(self.slots),
                "index": output_index}
        self.slots.append(slot)
        if output_index is not None:
            self._by_index[output_index] = slot
        if item_id is not None:
            self._by_item[item_id] = slot
        return slot

    def ordered(self) -> list[dict]:
        """In the model's own order: output index when known, else arrival."""
        return sorted(self.slots, key=lambda slot: (
            slot["index"] if isinstance(slot["index"], int) else slot["order"],
            slot["order"]))


class _StreamAssembler:
    """Fold Responses stream events into one completions-shaped reply."""

    def __init__(self, printer: ConsolePrinter):
        self.printer = printer
        self.text = ""
        self.reasoning = ""
        self.calls = _CallSlots()
        self.reasoning_ids: list[str] = []

    def feed(self, event: Any) -> None:
        event_type = _field(event, "type") or ""
        if event_type in TEXT_DELTA_EVENTS:
            self._text(_field(event, "delta") or "")
        elif event_type == REASONING_SUMMARY_DELTA_EVENT:
            self._reasoning(_field(event, "delta") or "")
        elif event_type == ARGS_DELTA_EVENT:
            self._arguments(event, _field(event, "delta") or "", final=False)
        elif event_type == ARGS_DONE_EVENT:
            arguments = _field(event, "arguments")
            if isinstance(arguments, str) and arguments:
                self._arguments(event, arguments, final=True)
        elif event_type in (ITEM_ADDED_EVENT,) + ITEM_DONE_EVENTS:
            self._output_item(event)
        elif event_type in FAILED_EVENTS:
            raise ResponsesError(f"Responses API reported {event_type}: "
                                 f"{json.dumps(_error_detail(event))[:300]}")
        # Every other event (created/in_progress/complete/usage/...) is ignored:
        # the deltas above are the whole reply, and an unknown event type must
        # never break a send.

    def result(self):
        self.printer.finish()
        calls = []
        for call in self.calls.ordered():
            if call["id"] or call["name"]:
                calls.append(call)
            elif call["arguments"]:
                # Arguments with no call_id/name cannot be answered by a
                # function_call_output, so they are dropped — but not quietly.
                global_logger.warning(
                    f"Dropped a Responses tool call with no call_id/name: "
                    f"{call['arguments'][:120]}")
        return _result(self.text, self.reasoning, calls, self.reasoning_ids)

    # -- pieces

    def _text(self, delta: str) -> None:
        if not delta:
            return
        self.text += delta
        self.printer.text(delta)

    def _reasoning(self, delta: str) -> None:
        if not delta:
            return
        self.reasoning += delta
        self.printer.reasoning(delta)

    def _arguments(self, event: Any, arguments: str, final: bool) -> None:
        slot = self.calls.slot(_field(event, "output_index"), _field(event, "item_id"))
        name = _field(event, "name")
        if isinstance(name, str) and name:
            slot["name"] = name
        if final:
            # The done event carries the complete arguments; it wins over the
            # accumulated deltas rather than appending to them.
            slot["arguments"] = arguments
        else:
            slot["arguments"] += arguments

    def _output_item(self, event: Any) -> None:
        item = _field(event, "item")
        item_type = _field(item, "type")
        if item_type == FUNCTION_CALL_TYPE:
            slot = self.calls.slot(_field(event, "output_index"), _field(item, "id"))
            slot["id"] = _field(item, "call_id") or slot["id"]
            slot["name"] = _field(item, "name") or slot["name"]
            arguments = _arguments(item)
            if arguments and not slot["arguments"]:
                slot["arguments"] = arguments
        elif item_type == REASONING_TYPE:
            reasoning_id = _field(item, "id")
            if isinstance(reasoning_id, str) and reasoning_id \
                    and reasoning_id not in self.reasoning_ids:
                self.reasoning_ids.append(reasoning_id)
            self._reasoning_from_item(item)

    def _reasoning_from_item(self, item: Any) -> None:
        """Recover reasoning text an item carries but no delta ever streamed.

        The summary deltas are the live channel; a completed item repeats the
        same text back. Taking it only while nothing streamed keeps the two from
        doubling the reasoning string.
        """
        if self.reasoning:
            return
        parts = [_field(summary, "text") for summary in (_field(item, "summary") or [])]
        text = "".join(part for part in parts if isinstance(part, str))
        if text:
            self.reasoning = text


def _error_detail(event: Any) -> Any:
    detail = _field(event, "error") or _field(_field(event, "response"), "error") \
        or _field(event, "response") or _field(event, "code")
    if detail is None:
        return {"type": _field(event, "type")}
    if isinstance(detail, dict):
        return detail
    return {"detail": str(detail)}


def _from_response(response: Any):
    """Map a non-streamed response's ``output`` items to the reply shape."""
    status = _field(response, "status")
    if status == "failed":
        raise ResponsesError(f"Responses API returned a failed response: "
                             f"{json.dumps(_error_detail(response))[:300]}")
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    calls: list[dict] = []
    reasoning_ids: list[str] = []
    for item in (_field(response, "output") or []):
        item_type = _field(item, "type")
        if item_type == MESSAGE_TYPE:
            for part in (_field(item, "content") or []):
                text = _field(part, "text")
                if isinstance(text, str) and text:
                    text_parts.append(text)
        elif item_type == FUNCTION_CALL_TYPE:
            calls.append({"id": _field(item, "call_id"), "name": _field(item, "name"),
                          "arguments": _arguments(item)})
        elif item_type == REASONING_TYPE:
            reasoning_id = _field(item, "id")
            if isinstance(reasoning_id, str) and reasoning_id:
                reasoning_ids.append(reasoning_id)
            for summary in (_field(item, "summary") or []):
                text = _field(summary, "text")
                if isinstance(text, str) and text:
                    reasoning_parts.append(text)
    return _result("".join(text_parts), "".join(reasoning_parts), calls, reasoning_ids)


def send(client, model: str, messages: list, tools: Any = None, temperature: Any = None,
         max_tokens: Any = None, extra_headers: Any = None, stream: bool = True,
         no_stream: bool = False):
    """One Responses-API send, returned in the completions path's shape.

    ``choices[0].message`` carries ``content``/``tool_calls``/``reasoning_content``
    exactly as ``client.chat.completions.create`` would, so no caller changes;
    ``reasoning_item_ids`` (on the result *and* the message) is the one extra,
    and callers must persist it into the assistant dict for the next turn.

    Streaming is ``stream and not no_stream`` — ``_send_with_retry`` speaks
    ``no_stream``, the SDK speaks ``stream``; both spellings mean the same here.
    A non-streamed reply prints nothing, like the completions path.

    ``temperature`` is taken for signature parity with the completions path and
    then dropped by :func:`request_payload` — no model served over this API
    accepts it.
    """
    streaming = bool(stream) and not no_stream
    payload = request_payload(model, messages, tools, temperature, max_tokens,
                              extra_headers, stream=streaming)
    response = client.responses.create(**payload)
    if not streaming:
        return _from_response(response)

    assembler = _StreamAssembler(ConsolePrinter())
    for event in response:
        assembler.feed(event)
    return assembler.result()


# --- what the session file stores ------------------------------------------

def assistant_message_dict(result) -> dict:
    """The assistant dict for one reply, in the history's own chat style.

    Same keys the completions path writes (``role``/``content``/``tool_calls``)
    plus ``reasoning_item_ids`` when the reply had reasoning items — the ids the
    next turn replays. ``reasoning_content`` is not stored, matching the
    completions path: it is console/telemetry output, not conversation state.
    """
    message = result.choices[0].message
    stored: dict[str, Any] = {"role": ASSISTANT_ROLE, "content": _field(message, "content") or ""}
    calls = _tool_calls_of(message)
    if calls:
        stored["tool_calls"] = [{"id": call["id"], "type": "function",
                                 "function": {"name": call["name"],
                                              "arguments": call["arguments"]}}
                                for call in calls]
    reasoning_ids = _reasoning_ids(message) or _reasoning_ids(result)
    if reasoning_ids:
        stored["reasoning_item_ids"] = list(reasoning_ids)
    return stored