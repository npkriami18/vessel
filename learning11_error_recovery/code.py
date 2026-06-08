#!/usr/bin/env python3
"""
learning11: Error Recovery — three recovery paths + exponential backoff.

Run:  python learning11_error_recovery/code.py
Need: pip install openai python-dotenv pyyaml + Azure config in .env

Changes from learning10 (s10):
  - LLM call wrapped in try/except with three recovery paths
  - Path 1: max_tokens -> escalate 8K->64K (no append on first escalation),
            then continuation prompt (max 3)
  - Path 2: context_length_exceeded -> reactive compact -> retry (once)
  - Path 3: 429/529 -> exponential backoff with jitter (max 10),
            fallback model on consecutive 429/503
  - with_retry wrapper for transient errors
  - RecoveryState tracks escalation / compact / overload / model

ASCII flow:
  messages -> prompt assembly -> compress+load -> [try] LLM [except] -> tools -> loop
                                                    |          |
                                              stop_reason   error type
                                              max_tokens?   context_too_long? -> compact
                                              escalate /    429/503? -> backoff
                                              continue      other? -> log + exit
"""

import json
import os
import random
import subprocess
import time
from pathlib import Path

try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
except ImportError:
    pass

from openai import AzureOpenAI
from dotenv import load_dotenv

load_dotenv(override=True)

api_version      = os.getenv("AZURE_API_VERSION")
endpoint         = os.getenv("AZURE_ENDPOINT")
subscription_key = os.getenv("AZURE_API_KEY")
deployment       = os.getenv("AZURE_DEPLOYMENT")
fallback_deployment = os.getenv("AZURE_FALLBACK_DEPLOYMENT")  # optional fallback

WORKDIR       = Path.cwd()
MEMORY_DIR    = WORKDIR / ".memory"
MEMORY_INDEX  = MEMORY_DIR / "MEMORY.md"

client = AzureOpenAI(
    api_version=api_version,
    azure_endpoint=endpoint,
    api_key=subscription_key,
)
PRIMARY_MODEL  = deployment
FALLBACK_MODEL = fallback_deployment


# ── Constants ──

ESCALATED_MAX_TOKENS = 64000
DEFAULT_MAX_TOKENS   = 8000
MAX_RECOVERY_RETRIES = 3
MAX_RETRIES          = 10
BASE_DELAY_MS        = 500
MAX_CONSECUTIVE_503  = 3
CONTINUATION_PROMPT  = (
    "Output token limit hit. Resume directly — "
    "no apology, no recap. Pick up mid-thought."
)


# ── Prompt Assembly ──

PROMPT_SECTIONS = {
    "identity":  "You are a coding agent. Act, don't explain.",
    "tools":     "Available tools: bash, read_file, write_file.",
    "workspace": f"Working directory: {WORKDIR}",
    "memory":    "Relevant memories are injected below when available.",
}


def assemble_system_prompt(context: dict) -> str:
    sections = [
        PROMPT_SECTIONS["identity"],
        PROMPT_SECTIONS["tools"],
        PROMPT_SECTIONS["workspace"],
    ]
    memories = context.get("memories", "")
    if memories:
        sections.append(f"Relevant memories:\n{memories}")
    return "\n\n".join(sections)


_last_context_key, _last_prompt = None, None


def get_system_prompt(context: dict) -> str:
    global _last_context_key, _last_prompt
    key = json.dumps(context, sort_keys=True, ensure_ascii=False, default=str)
    if key == _last_context_key and _last_prompt:
        print("  \033[90m[cache hit] system prompt unchanged\033[0m")
        return _last_prompt
    _last_context_key = key
    _last_prompt = assemble_system_prompt(context)
    loaded = ["identity", "tools", "workspace"]
    if context.get("memories"):
        loaded.append("memory")
    print(f"  \033[32m[assembled] sections: {', '.join(loaded)}\033[0m")
    return _last_prompt


# ── Tools ──

def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
    try:
        r = subprocess.run(
            command, shell=True, cwd=WORKDIR,
            capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        )
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"


def run_read(path: str, limit: int | None = None) -> str:
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    try:
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read file contents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path":  {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path":    {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
]

TOOL_HANDLERS = {"bash": run_bash, "read_file": run_read, "write_file": run_write}


# ── Error Recovery (learning11 new) ──

class RecoveryState:
    """Track recovery attempts across the loop."""
    def __init__(self):
        self.has_escalated                  = False
        self.recovery_count                 = 0
        self.consecutive_503                = 0
        self.has_attempted_reactive_compact = False
        self.current_model                  = PRIMARY_MODEL


