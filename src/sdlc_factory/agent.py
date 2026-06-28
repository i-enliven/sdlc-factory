import sys
import signal
import json
import logging
from pathlib import Path
from typing import Optional, Tuple, Any, List
import typer
import time
from datetime import datetime
from openai import OpenAI
import uuid
import re
import hashlib
import os
from openinference.instrumentation import using_session

from sdlc_factory.utils import get_config, abort, global_logger, get_workspace, format_size

from sdlc_factory.tools import (
    sdlc_advance_state,
    sdlc_search_codebase,
    sdlc_web_search,
    sdlc_store_memory,
    sdlc_query_traces,
    sdlc_execute_sql
)

HUMAN_PAUSE_REQUESTED = False

def interrupt_handler(signum, frame):
    global HUMAN_PAUSE_REQUESTED
    if HUMAN_PAUSE_REQUESTED:
        abort("\n[ABORT] Second interrupt received. Force quitting...")
    else:
        HUMAN_PAUSE_REQUESTED = True
        print("\n\n⚠️ [PAUSE REQUESTED] Finishing current tool execution before opening human prompt... (Press Ctrl+C again to hard abort)\n")


def _build_system_instruction(agent_name: str, agents_root: Path, exclude_files: list[str]) -> str:
    agent_dir = agents_root / agent_name
    if not agent_dir.exists():
        abort(f"Agent directory missing for {agent_name} in {agent_dir}")

    system_instruction = ""
    for md_file in sorted(agent_dir.glob("*.md")):
        if md_file.name in exclude_files:
            continue
        md_content = md_file.read_text(encoding='utf-8')
        system_instruction += f"\n# {md_file.name}\n````markdown\n{md_content}\n````\n"

    system_instruction = system_instruction.strip()
    if not system_instruction:
        abort(f"No markdown files found for {agent_name} in {agent_dir}")
    return system_instruction


def _setup_client(config_data: dict, system_instruction: str, target_temp: float, provider: str = "vllm") -> OpenAI:
    import os
    if provider == "google":
        base_url = "https://generativelanguage.googleapis.com/v1beta/openai/"
        api_key = config_data.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY") or config_data.get("vertex_api_key") or os.environ.get("OPENAI_API_KEY") or "EMPTY"
    else:
        base_url = config_data.get("vllm_base_url", "http://sagittarius-a.mara-balance.ts.net:8100/v1")
        api_key = config_data.get("vertex_api_key") or config_data.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY") or os.environ.get("OPENAI_API_KEY") or "EMPTY"
    api_timeout = float(config_data.get("api_timeout", 600.0))
    return OpenAI(base_url=base_url, api_key=api_key, timeout=api_timeout)

def _get_tools_schema(workflow) -> list[dict]:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "sdlc_advance_state",
                "description": "Invokes 'sdlc-factory advance-state'. Advances the SDLC pipeline phase natively.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string"},
                        "to": {"type": "string"},
                        "regression": {"type": "boolean"}
                    },
                    "required": ["task_id", "to"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "sdlc_search_codebase",
                "description": "Invokes 'sdlc-factory search-codebase'. Finds relevant codebase file embeddings via pgvector.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer"}
                    },
                    "required": ["query"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "sdlc_store_memory",
                "description": "Invokes 'sdlc-factory store-memory'. Stores an explicitly vectorized insight securely into Postgres.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "agent": {"type": "string"},
                        "task_context": {"type": "string"},
                        "resolution": {"type": "string"}
                    },
                    "required": ["agent", "task_context", "resolution"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "sdlc_web_search",
                "description": "Invokes 'sdlc-factory web-search'. Performs a web search using the internal SearxNG instance.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "The search query."},
                        "domain": {"type": "string", "description": "Optional domain to restrict the search to."}
                    },
                    "required": ["query"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "sdlc_query_traces",
                "description": "Safely queries OpenTelemetry telemetry spans from the database. Use this instead of running psycopg2 shell scripts to analyze historical agent executions.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query_type": {"type": "string", "description": "The type of query to run: 'errors' (to find failed traces), 'llm' (to find LLM completions), or 'recent'."},
                        "session_id": {"type": "string", "description": "Optional agent session ID to filter by."},
                        "agent_name": {"type": "string", "description": "MANDATORY target agent name to restrict the search to. You MUST provide this."},
                        "limit": {"type": "integer", "description": "Max number of traces to return (capped at 20)."},
                        "include_prompts": {"type": "boolean", "description": "If true, extracts heavily truncated llm.prompt and llm.output fields."}
                    },
                    "required": ["query_type", "agent_name"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "sdlc_execute_sql",
                "description": "Executes a raw SQL query safely against the telemetry Postgres database. All returned data is automatically truncated to prevent context window overflow. Use this to dynamically explore telemetry metadata, table structures, and complex historical correlations without writing raw psycopg2 python scripts.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "The raw SQL query to execute."},
                        "parameters": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional array of parameters for parameterizing the SQL query."
                        }
                    },
                    "required": ["query"]
                }
            }
        }
    ]
    if hasattr(workflow, "tools") and workflow.tools:
        tools.extend(workflow.tools)
    return tools

