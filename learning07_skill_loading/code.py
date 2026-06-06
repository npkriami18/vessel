#!/usr/bin/env python3
"""
learning07: Skill Loading — load specialized guidance only when needed.

  Layer 1 (cheap, always present):
    SYSTEM prompt includes skill names + one-line descriptions
    "Skills available: agent-builder, code-review, mcp-builder, pdf"

  Layer 2 (larger, on demand):
    Agent calls load_skill("code-review") → full SKILL.md content
    injected via tool result only for the current task

  skills/
    agent-builder/SKILL.md
    code-review/SKILL.md
    mcp-builder/SKILL.md
    pdf/SKILL.md

Changes from learning06:
  + SKILLS_DIR + skill registry scan at startup
  + build_system() injects a compact skill catalog into SYSTEM
  + load_skill(name) returns full SKILL.md content via registry lookup
  Parent/subagent loop unchanged: load_skill auto-dispatches via TOOL_HANDLERS.

Run: python learning07_skill_loading/code.py
Needs: pip install openai python-dotenv pyyaml + Azure config in .env
"""

import ast
import json
import os
import subprocess
from pathlib import Path

import yaml

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

api_version = os.getenv("AZURE_API_VERSION")
endpoint = os.getenv("AZURE_ENDPOINT")
subscription_key = os.getenv("AZURE_API_KEY")
deployment = os.getenv("AZURE_DEPLOYMENT")

