#!/bin/bash

GUNICORN_WORKERS="${GUNICORN_WORKERS:-${WEB_CONCURRENCY:-$((2 * $(nproc)))}}"
GUNICORN_MAX_REQUESTS="${GUNICORN_MAX_REQUESTS:-1200}"
GUNICORN_MAX_REQUESTS_JITTER="${GUNICORN_MAX_REQUESTS_JITTER:-50}"

python -m pretix migrate

gunicorn --name pretix --workers "${GUNICORN_WORKERS}" --max-requests "${GUNICORN_MAX_REQUESTS}" --max-requests-jitter "${GUNICORN_MAX_REQUESTS_JITTER}" --log-level=info --bind=0.0.0.0:8000 pretix.wsgi