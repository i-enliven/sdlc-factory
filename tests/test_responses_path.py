"""T7 — the OpenAI Responses wire API send path.

Copilot serves ``gpt-5*``/``grok-*``/``oswe*``/``mai-*`` models over
``client.responses.create``, not ``client.chat.completions.create`` (PLAN.md
§2.3). Two things are under test:

``providers/responses.py`` — translation in both directions. The internal
history format stays the chat-style dicts the session files already hold
(``providers/responses.to_responses_input`` / ``to_responses_tools``), and the
Responses reply — streamed or not — is reassembled into the exact object shape
the completions path returns, so ``_send_with_retry``'s callers need no changes.

The routing in ``agent.py``/``chat.py`` — ``wire_api="responses"`` sends through
``client.responses.create`` and never touches ``client.chat.completions``; the
default keeps the old path byte-for-byte.

No test here opens a socket: the client is a fake whose ``responses.create``
returns a scripted event iterator (streaming) or a response object (not).
"""

import contextlib
import json
from types import SimpleNamespace

import pytest

from sdlc_factory import agent as agent_module
from sdlc_factory import chat as chat_module
from sdlc_factory.providers import responses as responses_api
from sdlc_factory.providers.base import ResolvedAuth


# --- fake Responses client -------------------------------------------------

class ExplodingCompletions:
    """Any use of the completions path from a Responses send is a routing bug."""

    def create(self, **kwargs):  # pragma: no cover - assertion fires first
        raise AssertionError(f"chat.completions.create called: {kwargs}")


class FakeResponses:
    """``client.responses`` — records create() kwargs, replays scripted replies.

    One script is reused for every call; several scripts are consumed one per
    call, which is how a multi-turn test (tool call, then final answer) is
    scripted. A list is a streaming script (an event iterator); anything else is
    a plain response object.
    """

    def __init__(self, *scripts):
        self.calls = []
        self._scripts = list(scripts)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        script = self._scripts.pop(0) if len(self._scripts) > 1 else self._scripts[0]
        return iter(script) if isinstance(script, list) else script


class FakeClient:
    def __init__(self, *scripts):
        self.responses = FakeResponses(*scripts)
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=ExplodingCompletions().create))

    @property
    def responses_calls(self):
        return self.responses.calls


def event(type_, **fields):
    return SimpleNamespace(type=type_, **fields)


def output_item(item_type="function_call", id="fc_1", **fields):
    return SimpleNamespace(type=item_type, id=id, **fields)


def item_added(index, item):
    return event("response.output_item.added", output_index=index, item=item)


def item_done(index, item):
    return event("response.output_item.done", output_index=index, item=item)


def args_delta(index, item_id, delta):
    return event("response.function_call_arguments.delta",
                 output_index=index, item_id=item_id, delta=delta)


def args_done(index, item_id, arguments, name=None):
    return event("response.function_call_arguments.done",
                 output_index=index, item_id=item_id, arguments=arguments, name=name)


def text_delta(delta):
    return event("response.output_text.delta", output_index=0, item_id="msg_1", delta=delta)


def summary_delta(delta):
    return event("response.reasoning_summary_text.delta",
                 output_index=0, item_id="rs_1", summary_index=0, delta=delta)


# --- to_responses_input ----------------------------------------------------

