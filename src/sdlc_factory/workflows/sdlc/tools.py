import subprocess
import tempfile
import os
from typing import Optional

def run_cli_command(command: str, cwd: str = ".", timeout: Optional[int] = None) -> str:
    """Executes a terminal command and returns the stdout and stderr output. 
    Use this to run codebase searches, tools, and file manipulations.
    Args:
        command: The bash command to execute.
        cwd: The working directory to execute the command in.
        timeout: Optional timeout in seconds to prevent hanging.
    """
    out_file = tempfile.NamedTemporaryFile(mode='w+', delete=False)
    out_path = out_file.name
    
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            shell=True,
            stdout=out_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL
        )
        
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
            
            out_file.close()
            with open(out_path, 'r', errors='ignore') as f:
                out = f.read()
            if len(out) > 25000:
                out = out[:25000] + f"\n\n...[OUTPUT TRUNCATED: Original size {len(out)} chars exceeds 25K character safety limit]..."
            return f"Command execution timed out after {timeout} seconds.\nOutput before timeout:\n{out}"
            
        out_file.close()
        with open(out_path, 'r', errors='ignore') as f:
            output = f.read()
            
        if len(output) > 25000:
            output = output[:25000] + f"\n\n...[OUTPUT TRUNCATED: Original size {len(output)} chars exceeds 25K character safety limit]..."
            
        return output if output else "Command executed successfully with no output."
    except Exception as e:
        return f"Error executing command: {e}"
    finally:
        try:
            if not out_file.closed:
                out_file.close()
            os.remove(out_path)
        except Exception:
            pass
