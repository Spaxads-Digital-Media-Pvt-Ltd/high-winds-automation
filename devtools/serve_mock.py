#!/usr/bin/env python3
"""
devtools/serve_mock.py — local stand-in for the offer form.

Serves devtools/mock_offer/:

    http://127.0.0.1:8799/aef/index.html   American Emergency Fund

Point Settings -> Target URLs at it and press Start.  The whole pipeline runs —
sheet read, engine, retries, live preview, status write-back — without sending
anything to the real advertiser.

  aef/  Built from the live site's own JS (template/8735/js/fields.js +
        funnel.js): same field names, same option values, same required-field
        semantics as its validateStep().  A pass here is strong evidence.

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
        print("\n  Mock offer form running:")
        print(f"    American Emergency Fund   {base}/aef/index.html")
        print("\n  Point Settings -> Target URLs at it, then Start.")
        print("  Ctrl-C to stop.\n")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n  stopped")


if __name__ == "__main__":
    main()
