"""Registered types match across entry points, the library's warnings reach the log, and the deploy
cleanup removes an older format's files."""

from __future__ import annotations

import json
import logging
import logging.config
import subprocess
import sys
from pathlib import Path

import pytest
from app import deploy_cleanup
from app.caching import open_caches
from app.logging_config import LOGGING
from app.settings import Settings
from diskcache_oxide import Cache

APP_DIR = Path(__file__).resolve().parents[1]


def registered_after(module: str) -> list[str]:
    """The type names registered in a fresh process that imports only `module`."""
    script = f'import {module}, diskcache_oxide as d; print(__import__("json").dumps(sorted(d.registered_types())))'
    out = subprocess.run([sys.executable, '-c', script], cwd=APP_DIR, capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_the_web_app_and_the_worker_register_the_same_types() -> None:
    # A worker that forgot to import cache_types would read every Episode as a (logged) miss on a
    # durable cache, and run the slow path forever.
    web = registered_after('app.main')
    assert 'example.Episode' in web
    assert registered_after('app.worker') == web
    assert registered_after('app.deploy_cleanup') == web


def restamp(database: Path, version: int) -> None:
    """Marks a database as another on-disk format, from another process (closing a second SQLite
    library's connection in this one could drop the locks the cache holds)."""
    script = (
        'import sqlite3, sys\n'
        'con = sqlite3.connect(sys.argv[1])\n'
        'con.execute(f"PRAGMA user_version = {sys.argv[2]}")\n'
        'con.execute("PRAGMA wal_checkpoint(TRUNCATE)")\n'
        'con.close()\n'
    )
    subprocess.run([sys.executable, '-c', script, str(database), str(version)], check=True)


def test_library_warnings_reach_the_configured_handler(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # The diskcache_oxide logger exists from import; a dictConfig with the default
    # disable_existing_loggers=True would silence it. LOGGING sets it to False and names the logger.
    logging.config.dictConfig(LOGGING)
    old = tmp_path / 'c'
    Cache(old).close()
    restamp(old / 'diskcache-oxide.sqlite', 2)
    Cache(old).close()  # opens diskcache-oxide-format-3 beside the old cache, with a WARNING
    err = capsys.readouterr().err
    assert 'WARNING diskcache_oxide' in err and 'diskcache-oxide-format-3' in err, err


def test_deploy_cleanup_removes_an_older_formats_files(tmp_path: Path) -> None:
    settings = Settings(root=tmp_path / 'my-app')
    open_caches(settings.root).close()
    sessions = settings.root / 'state' / 'sessions'
    restamp(sessions / 'diskcache-oxide.sqlite', 2)  # as if an older release had written it
    caches = open_caches(settings.root)  # the new release moves into the format subdirectory
    assert Path(caches.sessions.directory).name == 'diskcache-oxide-format-3'
    caches.close()
    removed = deploy_cleanup.main(settings)
    assert str(sessions / 'diskcache-oxide.sqlite') in removed
    assert sorted(p.name for p in sessions.iterdir()) == ['diskcache-oxide-format-3']
    assert deploy_cleanup.main(settings) == [], 'nothing old left: a second run is harmless'