def retry_delay(attempt: int, retry_after: float | None = None) -> float:
    """Exponential backoff with jitter. Retry-After header takes priority."""
    if retry_after:
        return retry_after
    base   = min(BASE_DELAY_MS * (2 ** attempt), 32000) / 1000
    jitter = random.uniform(0, base * 0.25)
    return base + jitter


def _is_rate_limit(e: Exception) -> bool:
    msg  = str(e).lower()
    name = type(e).__name__.lower()
    return "ratelimit" in name or "429" in msg or "rate limit" in msg


def _is_overloaded(e: Exception) -> bool:
    msg  = str(e).lower()
    name = type(e).__name__.lower()
    return (
        "overloaded" in name or "503" in msg
        or "overloaded" in msg or "service unavailable" in msg
    )


def _extract_retry_after(e: Exception) -> float | None:
    """Try to pull Retry-After seconds from the exception if available."""
    try:
        # openai SDK may expose response headers on the exception
        headers = getattr(e, "response", None) and getattr(e.response, "headers", {})
        if headers and "retry-after" in headers:
            return float(headers["retry-after"])
    except Exception:
        pass
    return None


def with_retry(fn, state: RecoveryState):
    """Exponential backoff wrapper for transient 429/503 errors.
    Non-transient errors are re-raised for the outer handler."""
    for attempt in range(MAX_RETRIES):
        try:
            result = fn()
            state.consecutive_503 = 0
            return result
        except Exception as e:
            if _is_rate_limit(e):
                delay = retry_delay(attempt, _extract_retry_after(e))
                print(f"  \033[33m[429 rate limit] retry {attempt+1}/{MAX_RETRIES},"
                      f" wait {delay:.1f}s\033[0m")
                time.sleep(delay)
                continue

            if _is_overloaded(e):
                state.consecutive_503 += 1
                if state.consecutive_503 >= MAX_CONSECUTIVE_503:
                    if FALLBACK_MODEL:
                        state.current_model  = FALLBACK_MODEL
                        state.consecutive_503 = 0
                        print(f"  \033[31m[503 x{MAX_CONSECUTIVE_503}]"
                              f" switching to fallback: {FALLBACK_MODEL}\033[0m")
                    else:
                        state.consecutive_503 = 0
                        print(f"  \033[31m[503 x{MAX_CONSECUTIVE_503}]"
                              f" no AZURE_FALLBACK_DEPLOYMENT configured, continuing retry\033[0m")
                delay = retry_delay(attempt)
                print(f"  \033[33m[503 overloaded] retry {attempt+1}/{MAX_RETRIES},"
                      f" wait {delay:.1f}s\033[0m")
                time.sleep(delay)
                continue

            # Not transient — re-raise for outer try/except
            raise

    raise RuntimeError(f"Max retries ({MAX_RETRIES}) exceeded")


def is_context_too_long(e: Exception) -> bool:
    """Check whether an API error indicates the context/prompt is too long."""
    msg = str(e).lower()
    return (
        "context_length_exceeded" in msg
        or "maximum context length" in msg
        or "prompt_is_too_long" in msg
        or ("prompt" in msg and "long" in msg)
        or "max_context_window" in msg
    )


def _group_messages(messages: list) -> list[list]:
    """Partition into atomic groups: assistant-with-tool_calls + its tool results."""
    groups: list[list] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            group = [msg]
            i += 1
            while i < len(messages) and messages[i].get("role") == "tool":
                group.append(messages[i])
                i += 1
            groups.append(group)
        else:
            groups.append([msg])
            i += 1
    return groups


def reactive_compact(messages: list) -> list:
    """Emergency compact — keeps last 3 whole groups to avoid orphaned tool messages."""
    print("  \033[31m[reactive compact] trimming to last 3 groups\033[0m")
    groups = _group_messages(messages)
    tail   = [m for g in groups[-3:] for m in g]
    return [
        {
            "role":    "user",
            "content": (
                "[Reactive compact] Earlier conversation trimmed. "
                "Continue from where you left off."
            ),
        },
        *tail,
    ]


# ── Context ──

def update_context(context: dict, messages: list) -> dict:
    """Derive context from real state."""
    memories = ""
    if MEMORY_INDEX.exists():
        content = MEMORY_INDEX.read_text().strip()
        if content:
            memories = content
    return {
        "enabled_tools": list(TOOL_HANDLERS.keys()),
        "workspace":     str(WORKDIR),
        "memories":      memories,
    }


