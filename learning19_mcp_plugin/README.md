# learning19: MCP Plugin — External tools, standard protocol

learning01 → ... → learning17 → learning18 → `learning19` → learning20
> *'external tools, standard protocol'* — discover, connect, invoke.
>
> **Harness Layer**: Plugins — external capabilities through a standard protocol.

---

## The Problem

By learning18, the harness can coordinate autonomous teammates and isolate their filesystem work with task-bound git worktrees.

That is enough for safe parallel work inside one repository.

It is not enough for tool extensibility.

So far, every tool in the harness has been built in directly:

- `bash`
- `read_file`
- `write_file`
- task tools
- worktree tools
- team tools

That works when the harness owns all capabilities itself.

But real environments often need tools that live outside the harness.

For example:

- a Jira service that can search tickets and create issues
- a deployment system that can trigger releases and fetch logs
- an internal docs service that can search knowledge bases
- a third-party API wrapper written in a different language

If every external integration requires writing new harness-native tool code by hand, the system becomes harder to extend.

Several limitations follow:

1. **tool growth becomes expensive** — every new service means more handwritten adapter code
2. **integration style is inconsistent** — each external service may be wrapped differently
3. **tool reuse is poor** — tools created for one agent environment are harder to share with another
4. **the harness is too tightly coupled to implementations** — the agent should care about tool interfaces, not how each service was written

What is missing is a standard way to connect externally provided tools so the harness can discover and use them dynamically.

---

## The Solution

![MCP Architecture](images/mcp-architecture.en.svg)

learning19 extends learning18 with MCP plugin support.

MCP, the Model Context Protocol, gives the harness a standard way to connect to external tool providers, discover their tool definitions, and expose those tools to the agent.

The teaching version keeps the mechanism intentionally small:

| Capability | learning19 approach |
|-----------|----------------------|
| external tool connection | `connect_mcp(name)` |
| tool discovery | `MCPClient` stores discovered tool definitions |
| tool invocation | MCP handlers are called through `call_tool(...)` |
| name collision avoidance | `mcp__server__tool` prefixed names |
| safety normalization | `normalize_mcp_name(...)` |
| tool assembly | `assemble_tool_pool()` rebuilds built-in + MCP tools |

This is the key shift:

**the harness no longer needs every tool to be authored directly inside the harness. it can connect external tool providers through a standard protocol.**

That separation matters:

- the harness owns the agent loop
- MCP servers own external capabilities
- the MCP client discovers what tools exist
- the assembled tool pool presents both built-in and external tools as one callable set

---

## How It Works

### Four-part MCP model

The teaching version relies on four connected pieces:

1. **MCP client** — stores discovered tool definitions and invokes handlers
2. **connection tool** — connects to a named mock server and registers its tools
3. **tool name normalization** — makes server and tool names safe for namespacing
4. **tool pool assembly** — combines built-in tools and MCP tools into one list for the model

Each piece is small.

Together they let the harness load external tools dynamically.

### MCPClient: Discover and invoke tools

The teaching version uses a small `MCPClient` object.

A simplified version looks like this:

```python
class MCPClient:
	def __init__(self, name: str):
		self.name = name
		self.tools: list[dict] = []
		self._handlers: dict[str, callable] = {}

	def register(self, tool_defs, handlers):
		self.tools = tool_defs
		self._handlers = handlers

	def call_tool(self, tool_name: str, args: dict) -> str:
		handler = self._handlers.get(tool_name)
		if not handler:
			return f"MCP error: unknown tool '{tool_name}'"
		return handler(**args)
```

In the tutorial, this simulates two MCP protocol ideas:

- **tool discovery** like `tools/list`
- **tool invocation** like `tools/call`

The real protocol would use JSON-RPC over a transport such as stdio.

The teaching version uses mock Python handlers so the whole flow stays easy to run locally.

### connect_mcp: Connect a server and make its tools available

The harness adds a new built-in tool, `connect_mcp`.

A simplified version looks like this:

