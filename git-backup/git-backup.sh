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

# Pull latest changes
echo "Pulling latest changes..."
git pull origin main 2>/dev/null || echo "Pull failed (may be initial empty repo — continuing)"

# Add all files (respects .gitignore)
git add .

# Commit if there are changes
if git diff --staged --quiet; then
    echo "No changes to commit."
else
    echo "Changes detected. Committing..."
    git commit -m "Daily backup: $(date '+%Y-%m-%d %H:%M')"
    
    echo "Pushing to remote..."
    if git push origin main; then
        echo "Backup pushed successfully."
    else
        echo "Push failed. Attempting pull-rebase and retry..."
        git pull --rebase origin main
        git push origin main
        echo "Backup pushed after rebase."
    fi
fi

echo "[$(date)] Backup complete."
