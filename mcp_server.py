from mcp.server.fastmcp import FastMCP, Context
from mcp.server.fastmcp.prompts import base
from mcp.server.session import ServerSession
from pydantic import Field, BaseModel
import os

mcp = FastMCP("DocumentMCP", log_level="ERROR")

# Notes directory: use DOCUMENTS_DIR env or default ./docs (relative to CWD)
DOC_DIR = os.environ.get("DOCUMENTS_DIR", "./docs")


def _validate_doc_id(doc_id: str) -> None:
    """Reject path traversal and invalid names. Raises ValueError if invalid."""
    if not doc_id or not doc_id.strip():
        raise ValueError("Document id cannot be empty")
    if ".." in doc_id or os.sep in doc_id or (os.altsep and os.altsep in doc_id):
        raise ValueError("Document id cannot contain '..' or path separators")
    if os.path.isabs(doc_id):
        raise ValueError("Document id cannot be an absolute path")
    # Restrict to safe characters: letters, digits, dash, underscore, dot
    base = doc_id.strip()
    if not all(c.isalnum() or c in "-_." for c in base):
        raise ValueError("Document id may only contain letters, digits, '-', '_', and '.'")


def document_path(doc_id: str) -> str:
    _validate_doc_id(doc_id)
    return os.path.join(DOC_DIR, doc_id)


def document_exists(doc_id: str) -> bool:
    try:
        return os.path.exists(document_path(doc_id))
    except ValueError:
        return False


def _ensure_docs_dir() -> None:
    os.makedirs(DOC_DIR, exist_ok=True)


# Ensure notes directory exists when server loads
_ensure_docs_dir()


# -------------------------
# Completions (autocomplete for document IDs / resource URIs)
# Clients that support MCP completions can request suggestions as the user types.
# -------------------------


@mcp.completion()
async def document_completion(ctx: Context[ServerSession, None], partial: str):
    """Return completions for partial doc_id or URI (e.g. partial='meet' -> file://documents/meeting-notes.md)."""
    await ctx.info(f"Document completion for {partial}")
    await ctx.report_progress(0.25, 1.0, "document completion")
    try:
        _ensure_docs_dir()
        names = os.listdir(DOC_DIR)
    except OSError:
        return []
    completions = [
        base.Completion(
            text=f"file://documents/{doc_id}",
            kind="file",
            description=f"Document {doc_id}",
        )
        for doc_id in names
        if doc_id.startswith(partial)
    ]
    await ctx.report_progress(0.5, 1.0, "document completion")
    await ctx.debug(f"Document completions: {completions}")
    return completions


# -------------------------
# Resources
# -------------------------


@mcp.resource("file://documents")
def list_documents_resource():
    _ensure_docs_dir()
    return os.listdir(DOC_DIR)


@mcp.resource("file://documents/{doc_id}")
def read_document_resource(doc_id: str = Field(description="Document id")):
    if not document_exists(doc_id):
        raise ValueError(f"Document {doc_id} not found")
    with open(document_path(doc_id), "r", encoding="utf-8") as f:
        return {"doc_id": doc_id, "content": f.read()}


# -------------------------
# Tools
# -------------------------


@mcp.tool(name="list_documents")
async def list_documents(ctx: Context[ServerSession, None]):
    """List all note/document names in the notes store. Use this to see what notes exist before reading or editing."""
    _ensure_docs_dir()
    names = os.listdir(DOC_DIR)
    return {"documents": names, "count": len(names)}


@mcp.tool(name="read_document")
async def read_document(
    ctx: Context[ServerSession, None],
    doc_id: str = Field(description="Document id (filename) of the note to read"),
):
    """Read the full content of a note by its id. Use list_documents first to get valid doc_ids."""
    if not document_exists(doc_id):
        raise ValueError(f"Document {doc_id} not found")
    with open(document_path(doc_id), "r", encoding="utf-8") as f:
        content = f.read()
    return {"doc_id": doc_id, "content": content}


