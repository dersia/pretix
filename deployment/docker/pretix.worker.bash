#!/bin/bash

python -m pretix migrate

celery -A pretix.celery_app worker -l info