def test_to_responses_input_translates_a_full_multi_turn_history():
    messages = [
        {"role": "system", "content": "You are a coder."},
        {"role": "user", "content": "fix the bug"},
        {"role": "user", "content": [
            {"type": "text", "text": "look at this"},
            {"type": "image", "image_url": {"url": "data:image/png;base64,AAA"}},
        ]},
        {"role": "assistant", "content": "reading it now",
         "reasoning_item_ids": ["rs_1"],
         "tool_calls": [{"id": "call_1", "type": "function",
                         "function": {"name": "run_cli_command",
                                      "arguments": '{"command": "ls"}'}}]},
        {"role": "tool", "tool_call_id": "call_1", "name": "run_cli_command",
         "content": "file.py"},
        {"role": "assistant", "content": "", "tool_calls": []},
        {"role": "assistant", "content": "done"},
    ]

    assert responses_api.to_responses_input(messages) == [
        {"role": "user", "content": [{"type": "input_text", "text": "fix the bug"}]},
        {"role": "user", "content": [
            {"type": "input_text", "text": "look at this"},
            {"type": "input_image", "image_url": "data:image/png;base64,AAA"},
        ]},
        {"type": "reasoning", "id": "rs_1", "summary": []},
        {"type": "function_call", "call_id": "call_1", "name": "run_cli_command",
         "arguments": '{"command": "ls"}'},
        {"role": "assistant", "content": [{"type": "output_text", "text": "reading it now"}]},
        {"type": "function_call_output", "call_id": "call_1", "output": "file.py"},
        {"role": "assistant", "content": [{"type": "output_text", "text": "done"}]},
    ]


def test_to_responses_input_drops_an_empty_assistant_message():
    assert responses_api.to_responses_input(
        [{"role": "assistant", "content": ""}]) == []
    assert responses_api.to_responses_input(
        [{"role": "assistant", "content": None}]) == []


def test_to_responses_input_emits_no_reasoning_item_without_stored_ids():
    items = responses_api.to_responses_input(
        [{"role": "assistant", "content": "thinking out loud"}])

    assert [item["type"] for item in items if "type" in item] == []
    assert items == [{"role": "assistant",
                      "content": [{"type": "output_text", "text": "thinking out loud"}]}]


def test_to_responses_input_accepts_sdk_message_objects():
    """``chat.py`` appends the SDK's own message objects to the history."""
    message = SimpleNamespace(role="assistant", content="calling a tool",
                              tool_calls=[SimpleNamespace(
                                  id="call_9", type="function",
                                  function=SimpleNamespace(name="sdlc_store_memory",
                                                           arguments='{"a": 1}'))],
                              reasoning_item_ids=["rs_9"])

    assert responses_api.to_responses_input([message]) == [
        {"type": "reasoning", "id": "rs_9", "summary": []},
        {"type": "function_call", "call_id": "call_9", "name": "sdlc_store_memory",
         "arguments": '{"a": 1}'},
        {"role": "assistant",
         "content": [{"type": "output_text", "text": "calling a tool"}]},
    ]


def test_to_responses_input_reads_image_url_part_shape():
    """Both spellings of an image part are internal formats in the wild."""
    items = responses_api.to_responses_input([{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
        {"type": "image", "image_url": "https://x/z.png"},
    ]}])

    assert items[0]["content"] == [
        {"type": "input_image", "image_url": "https://x/y.png"},
        {"type": "input_image", "image_url": "https://x/z.png"},
    ]


def test_to_responses_input_reads_a_tool_result_of_content_parts():
    items = responses_api.to_responses_input([{
        "role": "tool", "tool_call_id": "call_1", "name": "run_cli_command",
        "content": [{"type": "text", "text": "a\n"}, {"type": "text", "text": "b"}]}])

    assert items == [{"type": "function_call_output", "call_id": "call_1",
                      "output": "a\nb"}]


def test_to_responses_input_re_encodes_parsed_tool_call_arguments():
    """Some providers hand back arguments as a dict, not a JSON string."""
    items = responses_api.to_responses_input([{"role": "assistant", "content": "",
        "tool_calls": [{"id": "call_1", "type": "function",
                        "function": {"name": "x", "arguments": {"a": 1}}}]}])

    assert items[0] == {"type": "function_call", "call_id": "call_1", "name": "x",
                        "arguments": '{"a": 1}'}


