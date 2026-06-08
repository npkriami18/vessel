#!/usr/bin/env python3
'''
learning13: Background Tasks — thread-based async execution + notification injection.

Run: python learning13_background_tasks/code.py
Needs: pip install openai python-dotenv + Azure config in .env

Changes from learning12:
- threading.Thread for background execution
- background_tasks dict for lifecycle tracking (bg_id, command, status)
- background_results dict + threading.Lock for thread-safe storage
- should_run_background: model explicit request via run_in_background param
- is_slow_operation: fallback heuristic when model doesn't specify
- start_background_task: dispatch to daemon thread, return bg task id
- collect_background_results: gather completed, return as notifications
- agent_loop: slow ops -> background + placeholder, inject notifications
- Notifications use <task_notification> format, not reused tool_call_id

Note: Teaching code keeps a basic agent loop to stay focused on background
tasks. learning11's full error recovery is omitted here to keep this file
focused on the background task layer.
'''

import json
import os
import random
import subprocess
import threading
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


def run_bash(command: str, run_in_background: bool = False) -> str:
	_ = run_in_background
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
				'properties': {
					'command': {'type': 'string'},
					'run_in_background': {'type': 'boolean'},
				},
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
					'blockedBy': {'type': 'array', 'items': {'type': 'string'}},
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


_bg_counter = 0
background_tasks: dict[str, dict] = {}
background_results: dict[str, str] = {}
background_lock = threading.Lock()


def is_slow_operation(tool_name: str, tool_input: dict) -> bool:
	if tool_name != 'bash':
		return False
	command = tool_input.get('command', '').lower()
	slow_keywords = [
		'install',
		'build',
		'test',
		'deploy',
		'compile',
		'docker build',
		'pip install',
		'npm install',
		'cargo build',
		'pytest',
		'make',
	]
	return any(keyword in command for keyword in slow_keywords)


def should_run_background(tool_name: str, tool_input: dict) -> bool:
	if tool_input.get('run_in_background'):
		return True
	return is_slow_operation(tool_name, tool_input)


def execute_tool(tool_call) -> str:
	handler = TOOL_HANDLERS.get(tool_call.function.name)
	if handler:
		return handler(**json.loads(tool_call.function.arguments))
	return f'Unknown tool: {tool_call.function.name}'


def start_background_task(tool_call) -> str:
	global _bg_counter
	_bg_counter += 1
	bg_id = f'bg_{_bg_counter:04d}'
	arguments = json.loads(tool_call.function.arguments)
	command = arguments.get('command', tool_call.function.name)

	def worker():
		result = execute_tool(tool_call)
		with background_lock:
			background_tasks[bg_id]['status'] = 'completed'
			background_results[bg_id] = result

	with background_lock:
		background_tasks[bg_id] = {
			'tool_call_id': tool_call.id,
			'command': command,
			'status': 'running',
		}
	thread = threading.Thread(target=worker, daemon=True)
	thread.start()
	print(f'  \033[33m[background] dispatched {bg_id}: {command[:40]}\033[0m')
	return bg_id


def collect_background_results() -> list[str]:
	with background_lock:
		ready_ids = [
			bg_id
			for bg_id, task in background_tasks.items()
			if task['status'] == 'completed'
		]
	notifications = []
	for bg_id in ready_ids:
		with background_lock:
			task = background_tasks.pop(bg_id)
			output = background_results.pop(bg_id, '')
		summary = output[:200] if len(output) > 200 else output
		notifications.append(
			'<task_notification>\n'
			f'  <task_id>{bg_id}</task_id>\n'
			f'  <status>completed</status>\n'
			f'  <command>{task["command"]}</command>\n'
			f'  <summary>{summary}</summary>\n'
			'</task_notification>'
		)
		print(
			f'  \033[32m[background done] {bg_id}: '
			f'{task["command"][:40]} ({len(output)} chars)\033[0m'
		)
	return notifications


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
			api_messages = [{'role': 'system', 'content': system}] + messages
			response = client.chat.completions.create(
				model=MODEL,
				messages=api_messages,
				tools=TOOLS,
				max_completion_tokens=8000,
			)
		except Exception as e:
			messages.append({'role': 'assistant', 'content': f'[Error] {type(e).__name__}: {e}'})
			return

		choice = response.choices[0]
		assistant_msg = choice.message
		message_dict = {'role': 'assistant', 'content': assistant_msg.content}
		if assistant_msg.tool_calls:
			message_dict['tool_calls'] = [tool_call.model_dump() for tool_call in assistant_msg.tool_calls]
		messages.append(message_dict)

		if choice.finish_reason != 'tool_calls':
			return

		tool_calls = assistant_msg.tool_calls or []
		for tool_call in tool_calls:
			name = tool_call.function.name
			arguments = json.loads(tool_call.function.arguments)
			print(f'\033[36m> {name}\033[0m')

			if should_run_background(name, arguments):
				bg_id = start_background_task(tool_call)
				messages.append(
					{
						'role': 'tool',
						'tool_call_id': tool_call.id,
						'content': (
							f'[Background task {bg_id} started] '
							f'Command: {arguments.get("command", "")}. '
							'Result will be available when complete.'
						),
					}
				)
			else:
				output = execute_tool(tool_call)
				print(str(output)[:300])
				messages.append(
					{
						'role': 'tool',
						'tool_call_id': tool_call.id,
						'content': output,
					}
				)

		bg_notifications = collect_background_results()
		if bg_notifications:
			messages.append({'role': 'user', 'content': '\n'.join(bg_notifications)})
			print(f'  \033[32m[inject] {len(bg_notifications)} background notification(s)\033[0m')
		context = update_context(context, messages)
		system = get_system_prompt(context)


if __name__ == '__main__':
	print('learning13: background tasks')
	print('Enter a question, press Enter to send. Type q to quit.\n')
	history = []
	context = update_context({}, [])
	while True:
		try:
			query = input('\033[36mlearning13 >> \033[0m')
		except (EOFError, KeyboardInterrupt):
			break
		if query.strip().lower() in ('q', 'exit', ''):
			break
		history.append({'role': 'user', 'content': query})
		agent_loop(history, context)
		context = update_context(context, history)
		last = history[-1]
		if last.get('role') == 'assistant' and last.get('content'):
			print(last['content'])
		print()
