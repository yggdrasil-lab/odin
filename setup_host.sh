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
    sudo chown -R $(id -u):$(id -g) "${REPO_DIR}"
fi

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
    sudo chown -R 10000:10000 "${LIVE_DIR}"
fi

# Muninn Gateway Config Directory (inside repo root)
MUNINN_DIR="${REPO_DIR}/config/muninn"
if [ ! -d "${MUNINN_DIR}" ]; then
    echo "Creating ${MUNINN_DIR}..."
    sudo mkdir -p "${MUNINN_DIR}"
    sudo chown -R 1000:1000 "${MUNINN_DIR}"
fi
# Hermes Agent Backups Directory
BACKUP_DIR="/mnt/storage/backups/odin/hermes"
if [ ! -d "${BACKUP_DIR}" ]; then
    echo "Creating ${BACKUP_DIR}..."
    sudo mkdir -p "${BACKUP_DIR}"
    sudo chown -R 1000:1000 "${BACKUP_DIR}"
fi

echo "Done. Host is ready for Odin deployment."
