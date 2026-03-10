from mcp.server.fastmcp import FastMCP, Context
from mcp.server.fastmcp.prompts import base
from mcp.server.session import ServerSession
from pydantic import Field, BaseModel
import os

mcp = FastMCP("DocumentMCP", log_level="ERROR")

DOC_DIR = "./docs"


def document_path(doc_id: str):
    return os.path.join(DOC_DIR, doc_id)


def document_exists(doc_id: str):
    return os.path.exists(document_path(doc_id))


# -------------------------
# Completions
# -------------------------


@mcp.completion()
async def document_completion(ctx: Context[ServerSession, None], partial: str):
    await ctx.info(f"Document completion for {partial}")
    await ctx.report_progress(0.25, 1.0, "document completion")
    completions = [
        base.Completion(
            text=f"file://documents/{doc_id}",
            kind="file",
            description=f"Document {doc_id}",
        )
        for doc_id in os.listdir(DOC_DIR)
        if doc_id.startswith(partial)
    ]
    await ctx.report_progress(0.5, 1.0, "document completion")
    await ctx.debug(f"Document completions: {completions}")
    return completions


# -------------------------
# Resources
# -------------------------


@mcp.resource("file://documents")
def list_documents():
    return os.listdir(DOC_DIR)


@mcp.resource("file://documents/{doc_id}")
def read_document(doc_id: str = Field(description="Document id")):
    if not document_exists(doc_id):
        raise ValueError(f"Document {doc_id} not found")

    with open(document_path(doc_id), "r", encoding="utf-8") as f:
        return {"doc_id": doc_id, "content": f.read()}


# -------------------------
# Tools
# -------------------------


@mcp.tool(name="create_document")
async def create_document(
    ctx: Context[ServerSession, None],
    doc_name: str = Field(description="Name of the document"),
    content: str = Field(description="Content of the document"),
):
    if document_exists(doc_name):
        raise ValueError(f"Document with name {doc_name} already exists")

    await ctx.info(f"creating document {doc_name}")
    with open(document_path(doc_name), "w", encoding="utf-8") as f:
        f.write(content)
    await ctx.debug(f"Document - {doc_name} created successfully")
    return {"status": "success", "document": doc_name}


@mcp.tool(name="edit_document")
async def edit_document(
    ctx: Context[ServerSession, None],
    doc_id: str = Field(description="Document id"),
    old_text: str = Field(description="Text to replace"),
    new_text: str = Field(description="Replacement text"),
):
    if not document_exists(doc_id):
        raise ValueError(f"Document {doc_id} not found")
    await ctx.info(f"Editing document {doc_id}")

    with open(document_path(doc_id), "r", encoding="utf-8") as f:
        data = f.read()
    await ctx.info(f"Reading document {doc_id}")
    await ctx.report_progress(0.25, 1.0, "reading file")
    updated = data.replace(old_text, new_text, 1)
    await ctx.info(f"Replacing text in document {doc_id}")
    await ctx.report_progress(0.5, 1.0, "replacing text")
    with open(document_path(doc_id), "w", encoding="utf-8") as f:
        f.write(updated)
    await ctx.info(f"Writing text to document {doc_id}")
    await ctx.report_progress(0.75, 1.0, "writing text")
    await ctx.debug(f"Document - {doc_id} Edited successfully")
    return {"status": "success", "document": doc_id}


class DeleteConfirm(BaseModel):
    confirm: bool = Field(description="Confirm deletion")


@mcp.tool(name="delete_document")
async def delete_document(
    ctx: Context[ServerSession, None],
    doc_id: str = Field(description="Document id to delete"),
):
    if not document_exists(doc_id):
        raise ValueError(f"Document {doc_id} not found")

    result = await ctx.elicit(
        message=(f"Are you sure you want to delete {doc_id} document"),
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
