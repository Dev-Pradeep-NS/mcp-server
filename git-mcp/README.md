# Git MCP Server

An MCP (Model Context Protocol) server that exposes Git and GitHub operations as tools, resources, and prompts. It lets AI agents clone repos, inspect files, manage branches, commit/push/pull, search code, and edit files within a configurable base directory.

## Overview

- **Framework:** [FastMCP](https://github.com/jlowin/fastmcp) with stdio transport
- **Entrypoint:** `server.py` — run with `python server.py` or via your MCP client
- **Security:** All repository paths are resolved under a single base directory; sensitive paths (`.git`, `.env`, SSH keys, credentials) cannot be edited

## Configuration

| Variable | Description | Default |
|----------|-------------|--------|
| `GIT_MCP_BASE_DIR` | Base directory for all repo operations; paths must resolve under this | Current working directory |
| `GIT_MCP_TIMEOUT` | Timeout in seconds for git and subprocess calls | `30` |
| `GITHUB_TOKEN` | GitHub API token; required for listing/cloning user repos and for GitHub API tools (SSH keys, deployments, Actions). See [Token scopes](#github-token-scopes) below. | — |
| `SSH_KEY_PASSPHRASE` | Optional; if set, used for SSH (clone/push/pull) so no per-operation prompt | — |

Load env from `.env` (e.g. in project root) via `load_dotenv()` at startup.

## Path resolution

- **Base directory:** `BASE_REPO_DIR` is set from `GIT_MCP_BASE_DIR` (or cwd). All repo operations use paths under this directory.
- **Path argument:** Tools and resources accept a `path` (or `repo`) that is either:
  - A path relative to `BASE_REPO_DIR`, or
  - An absolute path that must still be under `BASE_REPO_DIR`
- **Validation:** Paths containing `..` are rejected. Resolved path must start with `BASE_REPO_DIR`.
- **Git check:** Operations that need a repo (status, commit, branches, etc.) require the resolved path to be a directory containing a `.git` directory.

## SSH and passphrase

- For **clone**, **push**, and **pull**, SSH may ask for a passphrase.
- **Preferred:** Set `SSH_KEY_PASSPHRASE` in the environment so no interactive prompt is needed.
- **Otherwise:** The server uses MCP elicitation to ask the user for a passphrase for that single operation. The passphrase is passed to git via `SSH_ASKPASS` and the helper script `ssh_askpass_helper.sh`.
- **Helper script:** `ssh_askpass_helper.sh` in the same directory as `server.py` prints `GIT_MCP_SSH_PASSPHRASE` (when set for this run) or `SSH_KEY_PASSPHRASE`. Git/SSH are run with `SSH_ASKPASS` pointing to this script and `DISPLAY` set so SSH uses it in non-interactive mode.

### GitHub token scopes

For **listing/cloning repos** and **GitHub API features**, use a personal access token (classic or fine-grained) with:

| Feature | Classic PAT scope | Notes |
|--------|--------------------|--------|
| Repos list / clone | `repo` | Already used for `repo://repos` and clone. |
| **GitHub user SSH keys** | `read:public_key`, `write:public_key`, `admin:public_key` | List/get, add key, delete key. |
| **Deployments** | `repo` or `repo_deployment` | List deployments and deployment statuses. |
| **GitHub Actions runs** | `repo` | List workflow runs and summary. |

---

## Resources

Resources are read-only URIs the server exposes; clients can fetch them to get repo/file data.

| URI | Description |
|-----|-------------|
| `repo://repos` | List of user’s GitHub repos (name, private, ssh_url, default_branch). Requires `GITHUB_TOKEN`. Cached until next clone or error. |
| `repo://files/{repo}` | Newline-separated list of tracked files in the repo (`git ls-files`). |
| `repo://file/{repo}/{path}` | Content of the file at `path` at `HEAD` (`git show HEAD:{path}`). Rejects `..` in path. |
| `repo://commits/{repo}` | One-line commit log (`git log --oneline`). |
| `repo://branches/{repo}` | Local branch list (`git branch --list`). |
| `repo://info/{repo}` | Repo metadata: current branch, remote origin URL, last commit (hash + subject), repo size (if `du` available). |
| `github://user/keys` | List of SSH keys on the authenticated user's GitHub account (same as `list_github_ssh_keys`). Requires `GITHUB_TOKEN` with `read:public_key`. |

Error responses are JSON objects with an `"error"` key (e.g. `{"error": "Not a git repository"}`). Success for file/content resources is usually plain text.

---

## Prompts

Prompts are parameterized message templates the client can use to ask the model to do something with a repo or file.

| Name | Parameters | Purpose |
|------|------------|--------|
| `summarize_repository` | `repo` | “Summarize the repository {repo}” |
| `review_recent_changes` | `repo` | “Review the recent changes in the repository {repo}” |
| `explain_file` | `repo`, `path` | “Explain the file {path} in the repository {repo}” |
| `generate_documentation` | `repo`, `path` | “Generate documentation for the file {path} in the repository {repo}” |
| `summarize_ci_status` | `repo` | Asks the model to summarize GitHub Actions CI status using `actions_runs_summary`. |
| `deployment_status_summary` | `repo` | Asks the model to summarize deployment status using `list_deployments` and `deployment_status`. |

---

## Tools

### Clone and remote

- **`clone_repo`** (async)  
  - **Parameters:** `repo_name`, `path`  
  - Clones the user repo `repo_name` from GitHub (from `repo://repos`) into `path` under the base dir. `path` must not exist.  
  - May elicit SSH passphrase if `SSH_KEY_PASSPHRASE` is not set.  
  - Clears the repos cache on success.

### Status and diff

- **`status_changes`**  
  - **Parameters:** `path`  
  - Runs `git status --porcelain` in the repo at `path`. Returns “No changes (working tree clean)” if empty.

- **`diff_changes`**  
  - **Parameters:** `path`, `staged` (default `false`)  
  - Shows diff: with `staged=true` uses `git diff --cached`, otherwise `git diff`.

### Staging and commit

- **`add_changes`**  
  - **Parameters:** `path`  
  - Runs `git add .` in the repo. Returns “No changes to add” if status is clean.

- **`commit_changes`**  
  - **Parameters:** `commit_message`, `path`  
  - Runs `git add .` then `git commit -m <message>`. Returns “No changes to commit” if nothing to commit.

### Push and pull

- **`push_changes`** (async)  
  - **Parameters:** `path`, `branch`  
  - Elicits confirmation (“Push local changes to origin/{branch}?”). Then, if needed, elicits SSH passphrase. Runs `git push origin <branch>`.

- **`pull_repo`** (async)  
  - **Parameters:** `path`, `branch`  
  - Elicits SSH passphrase if `SSH_KEY_PASSPHRASE` is not set, then runs `git pull origin <branch>`.

### Branches

- **`create_branch`**  
  - **Parameters:** `path`, `branch`  
  - Creates and checks out the branch (`git checkout -b <branch>`).

- **`checkout_branch`**  
  - **Parameters:** `path`, `branch`  
  - Checks out the branch (`git checkout <branch>`).

### Search and structure

- **`search_code`**  
  - **Parameters:** `path`, `query`  
  - Runs `git grep -n <query>` in the repo. Returns “No matches” when exit code is 1.

- **`repo_structure`**  
  - **Parameters:** `path`, `depth` (default `3`)  
  - Shows directory structure: uses `tree -L <depth>` if available, else a tree built from `git ls-tree -r --name-only HEAD` (depth clamped 1–10).

### Edit file

- **`edit_file`** (async)  
  - **Parameters:** `repo_path`, `file_path`, `content`  
  - Overwrites the file at `file_path` inside the repo with `content`.  
  - Path must not contain `..` and must stay inside the repo.  
  - **Blocked for sensitive paths:** `.git`, `.env` (and `.env.*` except `.env.example`), common SSH key names (`id_rsa`, `id_ed25519`, etc.), and paths containing “credentials” (unless name ends with `.example`).  
  - Elicits confirmation (“Overwrite '{path}' with new content?”) before writing.

### GitHub user SSH keys (GitHub API)

- **`list_github_ssh_keys`**  
  - Lists SSH keys on the authenticated user's GitHub account (id, title, created_at, last_used). Requires `GITHUB_TOKEN` with `read:public_key`.

- **`add_github_ssh_key`**  
  - **Parameters:** `title`, `key` (full public key string, e.g. `ssh-ed25519 AAAA...`).  
  - Adds a public SSH key to the user's GitHub account. Requires `write:public_key`.

- **`get_github_ssh_key`**  
  - **Parameters:** `key_id` (from list).  
  - Returns details for one key. Requires `read:public_key`.

- **`delete_github_ssh_key`** (async)  
  - **Parameters:** `key_id`.  
  - Elicits confirmation, then removes the key from GitHub. Requires `admin:public_key`.

### Deployment status (GitHub API)

- **`list_deployments`**  
  - **Parameters:** `path` (repo path), optional `environment`, `per_page` (default 10).  
  - Lists deployments for the repo (owner/repo from `origin`). Requires `repo` or `repo_deployment`.

- **`deployment_status`**  
  - **Parameters:** `path`, `deployment_id`.  
  - Returns statuses for one deployment (success, failure, pending, etc.). Requires `repo` or `repo_deployment`.

### GitHub Actions (GitHub API)

- **`actions_runs_summary`**  
  - **Parameters:** `path` (repo path), optional `branch`, `per_page` (default 10).  
  - Returns a summary of recent workflow runs (id, name, status, conclusion, branch, created_at, url). Requires `repo`.

---

## Internal helpers (for maintainers)

- **`_git_env(passphrase_override)`** — Builds env for git subprocess; sets `SSH_ASKPASS` and optional `GIT_MCP_SSH_PASSPHRASE` when a passphrase is available.
- **`_resolve_repo_path(repo_or_path)`** — Resolves to absolute path under `BASE_REPO_DIR` or returns error.
- **`_ensure_git_repo(repo_path)`** — Returns `None` if path is a git repo, else an error string.
- **`_resolve_repo_path_and_ensure_git(repo_or_path)`** — Resolve + git check combined.
- **`_is_sensitive_path(relative_path)`** — True if path is in the blocked set for edits.
- **`_run_git(args, cwd, capture_output, passphrase_override)`** — Runs `git` with timeout and `_git_env`; returns `(returncode, stdout, stderr)`.
- **`_status_porcelain(repo_path)`** — Returns `(error_or_None, status_output)`.
- **`_fetch_github_repos(use_cache)`** — Calls GitHub API with pagination; returns list of repo dicts or `{error: ...}`.
- **`_get_all_repos_cached()`** — LRU-cached wrapper (maxsize=1); clear with `_get_all_repos_cached.cache_clear()`.
- **`_repo_structure_fallback(repo_path, depth)`** — Builds tree from `git ls-tree` when `tree` is not installed.
- **`_elicit_passphrase(data)`** — Extracts passphrase from elicit result (Pydantic model or dict).
- **`_github_headers()`** — Headers for GitHub REST API (Bearer token, Accept, X-GitHub-Api-Version).
- **`_get_owner_repo(repo_path)`** — Resolves repo path to (owner, repo) via `git remote get-url origin`; returns (None, None, error) on failure.
- **`_require_github_token()`** — Returns error string if `GITHUB_TOKEN` is not set.

## Pydantic models (elicitation)

- **`PushConfirm`** — `confirm: bool` for push confirmation.
- **`EditConfirm`** — `confirm: bool` for overwrite confirmation.
- **`DeleteKeyConfirm`** — `confirm: bool` for removing an SSH key from GitHub.
- **`PassphraseInput`** — `passphrase: str` for one-time SSH passphrase.

## Running the server

```bash
# From repo root, with env set (e.g. GITHUB_TOKEN, optional GIT_MCP_BASE_DIR, SSH_KEY_PASSPHRASE)
python git-mcp/server.py
```

The server uses stdio transport; connect your MCP client to its stdin/stdout.
