#!/usr/bin/env python3
'''
learning12: Task System — file-persisted task graph with blockedBy dependencies.

Run: python learning12_task_system/code.py
Needs: pip install openai python-dotenv + Azure config in .env

Changes from learning11:
- Task dataclass (id, subject, description, status, owner, blockedBy)
- TASKS_DIR = .tasks/ for persistent JSON storage
- create_task / save_task / load_task / list_tasks / get_task
- can_start: checks blockedBy all completed (missing deps = blocked)
- claim_task: set owner + pending -> in_progress
- complete_task: set completed + report unblocked downstream
- 5 new tools: create_task, list_tasks, get_task, claim_task, complete_task

Note: Teaching code keeps a basic agent loop to stay focused on the task
system. learning11's full error recovery is omitted here to keep this file
focused on the task system layer.
'''

import json
import os
import random
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

try:
	import readline
	readline.parse_and_bind('set bind-tty-special-chars off')
	readline.parse_and_bind('set input-meta on')
	readline.parse_and_bind('set output-meta on')
	readline.parse_and_bind('set convert-meta off')
except ImportError:
	pass

from dotenv import load_dotenv
from openai import AzureOpenAI

load_dotenv(override=True)

api_version = os.getenv('AZURE_API_VERSION')
endpoint = os.getenv('AZURE_ENDPOINT')
subscription_key = os.getenv('AZURE_API_KEY')
deployment = os.getenv('AZURE_DEPLOYMENT')

WORKDIR = Path.cwd()
MEMORY_DIR = WORKDIR / '.memory'
MEMORY_INDEX = MEMORY_DIR / 'MEMORY.md'
TASKS_DIR = WORKDIR / '.tasks'
TASKS_DIR.mkdir(exist_ok=True)

client = AzureOpenAI(
	api_version=api_version,
	azure_endpoint=endpoint,
	api_key=subscription_key,
)
MODEL = deployment


@dataclass
class Task:
	id: str
	subject: str
	description: str
	status: str
	owner: str | None
	blockedBy: list[str]


def _task_path(task_id: str) -> Path:
	return TASKS_DIR / f'{task_id}.json'


def create_task(
	subject: str,
	description: str = '',
	blockedBy: list[str] | None = None,
) -> Task:
	task = Task(
		id=f'task_{int(time.time())}_{random.randint(0, 9999):04d}',
		subject=subject,
		description=description,
		status='pending',
		owner=None,
		blockedBy=blockedBy or [],
	)
	save_task(task)
	return task


def save_task(task: Task):
	_task_path(task.id).write_text(json.dumps(asdict(task), indent=2))


def load_task(task_id: str) -> Task:
	return Task(**json.loads(_task_path(task_id).read_text()))


def list_tasks() -> list[Task]:
	return [
		Task(**json.loads(path.read_text()))
		for path in sorted(TASKS_DIR.glob('task_*.json'))
	]


def get_task(task_id: str) -> str:
	task = load_task(task_id)
	return json.dumps(asdict(task), indent=2)


def can_start(task_id: str) -> bool:
	task = load_task(task_id)
	for dep_id in task.blockedBy:
		if not _task_path(dep_id).exists():
			return False
		if load_task(dep_id).status != 'completed':
			return False
	return True


def claim_task(task_id: str, owner: str = 'agent') -> str:
	task = load_task(task_id)
	if task.status != 'pending':
		return f'Task {task_id} is {task.status}, cannot claim'
	if not can_start(task_id):
		deps = [
			d for d in task.blockedBy
			if not _task_path(d).exists() or load_task(d).status != 'completed'
		]
		return f'Blocked by: {deps}'
	task.owner = owner
	task.status = 'in_progress'
	save_task(task)
	print(f'  \033[36m[claim] {task.subject} → in_progress (owner: {owner})\033[0m')
	return f'Claimed {task.id} ({task.subject})'


def complete_task(task_id: str) -> str:
	task = load_task(task_id)
	if task.status != 'in_progress':
		return f'Task {task_id} is {task.status}, cannot complete'
	task.status = 'completed'
	save_task(task)
	unblocked = [
		task.subject
		for task in list_tasks()
		if task.status == 'pending' and task.blockedBy and can_start(task.id)
	]
	print(f'  \033[32m[complete] {task.subject} ✓\033[0m')
	message = f'Completed {task.id} ({task.subject})'
	if unblocked:
		joined = ', '.join(unblocked)
		message += f'\nUnblocked: {joined}'
		print(f'  \033[33m[unblocked] {joined}\033[0m')
	return message


