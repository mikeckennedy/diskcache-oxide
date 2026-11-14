"""gunicorn settings: `gunicorn -c gunicorn.conf.py`."""

import os

from app.logging_config import LOGGING

wsgi_app = 'app.main:create_app()'  # a factory: each worker builds its app, and opens its caches, itself
worker_class = 'uvicorn_worker.UvicornWorker'
workers = int(os.environ.get('WEB_WORKERS', '4'))
bind = os.environ.get('BIND', '0.0.0.0:8000')
logconfig_dict = LOGGING
# Behind a proxy, trust its X-Forwarded-For so request.client.host is the real client (the rate
# limits depend on it). Addresses or CIDR ranges: in Compose the proxy is another container with an
# address that changes, so give the Compose network's subnet (`docker network inspect`).
forwarded_allow_ips = os.environ.get('FORWARDED_ALLOW_IPS', '127.0.0.1,::1')
graceful_timeout = 10
