"""
MCP client: connects to the server (stdio), discovers capabilities,
and invokes tools/resources/prompts on behalf of the LLM.
Supports --chat mode with Ollama (e.g. qwen3:1.7b) and --list-notes for a notes app.
"""

import argparse
import asyncio
import json
import re
import sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import ollama

NOTES_SYSTEM_PROMPT = """You are a helpful notes assistant. You have access to a document store (notes) via tools:

- list_documents: see what notes exist (returns all note filenames)
- read_document(doc_id): read the full content of a note
- create_document(doc_name, content): write a new note (e.g. "meeting-notes.md" and content)
- edit_document(doc_id, old_text, new_text): replace one snippet in a note (first occurrence)
- update_document(doc_id, content): overwrite a note entirely with new content (use for "update this note")
- delete_document(doc_id): delete a note (user will be asked to confirm)

When the user says: write a note / save this / create a note -> use create_document.
When they say: what notes do I have? / list my notes -> use list_documents.
When they say: read X / show me X -> use read_document.
When they say: update this note / change this to / rewrite -> use update_document (or edit_document for a small change).
When they say: delete this note / remove X -> use delete_document.

For summarize / review / improve: use read_document(doc_id) to get the note content, then in your response:
- Summarize document: give a concise summary of the content.
- Review document: give feedback (clarity, structure, suggestions).
- Improve document: output an improved version (better clarity, grammar, structure); offer to save it with update_document if the user wants.

Always use list_documents or read_document when you need to find or show existing notes."""


def parse_tool_result(result) -> str:
    """Extract text from a tool result for the LLM or app (README: Parsing Tool Results)."""
    content = getattr(result, "content", []) or []
    if not content:
        return ""
    part = content[0]
    if hasattr(part, "text"):
        return part.text
    if isinstance(part, dict):
        return part.get("text", "")
    return str(part)


def _strip_think(content: str) -> str:
    """Remove <think>...</think> blocks from model output (e.g. qwen3 reasoning)."""
    if not content:
        return content
    return re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()


async def list_capabilities(session: ClientSession) -> None:
    """List the server's tools, resources, and prompts (capabilities)."""
    tools = (await session.list_tools()).tools
    resources = (await session.list_resources()).resources
    prompts = (await session.list_prompts()).prompts

    print("MCP Server capabilities")
    print("=" * 50)
    print(f"\nTools ({len(tools)}):")
    for t in tools:
        desc = (t.description or "No description").strip().split("\n")[0]
        print(f"  - {t.name}: {desc}")
    print(f"\nResources ({len(resources)}):")
    for r in resources:
        uri = getattr(r, "uri", r) if not isinstance(r, str) else r
        print(f"  - {uri}")
    print(f"\nPrompts ({len(prompts)}):")
    for p in prompts:
        desc = (
            (getattr(p, "description", None) or "No description").strip().split("\n")[0]
        )
        print(f"  - {getattr(p, 'name', p)}: {desc}")
    print("=" * 50)


async def mcp_tools_to_ollama(session: ClientSession) -> list[dict]:
    """Convert MCP server tools to Ollama tools schema."""
    response = await session.list_tools()
    tools = []
    for t in response.tools:
        schema = getattr(t, "inputSchema", None) or {"type": "object", "properties": {}}
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": (t.description or "No description").strip(),
                    "parameters": schema,
                },
            }
        )
    return tools


def _ollama_chat_sync(
    model: str, messages: list[dict], tools: list[dict] | None
) -> tuple:
    """Sync Ollama chat (run in thread to avoid blocking). Returns (response, tools_used)."""
    if tools:
        try:
            return ollama.chat(model=model, messages=messages, tools=tools), True
        except Exception as e:
            if "does not support tools" in str(e) or "400" in str(e):
                return ollama.chat(model=model, messages=messages), False
            raise
    return ollama.chat(model=model, messages=messages), False


