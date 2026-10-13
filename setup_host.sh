#!/bin/bash
set -e

# setup_host.sh
# This script prepares the Gaia host directories for the Odin stack services.
# It ensures the repository, live runtime, and backup directories exist.

echo "Setting up Odin directories on host..."

# Git Repository Root Directory on host
REPO_DIR="/opt/odin"
if [ ! -d "${REPO_DIR}" ]; then
    echo "Creating ${REPO_DIR}..."
    sudo mkdir -p "${REPO_DIR}"
fi
echo "Setting ownership of ${REPO_DIR}..."
TARGET_USER=${SUDO_USER:-$(id -un)}
TARGET_GROUP=${SUDO_GID:-$(id -g)}
sudo chown -R ${TARGET_USER}:${TARGET_GROUP} "${REPO_DIR}"

# Copy gitignore to the repository root on host
if [ -f ".gitignore" ]; then
    echo "Copying .gitignore to ${REPO_DIR}..."
    cp .gitignore "${REPO_DIR}/.gitignore"
fi

# Hermes Agent Live Data Directory (inside repo root)
LIVE_DIR="${REPO_DIR}/hermes"
if [ ! -d "${LIVE_DIR}" ]; then
    echo "Creating ${LIVE_DIR}..."
    sudo mkdir -p "${LIVE_DIR}"
fi
sudo mkdir -p "${LIVE_DIR}/home"
sudo chown -R 1000:1000 "${LIVE_DIR}"
sudo chmod -R 775 "${LIVE_DIR}"

# Antigravity CLI host directory (mounted into Hermes containers)
HOST_AGY_DIR="${HOST_AGY_CONFIG_DIR:-/home/${TARGET_USER}/.gemini/antigravity-cli}"
if [ ! -d "${HOST_AGY_DIR}" ]; then
    echo "Creating ${HOST_AGY_DIR}..."
    sudo mkdir -p "${HOST_AGY_DIR}"
fi
sudo chown -R 1000:1000 "${HOST_AGY_DIR}"
sudo chmod 700 "${HOST_AGY_DIR}"

# Mnemosyne Memory Directory
MNEMOSYNE_DIR="/opt/odin/mnemosyne"
if [ ! -d "${MNEMOSYNE_DIR}" ]; then
    echo "Creating ${MNEMOSYNE_DIR}..."
    sudo mkdir -p "${MNEMOSYNE_DIR}"
    sudo chown -R 1000:1000 "${MNEMOSYNE_DIR}"
    sudo chmod -R 775 "${MNEMOSYNE_DIR}"
fi

# Hermes Agent Backups Directory
BACKUP_DIR="/mnt/storage/backups/odin/hermes"
if [ ! -d "${BACKUP_DIR}" ]; then
    echo "Creating ${BACKUP_DIR}..."
    sudo mkdir -p "${BACKUP_DIR}"
    sudo chown -R 1000:1000 "${BACKUP_DIR}"
fi

# Ensure agy CLI is in PATH (symlink ~/.local/bin/agy -> /usr/local/bin/agy if present)
if [ -f "/home/${TARGET_USER}/.local/bin/agy" ]; then
    echo "Symlinking agy to /usr/local/bin/agy..."
    sudo ln -sf "/home/${TARGET_USER}/.local/bin/agy" /usr/local/bin/agy
fi

# Configure Git to ignore submodules in status/diff to prevent stalls across all 19 Forge repos
echo "Configuring Git to avoid submodule recursion stalls..."
git config --global status.submoduleSummary false
git config --global diff.ignoreSubmodules all
git config --global submodule.recurse false

# Verify Antigravity OAuth Token
if [ -f "${HOST_AGY_DIR}/jetski-standalone-oauth-token" ] || [ -f "${HOST_AGY_DIR}/antigravity-oauth-token" ]; then
    echo "✓ Antigravity OAuth token verified in ${HOST_AGY_DIR}"
    sudo chmod 600 "${HOST_AGY_DIR}"/*token* 2>/dev/null || true
else
    echo "⚠️  Notice: Antigravity OAuth token not found in ${HOST_AGY_DIR} yet."
    echo "   Run 'agy' on Gaia to log in before starting Hermes."
fi

echo "Done. Host is ready for Odin deployment."

# =============================================================================
# Obsidian Vault Write Access (for Huginn Agent)
# The vault is owned by root:root. This grants write access to 'others'
# (covers the container process regardless of UID) so Huginn can create and
# edit notes. This must be re-run after a Charon vault restore.
# =============================================================================
VAULT_PATH="${OBSIDIAN_VAULT_PATH:-/opt/atlas/vault}/second-brain"
if [ -d "${VAULT_PATH}" ]; then
    echo "Granting vault write access to Huginn Agent at ${VAULT_PATH}..."
    sudo chmod -R o+w "${VAULT_PATH}"
    sudo chown -R 1000:1000 "${VAULT_PATH}"
    echo "Vault permissions updated."
else
    echo "Warning: Vault not found at ${VAULT_PATH}. Skipping permission update."
    echo "Set OBSIDIAN_VAULT_PATH and re-run this script after the vault is restored."
fi