def _get_role(m) -> str:
    if isinstance(m, dict):
        return m.get("role", "")
    return getattr(m, "role", "")

def _estimate_tokens(messages: list) -> int:
    try:
        dumped = []
        for m in messages:
            if hasattr(m, "model_dump"):
                dumped.append(m.model_dump(mode="json"))
            else:
                dumped.append(m)
        # 1 token ~= 3 chars is a safer estimate for code / tool calls which are dense
        return len(json.dumps(dumped)) // 3
    except Exception:
        return 0

def _prune_messages(messages: list, max_tokens: int) -> list:
    if max_tokens <= 0 or len(messages) <= 1:
        return messages
        
    system_msgs = [m for m in messages if _get_role(m) == "system"]
    other_msgs = [m for m in messages if _get_role(m) != "system"]
    
    original_tokens = _estimate_tokens(messages)
    
    # Identify the very first user message so we can preserve it
    first_msg = None
    if other_msgs and _get_role(other_msgs[0]) == "user":
        first_msg = other_msgs[0]
        
    while other_msgs and _estimate_tokens(system_msgs + other_msgs) > max_tokens:
        has_first = (other_msgs[0] == first_msg)
        start_idx = 1 if has_first else 0
        
        if start_idx >= len(other_msgs):
            break
            
        slice_end = start_idx + 1
        while slice_end < len(other_msgs):
            if _get_role(other_msgs[slice_end]) == "tool":
                slice_end += 1
            else:
                break
                
        if slice_end >= len(other_msgs):
            other_msgs = [first_msg] if has_first else []
        else:
            other_msgs = ([first_msg] if has_first else []) + other_msgs[slice_end:]
            
    pruned = system_msgs + other_msgs
    if len(pruned) < len(messages):
        new_tokens = _estimate_tokens(pruned)
        global_logger.info(f"✂️ Pruned conversation history from {len(messages)} to {len(pruned)} messages ({original_tokens} -> {new_tokens} estimated tokens) to conserve tokens.", extra={"color": typer.colors.WHITE})
    return pruned

def _save_session(messages: list[dict], session_file: Path):
    try:
        dumped = []
        for m in messages:
            if hasattr(m, "model_dump"):
                dumped.append(m.model_dump(mode="json"))
            else:
                dumped.append(m)
        session_file.write_text(json.dumps(dumped, indent=2), encoding="utf-8")
    except Exception as e:
        global_logger.warning(f"Failed to serialize session history: {e}")

