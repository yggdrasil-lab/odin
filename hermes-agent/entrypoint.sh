#!/bin/bash
set -e

MODE=${1:-gateway}

if [ "$MODE" = "dashboard" ]; then
  echo "Starting Huginn Dashboard..."
  /opt/hermes/.venv/bin/python -m mnemosyne.install
  exec /opt/hermes/.venv/bin/hermes dashboard --host 0.0.0.0 --insecure
else
  echo "Starting Huginn Gateway..."

  # 1. Setup SSH directory and keys
  mkdir -p /opt/data/.ssh && chmod 700 /opt/data/.ssh
  if [ -f /run/secrets/hermes_ssh_key ]; then
    echo "Configuring SSH key..."
    tr -d "\r" < /run/secrets/hermes_ssh_key > /opt/data/.ssh/id_rsa
    echo "" >> /opt/data/.ssh/id_rsa
    chmod 600 /opt/data/.ssh/id_rsa
    ssh-keyscan github.com > /opt/data/.ssh/known_hosts 2>/dev/null
    chmod 600 /opt/data/.ssh/known_hosts

    # Copy SSH configuration to active home directories
    for dest_dir in "/opt/data/home/.ssh" "$HOME/.ssh"; do
      mkdir -p "$dest_dir" && chmod 700 "$dest_dir"
      cp /opt/data/.ssh/id_rsa "$dest_dir/id_rsa" && chmod 600 "$dest_dir/id_rsa"
      cp /opt/data/.ssh/known_hosts "$dest_dir/known_hosts" && chmod 600 "$dest_dir/known_hosts"
    done
  fi

  # 2. Configure GitHub CLI (gh) credentials
  if [ -n "${GH_SETUP_TOKEN}" ]; then
    echo "Logging in to GitHub CLI..."
    if echo "${GH_SETUP_TOKEN}" | runuser -u hermes -- gh auth login --with-token; then
      echo "GitHub CLI login successful."
    else
      echo "Warning: GitHub CLI authentication failed."
    fi
  fi

  # 3. Configure Git user name/email and safe directory
  runuser -u hermes -- git config --global user.name "${GIT_USER_NAME}"
  runuser -u hermes -- git config --global user.email "${GIT_USER_EMAIL}"
  git config --global safe.directory /app/vault

  # 4. Remove secret tokens from the process environment
  unset GH_SETUP_TOKEN
  unset GH_TOKEN
  unset GITHUB_TOKEN

  # 5. Initialize Mnemosyne and run the Hermes Agent gateway
  /opt/hermes/.venv/bin/python -m mnemosyne.install
  exec /opt/hermes/.venv/bin/hermes gateway run
fi
