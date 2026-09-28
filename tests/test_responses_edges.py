"""T8 coverage gate — the real branches ``responses.py`` still left unexecuted.

Every module under ``src/sdlc_factory/providers/`` has to clear 90% line
coverage (PLAN.md §3-T8). ``responses.py`` was at 97%; each line left unexecuted
turned out to be a behaviour worth naming rather than a defensive corner, so each
is pinned here: what a history carries when a field is missing or oddly typed,
which of ``output_index``/``item_id`` identifies a streamed tool call, and what a
failure event says when it carries no error body.
"""

import logging
from types import SimpleNamespace

import pytest

from sdlc_factory.providers import responses as responses_api


@pytest.fixture
def logs():
    """Every record the app logger emits, with INFO enabled for the duration."""
    records: list = []
    logger = logging.getLogger("sdlc_factory")
    previous_level = logger.level

    class Collector(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = Collector()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


class RecordingCompletions:
    """Any use of the completions path from a Responses send is a routing bug."""

    def create(self, **kwargs):  # pragma: no cover - assertion fires first
        raise AssertionError(f"chat.completions.create called: {kwargs}")


class FakeResponses:
    def __init__(self, script):
        self.calls = []
        self._script = script

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return iter(self._script) if isinstance(self._script, list) else self._script


class FakeClient:
    def __init__(self, script):
        self.responses = FakeResponses(script)
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=RecordingCompletions().create))


def event(type_, **fields):
    return SimpleNamespace(type=type_, **fields)


def output_item(item_type="function_call", id="fc_1", **fields):
    return SimpleNamespace(type=item_type, id=id, **fields)


def item_done(index, item):
    return event("response.output_item.done", output_index=index, item=item)


def send_stream(script, messages=None, model="grok-4"):
    return responses_api.send(FakeClient(script), model,
                              messages if messages is not None
                              else [{"role": "user", "content": "x"}],
                              [], 0.0, 100, None)


# --- a history whose fields are missing or oddly typed ---------------------

def test_content_that_is_neither_text_nor_parts_is_still_sent_as_text():
    """A content field of some other type must not break the translation."""
    items = responses_api.to_responses_input([{"role": "user", "content": 42}])
    assert items == [{"role": "user",
                      "content": [{"type": "input_text", "text": "42"}]}]

def test_a_tool_call_without_arguments_is_replayed_as_empty_arguments():
    """``arguments: None`` is a call with no arguments, not a broken history."""
    items = responses_api.to_responses_input([
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "call_1",
                         "function": {"name": "read", "arguments": None}}]}])
    assert items == [{"type": "function_call", "call_id": "call_1", "name": "read",
                      "arguments": ""}]

def test_an_image_part_with_no_url_is_dropped_with_a_warning(logs):
    """It cannot be sent; saying so is the difference between a mystery and a bug."""
    items = responses_api.to_responses_input([
        {"role": "user", "content": [
            {"type": "text", "text": "look:"},
            {"type": "image", "image_url": None},
            {"type": "image", "image_url": {"url": "https://x.test/a.png"}},
        ]}])
    assert items == [{"role": "user", "content": [
        {"type": "input_text", "text": "look:"},
        {"type": "input_image", "image_url": "https://x.test/a.png"}]}]
    assert any("no usable url" in record.getMessage() for record in logs)


# --- streamed tool calls: which identity names them -----------------------

def test_a_tool_call_found_by_item_id_adopts_the_output_index_that_later_arrives():
    """One call named three different ways across events stays one call.

    Servers differ in whether an event carries ``output_index``, ``item_id`` or
    both; the arguments of one call must never split into two calls.
    """
    result = send_stream([
        event("response.function_call_arguments.delta", item_id="fc_1", delta='{"a": '),
        event("response.function_call_arguments.delta", output_index=0, item_id="fc_1",
              delta="1"),
        event("response.function_call_arguments.delta", output_index=0,
              item_id="fc_other", delta=", \"b\": 2}"),
        item_done(0, output_item(id="fc_1", call_id="call_1", name="read",
                                 arguments='{"a": 1, "b": 2}')),
    ])

    calls = result.choices[0].message.tool_calls
    assert len(calls) == 1
    assert (calls[0].id, calls[0].function.name, calls[0].function.arguments) == (
        "call_1", "read", '{"a": 1, "b": 2}')


def test_an_arguments_delta_that_names_its_function_is_enough_to_keep_the_call():
    """No ``output_item.added`` event: the delta itself carries the name."""
    result = send_stream([
        event("response.function_call_arguments.delta", output_index=0, item_id="fc_1",
              name="read", delta='{"a": 1}'),
    ])

    call = result.choices[0].message.tool_calls[0]
    assert (call.function.name, call.function.arguments) == ("read", '{"a": 1}')


def test_a_completed_tool_call_carries_its_arguments_when_no_delta_did():
    """Some servers only fill the arguments in on the completed item."""
    result = send_stream([
        item_done(0, output_item(id="fc_1", call_id="call_1", name="read",
                                 arguments='{"a": 1}')),
    ])

    call = result.choices[0].message.tool_calls[0]
    assert (call.id, call.function.name, call.function.arguments) == (
        "call_1", "read", '{"a": 1}')


# --- empty deltas and empty failures --------------------------------------

def test_empty_text_and_reasoning_deltas_add_nothing(capsys):
    result = send_stream([
        event("response.output_text.delta", output_index=0, delta=""),
        event("response.reasoning_summary_text.delta", output_index=0, delta=""),
        event("response.output_text.delta", output_index=0, delta="real"),
    ])

    message = result.choices[0].message
    assert (message.content, message.reasoning_content) == ("real", None)
    assert capsys.readouterr().out == "real\n"


def test_a_failure_event_with_no_error_body_still_says_what_failed():
    with pytest.raises(responses_api.ResponsesError) as exc:
        send_stream([event("response.failed")])
    assert "response.failed" in str(exc.value)
