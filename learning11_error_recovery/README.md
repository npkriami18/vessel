# learning11: Error Recovery — Errors are normal, recovery is part of the loop

learning01 → ... → learning09 → learning10 → `learning11` → [learning12](../learning12_task_system/) → learning13 → ... → learning20
> *'errors are normal, recovery is part of the loop'* — retry transient failures, compact on overflow, continue after truncation.
>
> **Harness Layer**: Resilience — classify failures and recover without crashing the agent.

---

## The Problem

By learning10, the agent has tools, memory, context compaction, and runtime-assembled system prompts.

But one failure still breaks the whole run:

```text
Error: 529 overloaded
```

The process exits. No retry. No backoff. No model fallback. No context recovery.

That is fine for a toy script. It is not fine for a real agent.

In production, failures are normal. The most common ones are:

- **truncated output** — the model hits `max_tokens` before finishing
- **context overflow** — even after compaction, the prompt is still too large
- **transient API failures** — 429 rate limits, 529 overloads, network-style temporary errors

If the harness treats every error as fatal, long-running work becomes fragile. The missing capability is not another tool. It is a recovery layer around the model call.

---

## The Solution

![Error Recovery Overview](images/error-recovery-overview.en.svg)

learning11 keeps the learning10 loop shape: assemble the system prompt, call the model, execute tools, continue.

The new addition is a recovery wrapper around that loop.

Instead of one bare LLM call, the harness now handles three classes of failures with different strategies:

| Failure type | Signal | Recovery |
|-------------|--------|----------|
| Truncated output | `stop_reason == 'max_tokens'` | escalate token budget, then continue with a resume prompt |
| Context overflow | `prompt_too_long` | run reactive compact, then retry |
| Transient API failure | 429 / 529 | exponential backoff + jitter, fallback model after repeated overload |

The key idea is simple:

**not all errors mean the same thing, so they should not all be handled the same way.**

---

## How It Works

### Path 1: Recover from truncated output

Sometimes the model is doing the right work, but runs out of output budget before it can finish.

That shows up as:

```python
response.stop_reason == 'max_tokens'
```

The first recovery step is cheap: retry the exact same request with a larger output budget.

```python
if response.stop_reason == 'max_tokens':
	if not state.has_escalated:
		max_tokens = ESCALATED_MAX_TOKENS
		state.has_escalated = True
		continue
```

Important detail: on this first escalation, the truncated response is **not** appended to `messages`. The harness retries the same request with more room, so the model can finish cleanly.

If the larger budget still truncates, then the harness switches to continuation mode:

```python
if response.stop_reason == 'max_tokens':
	if not state.has_escalated:
		max_tokens = ESCALATED_MAX_TOKENS
		state.has_escalated = True
		continue
	messages.append({'role': 'assistant', 'content': response.content})
	if state.recovery_count < MAX_RECOVERY_RETRIES:
		messages.append({
			'role': 'user',
			'content': CONTINUATION_PROMPT,
		})
		state.recovery_count += 1
		continue
	return
```

So the strategy is:

1. **first truncation** → retry with more tokens
2. **later truncations** → preserve partial output and ask the model to resume directly
3. **too many continuations** → stop instead of looping forever

This keeps normal cases simple while still giving long outputs a path to completion.

### Path 2: Recover from context overflow

Even with the compaction pipeline from learning08, the request can still be too large.

That failure appears as `prompt_too_long`.

This is not a retry problem. Retrying the same oversized request changes nothing. The harness has to make the request smaller first.

So learning11 adds a reactive recovery branch:

```python
except PromptTooLongError:
	if not state.has_attempted_reactive_compact:
		messages[:] = reactive_compact(messages)
		state.has_attempted_reactive_compact = True
		continue
	return
```

`reactive_compact()` is the emergency path from learning08:

- save transcript state if needed
- summarize aggressively
- keep only compacted history plus a small recent tail

The teaching version uses a simplified implementation, but the control flow is the important part:

- compact once
- retry with smaller context
- if it still overflows, exit

That last step matters. Repeating the same emergency compact over and over just burns cycles.

### Path 3: Recover from transient failures

Some failures are temporary infrastructure problems, not prompt problems.

Typical examples:

- `429` rate limited
- `529` overloaded

These should usually be retried after a delay.

So the raw API call is wrapped in `with_retry()`:

```python
def retry_delay(attempt, retry_after=None):
	if retry_after:
		return retry_after
	base = min(500 * (2 ** attempt), 32000) / 1000
	return base + random.uniform(0, base * 0.25)


def with_retry(fn, state, max_retries=10):
	for attempt in range(max_retries):
		try:
			return fn()
		except (RateLimitError, OverloadedError) as e:
			delay = retry_delay(attempt, get_retry_after(e))
			time.sleep(delay)
			if is_overloaded_error(e):
				state.consecutive_529 += 1
				if state.consecutive_529 >= 3 and FALLBACK_MODEL:
					state.current_model = FALLBACK_MODEL
	raise MaxRetriesExceeded()
```

This gives the harness three protections:

1. **exponential backoff** — avoid hammering the API
2. **jitter** — avoid synchronized retry spikes
3. **fallback model switching** — after repeated overloads, move to another configured model

The base delay follows this pattern:

- attempt 1: ~0.5s
- attempt 2: ~1s
- attempt 3: ~2s
- capped at 32s, plus random jitter

If the server sends `Retry-After`, that value wins.

### Recovery state keeps the loop bounded

