#!/usr/bin/env python3
"""
learning08: Context Compact — compact old conversation state before the prompt overflows.

Run: python learning08_context_compact/code.py
Needs: pip install openai python-dotenv pyyaml + Azure config in .env
"""

import ast
import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import yaml
from dotenv import load_dotenv
from openai import AzureOpenAI

load_dotenv(override=True)

api_version = os.getenv("AZURE_API_VERSION")
endpoint = os.getenv("AZURE_ENDPOINT")
subscription_key = os.getenv("AZURE_API_KEY")
deployment = os.getenv("AZURE_DEPLOYMENT")

WORKDIR = Path.cwd()
SKILLS_DIR = WORKDIR / "skills"
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
TOOL_RESULTS_DIR = WORKDIR / ".task_outputs" / "tool-results"
client = AzureOpenAI(
    api_version=api_version,
    azure_endpoint=endpoint,
    api_key=subscription_key,
)
MODEL = deployment
CURRENT_TODOS: list[dict] = []

CONTEXT_LIMIT = 50_000
KEEP_RECENT_TOOL_RESULTS = 3
PERSIST_THRESHOLD = 30_000
TOOL_RESULT_BUDGET = 200_000
MAX_REACTIVE_RETRIES = 1


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        meta = {}
    return meta, parts[2].strip()


SKILL_REGISTRY: dict[str, dict] = {}


def _scan_skills():
    if not SKILLS_DIR.exists():
        return
    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue
        manifest = skill_dir / "SKILL.md"
        if manifest.exists():
            raw = manifest.read_text(encoding="utf-8")
            meta, _ = _parse_frontmatter(raw)
            name = meta.get("name", skill_dir.name)
            desc = meta.get("description", raw.split("\n")[0].lstrip("#").strip())
            SKILL_REGISTRY[name] = {"name": name, "description": desc, "content": raw}


_scan_skills()


def list_skills() -> str:
    if not SKILL_REGISTRY:
        return "(no skills found)"
    return "\n".join(f"- **{s['name']}**: {s['description']}" for s in SKILL_REGISTRY.values())


def load_skill(name: str) -> str:
    skill = SKILL_REGISTRY.get(name)
    if not skill:
        return f"Skill not found: {name}"
    return skill["content"]


def build_system() -> str:
    catalog = list_skills()
    return (
        f"You are a coding agent at {WORKDIR}. Use tools to solve tasks. "
        "Act, don't explain. Destructive operations require user approval. "
        "Before starting any multi-step task, use todo_write to plan your steps. "
        "Update status as you go. For complex sub-problems, use the task tool.\n"
        f"Skills available:\n{catalog}\n"
        "Use load_skill to get full details when needed. "
        "If the conversation is getting too large, you may call compact."
    )


SYSTEM = build_system()
SUB_SYSTEM = (
    f"You are a coding agent at {WORKDIR}. Use tools to solve the delegated task. "
    "Act, don't explain. Destructive operations require user approval. "
    "Complete the task you were given and return a concise conclusion. "
    "Do not delegate further."
)


def openai_tools(tool_defs: list[dict]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["input_schema"],
            },
        }
        for tool in tool_defs
    ]


def make_tool_block(tool_call) -> SimpleNamespace:
    return SimpleNamespace(
        id=tool_call.id,
        name=tool_call.function.name,
        input=json.loads(tool_call.function.arguments or "{}"),
    )


def extract_text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    chunks = []
    for block in content:
        if isinstance(block, dict) and block.get("type") in ("text", "output_text") and isinstance(block.get("text"), str):
            chunks.append(block["text"])
        elif getattr(block, "type", None) in ("text", "output_text"):
            text = getattr(block, "text", None)
            if isinstance(text, str):
                chunks.append(text)
    return "\n".join(chunks)