def test_to_responses_input_skips_a_role_it_cannot_translate():
    """An unknown role is dropped and named, not silently sent as user text."""
    assert responses_api.to_responses_input(
        [{"role": "model", "content": "hey"}, {"role": "user", "content": "hi"}]) == [
        {"role": "user", "content": [{"type": "input_text", "text": "hi"}]}]


def test_to_responses_input_ignores_history_that_is_not_a_list():
    assert responses_api.to_responses_input(None) == []
    assert responses_api.instructions_from(None) is None


def test_to_responses_tools_skips_a_tool_it_cannot_translate():
    assert responses_api.to_responses_tools(["not-a-tool"]) == []


def test_instructions_from_joins_system_messages():
    assert responses_api.instructions_from(
        [{"role": "system", "content": "a"}, {"role": "user", "content": "u"},
         {"role": "system", "content": "b"}]) == "a\n\nb"
    assert responses_api.instructions_from([{"role": "user", "content": "u"}]) is None


# --- to_responses_tools ----------------------------------------------------

def test_to_responses_tools_unwraps_chat_tools_and_disables_strict():
    chat_tools = [{
        "type": "function",
        "function": {
            "name": "sdlc_web_search",
            "description": "Search the web.",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}},
                           "required": ["query"]},
        },
    }]

    assert responses_api.to_responses_tools(chat_tools) == [{
        "type": "function",
        "name": "sdlc_web_search",
        "description": "Search the web.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}},
                       "required": ["query"]},
        "strict": False,
    }]


def test_to_responses_tools_passes_an_already_flat_tool_through():
    flat = {"type": "function", "name": "x", "parameters": {"type": "object"}}

    assert responses_api.to_responses_tools([flat]) == [
        {"type": "function", "name": "x", "parameters": {"type": "object"}, "strict": False}]
    assert flat == {"type": "function", "name": "x", "parameters": {"type": "object"}}


def test_to_responses_tools_handles_empty_input():
    assert responses_api.to_responses_tools(None) == []
    assert responses_api.to_responses_tools([]) == []


# --- send(): request shape -------------------------------------------------

def test_send_puts_system_in_instructions_and_maps_max_output_tokens():
    client = FakeClient([text_delta("ok"), item_done(0, output_item(
        item_type="message", id="msg_1",
        content=[{"type": "output_text", "text": "ok"}]))])
    messages = [{"role": "system", "content": "be terse"},
                {"role": "user", "content": "hi"}]

    responses_api.send(client, "grok-4", messages, [], 0.3, 1200,
                       {"X-Initiator": "user"}, stream=True)

    call = client.responses_calls[0]
    assert call["model"] == "grok-4"
    assert call["instructions"] == "be terse"
    assert call["input"] == [{"role": "user",
                              "content": [{"type": "input_text", "text": "hi"}]}]
    assert call["max_output_tokens"] == 1200
    assert call["stream"] is True
    assert call["extra_headers"] == {"X-Initiator": "user"}
    assert "tools" not in call


def test_send_omits_temperature_for_gpt5_models():
    client = FakeClient([text_delta("ok")])
    responses_api.send(client, "gpt-5.1-codex", [{"role": "user", "content": "hi"}],
                       [], 0.0, 100, None)

    assert "temperature" not in client.responses_calls[0]


@pytest.mark.parametrize("model", ["grok-4", "oswe-1.5", "mai-1", "gpt-4.1"])
def test_send_passes_temperature_for_other_models(model):
    client = FakeClient([text_delta("ok")])
    responses_api.send(client, model, [{"role": "user", "content": "hi"}], [], 0.0, 100, None)

    assert client.responses_calls[0]["temperature"] == 0.0


def test_send_converts_no_stream_to_a_non_streaming_request():
    client = FakeClient(SimpleNamespace(output=[]))

    responses_api.send(client, "grok-4", [{"role": "user", "content": "hi"}],
                       [], 0.5, 100, None, no_stream=True)

    assert client.responses_calls[0]["stream"] is False


# --- send(): streaming reassembly -----------------------------------------

