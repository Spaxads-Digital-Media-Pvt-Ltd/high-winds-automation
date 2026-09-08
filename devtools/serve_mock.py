#!/usr/bin/env python3
"""
devtools/serve_mock.py — local stand-in for offer forms.

Serves whatever is under devtools/mock_offer/ so Settings -> Target URLs
can point at a local page instead of a live advertiser.

Nothing is stored and nothing leaves your machine.
"""
from __future__ import annotations

import argparse
import functools
import http.server
import socketserver
from pathlib import Path

ROOT = Path(__file__).parent / "mock_offer"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8799)
    args = ap.parse_args()

    handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                directory=str(ROOT))
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", args.port), handler) as httpd:
        base = f"http://127.0.0.1:{args.port}"
        print("\n  Mock offer server running:")
        print(f"    {base}/")
        print("\n  Point Settings -> Target URLs at a page under this root, then Start.")
        print("  Ctrl-C to stop.\n")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n  stopped")


if __name__ == "__main__":
    main()
