"""
MCP client: connects to the server (stdio), discovers capabilities,
and invokes tools/resources/prompts on behalf of the LLM.
Supports --chat mode with Ollama (e.g. deepseek-r1:1.5b).
"""

import argparse
import asyncio
import json
import sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import ollama


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
        tools.append({
            "type": "function",
            "function": {
                "name": t.name,
                "description": (t.description or "No description").strip(),
                "parameters": schema,
            },
        })
    return tools


def _ollama_chat_sync(model: str, messages: list[dict], tools: list[dict] | None) -> tuple:
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
                    "arguments": tc.function.arguments if isinstance(tc.function.arguments, dict) else json.loads(tc.function.arguments or "{}"),
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
    messages = []
    tools_disabled = False  # set True if model doesn't support tools

    print(f"\nChat with Ollama model: {model}")
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
                print("Note: This model doesn't support tool calling; continuing without MCP tools.\n")
            msg = response.message
            messages.append(_message_to_dict(msg))

            if not getattr(msg, "tool_calls", None):
                if msg.content:
                    print(f"Assistant: {msg.content}")
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
                messages.append({
                    "role": "tool",
                    "tool_name": name,
                    "content": text,
                })
                print(f"  [tool: {name}] -> {text[:80]}{'...' if len(text) > 80 else ''}")

        print()


async def main(server_path: str, chat: bool = False, model: str = "deepseek-r1:1.5b") -> None:
    server_params = StdioServerParameters(
        command=sys.executable, args=[server_path], env=None
    )
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            if chat:
                await run_chat_loop(session, model)
                return

            # Discover and print capabilities
            await list_capabilities(session)

            # Demo: list documents (resource file://documents) and optionally read one
            doc_ids = []
            try:
                result = await session.read_resource("file://documents")
                content = getattr(result, "contents", result)
                if content and len(content) > 0:
                    raw = content[0]
                    text = (
                        getattr(raw, "text", raw) if not isinstance(raw, str) else raw
                    )
                    doc_ids = (
                        json.loads(text)
                        if isinstance(text, str) and text.startswith("[")
                        else text
                    )
                    if isinstance(doc_ids, list) and doc_ids:
                        print(f"\nDocuments available: {doc_ids}")
                        # Read first document as demo
                        first = doc_ids[0]
                        res = await session.read_resource(f"file://documents/{first}")
                        c = getattr(res, "contents", [])
                        if c:
                            body = getattr(c[0], "text", c[0])
                            print(
                                f"  Read file://documents/{first} -> {str(body)[:200]}..."
                            )
            except Exception as e:
                print(f"\n(Resource demo skipped: {e})")

            # Demo: get a prompt (summarize_document) with a sample doc_id
            try:
                sample_doc_id = doc_ids[0] if doc_ids else "README.md"
                prompt_result = await session.get_prompt(
                    "summarize_document",
                    arguments={"doc_id": sample_doc_id},
                )
                messages = getattr(prompt_result, "messages", [])
                if messages:
                    m = messages[0]
                    msg_content = (
                        getattr(m, "content", m) if hasattr(m, "content") else str(m)
                    )
                    print(
                        f"\nPrompt 'summarize_document' (sample): {str(msg_content)[:150]}..."
                    )
            except Exception as e:
                print(f"\n(Prompt demo skipped: {e})")

            # Example: call a tool and parse the result (README: Session, Parsing Tool Results)
            result = await session.call_tool(
                "create_document", {"doc_name": "demo.txt", "content": "Hello"}
            )
            text = parse_tool_result(
                result
            )  # -> e.g. '{"status": "success", "document": "demo.txt"}'
            # Pass text to the LLM or use programmatically (e.g. json.loads(text))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MCP client: connect to server, list capabilities, or chat with Ollama + MCP tools.")
    parser.add_argument("server_path", nargs="?", default="mcp_server.py", help="Path to MCP server script (default: mcp_server.py)")
    parser.add_argument("--chat", action="store_true", help="Start interactive chat with Ollama using MCP tools")
    parser.add_argument("--model", default="deepseek-r1:1.5b", help="Ollama model for --chat (default: deepseek-r1:1.5b). For MCP tool use, pick a model that supports tools (e.g. qwen3, llama3.1).")
    args = parser.parse_args()
    asyncio.run(main(args.server_path, chat=args.chat, model=args.model))
