#!/bin/sh
set -e

case "$APP_ROLE" in
  api)
    exec python -m uvicorn fimbox_api.main:app \
      --host 0.0.0.0 --port "${PORT:-8000}" --no-access-log
    ;;
  worker)
    exec /opt/conda/bin/python -m celery -A fimbox_api.celery_app:celery_app worker \
      --concurrency=1 --pool=solo \
      --queues="${CELERY_TASK_DEFAULT_QUEUE:-default}" --loglevel=info
    ;;
  *)
    echo "ERROR: APP_ROLE must be 'api' or 'worker', got: '$APP_ROLE'" >&2
    exit 1
    ;;
esac