def _send_with_retry(client, messages, tools, target_model, target_temp, target_max_tokens: int, session_id: str, session_file: Path, max_retries=20, base_delay=5, no_stream=False):
    time.sleep(0.5)
    for attempt in range(max_retries):
        try:
            with using_session(session_id):
                stream_kwargs = {"stream": True, "stream_options": {"include_usage": True}} if not no_stream else {"stream": False}
                res = client.chat.completions.create(
                    model=target_model,
                    messages=messages,
                    tools=tools,
                    temperature=target_temp,
                    max_tokens=target_max_tokens,
                    **stream_kwargs
                )
                
                if no_stream:
                    msg = res.choices[0].message
                    assistant_msg_dict = msg.model_dump(exclude_unset=True)
                    if "function_call" in assistant_msg_dict:
                        del assistant_msg_dict["function_call"]
                    messages.append(assistant_msg_dict)
                    _save_session(messages, session_file)
                    return res
                
                full_content = ""
                tool_calls_dict = {}
                active_tool_idx = -1
                
                prev_char_was_newline = True
                for chunk in res:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if delta.content:
                        full_content += delta.content
                        filtered_content = ""
                        for char in delta.content:
                            if char == '\n':
                                if not prev_char_was_newline:
                                    filtered_content += char
                                prev_char_was_newline = True
                            else:
                                filtered_content += char
                                prev_char_was_newline = False
                        if filtered_content:
                            typer.secho(filtered_content, nl=False, fg=typer.colors.CYAN)
                    if delta.tool_calls:
                        delta_dict = chunk.model_dump(exclude_unset=True).get("choices", [{}])[0].get("delta", {})
                        tc_dicts = delta_dict.get("tool_calls", [])
                        for tc, tc_dict in zip(delta.tool_calls, tc_dicts):
                            if tc_dict.get("id"):
                                active_tool_idx += 1
                            if active_tool_idx == -1:
                                active_tool_idx = 0
                            
                            idx = active_tool_idx
                            
                            if idx not in tool_calls_dict:
                                import copy
                                tool_calls_dict[idx] = copy.deepcopy(tc_dict)
                            else:
                                if tc_dict.get("id"):
                                    tool_calls_dict[idx]["id"] += tc_dict["id"]
                                if tc_dict.get("function", {}).get("name"):
                                    if "name" not in tool_calls_dict[idx]["function"]:
                                        tool_calls_dict[idx]["function"]["name"] = ""
                                    tool_calls_dict[idx]["function"]["name"] += tc_dict["function"]["name"]
                                if tc_dict.get("function", {}).get("arguments"):
                                    if "arguments" not in tool_calls_dict[idx]["function"]:
                                        tool_calls_dict[idx]["function"]["arguments"] = ""
                                    tool_calls_dict[idx]["function"]["arguments"] += tc_dict["function"]["arguments"]
                                for k, v in tc_dict.items():
                                    if k not in ["index", "id", "function", "type"]:
                                        tool_calls_dict[idx][k] = v

                if full_content and not prev_char_was_newline:
                    typer.secho("")
                
                from types import SimpleNamespace
                final_tool_calls = []
                for idx in sorted(tool_calls_dict.keys()):
                    tc = tool_calls_dict[idx]
                    func = SimpleNamespace(name=tc["function"]["name"], arguments=tc["function"]["arguments"])
                    final_tool_calls.append(SimpleNamespace(id=tc["id"], type="function", function=func))
                
                assistant_msg = SimpleNamespace(role="assistant", content=full_content, tool_calls=final_tool_calls if final_tool_calls else None)
                
                assistant_msg_dict = {
                    "role": "assistant",
                    "content": full_content,
                }
                if final_tool_calls:
                    assistant_msg_dict["tool_calls"] = []
                    for idx in sorted(tool_calls_dict.keys()):
                        tc_dict = dict(tool_calls_dict[idx])
                        if "index" in tc_dict:
                            del tc_dict["index"]
                        assistant_msg_dict["tool_calls"].append(tc_dict)
                    
                messages.append(assistant_msg_dict)
                _save_session(messages, session_file)
                
                return SimpleNamespace(choices=[SimpleNamespace(message=assistant_msg)])
        except Exception as e:
            error_str = str(e).lower()
            error_name = type(e).__name__.lower()
            
            if "400" in error_str or "invalid argument" in error_str:
                raise e
                
            if attempt < max_retries - 1:
                if "503" in error_str or "504" in error_str or "429" in error_str or "unavailable" in error_str or "timeout" in error_str or "timeout" in error_name or "deadline_exceeded" in error_str or "cancelled" in error_str:
                    if HUMAN_PAUSE_REQUESTED:
                        delay = 1
                        global_logger.info("⚠️ API connection interrupted by OS signal. Safely retrying to capture state...", extra={"color": typer.colors.YELLOW})
                    else:
                        delay = base_delay * (2 ** attempt)
                        global_logger.warning(f"⚠️ API Interruption ({type(e).__name__}): {e}. Retrying in {delay}s (Attempt {attempt+1}/{max_retries})...")
                else:
                    delay = base_delay * (2 ** attempt)
                    global_logger.warning(f"⚠️ Unexpected API Error: {error_name} - {e}. Retrying in {delay}s (Attempt {attempt+1}/{max_retries})...")
                
                time.sleep(delay)
                continue
            raise e

