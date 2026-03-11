import os
import requests
import subprocess
from functools import lru_cache
from mcp.server.fastmcp import FastMCP, Context
from mcp.server.session import ServerSession
from pydantic import Field, BaseModel
from dotenv import load_dotenv


mcp = FastMCP(name="git-server", log_level="ERROR")
load_dotenv()

# Base directory for all repo operations; paths are validated against this.
BASE_REPO_DIR = os.path.abspath(os.getenv("GIT_MCP_BASE_DIR", os.getcwd()))
# Timeout for git and subprocess calls (seconds).
GIT_CMD_TIMEOUT = int(os.getenv("GIT_MCP_TIMEOUT", "30"))
# GitHub API version for REST requests.
GITHUB_API_VERSION = "2022-11-28"

# SSH passphrase: when set, git/SSH use SSH_ASKPASS so clone/push/pull work without a TTY.
_GIT_MCP_DIR = os.path.dirname(os.path.abspath(__file__))
_SSH_ASKPASS_HELPER = os.path.join(_GIT_MCP_DIR, "ssh_askpass_helper.sh")


def _git_env(passphrase_override: str | None = None) -> dict[str, str]:
    """Environment for git subprocess. Uses SSH_ASKPASS when passphrase is from env or elicited."""
    env = os.environ.copy()
    has_passphrase = (
        passphrase_override is not None or os.getenv("SSH_KEY_PASSPHRASE")
    )
    if has_passphrase and os.path.isfile(_SSH_ASKPASS_HELPER):
        env["SSH_ASKPASS"] = _SSH_ASKPASS_HELPER
        env.setdefault("SSH_ASKPASS_REQUIRE", "force")
        env.setdefault("DISPLAY", " ")
        if passphrase_override is not None:
            env["GIT_MCP_SSH_PASSPHRASE"] = passphrase_override
    return env


def _resolve_repo_path(repo_or_path: str) -> tuple[str | None, str | None]:
    """
    Resolve a repo name or path to an absolute path under BASE_REPO_DIR.
    Returns (resolved_path, None) or (None, error_message).
    """
    if not repo_or_path or ".." in repo_or_path:
        return None, "Invalid path: empty or contains '..'"
    candidate = os.path.abspath(os.path.join(BASE_REPO_DIR, repo_or_path))
    if not candidate.startswith(BASE_REPO_DIR):
        return None, "Invalid path: must be under base directory"
    return candidate, None


def _ensure_git_repo(repo_path: str) -> str | None:
    """Return None if repo_path is a git repo, else an error message."""
    if not os.path.isdir(repo_path):
        return "Not a directory"
    if not os.path.exists(os.path.join(repo_path, ".git")):
        return "Not a git repository"
    return None


def _is_sensitive_path(relative_path: str) -> bool:
    """Return True if the path is sensitive and may not be edited (.git, .env, SSH keys, credentials)."""
    normalized = os.path.normpath(relative_path).replace("\\", "/").lstrip("/")
    parts = normalized.split("/")
    for i, part in enumerate(parts):
        segment = "/".join(parts[: i + 1])
        if segment == ".git" or segment.startswith(".git/"):
            return True
        if part == ".env" or (part.startswith(".env.") and part != ".env.example"):
            return True
        lower = part.lower()
        if lower in ("id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"):
            return True
        if "credentials" in lower and not lower.endswith(".example"):
            return True
    return False


def _resolve_repo_path_and_ensure_git(
    repo_or_path: str,
) -> tuple[str | None, str | None]:
    """
    Resolve path and ensure it is a git repo.
    Returns (resolved_path, None) or (None, error_message).
    """
    path, err = _resolve_repo_path(repo_or_path)
    if err:
        return None, err
    err = _ensure_git_repo(path)
    if err:
        return None, err
    return path, None


