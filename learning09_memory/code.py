#!/usr/bin/env python3
"""
learning09: Memory System — persistent, cross-session knowledge for the coding agent.

Storage:
    .memory/
      MEMORY.md          ← index (one line per memory, ≤200 lines)
      feedback_tabs.md   ← individual memory files (Markdown + YAML frontmatter)
      user_profile.md
      project_facts.md

Flow in agent_loop:
    1. Load MEMORY.md index into SYSTEM prompt (cheap, always present)
    2. Select relevant memories by filename/description → inject content
    3. Run compression pipeline from learning08
    4. After each turn ends → extract new memories from original messages
    5. Periodically consolidate (Dream)

Builds on learning08 (context compact). Usage:

    python learning09_memory/code.py
    Needs: pip install openai python-dotenv pyyaml + Azure config in .env
"""

import json
import os
import re
import subprocess
import time
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

api_version      = os.getenv("AZURE_API_VERSION")
endpoint         = os.getenv("AZURE_ENDPOINT")
subscription_key = os.getenv("AZURE_API_KEY")
deployment       = os.getenv("AZURE_DEPLOYMENT")

WORKDIR          = Path.cwd()
MEMORY_DIR       = WORKDIR / ".memory"; MEMORY_DIR.mkdir(exist_ok=True)
MEMORY_INDEX     = MEMORY_DIR / "MEMORY.md"
TRANSCRIPT_DIR   = WORKDIR / ".transcripts"
TOOL_RESULTS_DIR = WORKDIR / ".task_outputs" / "tool-results"

client = AzureOpenAI(
    api_version=api_version,
    azure_endpoint=endpoint,
    api_key=subscription_key,
)
MODEL = deployment


# ═══════════════════════════════════════════════════════════
#  NEW in learning09: Memory System
# ═══════════════════════════════════════════════════════════

MEMORY_TYPES = ["user", "feedback", "project", "reference"]


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


def write_memory_file(name: str, mem_type: str, description: str, body: str):
    """Write a single memory file with YAML frontmatter."""
    slug = name.lower().replace(" ", "-").replace("/", "-")
    filename = f"{slug}.md"
    filepath = MEMORY_DIR / filename
    filepath.write_text(
        f"---\nname: {name}\ndescription: {description}\ntype: {mem_type}\n---\n\n{body}\n"
    )
    _rebuild_index()
    return filepath


