#!/usr/bin/env python3
"""Serve the committed site/ under the production subpath for a faithful local preview.

Modeled after chameleon-rs (https://github.com/mikeckennedy/chameleon-rs).
This mirrors the nginx ``alias`` deployment (``site/`` served at ``/docs/diskcache-oxide``)
so local preview exercises the exact subpath the site is deployed under -- catching any
asset/link that would break only when hosted in a subdirectory. Port 8097, so it can run
next to chameleon-rs (8098) and chameleon_partials (8099).

    venv/bin/python scripts/serve_docs.py   # -> http://127.0.0.1:8097/docs/diskcache-oxide/
"""

from __future__ import annotations

import functools
import http.server
import socketserver
from pathlib import Path

PREFIX = '/docs/diskcache-oxide'
PORT = 8097
ROOT = Path(__file__).resolve().parent.parent / 'site'


class H(http.server.SimpleHTTPRequestHandler):
    def translate_path(self, path: str) -> str:
        c = path.split('?', 1)[0].split('#', 1)[0]
        if c.startswith(PREFIX):
            path = c[len(PREFIX) :] or '/'
        return super().translate_path(path)

    def send_head(self) -> object:
        if self.path in ('/', PREFIX):
            self.send_response(302)
            self.send_header('Location', PREFIX + '/')
            self.end_headers()
            return None
        return super().send_head()


class S(socketserver.TCPServer):
    allow_reuse_address = True


def main() -> None:
    if not ROOT.is_dir():
        raise SystemExit(f'Run build_docs.py first; {ROOT} missing')
    with S(('127.0.0.1', PORT), functools.partial(H, directory=str(ROOT))) as httpd:
        print(f'-> http://127.0.0.1:{PORT}{PREFIX}/  (Ctrl+C to stop)')
        httpd.serve_forever()


if __name__ == '__main__':
    main()