def _run_git(
    args: list[str],
    cwd: str,
    capture_output: bool = True,
    passphrase_override: str | None = None,
) -> tuple[int, str, str]:
    """Run a git command; returns (returncode, stdout, stderr). Uses SSH_ASKPASS when passphrase is set or elicited."""
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=capture_output,
            text=True,
            timeout=GIT_CMD_TIMEOUT,
            env=_git_env(passphrase_override),
        )
        return r.returncode, (r.stdout or ""), (r.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, "", f"Command timed out after {GIT_CMD_TIMEOUT}s"


def _status_porcelain(repo_path: str) -> tuple[str | None, str]:
    """
    Run git status --porcelain in repo_path.
    Returns (error_message, output). error_message is None on success.
    """
    path, err = _resolve_repo_path_and_ensure_git(repo_path)
    if err:
        return err, ""
    code, out, err_out = _run_git(["status", "--porcelain"], cwd=path)
    if code != 0:
        return err_out or "git status failed", ""
    return None, out


def _fetch_github_repos(use_cache: bool = False):
    """Fetch all repos from GitHub API with pagination. Optionally use cache."""
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        return {"error": "GITHUB_TOKEN environment variable not set"}

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }

    all_repos = []
    page = 1
    per_page = 100

    while True:
        url = f"https://api.github.com/user/repos?visibility=all&per_page={per_page}&page={page}"
        response = requests.get(url, headers=headers, timeout=GIT_CMD_TIMEOUT)

        if response.status_code != 200:
            return {"error": f"Failed to fetch repos: {response.text}"}

        try:
            repos_data = response.json()
        except ValueError:
            return {"error": "Invalid JSON response from GitHub API"}

        if not isinstance(repos_data, list):
            return {"error": "Unexpected response format: expected list of repos"}

        if not repos_data:
            break

        for repo in repos_data:
            all_repos.append(
                {
                    "name": repo["name"],
                    "private": repo["private"],
                    "ssh_url": repo["ssh_url"],
                    "default_branch": repo["default_branch"],
                }
            )
        if len(repos_data) < per_page:
            break
        page += 1

    return all_repos