PROMPT_SECTIONS = {
	'identity': 'You are a coding agent. Act, don\'t explain.',
	'tools': (
		'Available tools: bash, read_file, write_file, '
		'create_task, list_tasks, get_task, claim_task, complete_task.'
	),
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
		result = subprocess.run(
			command,
			shell=True,
			cwd=WORKDIR,
			capture_output=True,
			text=True,
			encoding='utf-8',
			errors='replace',
			timeout=120,
		)
		output = (result.stdout + result.stderr).strip()
		return output[:50000] if output else '(no output)'
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


def run_create_task(
	subject: str,
	description: str = '',
	blockedBy: list[str] | None = None,
) -> str:
	task = create_task(subject, description, blockedBy)
	deps = f' (blockedBy: {", ".join(blockedBy)})' if blockedBy else ''
	print(f'  \033[34m[create] {task.subject}{deps}\033[0m')
	return f'Created {task.id}: {task.subject}{deps}'


def run_list_tasks() -> str:
	tasks = list_tasks()
	if not tasks:
		return 'No tasks. Use create_task to add some.'
	lines = []
	for task in tasks:
		icon = {
			'pending': '○',
			'in_progress': '●',
			'completed': '✓',
		}.get(task.status, '?')
		deps = f' (blockedBy: {", ".join(task.blockedBy)})' if task.blockedBy else ''
		owner = f' [{task.owner}]' if task.owner else ''
		lines.append(f'  {icon} {task.id}: {task.subject} [{task.status}]{owner}{deps}')
	return '\n'.join(lines)


def run_get_task(task_id: str) -> str:
	try:
		return get_task(task_id)
	except FileNotFoundError:
		return f'Error: Task {task_id} not found'


def run_claim_task(task_id: str) -> str:
	return claim_task(task_id, owner='agent')


def run_complete_task(task_id: str) -> str:
	return complete_task(task_id)


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
	{
		'type': 'function',
		'function': {
			'name': 'create_task',
			'description': 'Create a new task with optional blockedBy dependencies.',
			'parameters': {
				'type': 'object',
				'properties': {
					'subject': {'type': 'string'},
					'description': {'type': 'string'},
					'blockedBy': {
						'type': 'array',
						'items': {'type': 'string'},
					},
				},
				'required': ['subject'],
			},
		},
	},
	{
		'type': 'function',
		'function': {
			'name': 'list_tasks',
			'description': 'List all tasks with status, owner, and dependencies.',
			'parameters': {
				'type': 'object',
				'properties': {},
				'required': [],
			},
		},
	},
	{
		'type': 'function',
		'function': {
			'name': 'get_task',
			'description': 'Get full details of a specific task by ID.',
			'parameters': {
				'type': 'object',
				'properties': {'task_id': {'type': 'string'}},
				'required': ['task_id'],
			},
		},
	},
	{
		'type': 'function',
		'function': {
			'name': 'claim_task',
			'description': 'Claim a pending task. Sets owner, changes status to in_progress.',
			'parameters': {
				'type': 'object',
				'properties': {'task_id': {'type': 'string'}},
				'required': ['task_id'],
			},
		},
	},
	{
		'type': 'function',
		'function': {
			'name': 'complete_task',
			'description': 'Complete an in-progress task. Reports unblocked downstream tasks.',
			'parameters': {
				'type': 'object',
				'properties': {'task_id': {'type': 'string'}},
				'required': ['task_id'],
			},
		},
	},
]

TOOL_HANDLERS = {
	'bash': run_bash,
	'read_file': run_read,
	'write_file': run_write,
	'create_task': run_create_task,
	'list_tasks': run_list_tasks,
	'get_task': run_get_task,
	'claim_task': run_claim_task,
	'complete_task': run_complete_task,
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
		try:
			response = client.chat.completions.create(
				model=MODEL,
				messages=[{'role': 'system', 'content': system}] + messages,
				tools=TOOLS,
				max_completion_tokens=8000,
			)
		except Exception as e:
			messages.append({'role': 'assistant', 'content': f'[Error] {type(e).__name__}: {e}'})
			return

		assistant_msg = response.choices[0].message
		tool_calls = assistant_msg.tool_calls
		messages.append({
			'role': 'assistant',
			'content': assistant_msg.content,
			'tool_calls': [
				{
					'id': tool_call.id,
					'type': 'function',
					'function': {
						'name': tool_call.function.name,
						'arguments': tool_call.function.arguments,
					},
				}
				for tool_call in (tool_calls or [])
			] or None,
		})

		if response.choices[0].finish_reason != 'tool_calls' or not tool_calls:
			return

		for tool_call in tool_calls:
			name = tool_call.function.name
			args = json.loads(tool_call.function.arguments)
			print(f'\033[36m> {name}\033[0m')
			handler = TOOL_HANDLERS.get(name)
			output = handler(**args) if handler else f'Unknown: {name}'
			print(str(output)[:300])
			messages.append({
				'role': 'tool',
				'tool_call_id': tool_call.id,
				'content': str(output),
			})

		context = update_context(context, messages)
		system = get_system_prompt(context)


if __name__ == '__main__':
	print('learning12: task system')
	print('Enter a question, press Enter to send. Type q to quit.\n')
	history = []
	context = update_context({}, [])
	while True:
		try:
			query = input('\033[36mlearning12 >> \033[0m')
		except (EOFError, KeyboardInterrupt):
			break
		if query.strip().lower() in ('q', 'exit', ''):
			break
		history.append({'role': 'user', 'content': query})
		agent_loop(history, context)
		context = update_context(context, history)
		last = history[-1]
		if last.get('role') == 'assistant':
			content = last.get('content')
			if content:
				print(content)
		print()