```python
def connect_mcp(name: str) -> str:
	if name in mcp_clients:
		return f"MCP server '{name}' already connected"
	factory = MOCK_SERVERS.get(name)
	if not factory:
		return f"Unknown server '{name}'"
	mcp_client = factory()
	mcp_clients[name] = mcp_client
	return f"Connected to '{name}'"
```

The important behavior is:

- if the server is already connected, do nothing
- if the server name is unknown, fail clearly
- otherwise create an MCP client from a mock server factory
- store that client so its tools can be included in future tool assembly

After connection, the server's tools are eligible to appear in the assembled tool pool.

### normalize_mcp_name: Make names safe for namespacing

External server names and tool names may contain characters that are awkward or unsafe in a generated tool name.

So learning19 normalizes them.

A simplified version looks like this:

```python
_DISALLOWED_CHARS = re.compile(r'[^a-zA-Z0-9_-]')

def normalize_mcp_name(name: str) -> str:
	return _DISALLOWED_CHARS.sub('_', name)
```

The rule is simple:

- keep letters, numbers, `_`, and `-`
- replace all other characters with `_`

That prevents strange names from breaking the generated namespace.

### assemble_tool_pool: Rebuild one unified tool surface

The model should not need to care whether a tool is built-in or external.

So the harness assembles them into one tool pool.

A simplified version looks like this:

```python
def assemble_tool_pool() -> tuple[list[dict], dict]:
	tools = list(BUILTIN_TOOLS)
	handlers = dict(BUILTIN_HANDLERS)

	for server_name, mcp_client in mcp_clients.items():
		safe_server = normalize_mcp_name(server_name)
		for tool_def in mcp_client.tools:
			safe_tool = normalize_mcp_name(tool_def['name'])
			prefixed = f'mcp__{safe_server}__{safe_tool}'
			tools.append({...})
			handlers[prefixed] = (
				lambda *, c=mcp_client, t=tool_def['name'], **kw:
					c.call_tool(t, kw)
			)

	return tools, handlers
```

This is where two important ideas meet:

#### 1. Unified exposure

Built-in tools and MCP tools are presented together.

From the model's point of view, both are just callable tools.

#### 2. Collision-free names

Every discovered tool gets a generated name in this form:

```text
mcp__<server>__<tool>
```

For example:

- `mcp__docs__search`
- `mcp__deploy__trigger_release`

That prevents one server's `search` tool from colliding with another server's `search` tool.

### Dynamic tool pool means prompt cache must change too

Earlier chapters used prompt caching to avoid rebuilding unchanged state repeatedly.

learning19 introduces a new problem: the tool pool is no longer fixed.

If the agent calls `connect_mcp('docs')`, then new tools become available immediately after that call.

A stale cache would still describe the old tool set.

So the teaching version rebuilds tools and system prompt each loop:

```python
def agent_loop(messages, context):
	tools, handlers = assemble_tool_pool()
	system = assemble_system_prompt(context)
	...
	if connected_new_server:
		tools, handlers = assemble_tool_pool()
		system = assemble_system_prompt(context)
```

The key lesson is:

**when the available tool surface changes at runtime, any cached tool description can become invalid.**

The teaching version solves this by removing the cache rather than introducing a more complex invalidation system.

### Descriptions carry simple capability hints

The tutorial annotates MCP tool descriptions with labels like:

- `(readOnly)`
- `(destructive)`

These are just text hints in the teaching version.

They help show that external tools may differ in operational impact.

The tutorial does not implement a full MCP permission system around those annotations.

### Lead-only MCP access in the teaching version

learning19 keeps one simplification from a teaching perspective:

- the **lead** can connect MCP servers and use MCP tools
- **teammates** still use their smaller fixed built-in subset

This is not a fundamental limit of MCP.

It is a scope choice to keep the code path focused on connection, discovery, and dynamic tool assembly.

### Putting it together

A typical flow looks like this:

```text
1. the harness starts with only built-in tools
2. the user asks to connect the docs server
3. the agent calls connect_mcp('docs')
4. the harness creates an MCP client and stores discovered tools
5. assemble_tool_pool() rebuilds the tool list
6. new names like mcp__docs__search become available
7. the agent calls the discovered MCP tool just like any other tool
8. later, another server such as deploy can be connected too
9. both servers' tools coexist through prefixed names
```