@mcp.tool(name="create_document")
async def create_document(
    ctx: Context[ServerSession, None],
    doc_name: str = Field(description="Filename for the note (e.g. meeting-notes.md)"),
    content: str = Field(description="Initial content of the note"),
):
    """Create a new note with the given name and content. Use a .md or .txt extension for clarity."""
    if document_exists(doc_name):
        raise ValueError(f"A note with name {doc_name} already exists")
    await ctx.info(f"creating note {doc_name}")
    with open(document_path(doc_name), "w", encoding="utf-8") as f:
        f.write(content)
    await ctx.debug(f"Document - {doc_name} created successfully")
    return {"status": "success", "document": doc_name}


@mcp.tool(name="edit_document")
async def edit_document(
    ctx: Context[ServerSession, None],
    doc_id: str = Field(description="Document id (filename) of the note to edit"),
    old_text: str = Field(description="Exact text to replace (first occurrence)"),
    new_text: str = Field(description="Replacement text"),
):
    """Replace the first occurrence of old_text with new_text in a note. Use read_document first to get exact text."""
    if not document_exists(doc_id):
        raise ValueError(f"Document {doc_id} not found")
    await ctx.info(f"Editing note {doc_id}")
    with open(document_path(doc_id), "r", encoding="utf-8") as f:
        data = f.read()
    updated = data.replace(old_text, new_text, 1)
    with open(document_path(doc_id), "w", encoding="utf-8") as f:
        f.write(updated)
    await ctx.debug(f"Document - {doc_id} Edited successfully")
    return {"status": "success", "document": doc_id}


@mcp.tool(name="update_document")
async def update_document(
    ctx: Context[ServerSession, None],
    doc_id: str = Field(description="Document id (filename) of the note to update"),
    content: str = Field(description="New full content for the note (overwrites entire note)"),
):
    """Overwrite a note entirely with new content. Use this when the user says 'update this note' or 'rewrite this note'."""
    if not document_exists(doc_id):
        raise ValueError(f"Document {doc_id} not found")
    await ctx.info(f"Updating note {doc_id}")
    with open(document_path(doc_id), "w", encoding="utf-8") as f:
        f.write(content)
    await ctx.debug(f"Document - {doc_id} updated successfully")
    return {"status": "success", "document": doc_id}


class DeleteConfirm(BaseModel):
    confirm: bool = Field(description="Confirm deletion")


@mcp.tool(name="delete_document")
async def delete_document(
    ctx: Context[ServerSession, None],
    doc_id: str = Field(description="Document id to delete"),
):
    """Delete a note. Uses elicitation: server asks the client for confirmation before deleting."""
    if not document_exists(doc_id):
        raise ValueError(f"Document {doc_id} not found")
    # Elicitation: client must prompt user and send accept/decline; then we delete or return deleted: false
    result = await ctx.elicit(
        message=f"Are you sure you want to delete the note '{doc_id}'?",
        schema=DeleteConfirm,
    )
    deleted = False
    if result.action == "accept":
        await ctx.info(f"Deleting document {doc_id}")
        os.remove(document_path(doc_id))
        deleted = True
    await ctx.debug(f"Document - {doc_id} deleted successfully")
    return {"doc_id": doc_id, "deleted": deleted}


# -------------------------
# Prompts
# -------------------------


@mcp.prompt(title="summarize_document")
def summarize_document(
    doc_id: str = Field(description="Document id to summarize"),
) -> list[base.Message]:
    prompt = f"""
            Summarize the following document.

            Resource:
            file://documents/{doc_id}

            Provide a concise summary.
            """
    return [base.UserMessage(prompt)]


@mcp.prompt(title="review_document")
def review_document(
    doc_id: str = Field(description="Document id to review"),
) -> list[base.Message]:
    prompt = f"""
            Review the following document and provide feedback.

            Resource:
            file://documents/{doc_id}
            """
    return [base.UserMessage(prompt)]


@mcp.prompt(title="improve_document")
def improve_document(
    doc_id: str = Field(description="Document id to improve"),
) -> list[base.Message]:
    prompt = f"""
            Improve the clarity, grammar, and structure of this document.

            Resource:
            file://documents/{doc_id}
            """
    return [base.UserMessage(prompt)]


if __name__ == "__main__":
    mcp.run(transport="stdio")
