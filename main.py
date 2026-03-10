"""
Entry point: run MCP client in chat mode with the local server and Ollama (qwen3:1.7b).
Equivalent to: uv run python mcp_client.py mcp_server.py --chat --model qwen3:1.7b
"""

import asyncio
from mcp_client import main as client_main

if __name__ == "__main__":
    asyncio.run(client_main("mcp_server.py", chat=True, model="qwen3:1.7b"))
