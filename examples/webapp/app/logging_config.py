"""One logging setup for the web workers (gunicorn's logconfig_dict) and the background worker.

`disable_existing_loggers` must be False: the diskcache_oxide logger exists from the moment the
package is imported, and dictConfig's default (True) would silence it, and with it every report a
durable cache makes instead of raising.
"""

from __future__ import annotations

LOGGING: dict = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'plain': {'format': '%(asctime)s %(levelname)s %(name)s [%(process)d]: %(message)s'},
    },
    'handlers': {
        'console': {'class': 'logging.StreamHandler', 'formatter': 'plain'},
    },
    'loggers': {
        'diskcache_oxide': {'level': 'INFO', 'handlers': ['console'], 'propagate': False},
        'app': {'level': 'INFO', 'handlers': ['console'], 'propagate': False},
        'gunicorn.error': {'level': 'INFO', 'handlers': ['console'], 'propagate': False},
    },
    'root': {'level': 'WARNING', 'handlers': ['console']},
}
