from __future__ import annotations

from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def serve_static_reports(directory: Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(directory), **kwargs)

    ThreadingHTTPServer((host, port), Handler).serve_forever()
