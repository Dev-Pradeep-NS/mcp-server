# Git commands

## Setup & config
- `git config --global user.name "Name"` — set your name
- `git config --global user.email "email@example.com"` — set your email
- `git init` — create a new repo in current directory
- `git clone <url>` — clone a repo

## Basic workflow
- `git status` — see changed/untracked files
- `git add <file>` or `git add .` — stage changes
- `git commit -m "message"` — commit staged changes
- `git diff` — unstaged changes; `git diff --staged` — staged changes

## Branches
- `git branch` — list branches
- `git branch <name>` — create branch
- `git checkout <branch>` or `git switch <branch>` — switch branch
- `git checkout -b <name>` or `git switch -c <name>` — create and switch
- `git merge <branch>` — merge branch into current
- `git rebase <branch>` — rebase current branch onto another

## Remote & sync
- `git remote -v` — list remotes
- `git fetch` — fetch from remote
- `git pull` — fetch and merge (e.g. `git pull origin main`)
- `git push` — push to remote (e.g. `git push -u origin main`)

## Undo & reset
- `git restore <file>` — discard unstaged changes in file
- `git restore --staged <file>` — unstage file
- `git reset --soft HEAD~1` — undo last commit, keep changes staged
- `git reset --mixed HEAD~1` — undo last commit, unstage changes
- `git reset --hard HEAD~1` — undo last commit and discard changes
- `git revert <commit>` — new commit that undoes a commit

## Stash
- `git stash` — stash working changes
- `git stash list` — list stashes
- `git stash pop` — apply and remove top stash
- `git stash apply` — apply top stash, keep it

## History & compare
- `git log` — commit history (add `--oneline`, `-p`, `--graph`)
- `git show <commit>` — show one commit
- `git log -p <file>` — history for a file

## Tags
- `git tag` — list tags
- `git tag <name>` — lightweight tag
- `git tag -a <name> -m "msg"` — annotated tag
- `git push origin <tagname>` — push a tag
