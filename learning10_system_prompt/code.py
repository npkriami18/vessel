#!/usr/bin/env python3
"""
learning10: System Prompt — runtime prompt assembly with caching.

Run: python learning10_system_prompt/code.py
Needs: pip install openai python-dotenv + Azure config in .env

Changes from learning09:
- PROMPT_SECTIONS: topic-keyed dict of prompt fragments
- assemble_system_prompt(context): select + join sections by real state
- get_system_prompt(context): deterministic cache via json.dumps
- agent_loop uses get_system_prompt(context) instead of hardcoded SYSTEM

Memory section loads when .memory/MEMORY.md exists.
"""

import json
import os
import subprocess
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

api_version      = os.getenv('AZURE_API_VERSION')
endpoint         = os.getenv('AZURE_ENDPOINT')
subscription_key = os.getenv('AZURE_API_KEY')
deployment       = os.getenv('AZURE_DEPLOYMENT')

WORKDIR      = Path.cwd()
MEMORY_DIR   = WORKDIR / '.memory'
MEMORY_INDEX = MEMORY_DIR / 'MEMORY.md'

client = AzureOpenAI(
    api_version=api_version,
    azure_endpoint=endpoint,
    api_key=subscription_key,
)
MODEL = deployment


PROMPT_SECTIONS = {
    'identity': 'You are a coding agent. Act, don\'t explain.',
    'tools': 'Available tools: bash, read_file, write_file.',
    'workspace': f'Working directory: {WORKDIR}',
}


def assemble_system_prompt(context: dict) -> str:
    sections = [
        PROMPT_SECTIONS['identity'],
        PROMPT_SECTIONS['tools'],
        PROMPT_SECTIONS['workspace'],
    ]

    memories = context.get('memories', '')
    if memories:
        sections.append(f'Relevant memories:\n{memories}')

    return '\n\n'.join(sections)


_last_context_key = None
_last_prompt = None


def get_system_prompt(context: dict) -> str:
    global _last_context_key, _last_prompt
    key = json.dumps(context, sort_keys=True, ensure_ascii=False, default=str)
    if key == _last_context_key and _last_prompt:
        print('  \033[90m[cache hit] system prompt unchanged\033[0m')
        return _last_prompt
    _last_context_key = key
    _last_prompt = assemble_system_prompt(context)

    loaded = ['identity', 'tools', 'workspace']
    if context.get('memories'):
        loaded.append('memory')
    print(f'  \033[32m[assembled] sections: {", ".join(loaded)}\033[0m')
    return _last_prompt


def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f'Path escapes workspace: {p}')
    return path


def run_bash(command: str) -> str:
    try:
        r = subprocess.run(
            command,
            shell=True,
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=120,
        )
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else '(no output)'
    except subprocess.TimeoutExpired:
        return 'Error: Timeout (120s)'
    except (FileNotFoundError, OSError) as e:
        return f'Error: {e}'


def run_read(path: str, limit: int | None = None) -> str:
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f'... ({len(lines) - limit} more lines)']
        return '\n'.join(lines)
    except Exception as e:
        return f'Error: {e}'


def run_write(path: str, content: str) -> str:
    try:
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        return f'Wrote {len(content)} bytes to {path}'
    except Exception as e:
        return f'Error: {e}'


TOOLS = [
    {
        'type': 'function',
        'function': {
            'name': 'bash',
            'description': 'Run a shell command.',
            'parameters': {
                'type': 'object',
                'properties': {'command': {'type': 'string'}},
                'required': ['command'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'read_file',
            'description': 'Read file contents.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string'},
                    'limit': {'type': 'integer'},
                },
                'required': ['path'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'write_file',
            'description': 'Write content to a file.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string'},
                    'content': {'type': 'string'},
                },
                'required': ['path', 'content'],
            },
        },
    },
]

TOOL_HANDLERS = {
    'bash': run_bash,
    'read_file': run_read,
    'write_file': run_write,
}


def update_context(context: dict, messages: list) -> dict:
    memories = ''
    if MEMORY_INDEX.exists():
        content = MEMORY_INDEX.read_text().strip()
        if content:
            memories = content
    return {
        'enabled_tools': list(TOOL_HANDLERS.keys()),
        'workspace': str(WORKDIR),
        'memories': memories,
    }


def agent_loop(messages: list, context: dict):
    system = get_system_prompt(context)
    while True:
        api_messages = [{ 'role': 'system', 'content': system }] + messages
        response = client.chat.completions.create(
            model=MODEL,
            messages=api_messages,
            tools=TOOLS,
            max_completion_tokens=8000,
        )
        msg = response.choices[0].message

        if not msg.tool_calls:
            final_text = msg.content or ''
            messages.append({'role': 'assistant', 'content': final_text})
            return final_text

        messages.append({
            'role': 'assistant',
            'content': msg.content,
            'tool_calls': [
                {
                    'id': tool_call.id,
                    'type': 'function',
                    'function': {
                        'name': tool_call.function.name,
                        'arguments': tool_call.function.arguments,
                    },
                }
                for tool_call in msg.tool_calls
            ],
        })

        for tool_call in msg.tool_calls:
            name = tool_call.function.name
            print(f'\033[36m> {name}\033[0m')

            handler = TOOL_HANDLERS.get(name)
            if not handler:
                output = f'Unknown: {name}'
            else:
                try:
                    args = json.loads(tool_call.function.arguments)
                except Exception as e:
                    output = f'Error parsing arguments: {e}'
                else:
                    if name == 'bash':
                        print(f"\033[33m$ {args.get('command', '')}\033[0m")
                    output = handler(**args)
                    print(str(output)[:200])

            messages.append({
                'role': 'tool',
                'tool_call_id': tool_call.id,
                'content': str(output),
            })

        context = update_context(context, messages)
        system = get_system_prompt(context)


if __name__ == '__main__':
    print('learning10: system prompt — runtime assembly')
    print('Enter a question, press Enter to send. Type q to quit.\n')

    history = []
    context = update_context({}, [])

    while True:
        try:
            query = input('\033[36mlearning10 >> \033[0m')
        except (EOFError, KeyboardInterrupt):
            break

        if query.strip().lower() in ('q', 'exit', ''):
            break

        history.append({'role': 'user', 'content': query})
        answer = agent_loop(history, context)
        context = update_context(context, history)

        if answer:
            print(answer)

        print()
