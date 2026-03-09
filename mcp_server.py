from pydantic import Field
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.prompts import base

mcp = FastMCP("DocumentMCP", log_level="ERROR")

docs = {
    "document.md": "This is the markdown file",
    "report.pdf": "This is the pdf file",
    "notes.txt": "This is the text file",
}


@mcp.tool(
    name="read_document",
    description="Read the contents of the document and return it as a string",
)
def read_document(doc_id: str = Field(description="Id of the document to read")):
    if doc_id not in docs:
        raise ValueError(f"Doc with the id {doc_id} not found")
    return docs[doc_id]


@mcp.tool(
    name="edit_document",
    description="Edit the document by replacing the string in the documents content with new string",
)
def edit_document(
    doc_id: str = Field(description="Id of the document to edit"),
    old_str: str = Field(
        description="The text to replace. Must match exactly including white space"
    ),
    new_str: str = Field(description="The new text to insert in place of old text"),
):
    if doc_id not in docs:
        raise ValueError(f"Doc with the id {doc_id} not found")

    docs[doc_id] = docs[doc_id].replace(old_str, new_str)


@mcp.resource("docs://documents", mime_type="application/json")
def list_docs() -> list[str]:
    return list(docs.keys())


@mcp.resource("docs://documents/{doc_id}", mime_type="text/plain")
def fetch_doc(doc_id: str) -> str:
    if doc_id not in docs:
        raise ValueError(f"Doc with the id {doc_id} not found")
    return docs[doc_id]


@mcp.prompt(
    name="format", description="Rewrites the content of the documeny in Markdown format"
)
def format_document(
    doc_id: str = Field(description="Id of the document to format"),
) -> list[base.Message]:
    prompt = f"""
        Your goal is to reformat a document to be written with markdown syntax.

        The id of the document you need to reformat is:
        <document_id>
        {doc_id}
        </document_id>

        Add in headers, bullet points, tables, etc as necessary. Feel free to add in structure.
        Use the 'edit_document' tool to edit the document. After the document has been reformatted...
        """
    return [base.UserMessage(prompt)]


if __name__ == "__main__":
    mcp.run(transport="stdio")
