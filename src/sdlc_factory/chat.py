import json
from pathlib import Path
import typer
from openai import OpenAI

from sdlc_factory.utils import get_config, abort, global_logger
from sdlc_factory.tools import sdlc_store_memory

def run_chat_session(session_id: str):
    """Runs a read-only interactive chat with a previous session."""
    config_data = get_config()
    
    sessions_root = config_data.get("sessions_root")
    if not sessions_root:
        abort("ERROR: 'sessions_root' is not defined.")
        
    session_dir = Path(sessions_root)
    session_file = session_dir / f"{session_id}.session"
    
    if not session_file.exists():
        abort(f"Session file not found: {session_file}")
        
    try:
        raw_data = json.loads(session_file.read_text(encoding="utf-8"))
        messages = raw_data
    except Exception as e:
        abort(f"Failed to parse session file: {e}")
        
    # Extract agent name
    parts = session_id.split("-")
    agent_name = parts[0]
    
    system_instruction = "### CHAT MODE OVERRIDE ###\nYou are in a read-only CHAT MODE with the human operator. Your tools have been disabled, EXCEPT for `sdlc_store_memory`. You may use it to save insights. Answer the user's questions based on your history.\n"

    models_config = config_data.get("models", {})
    agent_config = models_config.get(agent_name, {})
    target_model = agent_config.get("model", "gemini-3.1-pro-preview-customtools")
    target_temp = float(agent_config.get("temperature", 0.0))
    import os
    api_key = config_data.get("vertex_api_key") or config_data.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY") or os.environ.get("OPENAI_API_KEY") or "EMPTY"
    base_url = config_data.get("base_url", "http://sagittarius-a.mara-balance.ts.net:8100/v1")
    api_timeout = float(config_data.get("api_timeout", 600.0))
    client = OpenAI(base_url=base_url, api_key=api_key, timeout=api_timeout)

    tools = [
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
        }
    ]

    messages.insert(0, {"role": "system", "content": system_instruction.strip()})
    
    typer.secho(f"\n💬 Entering Chat Mode with {agent_name} (Session: {session_id})", fg=typer.colors.MAGENTA, bold=True)
    typer.secho("⚠️  Chat mode - tools disabled (except sdlc_store_memory). The session file will not be updated.", fg=typer.colors.YELLOW)
    typer.secho("Press Ctrl+D or Ctrl+C to exit.\n", fg=typer.colors.CYAN)
    
    while True:
        try:
            user_input = input("🗣️  > ").strip()
            if not user_input:
                continue
            
            messages.append({"role": "user", "content": user_input})
            
            response = client.chat.completions.create(
                model=target_model,
                messages=messages,
                tools=tools,
                temperature=target_temp
            )
            
            while response.choices[0].message.tool_calls:
                assistant_msg = response.choices[0].message
                messages.append(assistant_msg)
                
                tool_results = []
                for call in assistant_msg.tool_calls:
                    call_name = call.function.name
                    call_args = json.loads(call.function.arguments) if isinstance(call.function.arguments, str) else call.function.arguments
                    if call_name == "sdlc_store_memory":
                        global_logger.info(f"💾 Native CLI Called: sdlc_store_memory", extra={"color": typer.colors.GREEN})
                        try:
                            output = sdlc_store_memory(**call_args)
                            global_logger.info(f"[OUTPUT]:\\n{output}\\n")
                        except Exception as e:
                            output = f"Error: {e}"
                            global_logger.warning(f"Error executing sdlc_store_memory: {e}")
                    else:
                        output = f"Tool {call_name} is not allowed in chat mode."
                    
                    tool_results.append({
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call_name,
                        "content": output
                    })
                
                messages.extend(tool_results)
                response = client.chat.completions.create(
                    model=target_model,
                    messages=messages,
                    tools=tools,
                    temperature=target_temp
                )
            
            assistant_msg = response.choices[0].message
            messages.append(assistant_msg)
            agent_text = assistant_msg.content or ""
                        
            typer.secho(f"\n🤖 {agent_name}:\n{agent_text.strip()}\n", fg=typer.colors.MAGENTA)
            
        except EOFError:
            typer.secho("\n👋 Exiting chat mode.", fg=typer.colors.MAGENTA)
            break
        except KeyboardInterrupt:
            typer.secho("\n👋 Exiting chat mode.", fg=typer.colors.MAGENTA)
            break
        except Exception as e:
            global_logger.error(f"Chat Error: {e}")