def test_send_stream_reassembles_text_and_prints_it(capsys):
    client = FakeClient([text_delta("hello"), text_delta(" world")])

    result = responses_api.send(client, "grok-4", [{"role": "user", "content": "hi"}],
                                [], 0.0, 100, None)

    message = result.choices[0].message
    assert message.role == "assistant"
    assert message.content == "hello world"
    assert message.tool_calls is None
    assert message.reasoning_content is None
    assert result.reasoning_item_ids == []
    assert capsys.readouterr().out == "hello world\n"


def test_send_stream_reassembles_interleaved_parallel_tool_calls():
    client = FakeClient([
        text_delta("working"),
        item_added(0, output_item(id="fc_1", call_id="call_1", name="read", arguments="")),
        item_added(1, output_item(id="fc_2", call_id="call_2", name="write", arguments="")),
        args_delta(0, "fc_1", '{"path":'),
        args_delta(1, "fc_2", '{"pa'),
        args_delta(0, "fc_1", ' "a.py"}'),
        args_delta(1, "fc_2", 'th": "b.py"}'),
        args_done(0, "fc_1", '{"path": "a.py"}'),
        item_done(0, output_item(id="fc_1", call_id="call_1", name="read",
                                 arguments='{"path": "a.py"}')),
        item_done(1, output_item(id="fc_2", call_id="call_2", name="write",
                                 arguments='{"path": "b.py"}')),
    ])

    result = responses_api.send(client, "grok-4", [{"role": "user", "content": "go"}],
                                [], 0.0, 100, None)

    message = result.choices[0].message
    assert message.content == "working"
    assert [call.id for call in message.tool_calls] == ["call_1", "call_2"]
    assert [call.type for call in message.tool_calls] == ["function", "function"]
    assert [(call.function.name, call.function.arguments) for call in message.tool_calls] == [
        ("read", '{"path": "a.py"}'), ("write", '{"path": "b.py"}')]


def test_send_stream_maps_reasoning_summary_to_reasoning_content(capsys):
    client = FakeClient([
        summary_delta("thinking "),
        summary_delta("hard"),
        item_done(0, output_item(item_type="reasoning", id="rs_7")),
        text_delta("the answer"),
    ])

    result = responses_api.send(client, "grok-4", [{"role": "user", "content": "why"}],
                                [], 0.0, 100, None)

    message = result.choices[0].message
    assert message.reasoning_content == "thinking hard"
    assert message.content == "the answer"
    assert result.reasoning_item_ids == ["rs_7"]
    assert message.reasoning_item_ids == ["rs_7"]
    out = capsys.readouterr().out
    assert "💭 [Thinking] " in out
    assert "thinking hard" in out


def test_send_stream_collects_reasoning_ids_from_the_completed_event():
    """Some servers only name the reasoning item on ``output_item.completed``."""
    client = FakeClient([
        event("response.output_item.completed", output_index=0,
              item=output_item(item_type="reasoning", id="rs_8")),
        text_delta("done"),
    ])

    result = responses_api.send(client, "grok-4", [{"role": "user", "content": "x"}],
                                [], 0.0, 100, None)

    assert result.reasoning_item_ids == ["rs_8"]


def test_send_stream_matches_argument_deltas_that_name_only_an_item_id():
    """Real servers vary in which of output_index/item_id each event carries."""
    client = FakeClient([
        item_added(0, output_item(id="fc_1", call_id="call_1", name="read", arguments="")),
        event("response.function_call_arguments.delta", item_id="fc_1", delta='{"a": '),
        event("response.function_call_arguments.done", item_id="fc_1",
              arguments='{"a": 1}'),
    ])

    result = responses_api.send(client, "grok-4", [{"role": "user", "content": "x"}],
                                [], 0.0, 100, None)

    call = result.choices[0].message.tool_calls[0]
    assert (call.id, call.function.name, call.function.arguments) == (
        "call_1", "read", '{"a": 1}')


