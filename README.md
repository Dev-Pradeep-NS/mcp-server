# MCP Python SDK – Concepts with Examples

---

## Table of Contents

1. [Overview](#overview)
2. [Installation](#installation)
3. [Project Structure](#project-structure)
4. [Using as a notes app](#using-as-a-notes-app)
5. [Server](#server)
6. [Resources](#resources)
7. [Tools](#tools)
8. [Structured Output](#structured-output)
9. [Prompts](#prompts)
10. [Images](#images)
11. [Context](#context)
12. [Completions](#completions)
13. [Elicitation](#elicitation)
14. [Sampling](#sampling)
15. [Logging and Notifications](#logging-and-notifications)
16. [Authentication](#authentication)
17. [Session](#session)
18. [Request Context](#request-context)
19. [Running the Server](#running-the-server)
20. [HTTP Transport](#http-transport)
21. [ASGI Integration](#asgi-integration)
22. [SSE Servers](#sse-servers)
23. [Writing MCP Clients](#writing-mcp-clients)
24. [Parsing Tool Results](#parsing-tool-results)
25. [MCP Primitives](#mcp-primitives)
26. [Server Capabilities](#server-capabilities)

---

## Overview

**MCP (Model Context Protocol)** is an open standard that lets AI models (LLMs) interact with external tools, data sources, and services in a consistent way. Instead of each application building its own integrations, MCP defines how clients and servers communicate.

**Flow:**

```
User/App → LLM → MCP Client → MCP Server → Tools / Resources / Data
```

The LLM decides when to call tools or read resources; the MCP client talks to the MCP server; the server runs the actual logic (read files, call APIs, etc.).

---

## Installation

Install the official MCP Python SDK so you can build servers and clients.

**Using pip:**

```bash
pip install mcp
```

**Using uv (recommended for modern Python projects):**

```bash
uv add mcp
```

---

## Project Structure

A typical MCP-based Python project looks like this:

```
project/
├── mcp_server.py    # Defines tools, resources, prompts
├── mcp_client.py    # Connects to the server and calls it
└── docs/            # Optional: folder for document resources
```

- **mcp_server.py** – Your MCP server: registers tools, resources, and prompts.
- **mcp_client.py** – Connects to the server (e.g. via stdio or HTTP) and invokes tools/resources on behalf of the LLM.

---

## Using as a notes app

This project doubles as a **notes app**: the server stores notes as files in `./docs` (or the directory set by `DOCUMENTS_DIR`), and the client can list them or chat with an LLM that can create, read, edit, and delete notes via MCP tools.

**Quick start:**

```bash
# List all notes (uses list_documents tool)
uv run python mcp_client.py mcp_server.py --list-notes

# Chat with the notes assistant (Ollama required; uses create_document, read_document, edit_document, etc.)
uv run python mcp_client.py mcp_server.py --chat --model qwen3:1.7b
```

**Notes app features (all available in chat and via MCP tools):**

| What you want | Tool | Example |
|---------------|------|--------|
| **Write a note** | `create_document(doc_name, content)` | "Save this: ..." / "Create a note called ideas.md" |
| **See what notes I have** | `list_documents()` or client `--list-notes` | "What notes do I have?" / `python mcp_client.py mcp_server.py --list-notes` |
| **Read a note** | `read_document(doc_id)` | "Read meeting-notes.md" / "Show me my todo list" |
| **Update a note** | `update_document(doc_id, content)` | "Update shopping.txt with this: ..." / "Rewrite that note" |
| **Small edit in a note** | `edit_document(doc_id, old_text, new_text)` | "Change 'buy milk' to 'buy oat milk' in shopping.txt" |
| **Delete a note** | `delete_document(doc_id)` | "Delete old-notes.txt" (confirmation asked) |
| **Summarize / review / improve a note** | `read_document(doc_id)` then assistant responds | "Summarize meeting-notes.md" / "Review draft.txt" / "Improve this note" |

**Prompts (summarize_document, review_document, improve_document):**

The server exposes three **prompts** (message templates) for clients that call `get_prompt()`:

- **summarize_document**(doc_id) — template asking the LLM to summarize the document at `file://documents/{doc_id}`.
- **review_document**(doc_id) — template asking for feedback on the document.
- **improve_document**(doc_id) — template asking to improve clarity, grammar, and structure.

In **chat mode**, the assistant does not call these prompts directly; it uses `read_document(doc_id)` to get the content and then replies with a summary, review, or improved version in the conversation. So you can say "Summarize meeting-notes.md", "Review draft.txt", or "Improve the wording of ideas.md" and the assistant will read the note and respond accordingly (and can offer to save an improved version with `update_document`).

**Server behaviour:**

- Notes are stored under `./docs` by default; set `DOCUMENTS_DIR` to use another folder. The directory is created automatically if it does not exist.
- Document IDs (filenames) are validated: no `..`, path separators, or unsafe characters (only letters, digits, `-`, `_`, `.`).
- Tools: `list_documents`, `read_document`, `create_document`, `edit_document`, `update_document`, `delete_document`. Prompts: `summarize_document`, `review_document`, `improve_document` (for clients that use `get_prompt`; in chat the assistant uses `read_document` + its reply).

**Client behaviour:**

- Without flags: connects and prints server capabilities (tools, resources, prompts).
- `--list-notes`: calls `list_documents` and prints note names.
- `--chat`: starts an interactive notes assistant with a system prompt that encourages creating and managing notes; the model uses MCP tools to read/write the document store.

**Elicitation (user confirmation):**

The server uses MCP **elicitation** in one place: **delete_document**. Before deleting a note, the server calls `ctx.elicit(message="Are you sure...?", schema=DeleteConfirm)`. That sends a request to the client: “the tool needs user confirmation.” The client is supposed to prompt the user (e.g. “Delete note 'x'? [y/N]”), then send back **accept** or **decline**. If the client sends accept, the server deletes the file; otherwise it returns `deleted: false`.

- In **Cursor** (or other MCP hosts that support elicitation), the UI will show the confirmation and let you approve or cancel.
- In the **CLI chat client** (`--chat`), behavior depends on the MCP Python SDK: if `call_tool` blocks on elicitation, the client may need to handle the elicitation response and prompt the user. If the SDK does not yet support elicitation in the tool-call flow, delete may complete without confirmation in this client.

**Completions (autocomplete for document IDs):**

The server implements **document completion**: `@mcp.completion()` with a handler that, given a **partial** string (e.g. `"meet"` or `"file://documents/meet"`), returns matching note names as completions (e.g. `file://documents/meeting-notes.md` with kind `"file"` and a description). So when a client (e.g. Cursor, or a custom UI with a “document” or “resource” field) supports MCP completions, it can call the completion endpoint as the user types and show autocomplete suggestions from the actual notes in `./docs`. The **CLI client** in this repo does not use completions; it’s mainly for chat and `--list-notes`.

---

## Server

An **MCP server** is the process that exposes capabilities (tools, resources, prompts) to clients. You create it with **FastMCP** and then attach tools and resources to it.

**Example – minimal server:**

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP('DocumentServer')
# Add tools and resources here, then run the server
```

`'DocumentServer'` is the server name shown to clients. You then register tools and resources on `mcp` and finally run it (e.g. `mcp.run()`).

---

## Resources

**Resources** are read-only data that the server exposes via URIs. Clients (and thus the LLM) can “read” them without changing state. Typical use: documents, configs, or any static/semi-static data.

**Example – document resource:**

```python
@mcp.resource('file://documents/{doc_id}')
def read_document(doc_id: str):
    with open(f'./docs/{doc_id}', encoding='utf-8') as f:
        return f.read()
```

- **URI template:** `file://documents/{doc_id}` – e.g. `file://documents/notes.txt` → `doc_id = "notes.txt"`.
- The function returns the document body (e.g. string). The client can pass this to the LLM as context.

---

## Tools

**Tools** perform actions (side effects): delete a file, send an email, run a script, etc. They are invoked by the client when the LLM decides to use them.

**Example – delete document tool:**

```python
import os

@mcp.tool()
def delete_document(doc_id: str) -> str:
    path = f'./docs/{doc_id}'
    if os.path.exists(path):
        os.remove(path)
        return 'deleted'
    return 'file not found'
```

- `@mcp.tool()` registers the function as a tool; the client can call it by name with arguments (e.g. `doc_id`).
- Return value is sent back to the client/LLM (e.g. to confirm the action).

---

## Structured Output

Tools and resources can return **structured data** (e.g. JSON) so the client or LLM can parse it reliably.

**Example – returning a dict:**

```python
@mcp.tool()
def save_document(doc_id: str, content: str):
    path = f'./docs/{doc_id}'
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    return {'status': 'success', 'file': doc_id}
```

The client receives this structure and can pass it to the LLM or use it programmatically (e.g. `result['file']`).

---

## Prompts

**Prompts** are reusable prompt templates the server exposes. When a client requests a prompt by name (and arguments), the server returns a list of messages (e.g. system + user) that the client can send to the LLM. Useful for “summarize this”, “explain this doc”, etc.

**Example – summarize document prompt:**

```python
from mcp.types import UserMessage

@mcp.prompt()
def summarize_document(doc_id: str):
    return [
        UserMessage(content=f'Summarize the document at file://documents/{doc_id}')
    ]
```

When the user selects “Summarize document” and chooses `doc_id`, the client requests this prompt, gets the message list, and sends it to the LLM so it can use the corresponding resource (e.g. `file://documents/notes.txt`) for context.

---

## Images

Tools and resources can return **image data** (e.g. PNG, JPEG) in addition to text. Use cases: charts, screenshots, generated diagrams. The client can display them or pass them to a vision-capable LLM.

**Example idea:**

- A tool that generates a chart and returns it as base64 or a blob.
- A resource with URI like `image://reports/chart.png` that returns image bytes.

The exact return format (e.g. base64 in JSON, or binary in a specific MCP content type) depends on the MCP version and client support.

---

## Context

The **context** object (often `ctx`) is passed into tools (and some other handlers) and provides information about the current request and ways to interact with the session (e.g. logging).

**Example – using context in a tool:**

```python
from mcp.server.context import Context

@mcp.tool()
def my_tool(ctx: Context, query: str):
    ctx.log('Running tool with query: %s', query)
    # ... do work ...
    return {'result': 'done'}
```

- **ctx.log()** – Send progress or debug messages to the client (useful for long-running tools).
- You may also get **request_id** or other metadata from `ctx` depending on the SDK version.

---

## Completions

**Completions** let the server suggest possible values for a parameter (e.g. file names, document IDs). When the user types part of an input, the client can ask the server for suggestions and show autocomplete.

**Example idea:**

- User types `doc` in a “document id” field.
- Client calls a completion endpoint with partial input `doc`.
- Server returns suggestions: `doc1.txt`, `doc2.txt`, `notes.doc`.

So the server owns the list of valid or recommended options (e.g. from the `docs/` folder).

---

## Elicitation

**Elicitation** means the server (or client flow) asks the user for missing or ambiguous input before running a tool. For example, a “delete document” tool might need `doc_id`; if the user didn’t specify it, the system can prompt: “Which document should I delete?” and offer a list. This keeps tools simple while still guiding the user.

---

## Sampling

**Sampling** is when the client asks the LLM to **generate** content (e.g. text, code). For example: “Generate a summary of the document at file://documents/notes.txt.” The client sends the prompt (and any resource content) to the LLM and returns the model’s response. So “sampling” is the act of using the model to produce that response.

---

## Logging and Notifications

Servers can send **progress or status messages** to the client during a long-running tool so the UI can show “Processing…”, “Step 2/5”, etc.

**Example:**

```python
@mcp.tool()
def long_task(ctx: Context):
    ctx.log('Processing...')
    # ... step 1 ...
    ctx.log('Halfway there...')
    # ... step 2 ...
    ctx.log('Done.')
    return 'completed'
```

The client can display these messages in a log panel or status bar.

---

## Authentication

MCP servers can be **secured** so only authorized clients can connect. Common approaches:

- **API key** – Client sends a key in headers or config; server validates it.
- **OAuth** – Client obtains a token and sends it; server validates the token.

Configuration is usually done in the transport layer (e.g. HTTP middleware or environment variables) rather than inside individual tools.

---

## Session

A **session** is the active connection between one MCP client and one MCP server. Over a session, the client can call tools, read resources, and request prompts multiple times.

**Example – client calling a tool in a session:**

```python
# Pseudo-code on the client side
result = session.call_tool('read_document', {'doc_id': 'notes.txt'})
```

The client keeps the session open and reuses it for many requests instead of reconnecting each time.

---

## Request Context

Each request to the server can carry **metadata** (e.g. request ID, user ID). This is often available on the **request context** (e.g. `ctx`) so tools can log or trace requests.

**Example:**

```python
@mcp.tool()
def my_tool(ctx: Context):
    request_id = getattr(ctx, 'request_id', None)
    ctx.log('Request ID: %s', request_id)
    return 'ok'
```

Exact attribute names depend on the MCP Python SDK version (e.g. `ctx.request_id` if provided).

---

## Running the Server

After defining the server and registering tools/resources, you **run** it. The most common transport for local use is **stdio**: the server reads input and writes output on standard streams, and the client spawns it as a subprocess.

**Example – run with stdio:**

```python
if __name__ == '__main__':
    mcp.run(transport='stdio')
```

For other transports (e.g. HTTP), you’d use the appropriate method or integration (see below).

---

## HTTP Transport

You can run the MCP server over **HTTP** so remote clients can connect without sharing a process.

**Example – server URL:**

```
http://localhost:8000/mcp
```

The server would be started with an HTTP transport (e.g. uvicorn or the SDK’s HTTP runner) and might expose the MCP endpoint at `/mcp`. The client then connects to this URL instead of using stdio.

---

## ASGI Integration

You can **mount** the MCP server inside an **ASGI** application (e.g. FastAPI) so the same process serves both your API and MCP.

**Example – FastAPI:**

```python
from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP

mcp = FastMCP('MyServer')
# ... register tools, resources ...

app = FastAPI()
mcp_app = mcp.get_asgi_app()  # or equivalent in your SDK version
app.mount('/mcp', mcp_app)
```

Then the MCP endpoint is available at `http://localhost:8000/mcp` (or whatever port you use).

---

## SSE Servers

**Server-Sent Events (SSE)** let the server push updates to the client over a long-lived HTTP connection. Useful for streaming progress, logs, or partial results. An MCP server can support an SSE transport so clients receive real-time updates during tool execution.

---

## Writing MCP Clients

**MCP clients** run in your application (or in an AI assistant host like Cursor). They connect to an MCP server and translate LLM decisions into tool/resource calls, then pass results back to the LLM.

**Flow:**

```
User → LLM → MCP Client → MCP Server (tools/resources)
                ↑___________________________|
```

The client is responsible for: connecting (stdio subprocess or HTTP), sending requests (e.g. `call_tool`, `read_resource`), and returning the server’s response to the LLM.

---

## Parsing Tool Results

When the client calls a tool, the server returns a result. The client typically parses this and passes it to the LLM or your app. Results often have a **content** field (text or structured).

**Example – client-side parsing:**

```python
# After session.call_tool('read_document', {'doc_id': 'notes.txt'})
result = response  # from server
content = result.get('content', [])
# content might be a list of parts, e.g. [{'type': 'text', 'text': '...'}]
text = content[0]['text'] if content else ''
```

Exact shape depends on the MCP SDK and version; the idea is to read the structured result (e.g. `result['content']`) and extract text or data for the LLM.

---

## MCP Primitives

The core building blocks of MCP are:

| Primitive       | Purpose                                       |
| --------------- | --------------------------------------------- |
| **Tools**       | Perform actions (side effects).               |
| **Resources**   | Expose read-only data by URI.                 |
| **Prompts**     | Reusable prompt templates (message lists).    |
| **Sampling**    | Ask the model to generate content.            |
| **Elicitation** | Ask the user for missing or clarifying input. |

Servers implement one or more of these; clients use them to give the LLM access to your backend and data.

---

## Server Capabilities

When a client connects, the server **declares** what it supports (tools, resources, prompts, etc.). This is called **capabilities**. The client uses this to know what it can call (e.g. which tool names exist, which resource URI templates are available). In the MCP Python SDK, registering a tool/resource/prompt with `mcp` automatically advertises it in the server’s capabilities.

---

_This guide summarizes the main MCP concepts and gives minimal examples. For full details and API changes, refer to the official [MCP documentation](https://modelcontextprotocol.io) and the [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) on GitHub._