def _rebuild_index():
    """Rebuild MEMORY.md index from all memory files."""
    lines = []
    for f in sorted(MEMORY_DIR.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        raw = f.read_text()
        meta, body = _parse_frontmatter(raw)
        name = meta.get("name", f.stem)
        desc = meta.get("description", body.split("\n")[0][:80])
        lines.append(f"- [{name}]({f.name}) — {desc}")
    MEMORY_INDEX.write_text("\n".join(lines) + "\n" if lines else "")


def read_memory_index() -> str:
    """Read MEMORY.md index (injected into SYSTEM every turn)."""
    if not MEMORY_INDEX.exists():
        return ""
    text = MEMORY_INDEX.read_text().strip()
    return text if text else ""


def read_memory_file(filename: str) -> str | None:
    """Read a single memory file's full content."""
    path = MEMORY_DIR / filename
    if not path.exists():
        return None
    return path.read_text()


def list_memory_files() -> list[dict]:
    """List all memory files with metadata."""
    result = []
    for f in sorted(MEMORY_DIR.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        raw = f.read_text()
        meta, body = _parse_frontmatter(raw)
        result.append({
            "filename":    f.name,
            "name":        meta.get("name", f.stem),
            "description": meta.get("description", ""),
            "type":        meta.get("type", "user"),
            "body":        body,
        })
    return result


def select_relevant_memories(messages: list, max_items: int = 5) -> list[str]:
    """Select relevant memory filenames by matching recent conversation against
    memory names/descriptions via a small LLM call (falls back to keyword match)."""
    files = list_memory_files()
    if not files:
        return []

    # Collect recent user text for context
    recent_texts = []
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                recent_texts.append(content)
            if len(recent_texts) >= 3:
                break
    recent = " ".join(reversed(recent_texts))[:2000]

    if not recent.strip():
        return []

    catalog_lines = [f"{i}: {f['name']} — {f['description']}" for i, f in enumerate(files)]
    catalog = "\n".join(catalog_lines)

    prompt = (
        "Given the recent conversation and the memory catalog below, "
        "select the indices of memories that are clearly relevant. "
        "Return ONLY a JSON array of integers, e.g. [0, 3]. "
        "If none are relevant, return [].\n\n"
        f"Recent conversation:\n{recent}\n\n"
        f"Memory catalog:\n{catalog}"
    )

    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=200,
        )
        text = (response.choices[0].message.content or "").strip()
        match = re.search(r'\[.*?\]', text, re.DOTALL)
        if match:
            indices = json.loads(match.group())
            selected = []
            for idx in indices:
                if isinstance(idx, int) and 0 <= idx < len(files):
                    selected.append(files[idx]["filename"])
                    if len(selected) >= max_items:
                        break
            return selected
    except Exception:
        pass

    # Fallback: keyword matching on name + description
    keywords = [w.lower() for w in recent.split() if len(w) > 3]
    selected = []
    for f in files:
        text = (f["name"] + " " + f["description"]).lower()
        if any(kw in text for kw in keywords):
            selected.append(f["filename"])
            if len(selected) >= max_items:
                break
    return selected


def load_memories(messages: list) -> str:
    """Load relevant memory content for injection into context."""
    selected_files = select_relevant_memories(messages)
    if not selected_files:
        return ""
    parts = ["<relevant_memories>"]
    for filename in selected_files:
        content = read_memory_file(filename)
        if content:
            parts.append(content)
    parts.append("</relevant_memories>")
    return "\n\n".join(parts)


def extract_memories(messages: list):
    """Extract new memories from recent dialogue. Runs after each turn."""
    dialogue_parts = []
    for msg in messages[-10:]:
        role = msg.get("role", "?")
        content = msg.get("content", "")
        if isinstance(content, str) and content.strip():
            dialogue_parts.append(f"{role}: {content}")
    dialogue = "\n".join(dialogue_parts)

    if not dialogue.strip():
        return

    existing = list_memory_files()
    existing_desc = (
        "\n".join(f"- {m['name']}: {m['description']}" for m in existing)
        if existing else "(none)"
    )

    prompt = (
        "Extract user preferences, constraints, or project facts from this dialogue.\n"
        "Return a JSON array. Each item: {name, type, description, body}.\n"
        "- name: short kebab-case identifier (e.g. 'user-preference-tabs')\n"
        "- type: one of 'user' (user preference), 'feedback' (guidance), "
        "'project' (project fact), 'reference' (external pointer)\n"
        "- description: one-line summary for index lookup\n"
        "- body: full detail in markdown\n"
        "If nothing new or already covered by existing memories, return [].\n\n"
        f"Existing memories:\n{existing_desc}\n\n"
        f"Dialogue:\n{dialogue[:4000]}"
    )

    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=800,
        )
        text = (response.choices[0].message.content or "").strip()
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if not match:
            return
        items = json.loads(match.group())
        if not items:
            return
        count = 0
        for mem in items:
            name     = mem.get("name", f"memory_{int(time.time())}")
            mem_type = mem.get("type", "user")
            desc     = mem.get("description", "")
            body     = mem.get("body", "")
            if desc and body:
                write_memory_file(name, mem_type, desc, body)
                count += 1
        if count:
            print(f"\n\033[33m[Memory: extracted {count} new memories]\033[0m")
    except Exception:
        pass


CONSOLIDATE_THRESHOLD = 10