def test_send_stream_recovers_reasoning_a_completed_item_carries_alone():
    """No summary deltas streamed: the completed item's summary is all we get."""
    client = FakeClient([
        item_done(0, output_item(item_type="reasoning", id="rs_5",
                                 summary=[{"type": "summary_text", "text": "hidden thought"}])),
        text_delta("answer"),
    ])

    result = responses_api.send(client, "grok-4", [{"role": "user", "content": "x"}],
                                [], 0.0, 100, None)

    assert result.choices[0].message.reasoning_content == "hidden thought"
    assert result.reasoning_item_ids == ["rs_5"]


def test_send_stream_drops_a_tool_call_that_never_got_an_identity():
    """Arguments with no call_id can never be answered; they must not become a
    tool call the next turn cannot reply to."""
    client = FakeClient([args_delta(None, None, '{"a": 1}'), text_delta("done")])

    result = responses_api.send(client, "grok-4", [{"role": "user", "content": "x"}],
                                [], 0.0, 100, None)

    assert result.choices[0].message.content == "done"
    assert result.choices[0].message.tool_calls is None


def test_send_stream_raises_on_a_failed_response():
    client = FakeClient([event("response.failed",
                               response=SimpleNamespace(status="failed"))])

    with pytest.raises(Exception, match="failed"):
        responses_api.send(client, "grok-4", [{"role": "user", "content": "x"}],
                           [], 0.0, 100, None)


def test_send_stream_does_not_print_when_not_streaming():
    """``no_stream`` callers get no console output, exactly like completions."""
    client = FakeClient(SimpleNamespace(output=[
        SimpleNamespace(type="message", content=[
            SimpleNamespace(type="output_text", text="quiet")]),
    ]))

    result = responses_api.send(client, "grok-4", [{"role": "user", "content": "hi"}],
                                [], 0.0, 100, None, no_stream=True)

    assert result.choices[0].message.content == "quiet"


# --- send(): non-stream mapping --------------------------------------------

def test_send_non_stream_maps_output_items_to_the_completions_shape():
    client = FakeClient(SimpleNamespace(output=[
        SimpleNamespace(type="reasoning", id="rs_3",
                        summary=[{"type": "summary_text", "text": "because"}]),
        SimpleNamespace(type="message", content=[
            SimpleNamespace(type="output_text", text="final "),
            SimpleNamespace(type="output_text", text="answer")]),
        SimpleNamespace(type="function_call", id="fc_3", call_id="call_3",
                        name="sdlc_store_memory", arguments='{"a": 1}'),
    ]))

    result = responses_api.send(client, "grok-4", [{"role": "user", "content": "hi"}],
                                [], 0.0, 100, None, no_stream=True)

    message = result.choices[0].message
    assert message.content == "final answer"
    assert message.reasoning_content == "because"
    assert [(call.id, call.function.name, call.function.arguments)
            for call in message.tool_calls] == [
        ("call_3", "sdlc_store_memory", '{"a": 1}')]
    assert result.reasoning_item_ids == ["rs_3"]


def test_send_non_stream_reads_dict_output_items():
    client = FakeClient({"output": [{"type": "message", "id": "msg_1",
                                     "content": [{"type": "output_text",
                                                  "text": "dict reply"}]}]})

    result = responses_api.send(client, "grok-4", [{"role": "user", "content": "hi"}],
                                [], 0.0, 100, None, no_stream=True)

    assert result.choices[0].message.content == "dict reply"
    assert result.choices[0].message.tool_calls is None


def test_send_non_stream_raises_on_a_failed_response():
    client = FakeClient(SimpleNamespace(status="failed",
                                        error={"code": "server_error"}, output=[]))

    with pytest.raises(Exception, match="failed response"):
        responses_api.send(client, "grok-4", [{"role": "user", "content": "hi"}],
                           [], 0.0, 100, None, no_stream=True)


