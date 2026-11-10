#!/usr/bin/env python3
"""safe_exec.py - Anti-Stall Subprocess Execution Runner for Hermes & Odin Stack.

Enforces execution invariants:
1. Rejects literal newlines and unclosed quotes that wedge bash PTY in secondary prompt ('>').
2. Rejects interactive sudo commands (fails fast instead of hanging on password prompts).
3. Sets up clean process groups (os.setsid) and bounds execution with hard timeouts.
4. Kills full process tree (SIGTERM -> SIGKILL) on timeout to prevent wedged child processes.
5. Caps output to prevent context token overflow.
"""

from __future__ import annotations

import argparse
import os
import shlex
import signal
import subprocess
import sys
import time
from typing import Tuple


DEFAULT_TIMEOUT = 30
DEFAULT_MAX_OUTPUT = 60000


def validate_command(command: str) -> Tuple[bool, str]:
    """Validates command string for syntax hazards that wedge shells."""
    # Check for literal newlines
    if "\n" in command or "\r" in command:
        return False, "Command contains literal newline characters which wedge bash PTY. Chain commands with '&&' or ';', or write to a script file."

    # Check for trailing backslash
    stripped = command.strip()
    if stripped.endswith("\\"):
        return False, "Command ends with trailing backslash ('\\'), which awaits secondary input. Format on a single continuous line."

    # Check for unbalanced quotes
    try:
        shlex.split(command)
    except ValueError as e:
        return False, f"Command contains unbalanced quotes or syntax error: {e}"

    # Check for sudo
    words = command.strip().split()
    if words and words[0] == "sudo":
        return False, "Interactive 'sudo' commands are strictly prohibited in automated execution (stalls on password prompt)."
    for i, w in enumerate(words[:-1]):
        if w in ("&&", ";", "||", "|") and words[i + 1] == "sudo":
            return False, "Interactive 'sudo' commands are strictly prohibited in automated execution."

    return True, ""


DEFAULT_CWD = "/home/raymund" if os.path.isdir("/home/raymund") else "/opt/data"


def execute_safe_command(
    command: str,
    cwd: str = DEFAULT_CWD,
    timeout: int = DEFAULT_TIMEOUT,
    max_output: int = DEFAULT_MAX_OUTPUT,
) -> int:
    """Executes a command safely with process group isolation and timeout bounds."""
    # Validate command
    valid, reason = validate_command(command)
    if not valid:
        sys.stderr.write(f"ERROR [Command Validation Failed]: {reason}\n")
        return 126

    # Validate Cwd fallback
    target_cwd = cwd if os.path.isdir(cwd) else os.getcwd()
    t0 = time.perf_counter()

    # Launch subprocess in a new process group
    try:
        proc = subprocess.Popen(
            ["bash", "-eo", "pipefail", "-c", command],
            cwd=target_cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            preexec_fn=os.setsid,  # New process group for clean subtree kill
        )
    except Exception as e:
        sys.stderr.write(f"ERROR: Failed to launch process: {e}\n")
        return 1

    try:
        stdout_data, stderr_data = proc.communicate(timeout=timeout)
        elapsed = time.perf_counter() - t0
    except subprocess.TimeoutExpired:
        elapsed = time.perf_counter() - t0
        sys.stderr.write(f"\nERROR: Command timed out after {elapsed:.1f}s (timeout={timeout}s). Terminating process group...\n")
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
            time.sleep(0.5)
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout_data, stderr_data = proc.communicate()
        return 124

    # Output handling
    if stdout_data:
        if len(stdout_data) > max_output:
            half = max_output // 2
            truncated_stdout = stdout_data[:half] + f"\n\n... [TRUNCATED {len(stdout_data) - max_output} CHARACTERS] ...\n\n" + stdout_data[-half:]
            sys.stdout.write(truncated_stdout)
        else:
            sys.stdout.write(stdout_data)
        if not stdout_data.endswith("\n"):
            sys.stdout.write("\n")

    if stderr_data:
        if len(stderr_data) > max_output:
            half = max_output // 2
            truncated_stderr = stderr_data[:half] + f"\n\n... [TRUNCATED {len(stderr_data) - max_output} CHARACTERS] ...\n\n" + stderr_data[-half:]
            sys.stderr.write(truncated_stderr)
        else:
            sys.stderr.write(stderr_data)
        if not stderr_data.endswith("\n"):
            sys.stderr.write("\n")

    return proc.returncode


def main() -> None:
    parser = argparse.ArgumentParser(description="Anti-Stall Subprocess Execution Runner")
    parser.add_argument("command", help="Shell command string to execute")
    parser.add_argument("--cwd", default=DEFAULT_CWD, help=f"Execution working directory (default: {DEFAULT_CWD})")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help=f"Timeout in seconds (default: {DEFAULT_TIMEOUT})")
    parser.add_argument("--max-output", type=int, default=DEFAULT_MAX_OUTPUT, help=f"Maximum output characters (default: {DEFAULT_MAX_OUTPUT})")

    args = parser.parse_args()
    rc = execute_safe_command(
        command=args.command,
        cwd=args.cwd,
        timeout=args.timeout,
        max_output=args.max_output,
    )
    sys.exit(rc)


if __name__ == "__main__":
    main()