def _github_headers() -> dict[str, str]:
    """Headers for GitHub REST API. Requires GITHUB_TOKEN."""
    return {
        "Authorization": f"Bearer {os.getenv('GITHUB_TOKEN', '')}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }


def _get_owner_repo(repo_path: str) -> tuple[str | None, str | None, str | None]:
    """
    Resolve repo path to (owner, repo, error). Uses git remote get-url origin.
    Returns (owner, repo, None) or (None, None, error_message).
    """
    path, err = _resolve_repo_path_and_ensure_git(repo_path)
    if err:
        return None, None, err
    assert path is not None
    code, out, err_out = _run_git(["remote", "get-url", "origin"], cwd=path)
    if code != 0 or not out.strip():
        return None, None, "No origin remote or not a git repo"
    url = out.strip()
    # git@github.com:owner/repo.git or https://github.com/owner/repo[.git]
    owner, repo = None, None
    if url.startswith("git@github.com:") or "github.com:" in url.split("//")[-1]:
        part = url.replace("git@github.com:", "").split("//")[-1].strip()
        if "github.com:" in url:
            part = url.split("github.com:")[-1].strip()
        part = part.rstrip("/").removesuffix(".git")
        if "/" in part:
            owner, repo = part.split("/", 1)
    elif "github.com" in url:
        part = url.split("github.com")[-1].strip("/").removesuffix(".git")
        if part.startswith("/"):
            part = part.lstrip("/")
        if "/" in part:
            owner, repo = part.split("/", 1)
    if not owner or not repo:
        return None, None, f"Could not parse owner/repo from origin: {url}"
    return owner, repo, None


@lru_cache(maxsize=1)
def _get_all_repos_cached():
    """Cached wrapper; cache is invalidated by calling _get_all_repos_cached.cache_clear()."""
    return _fetch_github_repos()


@mcp.resource("repo://repos")
def get_all_repos():
    result = _get_all_repos_cached()
    if isinstance(result, dict) and "error" in result:
        _get_all_repos_cached.cache_clear()
        return result
    return result


@mcp.resource("repo://files/{repo}")
def get_files(repo: str):
    repo_path, err = _resolve_repo_path_and_ensure_git(repo)
    if err:
        return {"error": err}
    code, out, err_out = _run_git(["ls-files"], cwd=repo_path)
    if code != 0:
        return {"error": err_out or "git ls-files failed"}
    return out


@mcp.resource("repo://file/{repo}/{path}")
def get_file(repo: str, path: str):
    if ".." in path:
        return {"error": "Invalid path: contains '..'"}
    repo_path, err = _resolve_repo_path_and_ensure_git(repo)
    if err:
        return {"error": err}
    code, out, err_out = _run_git(["show", f"HEAD:{path}"], cwd=repo_path)
    if code != 0:
        return {"error": err_out or f"git show HEAD:{path} failed"}
    return out


@mcp.resource("repo://commits/{repo}")
def get_commits(repo: str):
    repo_path, err = _resolve_repo_path_and_ensure_git(repo)
    if err:
        return {"error": err}
    code, out, err_out = _run_git(["log", "--oneline"], cwd=repo_path)
    if code != 0:
        return {"error": err_out or "git log failed"}
    return out


@mcp.resource("repo://branches/{repo}")
def get_branches(repo: str):
    repo_path, err = _resolve_repo_path_and_ensure_git(repo)
    if err:
        return {"error": err}
    code, out, err_out = _run_git(["branch", "--list"], cwd=repo_path)
    if code != 0:
        return {"error": err_out or "git branch failed"}
    return out


@mcp.resource("repo://info/{repo}")
def get_repo_info(repo: str):
    """Return repo metadata: default branch, remote, last commit, size (for AI agents)."""
    repo_path, err = _resolve_repo_path_and_ensure_git(repo)
    if err:
        return {"error": err}
    lines: list[str] = []
    code, out, _ = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_path)
    lines.append(f"current_branch: {out.strip() if code == 0 else '?'}")
    code, out, _ = _run_git(["remote", "get-url", "origin"], cwd=repo_path)
    lines.append(f"remote_origin: {out.strip() if code == 0 else 'none'}")
    code, out, _ = _run_git(
        ["log", "-1", "--format=%H %s"],
        cwd=repo_path,
    )
    lines.append(f"last_commit: {out.strip() if code == 0 else 'none'}")
    try:
        r = subprocess.run(
            ["du", "-sh", "."],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=GIT_CMD_TIMEOUT,
        )
        if r.returncode == 0 and r.stdout:
            lines.append(f"repo_size: {r.stdout.split()[0]}")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return "\n".join(lines)


@mcp.prompt(name="summarize_repository")
def summarize_repository(repo: str):
    return [
        mcp.types.UserMessage(content=f"Summarize the repository {repo}"),
    ]


@mcp.prompt(name="review_recent_changes")
def review_recent_changes(repo: str):
    return [
        mcp.types.UserMessage(
            content=f"Review the recent changes in the repository {repo}"
        ),
    ]


@mcp.prompt(name="explain_file")
def explain_file(repo: str, path: str):
    return [
        mcp.types.UserMessage(
            content=f"Explain the file {path} in the repository {repo}"
        ),
    ]


@mcp.prompt(name="generate_documentation")
def generate_documentation(repo: str, path: str):
    return [
        mcp.types.UserMessage(
            content=f"Generate documentation for the file {path} in the repository {repo}"
        ),
    ]


@mcp.tool(name="clone_repo")
async def clone_repo(
    ctx: Context[ServerSession, None],
    repo_name: str = Field(description="Name of the repo to clone"),
    path: str = Field(
        description="Local path under base dir (e.g. repo name or subpath). Must not exist yet."
    ),
):
    target_path, err = _resolve_repo_path(path)
    if err:
        return err
    if os.path.exists(target_path):
        return "Directory already exists; choose a different path or remove it first"

    repo = get_all_repos()
    if isinstance(repo, dict) and "error" in repo:
        return repo["error"]

    matching_repos = [r for r in repo if r["name"] == repo_name]
    if not matching_repos:
        return f"Repo {repo_name} not found in user's repositories"
    ssh_repo_url = matching_repos[0]["ssh_url"]

    passphrase_override = None
    if not os.getenv("SSH_KEY_PASSPHRASE"):
        result = await ctx.elicit(
            message="SSH key passphrase required to clone (used only for this operation).",
            schema=PassphraseInput,
        )
        if result.action != "accept":
            return "Clone cancelled"
        passphrase_override = _elicit_passphrase(result.data)
        if not passphrase_override:
            return "Passphrase not provided"

    code, _, err_out = _run_git(
        ["clone", ssh_repo_url, target_path],
        cwd=BASE_REPO_DIR,
        passphrase_override=passphrase_override,
    )
    if code != 0:
        return err_out or "git clone failed"

    _get_all_repos_cached.cache_clear()
    return "Repository cloned"


