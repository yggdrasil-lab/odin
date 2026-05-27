#!/bin/sh
set -e

echo "[$(date)] Starting odin git backup..."

# Configure Git
git config --global user.email "${GIT_USER_EMAIL}"
git config --global user.name "${GIT_USER_NAME}"
# Trust mounted volume (host UID differs from container UID)
git config --global safe.directory /git

# Setup SSH keys from Docker Secret
mkdir -p /root/.ssh
if [ -f "/run/secrets/odin_git_backup_ssh_key" ]; then
    tr -d '\r' < /run/secrets/odin_git_backup_ssh_key > /root/.ssh/id_rsa
    echo "" >> /root/.ssh/id_rsa
    chmod 700 /root/.ssh
    chmod 600 /root/.ssh/id_rsa
else
    echo "ERROR: SSH key not found at /run/secrets/odin_git_backup_ssh_key"
    exit 1
fi

# Add github.com to known_hosts
ssh-keyscan github.com >> /root/.ssh/known_hosts 2>/dev/null
chmod 600 /root/.ssh/known_hosts

# Initialize git repository if it doesn't exist
if [ ! -d ".git" ]; then
    echo "No .git directory found. Initializing repository..."
    git init
    git remote add origin "${GIT_REPO_URL}"
fi

# Always ensure the remote URL is up-to-date
git remote set-url origin "${GIT_REPO_URL}"

# Fetch and ensure on main branch
echo "Fetching latest changes from remote..."
git fetch origin

# Ensure local branch is main
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
if [ "$CURRENT_BRANCH" != "main" ]; then
    echo "Current branch is '$CURRENT_BRANCH'. Switching to 'main'..."
    if ! git show-ref --verify --quiet refs/heads/main; then
        git checkout -b main origin/main 2>/dev/null || git checkout -b main
    else
        git checkout main
    fi
fi

# Merge remote changes, resolving conflicts in favour of remote.
# Preserves local commit history and uncommitted working-tree changes:
# - git merge -X theirs: local commits survive on top of remote;
#   when both sides touched the same file, remote wins.
# - If merge fails (uncommitted local changes conflict), fall back
#   to a safe hard-reset that keeps the working tree on disk so the
#   following `git add .` + commit still capture everything.
echo "Syncing with remote..."
git fetch origin main
if ! git merge -X theirs origin/main; then
  echo "Merge failed (likely uncommitted changes). Falling back to hard-reset..."
  git reset --hard origin/main
fi

# Untrack files now covered by updated .gitignore patterns.
# After syncing with remote, .gitignore reflects the latest remote version.
# Any tracked file matching the new patterns needs to be rm --cached
# so it won't be re-committed on the next backup. Uses --no-index to
# evaluate what WOULD be ignored regardless of current tracking status.
if [ -f ".gitignore" ]; then
  IGNORED_FILES=$(git ls-files --cached | git check-ignore --stdin --no-index 2>/dev/null || true)
  if [ -n "$IGNORED_FILES" ]; then
    echo "Cleaning up tracked files now covered by .gitignore:"
    printf '%s\n' $IGNORED_FILES | sed 's/^/  /'
    printf '%s\n' $IGNORED_FILES | xargs -r git rm --cached --
    echo "Cleanup complete."
  fi
fi

# Stage all files (respects .gitignore)
git add .

# Commit and push if there are changes
if git diff --staged --quiet; then
    echo "No changes to commit."
else
    echo "Changes detected. Committing..."
    git commit -m "Daily backup: $(date '+%Y-%m-%d %H:%M')"
    
    echo "Pushing to remote..."
    git push origin main
    echo "Backup pushed successfully."
fi

echo "[$(date)] Backup complete."
