"""gunicorn settings: `gunicorn -c gunicorn.conf.py`, from this directory."""

import os

wsgi_app = 'demo.wsgi:application'
workers = int(os.environ.get('WEB_WORKERS', '4'))
bind = os.environ.get('BIND', '127.0.0.1:8000')
# Load the app once in the master, then fork: the workers inherit Django and its open cache handles.
preload_app = True
# Longer than the cache's 1 s lock wait, so a blocked write raises Timeout before the worker is killed.
timeout = 30
graceful_timeout = 10