def _handle_human_pause(tool_results=None) -> Any:
    global HUMAN_PAUSE_REQUESTED
    signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        msg = "Agent execution paused." if tool_results is not None else "Agent execution paused before exiting."
        typer.secho(f"\n⏸️  [SYSTEM PAUSE] {msg}", fg=typer.colors.YELLOW, bold=True)
        user_msg = input("🤖 (Human Override) > ").strip()
        
        if user_msg:
            if tool_results:
                last_part = tool_results[-1]
                orig_result = last_part.get("content", "")
                override_text = f"### ⚠️ URGENT HUMAN OVERRIDE INSTRUCTION ⚠️\\nThe human operator has intercepted this execution and provided the following direct instruction you MUST follow:\\n\\n{user_msg}\\n\\n---\\nOriginal Tool Output:\\n{orig_result}"
                last_part["content"] = override_text
                global_logger.info(f"💉 Injected human instruction into '{last_part.get('name', 'tool')}' response.", extra={"color": typer.colors.GREEN})
                return True
            else:
                global_logger.info("💉 Injected human instruction directly to agent.", extra={"color": typer.colors.GREEN})
                return user_msg
    except KeyboardInterrupt:
        abort("\n[ABORT] Run cancelled by user during human pause.")
    except EOFError:
        global_logger.info("\n⏩ [CONTINUE] Resuming execution without override...", extra={"color": typer.colors.CYAN})
        return False if tool_results is not None else None
    finally:
        HUMAN_PAUSE_REQUESTED = False
        signal.signal(signal.SIGINT, interrupt_handler)
    return False if tool_results is not None else None

def _get_tree_prompt(session_cwd: Path) -> str:
    prompt_addition = ""
    try:
        tree_entries = []
        for root, dirs, files in os.walk(session_cwd):
            if '.git' in dirs:
                dirs.remove('.git')
            depth = len(Path(root).relative_to(session_cwd).parts)
            if depth > 1:
                dirs.clear()
                continue
            
            rel_root = Path(root).relative_to(session_cwd)
            root_str = "." if str(rel_root) == "." else f"./{rel_root}"
            
            try:
                dir_mtime = datetime.fromtimestamp(Path(root).stat().st_mtime).strftime('%Y-%m-%d %H:%M:%S')
            except Exception:
                dir_mtime = "Unknown"
            
            dir_size = "<DIR>"
            dir_root_str = f"{root_str}/" if root_str != "." else "./"
            dir_entry = (dir_root_str, dir_mtime, dir_size)
            if not any(entry[0] == dir_root_str for entry in tree_entries):
                tree_entries.append(dir_entry)
                
            for f in files:
                f_path = Path(root) / f
                try:
                    stat_info = f_path.stat()
                    f_mtime = datetime.fromtimestamp(stat_info.st_mtime).strftime('%Y-%m-%d %H:%M:%S')
                    f_size = format_size(stat_info.st_size)
                except Exception:
                    f_mtime = "Unknown"
                    f_size = "Unknown"
                tree_entries.append((f"{root_str}/{f}", f_mtime, f_size))
        
        tree_entries.sort(key=lambda x: x[0])
        header = f"{'PATH':<50} {'SIZE':<12} LAST MODIFIED\n" + "-" * 85
        ls_output = header + "\n" + "\n".join([f"{path:<50} {size:<12} [{mtime}]" for path, mtime, size in tree_entries])
        prompt_addition += f"\n\n## WORKSPACE DIRECTORY TREE\n```text\n{ls_output.strip()}\n```\n"
    except Exception as e:
        prompt_addition += f"\n\n## WORKSPACE DIRECTORY TREE\nError fetching tree: {e}\n\n"
    return prompt_addition