The important result is that the harness now separates tool hosting from tool usage.

The harness runs the agent loop.

External MCP servers provide capabilities.

---

## Changes from learning18

| Component | Before | After |
|-----------|--------|-------|
| tool source | built-in tools only | built-in tools plus dynamically discovered MCP tools |
| tool pool | fixed set | rebuilt through `assemble_tool_pool()` |
| external integration model | handwritten harness tools | standard protocol-based external tools |
| name safety | no MCP name handling needed | `normalize_mcp_name()` for safe generated names |
| namespace strategy | built-in names only | `mcp__server__tool` prefixed names |
| runtime tool changes | mostly static | new tools can appear after `connect_mcp` |
| prompt caching | usable with stable tool set | removed in teaching version because tool set is dynamic |
| new built-in tool | none | `connect_mcp` |
| teammate access | fixed reduced subset | unchanged in teaching version; MCP stays lead-only |

---

## Try It

```sh
cd learn-claude-code
python learning19_mcp_plugin/code.py
```

Try prompts like:

1. `Connect to the docs MCP server and search for deployment notes`
2. `Connect to the deploy server and trigger a deployment`
3. `Connect both servers and list what tools are now available`
4. `Use an MCP docs tool after connecting, then connect another server and use one of its tools too`

What to observe:

- after connecting, do new tool names appear with `mcp__...` prefixes?
- can tools from multiple MCP servers coexist at the same time?
- are server names and tool names normalized into safe generated tool names?
- does the harness rebuild the available tool list after connection?
- do MCP tool descriptions include simple annotations like `(readOnly)` or `(destructive)`?

---

## What's Next

The harness can now connect external tool providers through a standard protocol.

But the first 19 chapters still present capabilities one by one as focused teaching slices.

A practical harness needs them to work together in one integrated loop.

The next step is to combine tools, permissions, hooks, tasking, teams, worktrees, memory, background work, and MCP into a single comprehensive harness.

learning20 will bring the earlier chapters together into one complete agent environment.

<details>
<summary>Deep Dive into CC Source</summary>

> Teaching note: learning19 presents MCP as a compact connect-discover-call flow. Claude Code supports a much broader MCP system, but the core teaching idea is the same: external tools become available through a shared protocol rather than through one-off harness integrations.

### 1. Multiple transport types in CC

The teaching version uses mock handlers instead of a real transport.

CC supports several MCP transport types, including stdio and network-based options.

That means real MCP servers can live:

- as subprocesses
- behind HTTP-style transports
- in other integration environments

The tutorial intentionally avoids this complexity so the discovery and invocation model stays easy to inspect.

### 2. Tool merging is more careful in production

The teaching version rebuilds a combined tool list in a straightforward way.

Production systems need extra details such as:

- deterministic ordering
- duplicate handling
- cache boundary placement
- filtering based on configuration or permissions

The tutorial keeps only the central semantic point: built-in and MCP tools must be assembled into one visible tool pool.

### 3. Naming convention matches the core production idea

The tutorial uses the generated name pattern:

```text
mcp__server__tool
```

and normalizes disallowed characters into underscores.

That mirrors the essential production naming idea: external tools need a stable, collision-resistant namespace.

### 4. Real permission handling is richer

The teaching version only adds textual hints such as `(readOnly)` and `(destructive)`.

A production MCP system can treat those capabilities more structurally and route them through a permission model.

The teaching chapter stops short of that so it can focus on external tool discovery first.

### 5. Real configuration is more layered

The tutorial connects a server by name from a simple mock registry.

Production systems usually load MCP server configuration from multiple sources with explicit precedence rules.

That configuration complexity is omitted here because it is not the core lesson of the chapter.

### 6. Real MCP systems also handle authentication and lifecycle issues

A production client typically needs to manage concerns such as:

- authentication
- token refresh
- connection retries
- timeouts
- disconnect handling
- server-originated notifications

The teaching version deliberately replaces all of that with local mocks so the MCP architecture can be studied without outside dependencies.

</details>