def consolidate_memories():
    """Merge duplicate/stale memories. Triggered when file count ≥ threshold."""
    files = list_memory_files()
    if len(files) < CONSOLIDATE_THRESHOLD:
        return

    catalog = "\n\n".join(
        f"## {f['filename']}\nname: {f['name']}\ndescription: {f['description']}\n{f['body']}"
        for f in files
    )

    prompt = (
        "Consolidate the following memory files. Rules:\n"
        "1. Merge duplicates into one\n"
        "2. Remove outdated/contradicted memories\n"
        "3. Keep the total under 30 memories\n"
        "4. Preserve important user preferences above all\n"
        "Return a JSON array. Each item: {name, type, description, body}.\n\n"
        f"{catalog[:16000]}"
    )

    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=3000,
        )
        text = (response.choices[0].message.content or "").strip()
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if not match:
            return
        items = json.loads(match.group())

        # Remove old memory files (keep MEMORY.md)
        for f in MEMORY_DIR.glob("*.md"):
            if f.name != "MEMORY.md":
                f.unlink()

        for mem in items:
            name     = mem.get("name", f"memory_{int(time.time())}")
            mem_type = mem.get("type", "user")
            desc     = mem.get("description", "")
            body     = mem.get("body", "")
            if desc and body:
                write_memory_file(name, mem_type, desc, body)

        print(f"\n\033[33m[Memory: consolidated {len(files)} → {len(items)} memories]\033[0m")
    except Exception:
        pass


def build_system() -> str:
    index = read_memory_index()
    memories_section = f"\n\nMemories available:\n{index}" if index else ""
    return (
        f"You are a coding agent at {WORKDIR}."
        f"{memories_section}\n"
        "Relevant memories are injected below. Respect user preferences from memory.\n"
        "When the user says 'remember' or expresses a clear preference, extract it as a memory."
    )


SUB_SYSTEM = (
    f"You are a coding agent at {WORKDIR}. "
    "Complete the task you were given, then return a concise summary. "
    "Do not delegate further."
)


# ═══════════════════════════════════════════════════════════
#  FROM learning02-learning08: Basic Tools
# ═══════════════════════════════════════════════════════════

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
                if getattr(item, "type", None) == "text":
                    parts.append(getattr(item, "text", ""))
        return "\n".join(part for part in parts if part)
    return str(content)


# ═══════════════════════════════════════════════════════════
#  Subagent (simplified, OpenAI format)
# ═══════════════════════════════════════════════════════════

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
                "properties": {"path": {"type": "string"}},
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
]

SUB_HANDLERS = {"bash": run_bash, "read_file": run_read, "write_file": run_write}


class ToolBlock:
    def __init__(self, tool_id: str, name: str, input_data: dict):
        self.id    = tool_id
        self.name  = name
        self.input = input_data


def spawn_subagent(task: str) -> str:
    print(f"\n\033[35m[Subagent spawned]\033[0m")
    messages = [{"role": "user", "content": task}]

    for _ in range(30):
        api_messages = [{"role": "system", "content": SUB_SYSTEM}] + messages
        response = client.chat.completions.create(
            model=MODEL,
            messages=api_messages,
            tools=SUB_TOOLS,
            max_completion_tokens=8000,
        )
        msg = response.choices[0].message

        if not msg.tool_calls:
            final_text = msg.content or ""
            messages.append({"role": "assistant", "content": final_text})
            break

        messages.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ],
        })

        for tool_call in msg.tool_calls:
            name    = tool_call.function.name
            handler = SUB_HANDLERS.get(name)
            if not handler:
                output = f"Unknown: {name}"
            else:
                try:
                    args = json.loads(tool_call.function.arguments)
                except Exception as e:
                    output = f"Error parsing arguments: {e}"
                else:
                    output = handler(**args)
            print(f"  \033[90m[sub] {name}: {str(output)[:100]}\033[0m")
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": str(output),
            })

    result = extract_text(messages[-1].get("content", ""))
    if not result:
        for m in reversed(messages):
            if m.get("role") == "assistant":
                result = extract_text(m.get("content", ""))
                if result:
                    break
    if not result:
        result = "Subagent stopped after 30 turns without final answer."

    print(f"\033[35m[Subagent done]\033[0m")
    return result