# --- console printing: one printer, both wire APIs -------------------------

def test_console_printer_collapses_consecutive_newlines(capsys):
    printer = responses_api.ConsolePrinter()
    printer.text("a\n")
    printer.text("\nb")
    printer.finish()

    assert capsys.readouterr().out == "a\nb\n"


def test_console_printer_disabled_prints_nothing(capsys):
    printer = responses_api.ConsolePrinter(enabled=False)
    printer.reasoning("secret")
    printer.text("secret")
    printer.close_reasoning()
    printer.finish()

    assert capsys.readouterr().out == ""


def test_completions_path_prints_exactly_what_the_responses_path_prints(capsys, quiet_agent):
    """The refactor of the untouched completions path keeps its console output."""
    chunks = [
        SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
            reasoning_content="think", reasoning=None, content=None, tool_calls=None))]),
        SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
            reasoning_content=None, reasoning=None, content="answer\n\n", tool_calls=None))]),
    ]
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kwargs: chunks)), responses=SimpleNamespace(create=lambda **kw: None))

    call_send_with_retry(client, [{"role": "user", "content": "hi"}], quiet_agent)

    assert capsys.readouterr().out == "💭 [Thinking] think\nanswer\n"


# --- assistant_message_dict: what lands in the session file ----------------

def test_assistant_message_dict_round_trips_through_to_responses_input():
    result = SimpleNamespace(
        reasoning_item_ids=["rs_1"],
        choices=[SimpleNamespace(message=SimpleNamespace(
            role="assistant", content="calling tools",
            tool_calls=[SimpleNamespace(id="call_1", type="function",
                                        function=SimpleNamespace(
                                            name="run_cli_command",
                                            arguments='{"command": "ls"}'))],
            reasoning_content="hmm",
            reasoning_item_ids=["rs_1"]))])

    stored = responses_api.assistant_message_dict(result)

    assert stored == {
        "role": "assistant",
        "content": "calling tools",
        "tool_calls": [{"id": "call_1", "type": "function",
                        "function": {"name": "run_cli_command",
                                     "arguments": '{"command": "ls"}'}}],
        "reasoning_item_ids": ["rs_1"],
    }
    assert responses_api.to_responses_input([stored]) == [
        {"type": "reasoning", "id": "rs_1", "summary": []},
        {"type": "function_call", "call_id": "call_1", "name": "run_cli_command",
         "arguments": '{"command": "ls"}'},
        {"role": "assistant",
         "content": [{"type": "output_text", "text": "calling tools"}]},
    ]


def test_assistant_message_dict_stores_null_content_as_empty_string():
    """A reply with null content must not put ``None`` where history has text."""
    result = SimpleNamespace(
        reasoning_item_ids=[],
        choices=[SimpleNamespace(message=SimpleNamespace(
            role="assistant", content=None, tool_calls=None, reasoning_content=None))])

    assert responses_api.assistant_message_dict(result) == {
        "role": "assistant", "content": ""}


def test_assistant_message_dict_omits_absent_reasoning_ids():
    result = SimpleNamespace(
        reasoning_item_ids=[],
        choices=[SimpleNamespace(message=SimpleNamespace(
            role="assistant", content="plain", tool_calls=None,
            reasoning_content=None))])

    assert responses_api.assistant_message_dict(result) == {
        "role": "assistant", "content": "plain"}


# --- routing: agent.py -----------------------------------------------------

@pytest.fixture
def quiet_agent(monkeypatch, tmp_path):
    """No sleeps, no telemetry context; session files land in tmp_path."""
    monkeypatch.setattr(agent_module, "time", SimpleNamespace(sleep=lambda seconds: None))
    monkeypatch.setattr(agent_module, "using_session",
                        lambda session_id: contextlib.nullcontext())
    return tmp_path


