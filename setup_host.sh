#!/bin/bash
set -e

# setup_host.sh
# This script prepares the Gaia host directories for the Odin stack services.
# Specifically, it ensures the backup directory for the Hermes Agent exists.

echo "Setting up Odin directories on host..."

# Hermes Agent Backups / Data
DIR="/mnt/storage/backups/odin/hermes"
if [ ! -d "${DIR}" ]; then
    echo "Creating ${DIR}..."
    sudo mkdir -p "${DIR}"
    sudo chown -R 1000:1000 "${DIR}"
fi

echo "Done. Host is ready for Odin deployment."
