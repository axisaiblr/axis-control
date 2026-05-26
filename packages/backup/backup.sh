#!/bin/bash
#
# axis-backup entrypoint (#18).
#
# Two modes:
#   cron      (default) — install AXIS_BACKUP_CRON into crontab and
#                          hand off to crond as PID 1 (under tini).
#                          Each tick invokes `run-once`.
#   run-once             — do a single pg_dump + vmsingle-snapshot +
#                          S3-upload cycle. Useful for first-run
#                          smoke checks: `docker compose run --rm
#                          backup run-once`.
#
# Exits non-zero on the first error (set -e) so a failed cron tick is
# visible in `docker logs` rather than silently producing nothing.

set -euo pipefail

LOCAL_DIR=/var/lib/axis-backup
mkdir -p "$LOCAL_DIR"

# --- env --------------------------------------------------------------

# Required — fail loud on first run if any of these are missing.
: "${AXIS_BACKUP_POSTGRES_HOST:?required}"
: "${AXIS_BACKUP_POSTGRES_USER:?required}"
: "${AXIS_BACKUP_POSTGRES_PASSWORD:?required}"
: "${AXIS_BACKUP_POSTGRES_DB:?required}"
: "${AXIS_BACKUP_S3_ENDPOINT:?required}"
: "${AXIS_BACKUP_S3_BUCKET:?required}"
: "${AXIS_BACKUP_S3_ACCESS_KEY_ID:?required}"
: "${AXIS_BACKUP_S3_SECRET_ACCESS_KEY:?required}"
: "${AXIS_BACKUP_VMSINGLE_URL:?required}"

# Defaults — must match docker-compose.yml's interpolated defaults so
# an operator who only sets the required vars still gets a working
# schedule.
AXIS_BACKUP_POSTGRES_PORT="${AXIS_BACKUP_POSTGRES_PORT:-5432}"
AXIS_BACKUP_VMSINGLE_SNAPSHOT_DIR="${AXIS_BACKUP_VMSINGLE_SNAPSHOT_DIR:-/var/lib/vmsingle/snapshots}"
AXIS_BACKUP_S3_PREFIX="${AXIS_BACKUP_S3_PREFIX:-axis-control}"
AXIS_BACKUP_S3_REGION="${AXIS_BACKUP_S3_REGION:-ru-1}"
AXIS_BACKUP_CRON="${AXIS_BACKUP_CRON:-0 2 * * *}"
AXIS_BACKUP_LOCAL_RETENTION_DAYS="${AXIS_BACKUP_LOCAL_RETENTION_DAYS:-7}"

# aws-cli reads these from the env — no on-disk config file needed.
export AWS_ACCESS_KEY_ID="$AXIS_BACKUP_S3_ACCESS_KEY_ID"
export AWS_SECRET_ACCESS_KEY="$AXIS_BACKUP_S3_SECRET_ACCESS_KEY"
export AWS_DEFAULT_REGION="$AXIS_BACKUP_S3_REGION"

# --- one cycle --------------------------------------------------------

do_backup() {
  local ts pg_file snap_resp snap_name vm_file s3_base
  ts=$(date -u +%Y%m%d-%H%M%S)
  echo "[backup] start ts=$ts"

  # Postgres — pg_dump over the docker network. `--clean --if-exists`
  # so a restore against a non-empty database does the right thing.
  pg_file="$LOCAL_DIR/postgres-$ts.sql.gz"
  PGPASSWORD="$AXIS_BACKUP_POSTGRES_PASSWORD" pg_dump \
      -h "$AXIS_BACKUP_POSTGRES_HOST" \
      -p "$AXIS_BACKUP_POSTGRES_PORT" \
      -U "$AXIS_BACKUP_POSTGRES_USER" \
      -d "$AXIS_BACKUP_POSTGRES_DB" \
      --no-owner --no-privileges --clean --if-exists \
    | gzip -c > "$pg_file"
  echo "[backup] postgres -> $pg_file ($(wc -c < "$pg_file") bytes)"

  # VictoriaMetrics — POST /snapshot/create returns a name like
  # `{"status":"ok","snapshot":"20260526..."}`. The dir at
  # $SNAPSHOT_DIR/<name> appears on the vmsingle data volume, which
  # we have read-mounted; tar it up and ask vmsingle to delete the
  # snapshot to reclaim disk.
  snap_resp=$(curl -fsS -X POST "$AXIS_BACKUP_VMSINGLE_URL/snapshot/create")
  snap_name=$(echo "$snap_resp" | sed -n 's/.*"snapshot":"\([^"]*\)".*/\1/p')
  if [ -z "$snap_name" ]; then
      echo "[backup] could not parse vmsingle snapshot name from: $snap_resp" >&2
      exit 1
  fi
  vm_file="$LOCAL_DIR/vmsingle-$ts.tar.gz"
  tar -czf "$vm_file" -C "$AXIS_BACKUP_VMSINGLE_SNAPSHOT_DIR" "$snap_name"
  curl -fsS -X POST "$AXIS_BACKUP_VMSINGLE_URL/snapshot/delete?snapshot=$snap_name" \
    >/dev/null || true
  echo "[backup] vmsingle -> $vm_file ($(wc -c < "$vm_file") bytes)"

  # Offsite — same artifact name on the bucket so the local rolling
  # buffer and the bucket stay aligned.
  s3_base="s3://$AXIS_BACKUP_S3_BUCKET/$AXIS_BACKUP_S3_PREFIX"
  aws --endpoint-url "$AXIS_BACKUP_S3_ENDPOINT" s3 cp \
      "$pg_file" "$s3_base/postgres/$(basename "$pg_file")"
  aws --endpoint-url "$AXIS_BACKUP_S3_ENDPOINT" s3 cp \
      "$vm_file" "$s3_base/vmsingle/$(basename "$vm_file")"
  echo "[backup] uploaded to $s3_base"

  # Trim the local rolling buffer. Bucket-side retention is a
  # lifecycle rule the operator sets on the bucket (documented in
  # .env.example) — we deliberately do NOT prune the bucket from here,
  # to keep "delete remote backups" off this image's permission scope.
  find "$LOCAL_DIR" -type f -mtime "+$AXIS_BACKUP_LOCAL_RETENTION_DAYS" -delete

  echo "[backup] done ts=$ts"
}

# --- mode dispatch ----------------------------------------------------

mode="${1:-cron}"
case "$mode" in
  run-once)
    do_backup
    ;;
  cron)
    # crond on alpine reads /etc/crontabs/root. Route the cron job's
    # stdout/stderr to PID 1 so `docker logs` shows backup output the
    # same way it shows the supervisor's startup line.
    echo "$AXIS_BACKUP_CRON /backup.sh run-once >> /proc/1/fd/1 2>> /proc/1/fd/2" \
      > /etc/crontabs/root
    echo "[backup] cron schedule: $AXIS_BACKUP_CRON"
    exec crond -f -l 8
    ;;
  *)
    echo "usage: $0 [cron|run-once]" >&2
    exit 2
    ;;
esac