def call_send_with_retry(client, messages, session_dir, **kwargs):
    kwargs.setdefault("provider", None)
    no_stream = kwargs.pop("no_stream", False)
    return agent_module._send_with_retry(
        client, messages, [], "grok-4", 0.0, 100, "session-1",
        session_dir / "session-1.session", no_stream=no_stream, **kwargs)


def test_send_with_retry_routes_responses_to_responses_create(quiet_agent):
    client = FakeClient([text_delta("streamed reply")])
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]

    response = call_send_with_retry(client, messages, quiet_agent,
                                    wire_api="responses")

    assert len(client.responses_calls) == 1
    assert response.choices[0].message.content == "streamed reply"
    assert messages[-1] == {"role": "assistant", "content": "streamed reply"}
    saved = json.loads((quiet_agent / "session-1.session").read_text(encoding="utf-8"))
    assert saved[-1] == {"role": "assistant", "content": "streamed reply"}


def test_send_with_retry_responses_persists_tool_calls_and_reasoning_ids(quiet_agent):
    client = FakeClient([
        summary_delta("pondering"),
        item_done(0, output_item(item_type="reasoning", id="rs_42")),
        item_added(1, output_item(id="fc_9", call_id="call_9", name="run_cli_command",
                                  arguments="")),
        args_delta(1, "fc_9", '{"command": "ls"}'),
    ])
    messages = [{"role": "user", "content": "ls"}]

    response = call_send_with_retry(client, messages, quiet_agent, wire_api="responses")

    assert response.choices[0].message.tool_calls[0].function.arguments == '{"command": "ls"}'
    assert messages[-1]["tool_calls"] == [
        {"id": "call_9", "type": "function",
         "function": {"name": "run_cli_command", "arguments": '{"command": "ls"}'}}]
    assert messages[-1]["reasoning_item_ids"] == ["rs_42"]
    # The next turn must replay the reasoning item, not lose it.
    assert {"type": "reasoning", "id": "rs_42", "summary": []} in \
        responses_api.to_responses_input(messages)


def test_send_with_retry_responses_sends_copilot_headers(quiet_agent):
    """The per-request header hook is shared by both wire APIs."""
    from sdlc_factory.providers.copilot import CopilotProvider

    client = FakeClient([text_delta("ok")])
    call_send_with_retry(client, [{"role": "user", "content": "hi"}], quiet_agent,
                         wire_api="responses", provider=CopilotProvider())

    assert client.responses_calls[0]["extra_headers"]["X-Initiator"] == "user"


def test_send_with_retry_defaults_to_the_completions_path(quiet_agent):
    """The old path is untouched: same call, same kwargs, same reply shape."""
    chunk = SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
        content="legacy reply", tool_calls=None, reasoning_content=None,
        reasoning=None))], model_dump=lambda exclude_unset=True: {})
    calls = []

    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=lambda **kwargs: (calls.append(kwargs), [chunk])[1])),
        responses=SimpleNamespace(create=lambda **kwargs: pytest.fail(
            "responses.create must not be called by default")))

    response = call_send_with_retry(client, [{"role": "user", "content": "hi"}], quiet_agent)

    assert response.choices[0].message.content == "legacy reply"
    assert calls[0]["model"] == "grok-4"
    assert calls[0]["max_tokens"] == 100
    assert calls[0]["stream"] is True
    assert calls[0]["extra_headers"] is None