# ═══════════════════════════════════════════════════════════
#  FROM learning08: Compaction Pipeline
# ═══════════════════════════════════════════════════════════

CONTEXT_LIMIT     = 50000
KEEP_RECENT       = 3
PERSIST_THRESHOLD = 30000


def estimate_size(msgs) -> int:
    return len(str(msgs))


def _group_messages(messages: list) -> list[list]:
    """Partition messages into atomic groups that must stay together.
    An assistant message with tool_calls + its following tool messages = one group."""
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


def snip_compact(messages: list, max_messages: int = 50) -> list:
    if len(messages) <= max_messages:
        return messages
    groups = _group_messages(messages)
    if len(groups) <= 6:
        return messages
    keep_head, keep_tail = 3, 3
    if len(groups) <= keep_head + keep_tail:
        return messages
    middle = groups[keep_head:-keep_tail]
    snipped_count = sum(len(g) for g in middle)
    flat_head = [m for g in groups[:keep_head] for m in g]
    flat_tail = [m for g in groups[-keep_tail:] for m in g]
    return (
        flat_head
        + [{"role": "user", "content": f"[snipped {snipped_count} messages]"}]
        + flat_tail
    )


def micro_compact(messages: list) -> list:
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    if len(tool_msgs) <= KEEP_RECENT:
        return messages
    to_compact  = tool_msgs[:-KEEP_RECENT]
    compact_ids = {id(m) for m in to_compact}
    for msg in messages:
        if id(msg) in compact_ids:
            content = msg.get("content", "")
            if isinstance(content, str) and len(content) > 120:
                msg["content"] = "[Earlier tool result compacted. Re-run if needed.]"
    return messages


def persist_large_output(tool_call_id: str, output: str) -> str:
    if len(output) <= PERSIST_THRESHOLD:
        return output
    TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = TOOL_RESULTS_DIR / f"{tool_call_id}.txt"
    if not path.exists():
        path.write_text(output)
    return f"<persisted-output>\nFull output: {path}\nPreview:\n{output[:2000]}\n</persisted-output>"


def tool_result_budget(messages: list, max_bytes: int = 200_000) -> list:
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    total = sum(len(str(m.get("content", ""))) for m in tool_msgs)
    if total <= max_bytes:
        return messages
    ranked = sorted(tool_msgs, key=lambda m: len(str(m.get("content", ""))), reverse=True)
    for msg in ranked:
        if total <= max_bytes:
            break
        content = str(msg.get("content", ""))
        if len(content) <= PERSIST_THRESHOLD:
            continue
        tid = msg.get("tool_call_id", "unknown")
        msg["content"] = persist_large_output(tid, content)
        total = sum(len(str(m.get("content", ""))) for m in tool_msgs)
    return messages


def write_transcript(messages: list) -> Path:
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with path.open("w") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str) + "\n")
    return path


def summarize_history(messages: list) -> str:
    conversation = json.dumps(messages, default=str)[:80000]
    prompt = (
        "Summarize this coding-agent conversation so work can continue.\n"
        "Preserve: 1. current goal, 2. key findings, 3. files changed, "
        "4. remaining work, 5. user constraints.\n\n"
        + conversation
    )
    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_completion_tokens=2000,
    )
    return response.choices[0].message.content or "(empty summary)"


def compact_history(messages: list) -> list:
    transcript_path = write_transcript(messages)
    print(f"[transcript saved: {transcript_path}]")
    summary = summarize_history(messages)
    return [{"role": "user", "content": f"[Compacted]\n\n{summary}"}]


def reactive_compact(messages: list) -> list:
    write_transcript(messages)
    summary = summarize_history(messages)
    groups = _group_messages(messages)
    tail = [m for g in groups[-3:] for m in g]
    return [
        {"role": "user", "content": f"[Reactive compact]\n\n{summary}"},
        *tail,
    ]


