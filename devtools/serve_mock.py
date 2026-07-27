#!/usr/bin/env python3
"""
devtools/serve_mock.py — local stand-in for the American Emergency Fund form.

Serves devtools/mock_offer/, which reproduces the live site's DOM contract: one
step at a time inside #applicantForm, advanced by #nextBtn, the same field names
and option values as template/8735/js/fields.js, and the same required-field
semantics as its validateStep().  Completing it lands on a URL containing
cmd=RenderResult, which is what the filler treats as a delivered lead.

Use it to exercise the whole pipeline — sheet read, engine, live preview,
status write-back — without sending anything to the real advertiser.

    python devtools/serve_mock.py
    # then in the UI: Settings -> Target URLs -> http://127.0.0.1:8799/index.html

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
        url = f"http://127.0.0.1:{args.port}/index.html"
        print(f"\n  Mock offer form running at {url}")
        print(f"  Point Settings -> Target URLs at that address, then Start.")
        print(f"  Ctrl-C to stop.\n")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n  stopped")


if __name__ == "__main__":
    main()
