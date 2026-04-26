import json
from pathlib import Path
import typer
from google import genai
from google.genai import types

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
        history = [types.Content.model_validate(c) for c in raw_data]
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
    vertex_api_key = config_data.get("vertex_api_key")

    if vertex_api_key:
        client = genai.Client(vertexai=True, api_key=vertex_api_key, http_options={'timeout': 300000})
    else:
        api_key = config_data.get("gemini_api_key")
        client = genai.Client(api_key=api_key, http_options={'timeout': 300000}) if api_key else genai.Client(http_options={'timeout': 300000})        

    config = types.GenerateContentConfig(
        system_instruction=system_instruction.strip(),
        temperature=target_temp,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        tools=[sdlc_store_memory]
    )
    
    chat = client.chats.create(model=target_model, config=config, history=history)
    
    typer.secho(f"\n💬 Entering Chat Mode with {agent_name} (Session: {session_id})", fg=typer.colors.MAGENTA, bold=True)
    typer.secho("⚠️  Chat mode - tools disabled (except sdlc_store_memory). The session file will not be updated.", fg=typer.colors.YELLOW)
    typer.secho("Press Ctrl+D or Ctrl+C to exit.\n", fg=typer.colors.CYAN)
    
    while True:
        try:
            user_input = input("🗣️  > ").strip()
            if not user_input:
                continue
            
            response = chat.send_message(user_input)
            
            while response.function_calls:
                tool_results = []
                for call in response.function_calls:
                    if call.name == "sdlc_store_memory":
                        global_logger.info(f"💾 Native CLI Called: sdlc_store_memory", extra={"color": typer.colors.GREEN})
                        try:
                            output = sdlc_store_memory(**call.args)
                            global_logger.info(f"[OUTPUT]:\n{output}\n")
                        except Exception as e:
                            output = f"Error: {e}"
                            global_logger.warning(f"Error executing sdlc_store_memory: {e}")
                    else:
                        output = f"Tool {call.name} is not allowed in chat mode."
                    
                    tool_results.append(types.Part.from_function_response(
                        name=call.name,
                        response={"result": output}
                    ))
                response = chat.send_message(tool_results)
            
            agent_text = ""
            if getattr(response, "candidates", None) and getattr(response.candidates[0], "content", None) and getattr(response.candidates[0].content, "parts", None):
                for part in response.candidates[0].content.parts:
                    if getattr(part, "text", None):
                        agent_text += part.text
                        
            typer.secho(f"\n🤖 {agent_name}:\n{agent_text.strip()}\n", fg=typer.colors.MAGENTA)
            
        except EOFError:
            typer.secho("\n👋 Exiting chat mode.", fg=typer.colors.MAGENTA)
            break
        except KeyboardInterrupt:
            typer.secho("\n👋 Exiting chat mode.", fg=typer.colors.MAGENTA)
            break
        except Exception as e:
            global_logger.error(f"Chat Error: {e}")
