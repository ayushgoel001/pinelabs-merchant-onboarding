"""Serve generated sample documents on loopback with CORS, so a browser demo can pick them up in the upload control.

    python tools/serve_samples.py private/samples 8778
"""
import sys
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    folder, port = Path(sys.argv[1]), int(sys.argv[2])
    HTTPServer(("127.0.0.1", port), partial(Handler, directory=str(folder))).serve_forever()