def _process_tool_call(call, session_cwd: Path, cli_timeout: int, log_prefix: str, agent_tracer: logging.Logger, workflow) -> Tuple[dict, Path]:
    call_name = call.function.name
    
    call_args_list = []
    if isinstance(call.function.arguments, str):
        import json
        decoder = json.JSONDecoder()
        s = call.function.arguments
        idx = 0
        while idx < len(s):
            s_sub = s[idx:].strip()
            if not s_sub:
                break
            try:
                obj, parsed_len = decoder.raw_decode(s_sub)
                call_args_list.append(obj)
                stripped_len = len(s[idx:]) - len(s_sub)
                idx += stripped_len + parsed_len
            except json.JSONDecodeError as e:
                global_logger.warning(f"Failed to fully decode JSON args: {e}")
                if not call_args_list:
                    call_args_list = [json.loads(s)]
                break
    else:
        call_args_list = [call.function.arguments]

    combined_output = ""
    for i, call_args in enumerate(call_args_list):
        if i > 0:
            combined_output += "\n---\n"
            
        for k, v in list(call_args.items()):
            if isinstance(v, str):
                call_args[k] = v.strip().strip('"\'').strip()

        import copy
        sub_call = copy.copy(call)
        sub_call.function.arguments = call_args
        
        if call_name.startswith("sdlc_"):
            cmd_name = call_name.replace("_", "-").replace("sdlc-", "sdlc-factory ")
            cmd_str = f"{cmd_name} " + " ".join([f"--{k.replace('_', '-')} \"{v}\"" for k,v in call_args.items()])
            
            prefix = f"{log_prefix} " + typer.style("Native CLI:", fg=typer.colors.WHITE)
            cmd_colored = typer.style(cmd_str, fg=typer.colors.GREEN)
            agent_tracer.info(f"\\n[EXECUTING NATIVE COMMAND]:\\n{cmd_str}\\n")
            global_logger.info(f"{prefix} {cmd_colored}", extra={"color": None, "truncate_console": 240})
            try:
                if call_name == "sdlc_advance_state":
                    output = sdlc_advance_state(**call_args)
                elif call_name == "sdlc_search_codebase":
                    output = sdlc_search_codebase(**call_args)
                elif call_name == "sdlc_store_memory":
                    output = sdlc_store_memory(**call_args)
                elif call_name == "sdlc_web_search":
                    output = sdlc_web_search(**call_args)
                elif call_name == "sdlc_query_traces":
                    output = sdlc_query_traces(**call_args)
                elif call_name == "sdlc_execute_sql":
                    output = sdlc_execute_sql(**call_args)
                else:
                    output = f"SYSTEM ERROR: Unhandled SDLC tool '{call_name}'"
                    global_logger.warning(f"❌ Unhandled SDLC tool '{call_name}'")
            except Exception as e:
                output = f"SYSTEM ERROR: Failed to execute tool '{call_name}'. Invalid arguments or internal error: {e}\nPlease correct your arguments and try again."
                global_logger.warning(f"⚠️ Tool execution failed: {e}")
                
            agent_tracer.info(f"[OUTPUT]:\\n{output}\\n")
            combined_output += output
            
        elif hasattr(workflow, "process_tool_call"):
            result = workflow.process_tool_call(sub_call, session_cwd, cli_timeout, log_prefix, agent_tracer)
            if isinstance(result, tuple) and len(result) == 2:
                part = result[0]
                session_cwd = result[1]
            else:
                part = result
                
            if isinstance(part, dict):
                output = part.get("content", "")
            else:
                output = str(part)
                
            combined_output += output
            
        else:
            output = (
                f"SYSTEM ERROR: Tool '{call_name}' is not available in this context. "
                "Do not attempt to call this tool again. "
                "You must strictly use one of the explicitly provided native tools: "
                "['run_cli_command', 'sdlc_advance_state', 'sdlc_search_codebase', 'sdlc_store_memory']."
            )
            global_logger.warning(f"❌ Hallucination Detected: Unknown tool '{call_name}'")
            agent_tracer.info(f"[OUTPUT]:\\n{output}\\n")
            combined_output += output
            
    return {
        "role": "tool",
        "tool_call_id": call.id,
        "name": call_name,
        "content": combined_output
    }, session_cwd

