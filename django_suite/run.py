"""Run Django's test command from a Django sdist, in this process, against diskcache_oxide.DjangoCache.

    python -I django_suite/run.py <sdist root> <exact version> test [labels and options ...]

scripts/django-suite.sh fetches each pinned sdist and calls this. The sdist's root goes first on
`sys.path`, so its `django` shadows any installed copy, and its `tests/` goes last, so nothing in it
can shadow a real module. Never run Django's suite through the `django-admin` script or
`python -m django`: both import the installed Django before `sys.path` can change. Under `-I`,
`PYTHONPATH`, the working directory and `PYTHON_GIL` can't change what loads.
"""

import os
import sys
import sysconfig

HERE = os.path.dirname(os.path.abspath(__file__))
root = os.path.abspath(sys.argv[1])
version = sys.argv[2]
sys.path[:0] = [root, HERE]
sys.path.append(os.path.join(root, 'tests'))
os.environ['DJANGO_SETTINGS_MODULE'] = 'oxide_suite_settings'

import diskcache_oxide  # noqa: E402
import django  # noqa: E402

if os.path.dirname(os.path.abspath(django.__file__)) != os.path.join(root, 'django'):
    sys.exit(f'django was imported from {django.__file__}, not from {root}')
if django.get_version() != version:
    sys.exit(f'the sdist at {root} is Django {django.get_version()}, not {version}')
free_threaded = bool(sysconfig.get_config_var('Py_GIL_DISABLED'))
print(
    f'Django {version} from {root}\n'
    f'diskcache_oxide {diskcache_oxide.__version__} from {os.path.dirname(diskcache_oxide.__file__)}\n'
    f'Python {sys.version.split()[0]}{" (free-threaded)" if free_threaded else ""}',
    flush=True,
)

from django.core.management import execute_from_command_line  # noqa: E402

try:
    execute_from_command_line(['django-admin', *sys.argv[3:]])
    code = 0
except SystemExit as stop:
    code = stop.code if isinstance(stop.code, int) else (0 if stop.code is None else 1)

# Every Django module this run loaded came from the sdist.
strays = sorted(
    name
    for name, module in list(sys.modules.items())
    if (name == 'django' or name.startswith('django.'))
    and getattr(module, '__file__', None)
    and not os.path.abspath(module.__file__).startswith(root + os.sep)
)
# Django's test app has a generic name; make sure it's the sdist's.
test_app = sys.modules.get('cache.tests')
if test_app is not None and not os.path.abspath(test_app.__file__).startswith(os.path.join(root, 'tests') + os.sep):
    strays.append(f'cache.tests ({test_app.__file__})')
if strays:
    print(f'Django modules loaded from outside {root}: {strays[:5]}', flush=True)
    code = code or 1

# A guard on _core's free-threading flag: if the extension (or anything the suite loads) turned the GIL
# back on, the run tested the wrong thing.
if free_threaded and sys._is_gil_enabled():
    print('the GIL was turned back on during the run', flush=True)
    code = code or 1
elif free_threaded:
    print('the GIL stayed off', flush=True)
sys.exit(code)