def test_execute_agent_passes_the_resolved_wire_api(mocker, tmp_path, quiet_agent):
    """``auth.wire_api`` from ``make_client`` reaches the send path."""
    agent_dir = tmp_path / "dreamer"
    agent_dir.mkdir()
    (agent_dir / "SOUL.md").write_text("soul data")
    mocker.patch.object(agent_module, "get_config", return_value={
        "sessions_root": str(tmp_path),
        "models": {"dreamer": {"model": "gpt-5.1-codex", "provider": "github-copilot"}}})
    mock_workflow = mocker.MagicMock()
    mock_workflow.agents_dir = tmp_path
    mocker.patch("sdlc_factory.workflows.get_workflow", return_value=mock_workflow)
    mocker.patch("sdlc_factory.telemetry.setup_telemetry")

    client = FakeClient([text_delta("copilot answer")])
    auth = ResolvedAuth(base_url="https://example.invalid/v1", api_key="k",
                        wire_api="responses")
    mocker.patch.object(agent_module, "make_client",
                        return_value=(client, auth))

    assert agent_module.execute_agent("dreamer", "reflect") == "copilot answer"
    assert len(client.responses_calls) == 1
    assert client.responses_calls[0]["model"] == "gpt-5.1-codex"
    assert "temperature" not in client.responses_calls[0]


# --- routing: chat.py ------------------------------------------------------

def test_chat_session_routes_responses_to_responses_create(mocker, tmp_path):
    (tmp_path / "dreamer-123.session").write_text("[]", encoding="utf-8")
    mocker.patch.object(chat_module, "get_config", return_value={
        "sessions_root": str(tmp_path),
        "models": {"dreamer": {"model": "gpt-5.1-codex", "provider": "github-copilot"}}})
    client = FakeClient([text_delta("chat reply")])
    auth = ResolvedAuth(base_url="https://example.invalid/v1", api_key="k",
                        wire_api="responses")
    mocker.patch.object(chat_module, "make_client", return_value=(client, auth))
    mocker.patch("builtins.input", side_effect=["hello", EOFError])

    chat_module.run_chat_session("dreamer-123")

    assert len(client.responses_calls) == 1
    assert client.responses_calls[0]["model"] == "gpt-5.1-codex"


def test_chat_session_routes_responses_tool_calls_through_the_loop(mocker, tmp_path):
    """A tool call on the Responses path still runs the chat tool loop."""
    (tmp_path / "dreamer-123.session").write_text("[]", encoding="utf-8")
    mocker.patch.object(chat_module, "get_config", return_value={
        "sessions_root": str(tmp_path),
        "models": {"dreamer": {"model": "grok-4", "provider": "github-copilot"}}})
    store_memory = mocker.patch("sdlc_factory.chat.sdlc_store_memory", return_value="saved!")

    turn_one = [
        item_added(0, output_item(id="fc_1", call_id="call_1", name="sdlc_store_memory",
                                  arguments="")),
        args_delta(0, "fc_1", '{"agent": "dreamer"}'),
    ]
    turn_two = [text_delta("saved it")]
    client = FakeClient(turn_one, turn_two)
    auth = ResolvedAuth(base_url="https://example.invalid/v1", api_key="k",
                        wire_api="responses")
    mocker.patch.object(chat_module, "make_client", return_value=(client, auth))
    mocker.patch("builtins.input", side_effect=["save this", EOFError])

    chat_module.run_chat_session("dreamer-123")

    store_memory.assert_called_once_with(agent="dreamer")
    assert len(client.responses.calls) == 2
    replayed = client.responses.calls[1]["input"]
    assert {"type": "function_call_output", "call_id": "call_1",
            "output": "saved!"} in replayed


def test_chat_session_keeps_the_completions_path_by_default(mocker, tmp_path):
    (tmp_path / "coder-123.session").write_text("[]", encoding="utf-8")
    mocker.patch.object(chat_module, "get_config", return_value={
        "sessions_root": str(tmp_path), "models": {"coder": {"model": "local-vllm"}}})
    mock_client = mocker.patch.object(chat_module, "OpenAI", create=True).return_value
    reply = mocker.MagicMock()
    reply.choices = [mocker.MagicMock()]
    reply.choices[0].message.tool_calls = []
    reply.choices[0].message.content = "legacy reply"
    mock_client.chat.completions.create.return_value = reply
    mocker.patch("builtins.input", side_effect=["hello", EOFError])

    chat_module.run_chat_session("coder-123")

    assert mock_client.chat.completions.create.call_count == 1