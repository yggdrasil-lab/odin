#!/bin/sh
set -e

BACKUP_DIR="/backup"
DATA_DIR="/data"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

echo "[$(date)] Starting Hermes Agent database backup..."

# Find all .db or .sqlite files and back them up using sqlite3 backup command
found_db=false
for db in $(find "${DATA_DIR}" -name "*.db" -o -name "*.sqlite" -o -name "*.sqlite3"); do
  found_db=true
  filename=$(basename "$db")
  name="${filename%.*}"
  ext="${filename##*.}"
  
  # Preserving relative path structure under /backup if inside a subdirectory
  rel_path=$(echo "$db" | sed "s|^${DATA_DIR}/||")
  rel_dir=$(dirname "$rel_path")
  
  # Determine target directory under BACKUP_DIR
  if [ "$rel_dir" != "." ]; then
    target_dir="${BACKUP_DIR}/${rel_dir}"
    mkdir -p "${target_dir}"
    backup_file="${target_dir}/${name}_backup_${TIMESTAMP}.${ext}"
  else
    backup_file="${BACKUP_DIR}/${name}_backup_${TIMESTAMP}.${ext}"
  fi
  
  echo "Backing up $db to ${backup_file}..."
  
  # Run sqlite3 backup command to ensure consistency
  sqlite3 "$db" ".backup '${backup_file}'"
done

if [ "$found_db" = false ]; then
  echo "[$(date)] WARNING: No SQLite database files found under ${DATA_DIR}."
fi

# Prune backups older than BACKUP_KEEP_DAYS (default 30 days)
KEEP_DAYS=${BACKUP_KEEP_DAYS:-30}
echo "[$(date)] Pruning backups older than ${KEEP_DAYS} days..."
find "${BACKUP_DIR}" \( -name "*_backup_*.db" -o -name "*_backup_*.sqlite" -o -name "*_backup_*.sqlite3" \) -mtime +${KEEP_DAYS} -delete

echo "[$(date)] Backup process completed successfully."
