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
    sudo chown -R 1000:1000 "${LIVE_DIR}"
    sudo chmod -R 775 "${LIVE_DIR}"
fi

# Mnemosyne Memory Directory
MNEMOSYNE_DIR="${LIVE_DIR}/mnemosyne"
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
    echo "Vault permissions updated."
else
    echo "Warning: Vault not found at ${VAULT_PATH}. Skipping permission update."
    echo "Set OBSIDIAN_VAULT_PATH and re-run this script after the vault is restored."
fi
