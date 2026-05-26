#!/bin/bash
set -e

STACK_NAME="${STACK_NAME:-odin}"
GATEWAY_SERVICE="${STACK_NAME}_huginn-gateway"
DASHBOARD_SERVICE="${STACK_NAME}_huginn-dashboard"
LIVE_DIR="/opt/odin/hermes"
BACKUP_DIR="/mnt/storage/backups/odin/hermes"

# Check if run on Docker host
if ! [ -x "$(command -v docker)" ]; then
  echo "Error: Docker command not found. This script must be run on the Docker host."
  exit 1
fi

echo "=== Hermes Agent Restore Tool ==="

# Check if a specific file is provided as argument
if [ -n "$1" ]; then
  BACKUP_FILE="$1"
  if [ ! -f "${BACKUP_FILE}" ]; then
    # Try relative to backup folder
    if [ -f "${BACKUP_DIR}/${BACKUP_FILE}" ]; then
      BACKUP_FILE="${BACKUP_DIR}/${BACKUP_FILE}"
    else
      echo "Error: Backup file not found: ${BACKUP_FILE}"
      exit 1
    fi
  fi
  
  # Determine target file name inside live directory
  # Backup files are named like name_backup_TIMESTAMP.ext
  FILENAME=$(basename "${BACKUP_FILE}")
  # Extract database name (remove the _backup_TIMESTAMP part)
  # Example: hermes_backup_20260524_120000.db -> hermes.db
  BASE_NAME=$(echo "${FILENAME}" | sed -E 's/_backup_[0-9]{8}_[0-9]{6}//')
  
  echo "Target Database: ${BASE_NAME}"
  echo "Source Backup:   ${FILENAME}"
  echo ""
  
  read -p "Are you sure you want to restore this file? (y/N) " -n 1 -r
  echo
  if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Restore cancelled."
    exit 0
  fi
  
  BACKUP_PAIRS="${BACKUP_FILE}:${BASE_NAME}"
else
  # No file provided, search for latest database backups
  echo "Scanning for latest backups in ${BACKUP_DIR}..."
  
  # Find unique DB prefixes
  # Find files matching pattern *_backup_TIMESTAMP.ext and extract the base db name
  DB_BASENAMES=$(find "${BACKUP_DIR}" -name "*_backup_*.db" -o -name "*_backup_*.sqlite" -o -name "*_backup_*.sqlite3" | \
    sed "s|^${BACKUP_DIR}/||" | \
    sed -E 's/_backup_[0-9]{8}_[0-9]{6}//' | \
    sort -u)
    
  if [ -z "${DB_BASENAMES}" ]; then
    echo "No backups found in ${BACKUP_DIR}."
    exit 1
  fi
  
  BACKUP_PAIRS=""
  echo "Found the following databases to restore:"
  for db_base in ${DB_BASENAMES}; do
    # Find latest backup file for this database
    ext="${db_base##*.}"
    prefix="${db_base%.*}"
    
    # Locate latest backup matching this database
    # Handle subdirectories if there are any
    dir_part=$(dirname "${db_base}")
    file_part=$(basename "${prefix}")
    
    search_dir="${BACKUP_DIR}"
    if [ "${dir_part}" != "." ]; then
      search_dir="${BACKUP_DIR}/${dir_part}"
    fi
    
    LATEST_BACKUP=$(ls -t "${search_dir}/${file_part}"_backup_*."${ext}" 2>/dev/null | head -n 1)
    
    if [ -n "${LATEST_BACKUP}" ]; then
      echo "  - Database: ${db_base}"
      echo "    Latest:   $(basename "${LATEST_BACKUP}")"
      if [ -z "${BACKUP_PAIRS}" ]; then
        BACKUP_PAIRS="${LATEST_BACKUP}:${db_base}"
      else
        BACKUP_PAIRS="${BACKUP_PAIRS} ${LATEST_BACKUP}:${db_base}"
      fi
    fi
  done
  
  if [ -z "${BACKUP_PAIRS}" ]; then
    echo "Error: Could not locate latest backup files."
    exit 1
  fi
  
  echo ""
  read -p "Are you sure you want to restore the latest backups for these databases? (y/N) " -n 1 -r
  echo
  if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Restore cancelled."
    exit 0
  fi
fi

# Scaling down services to 0 replicas to prevent write locks during restore
echo "Scaling down ${GATEWAY_SERVICE} and ${DASHBOARD_SERVICE} to 0 replicas..."
docker service scale "${GATEWAY_SERVICE}=0" "${DASHBOARD_SERVICE}=0"

echo "Waiting for services to stop..."
while [ "$(docker service ps -q -f "desired-state=running" "${GATEWAY_SERVICE}" "${DASHBOARD_SERVICE}" 2>/dev/null | wc -l)" -gt 0 ]; do
  sleep 1
done

# Perform restore copies
for pair in ${BACKUP_PAIRS}; do
  src="${pair%%:*}"
  dest_rel="${pair##*:}"
  if [[ "${dest_rel}" == mnemosyne* ]]; then
    dest="/opt/odin/${dest_rel}"
  else
    dest="${LIVE_DIR}/${dest_rel}"
  fi
  
  # Ensure destination subdirectory exists
  dest_dir=$(dirname "${dest}")
  mkdir -p "${dest_dir}"
  
  echo "Restoring ${src} -> ${dest}..."
  cp "${src}" "${dest}"
done

# Ensure files are owned by the container user (1000:1000)
echo "Fixing permissions on ${LIVE_DIR}..."
chown -R 1000:1000 "${LIVE_DIR}"
if [ -d "/opt/odin/mnemosyne" ]; then
  echo "Fixing permissions on /opt/odin/mnemosyne..."
  chown -R 1000:1000 "/opt/odin/mnemosyne"
fi

# Scaling services back up to 1 replica
echo "Scaling up ${GATEWAY_SERVICE} and ${DASHBOARD_SERVICE} back to 1 replica..."
docker service scale "${GATEWAY_SERVICE}=1" "${DASHBOARD_SERVICE}=1"

echo "Restore process completed successfully!"
