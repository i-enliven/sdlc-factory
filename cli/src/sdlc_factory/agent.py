import sys
import signal
import json
import logging
from pathlib import Path
from typing import Optional
import typer
import time
from datetime import datetime
from google import genai
from google.genai import types
import uuid
from openinference.instrumentation import using_session

from sdlc_factory.utils import get_config, global_logger, abort

from sdlc_factory.tools import (
    run_cli_command,
    sdlc_query_state,
    sdlc_context,
    sdlc_advance_state,
    sdlc_search_codebase,
    sdlc_store_memory
)

HUMAN_PAUSE_REQUESTED = False

def interrupt_handler(signum, frame):
    global HUMAN_PAUSE_REQUESTED
    if HUMAN_PAUSE_REQUESTED:
        # Second Ctrl+C -> Hard abort
        abort("\n[ABORT] Second interrupt received. Force quitting...")
    else:
        # First Ctrl+C -> Flag for pause
        HUMAN_PAUSE_REQUESTED = True
        print("\n\n⚠️ [PAUSE REQUESTED] Finishing current tool execution before opening human prompt... (Press Ctrl+C again to hard abort)\n")

def execute_agent(agent_name: str, prompt: str, exclude_files: Optional[list[str]] = None, session_id: Optional[str] = None, is_resume: bool = False):
    """Executes the Antigravity subagent directly using the genai SDK."""
    agent_tracer = logging.getLogger(f"sdlc_factory.agent.{agent_name}")

    config_data = get_config()
    from sdlc_factory.telemetry import setup_telemetry
    setup_telemetry(config_data)
    session_id = session_id or f"{agent_name}-{str(uuid.uuid4())[:6]}"
    agents_root = config_data.get("agents_root")
    if not agents_root:
        abort("ERROR: 'agents_root' is not defined in the configuration (~/.sdlc-factory.json).")
        
    agent_dir = Path(agents_root) / agent_name
    if not agent_dir.exists():
        abort(f"Agent directory missing for {agent_name} in {agent_dir}")

    exclude_files = exclude_files or []
    system_instruction = ""
    for md_file in sorted(agent_dir.glob("*.md")):
        if md_file.name in exclude_files:
            continue
        md_content = md_file.read_text(encoding='utf-8')
        system_instruction += f"\n# {md_file.name}\n````markdown\n{md_content}\n````\n"

    system_instruction = system_instruction.strip()
    if not system_instruction:
        abort(f"No markdown files found for {agent_name} in {agent_dir}")

    cli_timeout = config_data.get("cli_command_timeout", 300)
    
    # Load agent-specific model and temperature configs
    models_config = config_data.get("models", {})
    agent_config = models_config.get(agent_name, {})
    target_model = agent_config.get("model", "gemini-2.5-flash")
    target_temp = float(agent_config.get("temperature", 0.0))
    agent_max_iterations = int(agent_config.get("max_iterations", 25))
    vertex_api_key = config_data.get("vertex_api_key")

    if vertex_api_key:
        client = genai.Client(vertexai=True, api_key=vertex_api_key, http_options={'timeout': 300000})
    else:
        api_key = config_data.get("gemini_api_key")
        client = genai.Client(api_key=api_key, http_options={'timeout': 300000}) if api_key else genai.Client(http_options={'timeout': 300000})        

    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        temperature=target_temp,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        tools=[
            run_cli_command,
            sdlc_query_state,
            sdlc_context,
            sdlc_advance_state,
            sdlc_search_codebase,
            sdlc_store_memory
        ]
    )
    
    session_dir = Path(agents_root) / "sessions"
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

    def save_session():
        try:
            hist = chat.get_history()
            if hist:
                dumped = [c.model_dump(mode="json") for c in hist]
                session_file.write_text(json.dumps(dumped, indent=2), encoding="utf-8")
        except Exception as e:
            global_logger.warning(f"Failed to serialize session history: {e}")
    
    def send_with_retry(payload, max_retries=7, base_delay=5):
        time.sleep(0.5)
        for attempt in range(max_retries):
            try:
                with using_session(session_id):
                    res = chat.send_message(payload)
                    save_session()
                    return res
            except Exception as e:
                error_str = str(e).lower()
                error_name = type(e).__name__.lower()
                if "503" in error_str or "504" in error_str or "429" in error_str or "unavailable" in error_str or "timeout" in error_str or "timeout" in error_name or "deadline_exceeded" in error_str or "cancelled" in error_str:
                    if attempt < max_retries - 1:
                        if HUMAN_PAUSE_REQUESTED:
                            delay = 1
                            global_logger.info("⚠️ API connection interrupted by OS signal. Safely retrying to capture state...", extra={"color": typer.colors.YELLOW})
                        else:
                            delay = base_delay * (2 ** attempt)
                            global_logger.warning(f"⚠️ API Timeout/Overloaded (Attempt {attempt+1}/{max_retries}). Retrying in {delay}s...")
                        time.sleep(delay)
                        continue
                raise e

    def handle_human_pause(tool_results=None):
        global HUMAN_PAUSE_REQUESTED
        signal.signal(signal.SIGINT, signal.default_int_handler)
        try:
            msg = "Agent execution paused." if tool_results else "Agent execution paused before exiting."
            typer.secho(f"\n⏸️  [SYSTEM PAUSE] {msg}", fg=typer.colors.CYAN, bold=True)
            user_msg = input("🤖 (Human Override) > ").strip()
            
            if user_msg:
                if tool_results:
                    last_part = tool_results[-1]
                    orig_result = last_part.function_response.response.get("result", "")
                    override_text = f"### ⚠️ URGENT HUMAN OVERRIDE INSTRUCTION ⚠️\nThe human operator has intercepted this execution and provided the following direct instruction you MUST follow:\n\n{user_msg}\n\n---\nOriginal Tool Output:\n{orig_result}"
                    last_part.function_response.response["result"] = override_text
                    global_logger.info(f"💉 Injected human instruction into '{last_part.function_response.name}' response.", extra={"color": typer.colors.GREEN})
                    return None
                else:
                    global_logger.info("💉 Injected human instruction directly to agent.", extra={"color": typer.colors.GREEN})
                    return user_msg
        except KeyboardInterrupt:
            abort("\n[ABORT] Run cancelled by user during human pause.")
        except EOFError:
            global_logger.info("\n⏩ [CONTINUE] Resuming execution without override...", extra={"color": typer.colors.CYAN})
            return None
        finally:
            HUMAN_PAUSE_REQUESTED = False
            signal.signal(signal.SIGINT, interrupt_handler)
        return None

    agent_tracer.info(f"=== INITIAL PROMPT ===\n{prompt}\n")
    
    # --- BIND CUSTOM SIGNAL HANDLER ---
    global HUMAN_PAUSE_REQUESTED
    HUMAN_PAUSE_REQUESTED = False
    original_sigint = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, interrupt_handler)
    # ----------------------------------

    try:
        if resumed:
            global_logger.info("⏸️  [SESSION RESUMED] Triggering Human Override prompt for instructions...", extra={"color": typer.colors.CYAN})
            user_msg = handle_human_pause()
            if user_msg:
                response = send_with_retry(user_msg)
            else:
                response = send_with_retry("SYSTEM: Session resumed. Please continue.")
        else:
            response = send_with_retry(prompt)
        
        import re
        workdir_match = re.search(r"\*Workdir\*:\s*([^\n]+)", prompt)
        session_cwd = Path(workdir_match.group(1).strip()).resolve() if workdir_match else Path.cwd().resolve()
        
        max_context_iterations = agent_max_iterations
        iteration_count = 0
        last_call_signature = None
        consecutive_call_count = 0
        
        while True:
            iteration_count += 1
            if iteration_count > max_context_iterations:
                global_logger.error(f"🔴 OOM SAFEGUARD: Agent exceeded {max_context_iterations} tool iterations.", extra={"bold": True})
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
                # --- 1. REPETITION TRACKER ---
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
                # -----------------------------

                tool_results = []
                for call in response.function_calls:
                    
                    # --- 2. THE 3X WARNING INTERVENTION ---
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
                    # --------------------------------------

                    # --- TOOL EXECUTION ---
                    if call.name == "run_cli_command":
                        cmd = call.args.get("command", "").strip()
                        req_cwd = call.args.get("cwd", None)
                        
                        if req_cwd:
                            exec_cwd = str(Path(session_cwd).joinpath(req_cwd).resolve())
                        else:
                            exec_cwd = str(session_cwd)
                        
                        agent_tracer.info(f"\n[EXECUTING COMMAND in {exec_cwd}]:\n{cmd}\n")
                        global_logger.info(f"🔧 [{iteration_count:03}/{max_context_iterations:03}] Running CLI: {cmd}", extra={"color": typer.colors.MAGENTA, "truncate_console": 150})
                        if cmd.startswith("cd "):
                            import re
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
                        
                        icons = {"sdlc_query_state": "🔄", "sdlc_context": "🧠", "sdlc_advance_state": "🚀", "sdlc_search_codebase": "🔍", "sdlc_store_memory": "💾"}
                        colors = {"sdlc_query_state": typer.colors.CYAN, "sdlc_context": typer.colors.MAGENTA, "sdlc_advance_state": typer.colors.GREEN, "sdlc_search_codebase": typer.colors.YELLOW, "sdlc_store_memory": typer.colors.BLUE}
                        
                        agent_tracer.info(f"\n[EXECUTING NATIVE COMMAND]:\n{cmd_str}\n")
                        global_logger.info(f"{icons.get(call.name, '⚙️')} Native CLI Called: {cmd_str}", extra={"color": colors.get(call.name, typer.colors.WHITE), "truncate_console": 150})
                        
                        if call.name == "sdlc_query_state":
                            output = sdlc_query_state(**call.args)
                        elif call.name == "sdlc_context":
                            output = sdlc_context(**call.args)
                        elif call.name == "sdlc_advance_state":
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
                            "['run_cli_command', 'sdlc_query_state', 'sdlc_context', "
                            "'sdlc_advance_state', 'sdlc_search_codebase', 'sdlc_store_memory']."
                        )
                        global_logger.warning(f"❌ Hallucination Detected: Unknown tool '{call.name}'")
                    
                    agent_tracer.info(f"[OUTPUT]:\n{output}\n")
                    
                    tool_results.append(types.Part.from_function_response(
                        name=call.name,
                        response={"result": output}
                    ))
                # ---------------------------------------

                # --- NEW HUMAN OVERRIDE INJECTION BLOCK ---
                if HUMAN_PAUSE_REQUESTED:
                    handle_human_pause(tool_results)
                # ------------------------------------------

                response = send_with_retry(tool_results)
            else:
                if HUMAN_PAUSE_REQUESTED:
                    user_msg = handle_human_pause()
                    if user_msg:
                        response = send_with_retry(user_msg)
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

    finally:
        # --- RESTORE ORIGINAL SIGNAL HANDLER ---
        # Ensures the heartbeat loop returns to normal behavior when the agent sleeps
        signal.signal(signal.SIGINT, original_sigint)

