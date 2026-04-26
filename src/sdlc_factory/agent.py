import sys
import signal
import json
import logging
from pathlib import Path
from typing import Optional, Tuple, Any, List
import typer
import time
from datetime import datetime
from google import genai
from google.genai import types
import uuid
import re
import os
from openinference.instrumentation import using_session

from sdlc_factory.utils import get_config, abort, global_logger, get_workspace, format_size

from sdlc_factory.tools import (
    run_cli_command,
    sdlc_advance_state,
    sdlc_search_codebase,
    sdlc_store_memory
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


def _setup_client(config_data: dict, system_instruction: str, target_temp: float) -> genai.Client:
    vertex_api_key = config_data.get("vertex_api_key")
    if vertex_api_key:
        client = genai.Client(vertexai=True, api_key=vertex_api_key, http_options={'timeout': 300000})
    else:
        api_key = config_data.get("gemini_api_key")
        client = genai.Client(api_key=api_key, http_options={'timeout': 300000}) if api_key else genai.Client(http_options={'timeout': 300000})        
    return client

def _get_genai_config(system_instruction: str, target_temp: float) -> types.GenerateContentConfig:
    return types.GenerateContentConfig(
        system_instruction=system_instruction,
        temperature=target_temp,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        tools=[
            run_cli_command,
            sdlc_advance_state,
            sdlc_search_codebase,
            sdlc_store_memory
        ]
    )

def _save_session(chat, session_file: Path):
    try:
        hist = chat.get_history()
        if hist:
            dumped = [c.model_dump(mode="json") for c in hist]
            session_file.write_text(json.dumps(dumped, indent=2), encoding="utf-8")
    except Exception as e:
        global_logger.warning(f"Failed to serialize session history: {e}")

def _send_with_retry(chat, session_id: str, session_file: Path, payload, max_retries=7, base_delay=5):
    time.sleep(0.5)
    for attempt in range(max_retries):
        try:
            with using_session(session_id):
                res = chat.send_message(payload)
                _save_session(chat, session_file)
                return res
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
                orig_result = last_part.function_response.response.get("result", "")
                override_text = f"### ⚠️ URGENT HUMAN OVERRIDE INSTRUCTION ⚠️\nThe human operator has intercepted this execution and provided the following direct instruction you MUST follow:\n\n{user_msg}\n\n---\nOriginal Tool Output:\n{orig_result}"
                last_part.function_response.response["result"] = override_text
                global_logger.info(f"💉 Injected human instruction into '{last_part.function_response.name}' response.", extra={"color": typer.colors.GREEN})
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

def _process_tool_call(call, session_cwd: Path, cli_timeout: int, log_prefix: str, agent_tracer: logging.Logger) -> Tuple[types.Part, Path]:
    if call.name == "run_cli_command":
        cmd = call.args.get("command", "").strip()
        req_cwd = call.args.get("cwd", None)
        
        if req_cwd:
            exec_cwd = str(Path(session_cwd).joinpath(req_cwd).resolve())
        else:
            exec_cwd = str(session_cwd)
        
        agent_tracer.info(f"\n[EXECUTING COMMAND in {exec_cwd}]:\n{cmd}\n")
        prefix = typer.style(f"{log_prefix} Running CLI:", fg=typer.colors.WHITE)
        cmd_colored = typer.style(cmd, fg=typer.colors.GREEN)
        global_logger.info(f"{prefix} {cmd_colored}", extra={"color": None, "truncate_console": 150})
        if cmd.startswith("cd "):
            target = re.split(r'&&|;', cmd)[0][3:].strip().strip("'\"")
            new_cwd = Path(exec_cwd).joinpath(target).resolve()
            
            if new_cwd.exists() and new_cwd.is_dir():
                session_cwd = new_cwd
                if "&&" not in cmd and ";" not in cmd:
                    output = f"Successfully changed directory to {session_cwd}"
                else:
                    output = run_cli_command(cmd, exec_cwd, timeout=cli_timeout)
            else:
                output = f"bash: cd: {target}: No such file or directory"
        else:
            output = run_cli_command(cmd, exec_cwd, timeout=cli_timeout)
            
    elif call.name.startswith("sdlc_"):
        cmd_name = call.name.replace("_", "-").replace("sdlc-", "sdlc-factory ")
        cmd_str = f"{cmd_name} " + " ".join([f"--{k.replace('_', '-')} \"{v}\"" for k,v in call.args.items()])
        
        prefix = typer.style(f"{log_prefix} Native CLI:", fg=typer.colors.WHITE)
        cmd_colored = typer.style(cmd_str, fg=typer.colors.GREEN)
        agent_tracer.info(f"\n[EXECUTING NATIVE COMMAND]:\n{cmd_str}\n")
        global_logger.info(f"{prefix} {cmd_colored}", extra={"color": None, "truncate_console": 150})
        if call.name == "sdlc_advance_state":
            output = sdlc_advance_state(**call.args)
        elif call.name == "sdlc_search_codebase":
            output = sdlc_search_codebase(**call.args)
        elif call.name == "sdlc_store_memory":
            output = sdlc_store_memory(**call.args)
    else:
        output = (
            f"SYSTEM ERROR: Tool '{call.name}' is not available in this context. "
            "Do not attempt to call this tool again. "
            "You must strictly use one of the explicitly provided native tools: "
            "['run_cli_command', 'sdlc_advance_state', 'sdlc_search_codebase', 'sdlc_store_memory']."
        )
        global_logger.warning(f"❌ Hallucination Detected: Unknown tool '{call.name}'")
    
    agent_tracer.info(f"[OUTPUT]:\n{output}\n")
    
    return types.Part.from_function_response(
        name=call.name,
        response={"result": output}
    ), session_cwd

def execute_agent(agent_name: str, prompt: str, exclude_files: Optional[list[str]] = None, session_id: Optional[str] = None, is_resume: bool = False, workflow_name: str = "sdlc"):
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
    target_temp = float(agent_config.get("temperature", 0.0))
    agent_max_iterations = int(agent_config.get("max_iterations", 25))

    client = _setup_client(config_data, system_instruction, target_temp)
    config = _get_genai_config(system_instruction, target_temp)
    
    sessions_root = config_data.get("sessions_root")
    if not sessions_root:
        abort("ERROR: 'sessions_root' is not defined in the configuration.")
    session_dir = Path(sessions_root)
    session_dir.mkdir(parents=True, exist_ok=True)
    session_file = session_dir / f"{session_id}.session"

    if is_resume and not session_file.exists():
        abort(f"Cannot resume: Session file not found at {session_file}")

    history = None
    resumed = False
    if session_file.exists():
        try:
            raw_data = json.loads(session_file.read_text(encoding="utf-8"))
            history = [types.Content.model_validate(c) for c in raw_data]
            resumed = True
            global_logger.info(f"🔄 Resuming session from {session_file.name}", extra={"color": typer.colors.CYAN})
        except Exception as e:
            global_logger.warning(f"Failed to parse session file {session_file}: {e}")

    chat = client.chats.create(model=target_model, config=config, history=history)
    
    global HUMAN_PAUSE_REQUESTED
    HUMAN_PAUSE_REQUESTED = False
    original_sigint = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, interrupt_handler)

    try:
        workdir_match = re.search(r"\*Workdir\*:\s*([^\n]+)", prompt)
        session_cwd = Path(workdir_match.group(1).strip()).resolve() if workdir_match else Path.cwd().resolve()

        if not resumed:
            prompt += _get_tree_prompt(session_cwd)

        agent_tracer.info(f"=== INITIAL PROMPT ===\n{prompt}\n")
        if resumed:
            global_logger.info("⏸️  [SESSION RESUMED] Triggering Human Override prompt for instructions...", extra={"color": typer.colors.CYAN})
            user_msg = _handle_human_pause()
            if user_msg:
                response = _send_with_retry(chat, session_id, session_file, user_msg)
            else:
                response = _send_with_retry(chat, session_id, session_file, "SYSTEM: Session resumed. Please continue.")
        else:
            response = _send_with_retry(chat, session_id, session_file, prompt)
        
        iteration_count = 0
        last_call_signature = None
        consecutive_call_count = 0
        
        while True:
            iteration_count += 1
            if iteration_count > agent_max_iterations:
                global_logger.error(f"🔴 OOM SAFEGUARD: Agent exceeded {agent_max_iterations} tool iterations.", extra={"bold": True})
                abort("Agent hit the token limit without advancing the state. Halting factory daemon.")
                
            agent_text = ""
            if getattr(response, "candidates", None):
                candidate = response.candidates[0]
                if getattr(candidate, "content", None) and getattr(candidate.content, "parts", None):
                    for part in candidate.content.parts:
                        if getattr(part, "text", None):
                            agent_text += part.text

            if agent_text:
                agent_tracer.info(f"\n[AGENT THOUGHTS]\n{agent_text}\n")
            
            if response.function_calls:
                try:
                    hist_size = len(json.dumps([c.model_dump(mode="json") for c in chat.get_history()]).encode("utf-8")) if chat.get_history() else 0
                except Exception as e:
                    global_logger.warning(f"Failed to calculate history size for telemetry: {e}")
                    hist_size = 0
                context_bytes = len(system_instruction.encode("utf-8")) + hist_size
                context_size_str = format_size(context_bytes)
                log_prefix = f"[{iteration_count:03}/{agent_max_iterations:03} - {context_size_str}]"
                
                current_call_signature = json.dumps(
                    [{"name": c.name, "args": dict(c.args or {})} for c in response.function_calls],
                    sort_keys=True
                )
                
                if current_call_signature == last_call_signature:
                    consecutive_call_count += 1
                else:
                    consecutive_call_count = 1
                    last_call_signature = current_call_signature
                    
                if consecutive_call_count >= 4:
                    global_logger.error("🚨 FATAL REPETITION LOOP: 4 identical tool calls detected. Aborting.", extra={"bold": True})
                    abort(f"Agent trapped in repetitive tool loop using: {current_call_signature}")

                tool_results = []
                for i, call in enumerate(response.function_calls):
                    
                    if HUMAN_PAUSE_REQUESTED:
                        override = _handle_human_pause(tool_results)
                        if override:
                            remaining_calls = response.function_calls[i:]
                            for rem_idx, rem_call in enumerate(remaining_calls):
                                dummy_text = "ABORTED BY HUMAN OVERRIDE IN PREVIOUS STEP"
                                if isinstance(override, str) and rem_idx == 0:
                                    dummy_text = f"### ⚠️ URGENT HUMAN OVERRIDE INSTRUCTION ⚠️\nThe human operator has intercepted this execution and provided the following direct instruction you MUST follow:\n\n{override}\n\n(Note: This tool and subsequent parallel tools were aborted)"
                                
                                tool_results.append(types.Part.from_function_response(
                                    name=rem_call.name,
                                    response={"result": dummy_text}
                                ))
                            break
                    
                    if consecutive_call_count == 3:
                        output = (
                            f"SYSTEM INTERVENTION: You have executed the tool '{call.name}' with the exact same arguments 3 times in a row. "
                            "You are stuck in a logical loop. DO NOT repeat this action. "
                            "Evaluate the previous errors and try a completely different approach. "
                            "If you cannot resolve this, DO NOT force a success state. Instead, write an escalation report to 'issues/ISSUE-FATAL.md' "
                            "via the CLI and advance the state to BLOCKED. A 4th attempt of this exact tool call will trigger a hard system abort."
                        )
                        global_logger.warning(f"⚠️ Injecting Repetition Intervention for '{call.name}'", extra={"color": typer.colors.YELLOW})
                        
                        tool_results.append(types.Part.from_function_response(
                            name=call.name,
                            response={"result": output, "status": "blocked"}
                        ))
                        continue

                    part, session_cwd = _process_tool_call(
                        call, session_cwd, cli_timeout, log_prefix, agent_tracer
                    )
                    tool_results.append(part)

                response = _send_with_retry(chat, session_id, session_file, tool_results)
            else:
                if HUMAN_PAUSE_REQUESTED:
                    user_msg = _handle_human_pause()
                    if user_msg:
                        response = _send_with_retry(chat, session_id, session_file, user_msg)
                        continue
                break
                
        final_text = ""
        if getattr(response, "candidates", None):
            candidate = response.candidates[0]
            if getattr(candidate, "content", None) and getattr(candidate.content, "parts", None):
                for part in candidate.content.parts:
                    if getattr(part, "text", None):
                        final_text += part.text
        return final_text.strip() if final_text.strip() else "Agent execution completed successfully via CLI tools."

    except Exception as e:
        global_logger.error(f"🔴 Fatal API/Execution Error: {e}", extra={"bold": True})
        abort(f"Agent execution failed due to unhandled exception: {e}")
        
    finally:
        signal.signal(signal.SIGINT, original_sigint)