# ── Agent Loop ──

def agent_loop(messages: list, context: dict):
    """Main loop with error recovery wrapping LLM calls."""
    system     = get_system_prompt(context)
    state      = RecoveryState()
    max_tokens = DEFAULT_MAX_TOKENS

    while True:
        # Build api_messages with system prepended
        api_messages = (
            messages
            if messages and messages[0].get("role") == "system"
            else [{"role": "system", "content": system}] + messages
        )

        # ── LLM call: with_retry handles 429/503, outer handles rest ──
        try:
            response = with_retry(
                lambda mt=max_tokens, mdl=state.current_model: (
                    client.chat.completions.create(
                        model=mdl,
                        messages=api_messages,
                        tools=TOOLS,
                        max_completion_tokens=mt,
                    )
                ),
                state,
            )
        except Exception as e:
            # Path 2: context too long -> reactive compact (once)
            if is_context_too_long(e):
                if not state.has_attempted_reactive_compact:
                    messages[:] = reactive_compact(messages)
                    state.has_attempted_reactive_compact = True
                    continue
                print("  \033[31m[unrecoverable] still too long after compact\033[0m")
                messages.append({
                    "role":    "assistant",
                    "content": "[Error] Context too large, cannot continue.",
                })
                return

            # Unrecoverable
            name = type(e).__name__
            print(f"  \033[31m[unrecoverable] {name}: {str(e)[:100]}\033[0m")
            messages.append({
                "role":    "assistant",
                "content": f"[Error] {name}: {str(e)[:200]}",
            })
            return

        msg = response.choices[0].message

        # ── Path 1: finish_reason == "length" -> escalate or continue ──
        if response.choices[0].finish_reason == "length":
            if not state.has_escalated:
                # First escalation: discard truncated output, retry with more tokens
                max_tokens        = ESCALATED_MAX_TOKENS
                state.has_escalated = True
                print(f"  \033[33m[length] escalating"
                      f" {DEFAULT_MAX_TOKENS} -> {ESCALATED_MAX_TOKENS}\033[0m")
                continue

            # Still truncated at 64K: save output + send continuation prompt
            messages.append({"role": "assistant", "content": msg.content or ""})
            if state.recovery_count < MAX_RECOVERY_RETRIES:
                messages.append({"role": "user", "content": CONTINUATION_PROMPT})
                state.recovery_count += 1
                print(f"  \033[33m[length] continuation"
                      f" {state.recovery_count}/{MAX_RECOVERY_RETRIES}\033[0m")
                continue
            print("  \033[31m[length] recovery limit reached\033[0m")
            return

        # ── Normal completion ──
        if not msg.tool_calls:
            messages.append({"role": "assistant", "content": msg.content or ""})
            return

        # ── Tool execution ──
        # Append assistant message with tool_calls first
        messages.append({
            "role":       "assistant",
            "content":    msg.content,
            "tool_calls": [
                {
                    "id":       tc.id,
                    "type":     "function",
                    "function": {
                        "name":      tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ],
        })

        # Then append each tool result immediately after
        for tool_call in msg.tool_calls:
            name    = tool_call.function.name
            print(f"\033[36m> {name}\033[0m")
            handler = TOOL_HANDLERS.get(name)
            if not handler:
                output = f"Unknown: {name}"
            else:
                try:
                    args = json.loads(tool_call.function.arguments)
                except Exception as exc:
                    output = f"Error parsing arguments: {exc}"
                else:
                    if name == "bash":
                        print(f"\033[33m$ {args.get('command', '')}\033[0m")
                    output = handler(**args)
                    print(str(output)[:200])

            messages.append({
                "role":         "tool",
                "tool_call_id": tool_call.id,
                "content":      str(output),
            })

        context = update_context(context, messages)
        system  = get_system_prompt(context)


if __name__ == "__main__":
    print("learning11: Error Recovery — three recovery paths + exponential backoff")
    print("Enter a prompt, or q to quit.\n")

    history = []
    context = update_context({}, [])

    while True:
        try:
            query = input("\033[36mlearning11 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break

        if query.strip().lower() in ("q", "exit", ""):
            break

        turn_start = len(history)
        history.append({"role": "user", "content": query})
        agent_loop(history, context)
        context = update_context(context, history)

        for msg in history[turn_start:]:
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content", "")
            if isinstance(content, str) and content:
                print(content)

        print()