# ═══════════════════════════════════════════════════════════
#  Tool Definitions (OpenAI format)
# ═══════════════════════════════════════════════════════════

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
                "properties": {"path": {"type": "string"}},
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
                    "path":     {"type": "string"},
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
            "name": "task",
            "description": "Launch a subagent to handle a subtask.",
            "parameters": {
                "type": "object",
                "properties": {"description": {"type": "string"}},
                "required": ["description"],
            },
        },
    },
]

TOOL_HANDLERS = {
    "bash":       run_bash,
    "read_file":  run_read,
    "write_file": run_write,
    "edit_file":  run_edit,
    "glob":       run_glob,
    "task":       spawn_subagent,
}


# ═══════════════════════════════════════════════════════════
#  agent_loop — learning09: inject memories + extract after each turn
# ═══════════════════════════════════════════════════════════

MAX_REACTIVE_RETRIES = 1


def agent_loop(messages: list):
    reactive_retries = 0

    # learning09: inject relevant memory content into the current user turn
    memories_content = load_memories(messages)
    memory_turn = len(messages) - 1 if messages and isinstance(messages[-1].get("content"), str) else None

    # build system once per user turn; memory index updated after loop returns
    system = build_system()

    while True:
        # learning09: save pre-compression snapshot for accurate memory extraction
        pre_compress = [
            {"role": m.get("role", ""), "content": str(m.get("content", ""))}
            for m in messages
        ]

        # learning08: compression pipeline (budget → snip → micro)
        messages[:] = tool_result_budget(messages)
        messages[:] = snip_compact(messages)
        messages[:] = micro_compact(messages)

        if estimate_size(messages) > CONTEXT_LIMIT:
            print("[auto compact]")
            messages[:] = compact_history(messages)

        # Build api_messages: system first, then inject memories into the
        # relevant user turn if available
        api_messages = messages
        if memories_content and memory_turn is not None and memory_turn < len(messages):
            api_messages = messages.copy()
            api_messages[memory_turn] = {
                **messages[memory_turn],
                "content": memories_content + "\n\n" + messages[memory_turn]["content"],
            }
        api_messages = (
            api_messages
            if api_messages and api_messages[0].get("role") == "system"
            else [{"role": "system", "content": system}] + api_messages
        )

        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=api_messages,
                tools=TOOLS,
                max_completion_tokens=8000,
            )
            reactive_retries = 0
        except Exception as e:
            err = str(e).lower()
            is_too_long = (
                "context_length_exceeded" in err
                or "maximum context length" in err
                or "too many tokens" in err
            )
            if is_too_long and reactive_retries < MAX_REACTIVE_RETRIES:
                print("[reactive compact]")
                messages[:] = reactive_compact(messages)
                reactive_retries += 1
                continue
            raise

        msg = response.choices[0].message

        if not msg.tool_calls:
            final_text = msg.content or ""
            messages.append({"role": "assistant", "content": final_text})
            # learning09: extract from pre-compression snapshot for full fidelity
            extract_memories(pre_compress)
            consolidate_memories()
            return final_text

        messages.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ],
        })

        for tool_call in msg.tool_calls:
            name = tool_call.function.name
            print(f"\033[36m> {name}\033[0m")

            handler = TOOL_HANDLERS.get(name)
            if not handler:
                output = f"Unknown: {name}"
            else:
                try:
                    args = json.loads(tool_call.function.arguments)
                except Exception as e:
                    output = f"Error parsing arguments: {e}"
                else:
                    if name == "bash":
                        print(f"\033[33m$ {args.get('command', '')}\033[0m")
                    else:
                        print(f"\033[33m> {name}\033[0m")
                    output = handler(**args)
                    print(str(output)[:200])

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": str(output),
            })


if __name__ == "__main__":
    print("learning09: Memory — persistent cross-session knowledge")
    print("Enter a prompt, or q to quit.\n")

    history = []
    while True:
        try:
            query = input("\033[36mlearning09 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break

        if query.strip().lower() in ("q", "exit", ""):
            break

        history.append({"role": "user", "content": query})
        answer = agent_loop(history)

        if answer:
            print(answer)

        print()