WORKDIR = Path.cwd()
SKILLS_DIR = WORKDIR / "skills"
client = AzureOpenAI(
    api_version=api_version,
    azure_endpoint=endpoint,
    api_key=subscription_key,
)
MODEL = deployment
CURRENT_TODOS: list[dict] = []


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from SKILL.md. Returns (meta, body)."""
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
    """Scan skills/ dir and populate SKILL_REGISTRY."""
    if not SKILLS_DIR.exists():
        return

    for d in sorted(SKILLS_DIR.iterdir()):
        if not d.is_dir():
            continue

        manifest = d / "SKILL.md"
        if manifest.exists():
            raw = manifest.read_text(encoding="utf-8")
            meta, _ = _parse_frontmatter(raw)
            name = meta.get("name", d.name)
            desc = meta.get("description", raw.split("\n")[0].lstrip("#").strip())
            SKILL_REGISTRY[name] = {
                "name": name,
                "description": desc,
                "content": raw,
            }


_scan_skills()


def list_skills() -> str:
    """List all discovered skills as name + one-line description."""
    if not SKILL_REGISTRY:
        return "(no skills found)"

    return "\n".join(
        f"- **{s['name']}**: {s['description']}"
        for s in SKILL_REGISTRY.values()
    )


def build_system() -> str:
    """Build SYSTEM prompt with the lightweight skill catalog."""
    catalog = list_skills()
    return (
        f"You are a coding agent at {WORKDIR}. Use tools to solve tasks. "
        "Act, don't explain. Destructive operations require user approval. "
        "Before starting any multi-step task, use todo_write to plan your steps. "
        "Update status as you go. For complex sub-problems, use the task tool.\n"
        f"Skills available:\n{catalog}\n"
        "Use load_skill to get full details when needed."
    )


SYSTEM = build_system()

SUB_SYSTEM = (
    f"You are a coding agent at {WORKDIR}. Use tools to solve the delegated task. "
    "Act, don't explain. Destructive operations require user approval. "
    "Complete the task you were given and return a concise conclusion. "
    "Do not delegate further."
)


# ═══════════════════════════════════════════════════════════
#  FROM learning02-learning06 (unchanged): Tool Implementations
# ═══════════════════════════════════════════════════════════

def run_bash(command: str) -> str:
    try:
        r = subprocess.run(
            command,
            shell=True,
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"


def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
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
        icon = {
            "pending": " ",
            "in_progress": "\033[36m▸\033[0m",
            "completed": "\033[32m✓\033[0m",
        }[todo["status"]]
        lines.append(f"  [{icon}] {todo['content']}")

    print("\n".join(lines))
    return f"Updated {len(CURRENT_TODOS)} tasks"


def extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if not content:
        return ""
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(item.get("text", ""))
            else:
                item_type = getattr(item, "type", None)
                if item_type == "text":
                    parts.append(getattr(item, "text", ""))
        return "\n".join(part for part in parts if part)
    return str(content)


def load_skill(name: str) -> str:
    """Load full skill content by registry lookup, not by path."""
    skill = SKILL_REGISTRY.get(name)
    if not skill:
        return f"Skill not found: {name}"
    return skill["content"]


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
                "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
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
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace exact text in a file once.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Find files matching a glob pattern.",
            "parameters": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}},
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "todo_write",
            "description": "Create and manage a task list for your current coding session.",
            "parameters": {
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {"type": "string"},
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "completed"],
                                },
                            },
                            "required": ["content", "status"],
                        },
                    },
                },
                "required": ["todos"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task",
            "description": "Launch a subagent to handle a complex subtask. Returns only the final conclusion.",
            "parameters": {
                "type": "object",
                "properties": {"description": {"type": "string"}},
                "required": ["description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "load_skill",
            "description": "Load the full content of a skill by name.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
]

SUB_TOOLS = [
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
                "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
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
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace exact text in a file once.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Find files matching a glob pattern.",
            "parameters": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}},
                "required": ["pattern"],
            },
        },
    },
]

TOOL_HANDLERS = {
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
    "todo_write": run_todo_write,
    "load_skill": load_skill,
}

SUB_HANDLERS = {
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
}


# ═══════════════════════════════════════════════════════════
#  FROM learning04-learning06 (unchanged): Hook System
# ═══════════════════════════════════════════════════════════

HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": []}


def register_hook(event: str, callback):
    HOOKS[event].append(callback)


def trigger_hooks(event: str, *args):
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None


DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if=", "> /dev/sda"]
DESTRUCTIVE = ["rm ", "> /etc/", "chmod 777"]


class ToolBlock:
    def __init__(self, tool_id: str, name: str, input_data: dict):
        self.id = tool_id
        self.name = name
        self.input = input_data


def permission_hook(block):
    if block.name == "bash":
        for pattern in DENY_LIST:
            if pattern in block.input.get("command", ""):
                print(f"\n\033[31m⛔ Blocked: '{pattern}'\033[0m")
                return "Permission denied by deny list"

        for kw in DESTRUCTIVE:
            if kw in block.input.get("command", ""):
                print(f"\n\033[33m⚠ Potentially destructive command\033[0m")
                print(f"   Tool: {block.name}({block.input})")
                choice = input("   Allow? [y/N] ").strip().lower()
                if choice not in ("y", "yes"):
                    return "Permission denied by user"

    if block.name in ("write_file", "edit_file"):
        path = block.input.get("path", "")
        if not (WORKDIR / path).resolve().is_relative_to(WORKDIR):
            print(f"\n\033[33m⚠ Writing outside workspace\033[0m")
            print(f"   Tool: {block.name}({block.input})")
            choice = input("   Allow? [y/N] ").strip().lower()
            if choice not in ("y", "yes"):
                return "Permission denied by user"

    return None


def log_hook(block):
    args_preview = str(list(block.input.values())[:2])[:60]
    print(f"\033[90m[HOOK] {block.name}({args_preview})\033[0m")
    return None


def large_output_hook(block, output):
    if len(str(output)) > 100000:
        print(f"\033[33m[HOOK] ⚠ Large output from {block.name}: {len(str(output))} chars\033[0m")
    return None


def context_inject_hook(query: str):
    print(f"\033[90m[HOOK] UserPromptSubmit: working in {WORKDIR}\033[0m")
    return None


def summary_hook(messages: list):
    tool_count = sum(1 for m in messages if m.get("role") == "tool")
    print(f"\033[90m[HOOK] Stop: session used {tool_count} tool calls\033[0m")
    return None


register_hook("UserPromptSubmit", context_inject_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", summary_hook)


# ═══════════════════════════════════════════════════════════
#  FROM learning06 (unchanged): Subagent helpers
# ═══════════════════════════════════════════════════════════

rounds_since_todo = 0


def run_subagent_loop(messages: list) -> str:
    for _ in range(30):
        api_messages = [{"role": "system", "content": SUB_SYSTEM}] + messages

        response = client.chat.completions.create(
            model=MODEL,
            messages=api_messages,
            tools=SUB_TOOLS,
            max_completion_tokens=20000,
        )

        msg = response.choices[0].message

        if not msg.tool_calls:
            final_text = msg.content or ""
            messages.append({"role": "assistant", "content": final_text})
            return final_text

        messages.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ],
        })

        for tool_call in msg.tool_calls:
            name = tool_call.function.name
            handler = SUB_HANDLERS.get(name)

            if not handler:
                output = f"Unknown: {name}"
            else:
                try:
                    args = json.loads(tool_call.function.arguments)
                except Exception as e:
                    output = f"Error parsing arguments: {e}"
                else:
                    block = ToolBlock(tool_call.id, name, args)
                    blocked = trigger_hooks("PreToolUse", block)
                    if blocked:
                        output = str(blocked)
                    else:
                        if name == "bash":
                            print(f"\033[35m[sub] $ {args.get('command', '')}\033[0m")
                        else:
                            print(f"\033[35m[sub] > {name}\033[0m")
                        output = handler(**args)
                        print(f"\033[90m{str(output)[:200]}\033[0m")
                        trigger_hooks("PostToolUse", block, output)

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": output,
            })

    return "Subagent stopped after 30 turns without final answer."


def spawn_subagent(description: str) -> str:
    print("\033[35m[Subagent spawned]\033[0m")
    result = run_subagent_loop([{"role": "user", "content": description}])
    result = extract_text(result)
    if not result:
        result = "Subagent completed without a text conclusion."
    print("\033[35m[Subagent done]\033[0m")
    return result


TOOL_HANDLERS["task"] = spawn_subagent


# ═══════════════════════════════════════════════════════════
#  agent_loop — Azure style + todo reminder + skill loading
# ═══════════════════════════════════════════════════════════

def agent_loop(messages: list):
    global rounds_since_todo

    while True:
        if rounds_since_todo >= 3 and messages:
            messages.append({
                "role": "user",
                "content": "<reminder>Update your todos.</reminder>",
            })
            rounds_since_todo = 0

        api_messages = (
            messages
            if messages and messages[0].get("role") == "system"
            else [{"role": "system", "content": SYSTEM}] + messages
        )

        response = client.chat.completions.create(
            model=MODEL,
            messages=api_messages,
            tools=TOOLS,
            max_completion_tokens=20000,
        )

        msg = response.choices[0].message

        if not msg.tool_calls:
            final_text = msg.content or ""
            messages.append({"role": "assistant", "content": final_text})
            force = trigger_hooks("Stop", messages)
            if force:
                messages.append({"role": "user", "content": force})
                continue
            return final_text

        rounds_since_todo += 1

        messages.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ],
        })

        for tool_call in msg.tool_calls:
            name = tool_call.function.name
            handler = TOOL_HANDLERS.get(name)

            if not handler:
                output = f"Unknown: {name}"
            else:
                try:
                    args = json.loads(tool_call.function.arguments)
                except Exception as e:
                    output = f"Error parsing arguments: {e}"
                else:
                    block = ToolBlock(tool_call.id, name, args)
                    blocked = trigger_hooks("PreToolUse", block)
                    if blocked:
                        output = str(blocked)
                    else:
                        if name == "bash":
                            print(f"\033[33m$ {args.get('command', '')}\033[0m")
                        else:
                            print(f"\033[33m> {name}\033[0m")

                        output = handler(**args)
                        print(str(output)[:200])
                        trigger_hooks("PostToolUse", block, output)

                        if name == "todo_write":
                            rounds_since_todo = 0

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": output,
            })


if __name__ == "__main__":
    print("learning07: Skill Loading — keep a small catalog, load details on demand")
    print("Enter a prompt, or q to quit.\n")

    history = []
    while True:
        try:
            query = input("\033[36mlearning07 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break

        if query.strip().lower() in ("q", "exit", ""):
            break

        trigger_hooks("UserPromptSubmit", query)
        history.append({"role": "user", "content": query})
        answer = agent_loop(history)

        if answer:
            print(answer)

        print()
