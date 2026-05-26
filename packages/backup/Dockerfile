# syntax=docker/dockerfile:1.7
#
# axis-backup sidecar image (#18).
#
# Runs a cron loop on the management VPS that:
#   1. pg_dumps the control-plane database over the docker network.
#   2. Asks vmsingle to take a snapshot via its HTTP API, then tars
#      the snapshot dir read-mounted from the vmsingle volume.
#   3. Uploads both artifacts to an S3-compatible bucket (Timeweb in
#      production; any S3 endpoint works).
#   4. Trims the local rolling buffer to N days so a fat-fingered
#      `docker compose down -v` is recoverable without a download.
#
# Everything that drifts between deploys (schedule, retention, credentials,
# endpoint) is taken from AXIS_BACKUP_* env vars — see docker-compose.yml
# and .env.example for the full set.

FROM alpine:3.20

# postgresql16-client matches the postgres:16-alpine server we dump from.
# aws-cli is the smallest dependency that speaks the S3 API including
# `--endpoint-url` (rules out s3cmd, which can't override the host).
# tini is PID 1 so the cron-supervisor receives signals cleanly.
RUN apk add --no-cache \
      bash \
      postgresql16-client \
      aws-cli \
      curl \
      tar \
      gzip \
      coreutils \
      findutils \
      tini

COPY packages/backup/backup.sh /backup.sh
RUN chmod +x /backup.sh

# `cron` keeps the supervisor running and runs `/backup.sh run-once`
# on the configured schedule. `run-once` (manual) executes a single
# backup cycle and exits — useful for first-run smoke checks.
ENTRYPOINT ["/sbin/tini", "--", "/backup.sh"]
CMD ["cron"]