def run_bash(command: str) -> str:
    try:
        result = subprocess.run(command, shell=True, cwd=WORKDIR, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        output = (result.stdout + result.stderr).strip()
        return output[:50000] if output else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"


def safe_path(path_str: str) -> Path:
    path = (WORKDIR / path_str).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {path_str}")
    return path


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


def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        file_path = safe_path(path)
        text = file_path.read_text()
        if old_text not in text:
            return f"Error: text not found in {path}"
        file_path.write_text(text.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


def run_glob(pattern: str) -> str:
    import glob as g
    try:
        results = []
        for match in g.glob(pattern, root_dir=WORKDIR):
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR):
                results.append(match)
        return "\n".join(results) if results else "(no matches)"
    except Exception as e:
        return f"Error: {e}"


def _normalize_todos(todos):
    if isinstance(todos, str):
        try:
            todos = json.loads(todos)
        except json.JSONDecodeError:
            try:
                todos = ast.literal_eval(todos)
            except (SyntaxError, ValueError):
                return None, "Error: todos must be a list or JSON array string"
    if not isinstance(todos, list):
        return None, "Error: todos must be a list"
    for i, todo in enumerate(todos):
        if not isinstance(todo, dict):
            return None, f"Error: todos[{i}] must be an object"
        if "content" not in todo or "status" not in todo:
            return None, f"Error: todos[{i}] missing 'content' or 'status'"
        if todo["status"] not in ("pending", "in_progress", "completed"):
            return None, f"Error: todos[{i}] has invalid status '{todo['status']}'"
    return todos, None


def run_todo_write(todos: list) -> str:
    global CURRENT_TODOS
    todos, error = _normalize_todos(todos)
    if error:
        return error
    CURRENT_TODOS = todos
    lines = ["\n\033[33m## Current Tasks\033[0m"]
    for todo in CURRENT_TODOS:
        icon = {"pending": " ", "in_progress": "\033[36m▸\033[0m", "completed": "\033[32m✓\033[0m"}[todo["status"]]
        lines.append(f"  [{icon}] {todo['content']}")
    print("\n".join(lines))
    return f"Updated {len(CURRENT_TODOS)} tasks"


SUB_TOOLS = [
    {"name": "bash", "description": "Run a shell command.", "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once.", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files matching a glob pattern.", "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
]

SUB_HANDLERS = {"bash": run_bash, "read_file": run_read, "write_file": run_write, "edit_file": run_edit, "glob": run_glob}


def trigger_hooks(event: str, *args):
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None


def spawn_subagent(description: str) -> str:
    print("\n\033[35m[Subagent spawned]\033[0m")
    messages = [{"role": "user", "content": description}]
    for _ in range(30):
        response = client.chat.completions.create(model=MODEL, messages=[{"role": "system", "content": SUB_SYSTEM}] + messages, tools=openai_tools(SUB_TOOLS), max_completion_tokens=8000)
        msg = response.choices[0].message
        assistant_message = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            assistant_message["tool_calls"] = [tool_call.model_dump() for tool_call in msg.tool_calls]
        messages.append(assistant_message)
        if not msg.tool_calls:
            break
        for tool_call in msg.tool_calls:
            block = make_tool_block(tool_call)
            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                messages.append({"role": "tool", "tool_call_id": block.id, "content": str(blocked)})
                continue
            handler = SUB_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"
            trigger_hooks("PostToolUse", block, output)
            print(f"  \033[90m[sub] {block.name}: {str(output)[:100]}\033[0m")
            messages.append({"role": "tool", "tool_call_id": block.id, "content": str(output)})
    result = extract_text(messages[-1]["content"])
    if not result:
        for msg in reversed(messages):
            if msg["role"] == "assistant":
                result = extract_text(msg["content"])
                if result:
                    break
        if not result:
            result = "Subagent stopped after 30 turns without final answer."
    print("\033[35m[Subagent done]\033[0m")
    return result


def estimate_size(messages: list) -> int:
    return len(json.dumps(messages, default=str))


def snip_compact(messages: list, max_messages: int = 50) -> list:
    if len(messages) <= max_messages:
        return messages
    keep_head = 3
    keep_tail = max_messages - keep_head
    snipped = len(messages) - keep_head - keep_tail
    return messages[:keep_head] + [{"role": "user", "content": f"[snipped {snipped} messages from conversation middle]"}] + messages[-keep_tail:]


def collect_tool_results(messages: list) -> list[dict]:
    return [message for message in messages if message.get("role") == "tool"]


def micro_compact(messages: list) -> list:
    tool_results = collect_tool_results(messages)
    if len(tool_results) <= KEEP_RECENT_TOOL_RESULTS:
        return messages
    for message in tool_results[:-KEEP_RECENT_TOOL_RESULTS]:
        if len(str(message.get("content", ""))) > 120:
            message["content"] = "[Earlier tool result compacted. Re-run if needed.]"
    return messages


def persist_large_output(tool_use_id: str, output: str) -> str:
    if len(output) <= PERSIST_THRESHOLD:
        return output
    TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = TOOL_RESULTS_DIR / f"{tool_use_id}.txt"
    if not path.exists():
        path.write_text(output, encoding="utf-8")
    return f"<persisted-output>\nFull output: {path}\nPreview:\n{output[:2000]}\n</persisted-output>"


def tool_result_budget(messages: list, max_bytes: int = TOOL_RESULT_BUDGET) -> list:
    tool_messages = [message for message in messages if message.get("role") == "tool"]
    total = sum(len(str(message.get("content", ""))) for message in tool_messages)
    if total <= max_bytes:
        return messages
    ranked = sorted(tool_messages, key=lambda message: len(str(message.get("content", ""))), reverse=True)
    for message in ranked:
        if total <= max_bytes:
            break
        content = str(message.get("content", ""))
        if len(content) <= PERSIST_THRESHOLD:
            continue
        tool_use_id = message.get("tool_call_id", "unknown")
        message["content"] = persist_large_output(tool_use_id, content)
        total = sum(len(str(candidate.get("content", ""))) for candidate in tool_messages)
    return messages


def write_transcript(messages: list) -> Path:
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for message in messages:
            handle.write(json.dumps(message, default=str) + "\n")
    return path


def summarize_history(messages: list) -> str:
    conversation = json.dumps(messages, default=str)[:80_000]
    prompt = (
        "Summarize this coding-agent conversation so work can continue.\n"
        "Preserve: 1. current goal, 2. key findings/decisions, 3. files read/changed, 4. remaining work, 5. user constraints.\n"
        "Be compact but concrete. Respond with text only.\n\n"
        f"{conversation}"
    )
    response = client.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": prompt}], max_completion_tokens=2000)
    return (response.choices[0].message.content or "").strip() or "(empty summary)"


def compact_history(messages: list) -> list:
    transcript_path = write_transcript(messages)
    print(f"[transcript saved: {transcript_path}]")
    return [{"role": "user", "content": f"[Compacted]\n\n{summarize_history(messages)}"}]


def reactive_compact(messages: list) -> list:
    transcript_path = write_transcript(messages)
    print(f"[reactive transcript saved: {transcript_path}]")
    return [{"role": "user", "content": f"[Reactive compact]\n\n{summarize_history(messages)}"}, *messages[-5:]]


TOOLS = [
    {"name": "bash", "description": "Run a shell command.", "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once.", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files matching a glob pattern.", "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
    {"name": "todo_write", "description": "Create and manage a task list for your current coding session.", "input_schema": {"type": "object", "properties": {"todos": {"type": "array", "items": {"type": "object", "properties": {"content": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}}, "required": ["content", "status"]}}}, "required": ["todos"]}},
    {"name": "task", "description": "Launch a subagent to handle a complex subtask. Returns only the final conclusion.", "input_schema": {"type": "object", "properties": {"description": {"type": "string"}}, "required": ["description"]}},
    {"name": "load_skill", "description": "Load the full content of a skill by name.", "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "compact", "description": "Summarize earlier conversation to free context space.", "input_schema": {"type": "object", "properties": {"focus": {"type": "string"}}}},
]

TOOL_HANDLERS = {"bash": run_bash, "read_file": run_read, "write_file": run_write, "edit_file": run_edit, "glob": run_glob, "todo_write": run_todo_write, "task": spawn_subagent, "load_skill": load_skill}
HOOKS = {"PreToolUse": [], "PostToolUse": []}
DENY_LIST = ["rm -rf /", "sudo", "shutdown"]


def permission_hook(block):
    if block.name == "bash":
        for pattern in DENY_LIST:
            if pattern in block.input.get("command", ""):
                return "Permission denied"
    return None


def log_hook(block):
    print(f"\033[90m[HOOK] {block.name}\033[0m")
    return None


HOOKS["PreToolUse"].append(permission_hook)
HOOKS["PreToolUse"].append(log_hook)


def is_prompt_too_long_error(error: Exception) -> bool:
    text = str(error).lower()
    return "prompt_too_long" in text or "too many tokens" in text or "413" in text or "context_length" in text


def agent_loop(messages: list):
    reactive_retries = 0
    while True:
        messages[:] = tool_result_budget(messages)
        messages[:] = snip_compact(messages)
        messages[:] = micro_compact(messages)
        if estimate_size(messages) > CONTEXT_LIMIT:
            print("[auto compact]")
            messages[:] = compact_history(messages)
        try:
            response = client.chat.completions.create(model=MODEL, messages=[{"role": "system", "content": SYSTEM}] + messages, tools=openai_tools(TOOLS), max_completion_tokens=8000)
            reactive_retries = 0
        except Exception as e:
            if is_prompt_too_long_error(e) and reactive_retries < MAX_REACTIVE_RETRIES:
                print("[reactive compact]")
                messages[:] = reactive_compact(messages)
                reactive_retries += 1
                continue
            raise
        msg = response.choices[0].message
        assistant_message = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            assistant_message["tool_calls"] = [tool_call.model_dump() for tool_call in msg.tool_calls]
        messages.append(assistant_message)
        if not msg.tool_calls:
            return
        compact_called = False
        for tool_call in msg.tool_calls:
            block = make_tool_block(tool_call)
            print(f"\033[36m> {block.name}\033[0m")
            if block.name == "compact":
                messages[:] = compact_history(messages)
                messages.append({"role": "tool", "tool_call_id": block.id, "content": "[Compacted. Conversation history has been summarized.]"})
                compact_called = True
                break
            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                messages.append({"role": "tool", "tool_call_id": block.id, "content": str(blocked)})
                continue
            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"
            trigger_hooks("PostToolUse", block, output)
            print(str(output)[:200])
            messages.append({"role": "tool", "tool_call_id": block.id, "content": str(output)})
        if compact_called:
            continue


if __name__ == "__main__":
    print("learning08: Context Compact — four-layer compaction pipeline")
    print("Enter a prompt. Press Enter to send. Type q to quit.\n")
    history = []
    while True:
        try:
            query = input("\033[36mlearning08 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        assistant_message = history[-1].get("content") if history else None
        if isinstance(assistant_message, str):
            print(assistant_message)
        print()