def _message_to_dict(msg) -> dict:
    """Convert Ollama response.message to a dict for the messages list."""
    d = {"role": "assistant", "content": msg.content or ""}
    if getattr(msg, "tool_calls", None):
        d["tool_calls"] = [
            {
                "type": "function",
                "function": {
                    "index": i,
                    "name": tc.function.name,
                    "arguments": tc.function.arguments
                    if isinstance(tc.function.arguments, dict)
                    else json.loads(tc.function.arguments or "{}"),
                },
            }
            for i, tc in enumerate(msg.tool_calls)
        ]
    return d


async def run_chat_loop(session: ClientSession, model: str) -> None:
    """Interactive chat with Ollama; MCP tool calls are executed via the session."""
    tools = await mcp_tools_to_ollama(session)
    if not tools:
        print("No tools exposed by the MCP server; chat will not use tools.")
    messages = [{"role": "system", "content": NOTES_SYSTEM_PROMPT}]
    tools_disabled = False  # set True if model doesn't support tools

    print(f"\nNotes assistant (Ollama: {model})")
    print("MCP tools are available. Type 'quit' to exit.\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not user_input:
            continue
        if user_input.lower() == "quit":
            break

        messages.append({"role": "user", "content": user_input})

        active_tools = None if tools_disabled else tools
        while True:
            response, tools_used = await asyncio.to_thread(
                _ollama_chat_sync, model, messages, active_tools
            )
            if not tools_used and active_tools and not tools_disabled:
                tools_disabled = True
                print(
                    "Note: This model doesn't support tool calling; continuing without MCP tools.\n"
                )
            msg = response.message
            messages.append(_message_to_dict(msg))

            if not getattr(msg, "tool_calls", None):
                if msg.content:
                    print(f"Assistant: {_strip_think(msg.content)}")
                break

            # Execute each tool call via MCP
            for tc in msg.tool_calls:
                name = tc.function.name
                args = tc.function.arguments
                if isinstance(args, str):
                    args = json.loads(args or "{}")
                try:
                    result = await session.call_tool(name, args)
                    text = parse_tool_result(result)
                except Exception as e:
                    text = str(e)
                messages.append(
                    {
                        "role": "tool",
                        "tool_name": name,
                        "content": text,
                    }
                )
                print(
                    f"  [tool: {name}] -> {text[:80]}{'...' if len(text) > 80 else ''}"
                )

        print()


async def list_notes(session: ClientSession) -> None:
    """Call list_documents tool and print note names."""
    try:
        result = await session.call_tool("list_documents", {})
        text = parse_tool_result(result)
        data = json.loads(text) if text.strip().startswith("{") else {}
        docs = data.get("documents", [])
        count = data.get("count", len(docs))
        if not docs:
            print("No notes yet. Use --chat to create notes with the assistant.")
            return
        print(f"Notes ({count}):")
        for name in sorted(docs):
            print(f"  - {name}")
    except Exception as e:
        print(f"Could not list notes: {e}")


async def main(
    server_path: str,
    chat: bool = False,
    list_notes_mode: bool = False,
    model: str = "qwen3:1.7b",
) -> None:
    server_params = StdioServerParameters(
        command=sys.executable, args=[server_path], env=None
    )
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            if list_notes_mode:
                await list_notes(session)
                return
            if chat:
                await run_chat_loop(session, model)
                return

            await list_capabilities(session)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="MCP client: connect to server, list capabilities, or chat with Ollama + MCP tools."
    )
    parser.add_argument(
        "server_path",
        nargs="?",
        default="mcp_server.py",
        help="Path to MCP server script (default: mcp_server.py)",
    )
    parser.add_argument(
        "--chat",
        action="store_true",
        help="Start interactive chat with Ollama (notes assistant with MCP tools)",
    )
    parser.add_argument(
        "--list-notes",
        action="store_true",
        help="List all notes (document names) and exit",
    )
    parser.add_argument(
        "--model",
        default="qwen3:1.7b",
        help="Ollama model for --chat (default: qwen3:1.7b). For MCP tool use, pick a model that supports tools (e.g. qwen3:1.7b, llama3.1).",
    )
    args = parser.parse_args()
    asyncio.run(
        main(
            args.server_path,
            chat=args.chat,
            list_notes_mode=args.list_notes,
            model=args.model,
        )
    )