@mcp.tool(name="status_changes")
def status_changes(
    path: str = Field(
        description="Repo path (relative to base dir or absolute under base)"
    ),
):
    repo_path, err = _resolve_repo_path_and_ensure_git(path)
    if err:
        return err
    code, out, err_out = _run_git(["status", "--porcelain"], cwd=repo_path)
    if code != 0:
        return err_out or "git status failed"
    if not out.strip():
        return "No changes (working tree clean)"
    return out


@mcp.tool(name="add_changes")
def add_changes(
    path: str = Field(
        description="Repo path (relative to base dir or absolute under base)"
    ),
):
    repo_path, err = _resolve_repo_path_and_ensure_git(path)
    if err:
        return err
    _, porcelain = _status_porcelain(path)
    if not porcelain.strip():
        return "No changes to add"
    code, _, err_out = _run_git(["add", "."], cwd=repo_path)
    if code != 0:
        return err_out or "git add failed"
    return "Changes added"


@mcp.tool(name="commit_changes")
def commit_changes(
    commit_message: str = Field(description="Commit message for the changes"),
    path: str = Field(
        description="Repo path (relative to base dir or absolute under base)"
    ),
):
    repo_path, err = _resolve_repo_path_and_ensure_git(path)
    if err:
        return err
    _, porcelain = _status_porcelain(path)
    if not porcelain.strip():
        return "No changes to commit"
    code, _, err_out = _run_git(["add", "."], cwd=repo_path)
    if code != 0:
        return err_out or "git add failed"
    code, _, err_out = _run_git(["commit", "-m", commit_message], cwd=repo_path)
    if code != 0:
        return err_out or "git commit failed"
    return "Changes committed"


class PushConfirm(BaseModel):
    confirm: bool = Field(description="Confirm push to remote")


class PassphraseInput(BaseModel):
    passphrase: str = Field(description="SSH key passphrase (used only for this operation)")


def _elicit_passphrase(data: object) -> str | None:
    """Extract passphrase from elicit result.data (model instance or dict)."""
    if data is None:
        return None
    if hasattr(data, "passphrase"):
        return getattr(data, "passphrase") or None
    if isinstance(data, dict):
        return data.get("passphrase")
    return None


@mcp.tool(name="push_changes")
async def push_changes(
    ctx: Context[ServerSession, None],
    path: str = Field(
        description="Repo path (relative to base dir or absolute under base)"
    ),
    branch: str = Field(description="Branch to push the changes to"),
):
    repo_path, err = _resolve_repo_path_and_ensure_git(path)
    if err:
        return err
    result = await ctx.elicit(
        message=f"Push local changes to origin/{branch}?",
        schema=PushConfirm,
    )
    if result.action != "accept":
        return "Push cancelled"

    passphrase_override = None
    if not os.getenv("SSH_KEY_PASSPHRASE"):
        result = await ctx.elicit(
            message="SSH key passphrase required to push (used only for this operation).",
            schema=PassphraseInput,
        )
        if result.action != "accept":
            return "Push cancelled"
        passphrase_override = _elicit_passphrase(result.data)
        if not passphrase_override:
            return "Passphrase not provided"

    code, _, err_out = _run_git(
        ["push", "origin", branch],
        cwd=repo_path,
        passphrase_override=passphrase_override,
    )
    if code != 0:
        return err_out or "git push failed"
    return "Changes pushed"


