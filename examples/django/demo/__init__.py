from typing import Optional

# The process that loaded the app and touched the cache first: gunicorn's master under preload_app
# (demo/wsgi.py sets it), so every worker reports the same pid, and none reports its own.
warmed_in: Optional[int] = None
