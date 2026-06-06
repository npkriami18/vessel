#!/usr/bin/env python3.12
"""
learning01_agent_loop.py - The Agent Loop

The entire secret of an AI coding agent in one pattern:

    while stop_reason == "tool_use":
        response = LLM(messages, tools)
        execute tools
        append results

This is the core loop: feed tool results back to the model
until the model decides to stop.
"""

import os
import json
import subprocess
from openai import AzureOpenAI
from dotenv import load_dotenv

load_dotenv(override=True)

# Azure OpenAI Configuration
api_version = os.getenv("AZURE_API_VERSION")
endpoint = os.getenv("AZURE_ENDPOINT")
subscription_key = os.getenv("AZURE_API_KEY")
deployment = os.getenv("AZURE_DEPLOYMENT")

client = AzureOpenAI(
    api_version=api_version,
    azure_endpoint=endpoint,
    api_key=subscription_key,
)

MODEL = deployment

SYSTEM = (
    f"You are a coding agent at {os.getcwd()}. "
    "Use bash to solve tasks. Act, don't explain."
)

# Tool definition
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string"
                    }
                },
                "required": ["command"]
            }
        }
    }
]


def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]

    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"

    try:
        r = subprocess.run(
            command,
            shell=True,
            cwd=os.getcwd(),
            capture_output=True,
            text=True,
            timeout=120,
        )

        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"

    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"

    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"


def agent_loop(messages: list):
    while True:
        # Add system prompt if not already present
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

        # No tool calls => final answer
        if not msg.tool_calls:
            final_text = msg.content or ""

            messages.append({
                "role": "assistant",
                "content": final_text,
            })

            return final_text

        # Save assistant tool-call message
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

        # Execute tool calls
        for tool_call in msg.tool_calls:
            if tool_call.function.name != "bash":
                continue

            try:
                args = json.loads(tool_call.function.arguments)
                command = args["command"]
            except Exception as e:
                output = f"Error parsing arguments: {e}"
            else:
                print(f"\033[33m$ {command}\033[0m")
                output = run_bash(command)
                print(output[:200])

            # Feed tool result back to model
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": output,
            })


if __name__ == "__main__":
    print("learning01: Agent loop")

    history = []

    while True:
        try:
            query = input("\033[36mlearning01 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break

        if query.strip().lower() in ("q", "exit", ""):
            break

        history.append({
            "role": "user",
            "content": query,
        })

        answer = agent_loop(history)

        if answer:
            print(answer)

        print()