@mcp.tool(name="pull_repo")
async def pull_repo(
    ctx: Context[ServerSession, None],
    path: str = Field(
        description="Repo path (relative to base dir or absolute under base)"
    ),
    branch: str = Field(description="Branch to pull the changes from"),
):
    repo_path, err = _resolve_repo_path_and_ensure_git(path)
    if err:
        return err

    passphrase_override = None
    if not os.getenv("SSH_KEY_PASSPHRASE"):
        result = await ctx.elicit(
            message="SSH key passphrase required to pull (used only for this operation).",
            schema=PassphraseInput,
        )
        if result.action != "accept":
            return "Pull cancelled"
        passphrase_override = _elicit_passphrase(result.data)
        if not passphrase_override:
            return "Passphrase not provided"

    code, _, err_out = _run_git(
        ["pull", "origin", branch],
        cwd=repo_path,
        passphrase_override=passphrase_override,
    )
    if code != 0:
        return err_out or "git pull failed"
    return "Changes pulled"


@mcp.tool(name="create_branch")
def create_branch(
    path: str = Field(
        description="Repo path (relative to base dir or absolute under base)"
    ),
    branch: str = Field(description="Branch to create (and switch to)"),
):
    repo_path, err = _resolve_repo_path_and_ensure_git(path)
    if err:
        return err
    code, _, err_out = _run_git(["checkout", "-b", branch], cwd=repo_path)
    if code != 0:
        return err_out or "git checkout -b failed"
    return "Branch created and checked out"


@mcp.tool(name="checkout_branch")
def checkout_branch(
    path: str = Field(
        description="Repo path (relative to base dir or absolute under base)"
    ),
    branch: str = Field(description="Branch to checkout"),
):
    repo_path, err = _resolve_repo_path_and_ensure_git(path)
    if err:
        return err
    code, _, err_out = _run_git(["checkout", branch], cwd=repo_path)
    if code != 0:
        return err_out or "git checkout failed"
    return "Branch checked out"


@mcp.tool(name="search_code")
def search_code(
    path: str = Field(
        description="Repo path (relative to base dir or absolute under base)"
    ),
    query: str = Field(description="Query to search for"),
):
    repo_path, err = _resolve_repo_path_and_ensure_git(path)
    if err:
        return err
    code, out, err_out = _run_git(["grep", "-n", query], cwd=repo_path)
    if code not in (0, 1):
        return err_out or "git grep failed"
    return out if out else "No matches"


def _repo_structure_fallback(repo_path: str, depth: int) -> str:
    """Build tree-like structure using git ls-tree (no external tree)."""
    depth = max(1, min(depth, 10))
    code, out, _ = _run_git(["ls-tree", "-r", "--name-only", "HEAD"], cwd=repo_path)
    if code != 0:
        return "(git ls-tree failed)"
    paths = sorted(p for p in out.strip().splitlines() if p)
    if not paths:
        return "(empty repo or no committed files)"
    seen: set[str] = set()
    result: list[str] = []
    for path in paths:
        parts = path.split("/")
        for d in range(1, min(len(parts), depth) + 1):
            prefix = "/".join(parts[:d])
            if prefix in seen:
                continue
            seen.add(prefix)
            indent = "  " * (d - 1)
            result.append(f"{indent}{parts[d - 1]}")
    return "\n".join(result) if result else "(no paths at selected depth)"