Recovery behavior needs memory across retries inside the same turn.

A small state object tracks that:

```python
@dataclass
class RecoveryState:
	current_model: str = MODEL
	has_escalated: bool = False
	recovery_count: int = 0
	has_attempted_reactive_compact: bool = False
	consecutive_529: int = 0
```

Without this state, the harness would not know:

- whether token escalation already happened
- how many continuation retries were used
- whether reactive compact already ran
- when to switch to the fallback model

This is what turns a one-shot error handler into a controlled recovery loop.

### Putting it together in the agent loop

All the pieces fit around the existing loop structure:

```python
def agent_loop(messages, context):
	system = get_system_prompt(context)
	state = RecoveryState()
	max_tokens = 8000

	while True:
		try:
			response = with_retry(
				lambda: client.messages.create(
					model=state.current_model,
					system=system,
					messages=messages,
					tools=TOOLS,
					max_tokens=max_tokens,
				),
				state,
			)
		except Exception as e:
			if is_prompt_too_long_error(e):
				if not state.has_attempted_reactive_compact:
					messages[:] = reactive_compact(messages)
					state.has_attempted_reactive_compact = True
					continue
			return

		if response.stop_reason == 'max_tokens':
			if not state.has_escalated:
				max_tokens = ESCALATED_MAX_TOKENS
				state.has_escalated = True
				continue
			messages.append({'role': 'assistant', 'content': response.content})
			messages.append({'role': 'user', 'content': CONTINUATION_PROMPT})
			state.recovery_count += 1
			continue

		messages.append({'role': 'assistant', 'content': response.content})

		if response.stop_reason != 'tool_use':
			return
		# ... tool execution ...
```

The structure stays readable because each failure type owns one branch:

- `with_retry()` handles temporary API failures
- outer `except` handles oversized prompts
- `stop_reason == 'max_tokens'` handles truncated output

That separation is the main design lesson in learning11.

---

## Changes From learning10

| Component | Before (learning10) | After (learning11) |
|-----------|-------------|-------------|
| Error handling | None | Recovery paths for truncation, overflow, and transient failures |
| New constants | — | `ESCALATED_MAX_TOKENS`, `MAX_RETRIES`, `BASE_DELAY_MS`, `FALLBACK_MODEL` |
| New functions | — | `with_retry`, `retry_delay`, `reactive_compact`, `is_prompt_too_long_error` |
| New state | — | `RecoveryState` |
| Tools | bash, read_file, write_file (3) | bash, read_file, write_file (3) — unchanged |
| Loop | Direct model call | Retry wrapper + recovery branches + bounded continuation |

---

## Try It

```sh
cd learn-claude-code
python learning11_error_recovery/code.py
```

What to watch for:

1. Long responses should first log a token escalation before switching to continuation behavior
2. Oversized context should trigger reactive compact instead of crashing immediately
3. 429 or 529 failures should show backoff timing and retry attempts
4. Repeated overloads should switch to the fallback model if configured

Try these prompts:

1. `Generate a very long implementation with tests and documentation for a small web server`
2. `Read many large files in sequence so the conversation grows quickly`
3. `Continue working even if the API overloads temporarily`

---

## What's Next

The agent can now survive common runtime failures. But it still treats work as one request in, one result out.

Real agent systems need a structured way to manage work over time:

- track multiple tasks
- express dependencies
- persist progress to disk
- resume later

learning12 Task System → tasks become persistent state, not just conversation intent.

<details>
<summary>Deep Dive Into CC Source Code</summary>

> The following is based on CC source code: `query.ts`, `services/api/withRetry.ts`, `query/tokenBudget.ts`, and `utils/tokenBudget.ts`.

### Reason/transition codes go beyond the teaching version

The teaching version focuses on the three most common recovery paths. CC has a much larger state machine around query execution, including transitions like:

- `completed`
- `next_turn`
- `max_output_tokens_escalate`
- `max_output_tokens_recovery`
- `reactive_compact_retry`
- `prompt_too_long`
- `collapse_drain_retry`
- `model_error`
- `image_error`
- `aborted_streaming`
- `aborted_tools`
- `stop_hook_blocking`
- `stop_hook_prevented`
- `hook_stopped`
- `token_budget_continuation`
- `blocking_limit`
- `max_turns`

The important lesson is not the exact count. It is that real harnesses classify failures and transitions explicitly instead of treating everything as one generic exception path.

### Backoff formula

CC's retry delay follows this form:

```text
delay = min(500 × 2^(attempt-1), 32000) + random(0~25%)
```

So retries grow quickly at first, then cap out.

| Attempt | Base delay | Jitter |
|--------|------------|--------|
| 1 | 500ms | 0-125ms |
| 2 | 1000ms | 0-250ms |
| 3 | 2000ms | 0-500ms |
| 4 | 4000ms | 0-1000ms |
| 7+ | 32000ms | 0-8000ms |

If the server returns `Retry-After`, that value takes precedence.

### Continuation prompt

A representative continuation prompt looks like this:

```text
Output token limit hit. Resume directly — no apology, no recap of what you were doing. Pick up mid-thought if that is where the cut happened. Break remaining work into smaller pieces.
```

That wording matters because it prevents the model from wasting output on summary or repetition.

### Why recovery belongs in the harness

These behaviors are not task-specific. They are infrastructure behaviors:

- retry policy
- token escalation
- fallback routing
- context recovery

So they belong in the harness layer, not in individual task prompts.

</details>