def execute_agent(agent_name: str, prompt: str, exclude_files: Optional[list[str]] = None, session_id: Optional[str] = None, is_resume: bool = False, workflow_name: str = "sdlc", no_stream: bool = False):
    """Executes the subagent directly using the genai SDK."""
    agent_tracer = logging.getLogger(f"sdlc_factory.agent.{agent_name}")

    config_data = get_config()
    from sdlc_factory.telemetry import setup_telemetry
    setup_telemetry(config_data)
    
    session_id = session_id or f"{agent_name}-{str(uuid.uuid4())[:6]}"
    
    from sdlc_factory.workflows import get_workflow
    workflow = get_workflow(workflow_name)
    agents_root = workflow.agents_dir
    
    if not agents_root.exists():
        abort(f"ERROR: Agents directory not found at {agents_root}")

    exclude_files = exclude_files or []
    system_instruction = _build_system_instruction(agent_name, Path(agents_root), exclude_files)

    cli_timeout = config_data.get("cli_command_timeout", 300)
    
    models_config = config_data.get("models", {})
    agent_config = models_config.get(agent_name, {})
    target_model = agent_config.get("model", "gemini-3.1-pro-preview-customtools")
    provider = agent_config.get("provider", "vllm")
    target_temp = float(agent_config.get("temperature", 0.0))
    agent_max_iterations = int(agent_config.get("max_iterations", 25))
    target_max_tokens = int(agent_config.get("generation_max_tokens", config_data.get("generation_max_tokens", 24000)))
    max_model_len = int(agent_config.get("max_model_len", config_data.get("max_model_len", 65536)))
    prune_token_limit = max_model_len - target_max_tokens - 1000
    if prune_token_limit < 4000:
        prune_token_limit = 4000

    client = _setup_client(config_data, system_instruction, target_temp, provider=provider)
    tools_schema = _get_tools_schema(workflow)
    
    sessions_root = config_data.get("sessions_root")
    if not sessions_root:
        abort("ERROR: 'sessions_root' is not defined in the configuration.")
    session_dir = Path(sessions_root)
    session_dir.mkdir(parents=True, exist_ok=True)
    session_file = session_dir / f"{session_id}.session"

    if is_resume and not session_file.exists():
        abort(f"Cannot resume: Session file not found at {session_file}")

    messages = [{"role": "system", "content": system_instruction}]
    resumed = False
    if session_file.exists():
        try:
            raw_data = json.loads(session_file.read_text(encoding="utf-8"))
            messages = raw_data
            resumed = True
            if messages and messages[0].get("role") == "system":
                messages[0]["content"] = system_instruction
            global_logger.info(f"🔄 Resuming session from {session_file.name}", extra={"color": typer.colors.CYAN})
        except Exception as e:
            global_logger.warning(f"Failed to parse session file {session_file}: {e}")
    
    global HUMAN_PAUSE_REQUESTED
    HUMAN_PAUSE_REQUESTED = False
    original_sigint = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, interrupt_handler)

    try:
        workdir_match = re.search(r"\*Workdir\*:\s*([^\n]+)", prompt)
        session_cwd = Path(workdir_match.group(1).strip()).resolve() if workdir_match else Path.cwd().resolve()

        if not resumed and agent_name != "dreamer":
            prompt += _get_tree_prompt(session_cwd)

        agent_tracer.info(f"=== INITIAL PROMPT ===\n{prompt}\n")
        if resumed:
            global_logger.info("⏸️  [SESSION RESUMED] Triggering Human Override prompt for instructions...", extra={"color": typer.colors.CYAN})
            user_msg = _handle_human_pause()
            if user_msg:
                messages.append({"role": "user", "content": user_msg})
                messages = _prune_messages(messages, prune_token_limit)
                response = _send_with_retry(client, messages, tools_schema, target_model, target_temp, target_max_tokens, session_id, session_file, no_stream=no_stream)
            else:
                messages.append({"role": "user", "content": "SYSTEM: Session resumed. Please continue."})
                messages = _prune_messages(messages, prune_token_limit)
                response = _send_with_retry(client, messages, tools_schema, target_model, target_temp, target_max_tokens, session_id, session_file, no_stream=no_stream)
        else:
            messages.append({"role": "user", "content": prompt})
            messages = _prune_messages(messages, prune_token_limit)
            response = _send_with_retry(client, messages, tools_schema, target_model, target_temp, target_max_tokens, session_id, session_file, no_stream=no_stream)
        
        iteration_count = 0
        tool_execution_count = 0
        historical_signatures = []
        empty_response_count = 0
        
        while True:
            iteration_count += 1
            if iteration_count > agent_max_iterations:
                global_logger.error(f"🔴 OOM SAFEGUARD: Agent exceeded {agent_max_iterations} tool iterations.", extra={"bold": True})
                abort("Agent hit the token limit without advancing the state. Halting factory daemon.")
                
            agent_text = ""
            if response and response.choices and response.choices[0].message.content:
                agent_text = response.choices[0].message.content

            if agent_text and not no_stream:
                agent_tracer.info(f"\n[AGENT THOUGHTS]\n{agent_text}\n")
            
            if response and response.choices and response.choices[0].message.tool_calls:
                empty_response_count = 0
                context_tokens = _estimate_tokens(messages)
                if context_tokens == 0:
                    context_tokens = len(system_instruction) // 4
                context_size_str = f"{context_tokens}tk"
                if context_tokens > max_model_len:
                    global_logger.error(f"🚨 OOM SAFEGUARD: Context tokens exceeded {max_model_len} ({context_size_str}). Aborting.", extra={"bold": True})
                    abort("Agent token limits exceeded the safety threshold. Halting factory daemon.")
                    
                if context_tokens > prune_token_limit:
                    context_size_str = typer.style(context_size_str, fg=typer.colors.RED, bold=True)
                    
                current_call_signature = json.dumps(
                    [{"name": c.function.name, "args": c.function.arguments} for c in response.choices[0].message.tool_calls],
                    sort_keys=True
                )
                
                md5_hash = hashlib.md5(current_call_signature.encode("utf-8")).hexdigest()
                historical_signatures.append(md5_hash)
                
                loop_detected = False
                warning_loop_detected = False
                for w in range(1, 8):
                    if len(historical_signatures) >= w * 4:
                        if historical_signatures[-w:] == historical_signatures[-2*w:-w] == historical_signatures[-3*w:-2*w] == historical_signatures[-4*w:-3*w]:
                            loop_detected = True
                            break
                    if len(historical_signatures) >= w * 3:
                        if historical_signatures[-w:] == historical_signatures[-2*w:-w] == historical_signatures[-3*w:-2*w]:
                            warning_loop_detected = True
                            
                if loop_detected:
                    global_logger.error(f"🚨 FATAL REPETITION LOOP: Cyclical tool repetition detected (Pattern size {w}). Aborting.", extra={"bold": True})
                    abort(f"Agent trapped in repetitive tool loop using pattern size {w}: {current_call_signature}")

                tool_results = []
                for i, call in enumerate(response.choices[0].message.tool_calls):
                    tool_execution_count += 1
                    log_prefix = f"[{tool_execution_count:03}/{agent_max_iterations:03} - {context_size_str}]"
                    
                    if HUMAN_PAUSE_REQUESTED:
                        override = _handle_human_pause(tool_results)
                        if override:
                            remaining_calls = response.choices[0].message.tool_calls[i:]
                            for rem_idx, rem_call in enumerate(remaining_calls):
                                dummy_text = "ABORTED BY HUMAN OVERRIDE IN PREVIOUS STEP"
                                if isinstance(override, str) and rem_idx == 0:
                                    dummy_text = f"### ⚠️ URGENT HUMAN OVERRIDE INSTRUCTION ⚠️\nThe human operator has intercepted this execution and provided the following direct instruction you MUST follow:\n\n{override}\n\n(Note: This tool and subsequent parallel tools were aborted)"
                                
                                tool_results.append({
                                    "role": "tool",
                                    "tool_call_id": rem_call.id,
                                    "name": rem_call.function.name,
                                    "content": dummy_text
                                })
                            break
                    
                    if warning_loop_detected:
                        output = (
                            f"SYSTEM INTERVENTION: You are executing a cyclical sequence of tools. "
                            "You are stuck in a logical loop. DO NOT repeat this action sequence. "
                            "Evaluate the previous errors and try a completely different approach. "
                            "If you cannot resolve this, DO NOT force a success state. Instead, write an escalation report to 'issues/ISSUE-FATAL.md' "
                            "via the CLI and advance the state to BLOCKED. A 4th attempt of this exact sequence will trigger a hard system abort."
                        )
                        global_logger.warning(f"⚠️ Injecting Repetition Intervention for '{call.function.name}'", extra={"color": typer.colors.YELLOW})
                        
                        tool_results.append({
                            "role": "tool",
                            "tool_call_id": call.id,
                            "name": call.function.name,
                            "content": output
                        })
                        continue

                    part, session_cwd = _process_tool_call(
                        call, session_cwd, cli_timeout, log_prefix, agent_tracer, workflow
                    )
                    tool_results.append(part)

                messages.extend(tool_results)
                messages = _prune_messages(messages, prune_token_limit)
                should_yield = False
                for part in tool_results:
                    if part.get("name") == "sdlc_advance_state":
                        try:
                            res_data = json.loads(part.get("content", "{}"))
                            if res_data.get("status") == "success":
                                should_yield = True
                                break
                        except Exception:
                            pass
                
                # if should_yield:
                #     global_logger.info("🛑 State successfully advanced. Forcing agent yield to prevent hallucinatory continuation.", extra={"color": typer.colors.MAGENTA})
                #     break

                response = _send_with_retry(client, messages, tools_schema, target_model, target_temp, target_max_tokens, session_id, session_file, no_stream=no_stream)
            else:
                agent_content = ""
                if response and response.choices and getattr(response.choices[0].message, "content", None):
                    agent_content = response.choices[0].message.content
                if not agent_content.strip() and not (response and response.choices and getattr(response.choices[0].message, "tool_calls", None)):
                    empty_response_count += 1
                    if empty_response_count <= 3 and not no_stream:
                        global_logger.warning(f"⚠️ Empty response detected from LLM (possible EOS bug). Prompting to continue... (Attempt {empty_response_count}/3)", extra={"color": typer.colors.YELLOW})
                        warning_msg = f"SYSTEM: You generated an empty response without making any tool calls. If you are stuck, please explain why. Otherwise, please continue executing tools to complete the task. (Attempt {empty_response_count} of 3)"
                        if empty_response_count > 1 and messages and messages[-1].get("role") == "user" and "SYSTEM: You generated an empty response" in messages[-1].get("content", ""):
                            messages[-1]["content"] = warning_msg
                        else:
                            messages.append({"role": "user", "content": warning_msg})
                        messages = _prune_messages(messages, prune_token_limit)
                        response = _send_with_retry(client, messages, tools_schema, target_model, target_temp, target_max_tokens, session_id, session_file, no_stream=no_stream)
                        continue

                if HUMAN_PAUSE_REQUESTED:
                    user_msg = _handle_human_pause()
                    if user_msg:
                        messages.append({"role": "user", "content": user_msg})
                        messages = _prune_messages(messages, prune_token_limit)
                        response = _send_with_retry(client, messages, tools_schema, target_model, target_temp, target_max_tokens, session_id, session_file, no_stream=no_stream)
                        continue
                break
                
        final_text = ""
        if response and response.choices and response.choices[0].message.content:
            final_text = response.choices[0].message.content
        return final_text.strip() if final_text.strip() else "Agent execution completed successfully via CLI tools."

    except Exception as e:
        global_logger.error(f"🔴 Fatal API/Execution Error: {e}", extra={"bold": True})
        abort(f"Agent execution failed due to unhandled exception: {e}")
        
    finally:
        signal.signal(signal.SIGINT, original_sigint)