@mcp.tool(name="repo_structure")
def repo_structure(
    path: str = Field(
        description="Repo path (relative to base dir or absolute under base)"
    ),
    depth: int = Field(default=3, description="Directory depth for tree (default 3)"),
):
    """List repository structure (like tree -L N) for AI reasoning."""
    repo_path, err = _resolve_repo_path_and_ensure_git(path)
    if err:
        return err
    depth = max(1, min(depth, 10))
    r = subprocess.run(
        ["tree", "-L", str(depth), "--noreport", "-I", ".git"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=GIT_CMD_TIMEOUT,
    )
    if r.returncode == 0 and r.stdout:
        return r.stdout.strip()
    return _repo_structure_fallback(repo_path, depth)


@mcp.tool(name="diff_changes")
def diff_changes(
    path: str = Field(
        description="Repo path (relative to base dir or absolute under base)"
    ),
    staged: bool = Field(
        default=False,
        description="Show staged changes (--cached) instead of working tree",
    ),
):
    """Show diff for working tree or staged changes."""
    repo_path, err = _resolve_repo_path_and_ensure_git(path)
    if err:
        return err
    args = ["diff", "--cached"] if staged else ["diff"]
    code, out, err_out = _run_git(args, cwd=repo_path)
    if code != 0:
        return err_out or "git diff failed"
    return out.strip() or "No diff"


class EditConfirm(BaseModel):
    confirm: bool = Field(description="Confirm overwriting file content")


class DeleteKeyConfirm(BaseModel):
    confirm: bool = Field(description="Confirm removing SSH key from GitHub account")


@mcp.tool(name="edit_file")
async def edit_file(
    ctx: Context[ServerSession, None],
    repo_path: str = Field(
        description="Repo path (relative to base dir or absolute under base)"
    ),
    file_path: str = Field(description="Path to the file inside the repo"),
    content: str = Field(description="Content to write to the file"),
):
    base, err = _resolve_repo_path_and_ensure_git(repo_path)
    if err:
        return err
    if ".." in file_path:
        return "Invalid path: contains '..'"
    full = os.path.normpath(os.path.join(base, file_path))
    if not full.startswith(base):
        return "Invalid path: outside repo"
    relative = os.path.relpath(full, base).replace("\\", "/")
    if _is_sensitive_path(relative):
        return "Editing this file is not allowed (sensitive: .git, .env, SSH keys, credentials)"
    result = await ctx.elicit(
        message=f"Overwrite '{relative}' with new content?",
        schema=EditConfirm,
    )
    if result.action != "accept":
        return "Edit cancelled"
    with open(full, "w") as f:
        f.write(content)
    return "File edited"


# --------------- GitHub user SSH keys (GitHub API) ---------------


def _require_github_token() -> str | None:
    """Return error message if GITHUB_TOKEN is not set."""
    if not os.getenv("GITHUB_TOKEN"):
        return "GITHUB_TOKEN environment variable not set"
    return None


@mcp.tool(name="list_github_ssh_keys")
def list_github_ssh_keys():
    """List SSH keys registered on the authenticated user's GitHub account. Requires GITHUB_TOKEN with read:public_key scope."""
    err = _require_github_token()
    if err:
        return err
    url = "https://api.github.com/user/keys"
    resp = requests.get(url, headers=_github_headers(), timeout=GIT_CMD_TIMEOUT)
    if resp.status_code != 200:
        return f"GitHub API error: {resp.status_code} {resp.text}"
    try:
        keys = resp.json()
    except ValueError:
        return "Invalid JSON from GitHub API"
    if not isinstance(keys, list):
        return "Unexpected response format"
    lines = []
    for k in keys:
        key_id = k.get("id", "?")
        title = k.get("title", "?")
        created = k.get("created_at", "")[:10]
        last_used = (k.get("last_used") or "never")[:10] if k.get("last_used") else "never"
        lines.append(f"id={key_id} title={title} created={created} last_used={last_used}")
    return "\n".join(lines) if lines else "No SSH keys on GitHub"


@mcp.tool(name="add_github_ssh_key")
def add_github_ssh_key(
    title: str = Field(description="Descriptive name for the key (e.g. 'MacBook' or 'CI')"),
    key: str = Field(description="Full public key content (e.g. ssh-ed25519 AAAA... or ssh-rsa AAAA...)"),
):
    """Add a public SSH key to the authenticated user's GitHub account. Requires GITHUB_TOKEN with write:public_key scope."""
    err = _require_github_token()
    if err:
        return err
    key = key.strip()
    if not key.startswith(("ssh-ed25519 ", "ssh-rsa ", "ecdsa-sha2-")):
        return "Invalid key: must be a public key (ssh-ed25519, ssh-rsa, or ecdsa-sha2-...)"
    url = "https://api.github.com/user/keys"
    resp = requests.post(
        url,
        headers=_github_headers(),
        json={"title": title or "git-mcp", "key": key},
        timeout=GIT_CMD_TIMEOUT,
    )
    if resp.status_code == 201:
        return "SSH key added to GitHub"
    return f"GitHub API error: {resp.status_code} {resp.text}"


@mcp.tool(name="get_github_ssh_key")
def get_github_ssh_key(
    key_id: int = Field(description="GitHub key ID (from list_github_ssh_keys)"),
):
    """Get details for one SSH key on the authenticated user's GitHub account. Requires GITHUB_TOKEN with read:public_key scope."""
    err = _require_github_token()
    if err:
        return err
    url = f"https://api.github.com/user/keys/{key_id}"
    resp = requests.get(url, headers=_github_headers(), timeout=GIT_CMD_TIMEOUT)
    if resp.status_code != 200:
        return f"GitHub API error: {resp.status_code} {resp.text}"
    try:
        k = resp.json()
    except ValueError:
        return "Invalid JSON from GitHub API"
    return f"id={k.get('id')} title={k.get('title')} created_at={k.get('created_at')} last_used={k.get('last_used') or 'never'}\nkey={k.get('key', '')}"


@mcp.tool(name="delete_github_ssh_key")
async def delete_github_ssh_key(
    ctx: Context[ServerSession, None],
    key_id: int = Field(description="GitHub key ID to remove (from list_github_ssh_keys)"),
):
    """Remove an SSH key from the authenticated user's GitHub account. Asks for confirmation. Requires GITHUB_TOKEN with admin:public_key scope."""
    err = _require_github_token()
    if err:
        return err
    result = await ctx.elicit(
        message=f"Remove SSH key id={key_id} from your GitHub account?",
        schema=DeleteKeyConfirm,
    )
    if result.action != "accept":
        return "Delete cancelled"
    url = f"https://api.github.com/user/keys/{key_id}"
    resp = requests.delete(url, headers=_github_headers(), timeout=GIT_CMD_TIMEOUT)
    if resp.status_code in (204, 200):
        return "SSH key removed from GitHub"
    return f"GitHub API error: {resp.status_code} {resp.text}"


@mcp.resource("github://user/keys")
def resource_github_ssh_keys():
    """List GitHub user SSH keys (same as list_github_ssh_keys)."""
    return list_github_ssh_keys()


# --------------- Deployment status (GitHub API) ---------------


@mcp.tool(name="list_deployments")
def list_deployments(
    path: str = Field(
        description="Repo path (relative to base dir or absolute under base); uses origin to get owner/repo"
    ),
    environment: str | None = Field(default=None, description="Filter by environment name (e.g. production, staging)"),
    per_page: int = Field(default=10, description="Max number of deployments to return (default 10)"),
):
    """List deployments for the repository (GitHub Deployments API). Requires GITHUB_TOKEN with repo or repo_deployment scope."""
    err = _require_github_token()
    if err:
        return err
    owner, repo, err = _get_owner_repo(path)
    if err:
        return err
    url = f"https://api.github.com/repos/{owner}/{repo}/deployments"
    params = {"per_page": min(100, max(1, per_page))}
    if environment:
        params["environment"] = environment
    resp = requests.get(url, headers=_github_headers(), params=params, timeout=GIT_CMD_TIMEOUT)
    if resp.status_code != 200:
        return f"GitHub API error: {resp.status_code} {resp.text}"
    try:
        deployments = resp.json()
    except ValueError:
        return "Invalid JSON from GitHub API"
    if not isinstance(deployments, list):
        return "Unexpected response format"
    lines = []
    for d in deployments:
        dep_id = d.get("id", "?")
        ref = d.get("ref", "?")
        task = d.get("task", "?")
        env = d.get("environment") or "none"
        created = (d.get("created_at") or "")[:19]
        lines.append(f"id={dep_id} ref={ref} task={task} environment={env} created_at={created}")
    return "\n".join(lines) if lines else "No deployments found"


@mcp.tool(name="deployment_status")
def deployment_status(
    path: str = Field(
        description="Repo path (relative to base dir or absolute under base)"
    ),
    deployment_id: int = Field(description="Deployment ID from list_deployments"),
):
    """Get statuses for a single deployment (success, failure, pending, etc.). Requires GITHUB_TOKEN with repo or repo_deployment scope."""
    err = _require_github_token()
    if err:
        return err
    owner, repo, err = _get_owner_repo(path)
    if err:
        return err
    url = f"https://api.github.com/repos/{owner}/{repo}/deployments/{deployment_id}/statuses"
    resp = requests.get(url, headers=_github_headers(), params={"per_page": 20}, timeout=GIT_CMD_TIMEOUT)
    if resp.status_code != 200:
        return f"GitHub API error: {resp.status_code} {resp.text}"
    try:
        statuses = resp.json()
    except ValueError:
        return "Invalid JSON from GitHub API"
    if not isinstance(statuses, list):
        return "Unexpected response format"
    lines = []
    for s in statuses:
        state = s.get("state", "?")
        desc = (s.get("description") or "").replace("\n", " ")
        created = (s.get("created_at") or "")[:19]
        lines.append(f"state={state} description={desc} created_at={created}")
    return "\n".join(lines) if lines else "No statuses for this deployment"


# --------------- GitHub Actions runs (GitHub API) ---------------


@mcp.tool(name="actions_runs_summary")
def actions_runs_summary(
    path: str = Field(
        description="Repo path (relative to base dir or absolute under base); uses origin to get owner/repo"
    ),
    branch: str | None = Field(default=None, description="Filter by branch name"),
    per_page: int = Field(default=10, description="Number of recent runs to include (default 10)"),
):
    """Summarize recent GitHub Actions workflow runs (success, failure, in_progress, etc.). Requires GITHUB_TOKEN with repo scope."""
    err = _require_github_token()
    if err:
        return err
    owner, repo, err = _get_owner_repo(path)
    if err:
        return err
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs"
    params = {"per_page": min(100, max(1, per_page))}
    if branch:
        params["branch"] = branch
    resp = requests.get(url, headers=_github_headers(), params=params, timeout=GIT_CMD_TIMEOUT)
    if resp.status_code != 200:
        return f"GitHub API error: {resp.status_code} {resp.text}"
    try:
        data = resp.json()
    except ValueError:
        return "Invalid JSON from GitHub API"
    runs = data.get("workflow_runs") if isinstance(data, dict) else None
    if not isinstance(runs, list):
        return "Unexpected response format"
    lines = []
    for r in runs:
        run_id = r.get("id", "?")
        name = r.get("name", "?")
        status = r.get("status", "?")
        conclusion = r.get("conclusion") or "(running)"
        head_branch = r.get("head_branch", "?")
        created = (r.get("created_at") or "")[:19]
        html_url = r.get("html_url", "")
        lines.append(f"id={run_id} name={name} status={status} conclusion={conclusion} branch={head_branch} created={created} url={html_url}")
    return "\n".join(lines) if lines else "No workflow runs found"


@mcp.prompt(name="summarize_ci_status")
def summarize_ci_status(repo: str):
    """Prompt to summarize GitHub Actions CI status for a repository. Use actions_runs_summary tool with path=repo first."""
    return [
        mcp.types.UserMessage(
            content=f"Summarize the GitHub Actions CI status for the repository '{repo}'. Use the actions_runs_summary tool with path '{repo}' to get recent workflow runs, then report success/failure and any issues."
        ),
    ]


@mcp.prompt(name="deployment_status_summary")
def deployment_status_summary(repo: str):
    """Prompt to summarize deployment status for a repository. Use list_deployments and deployment_status tools with path=repo first."""
    return [
        mcp.types.UserMessage(
            content=f"Summarize the deployment status for the repository '{repo}'. Use list_deployments with path '{repo}', then deployment_status for any deployment IDs of interest, and report success/failure/pending."
        ),
    ]


if __name__ == "__main__":
    mcp.run(transport="stdio")
