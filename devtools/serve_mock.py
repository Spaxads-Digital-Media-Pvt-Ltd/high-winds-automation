#!/usr/bin/env python3
"""
devtools/serve_mock.py — local stand-ins for both offer forms.

Serves devtools/mock_offer/, giving one mock per offer:

    http://127.0.0.1:8799/aef/index.html   American Emergency Fund
    http://127.0.0.1:8799/mlw/index.html   MyLendingWallet

Point Settings -> Target URLs at the relevant one and press Start.  The whole
pipeline runs — sheet read, engine, retries, live preview, status write-back —
without sending anything to the real advertiser.

How faithful each one is differs, and it matters:

  aef/  Built from the live site's own JS (template/8735/js/fields.js +
        funnel.js): same field names, same option values, same required-field
        semantics as its validateStep().  A pass here is strong evidence.

  mlw/  Built from the live site's OBSERVED DOM contract only — regenerated form
        id per render, choices as <button> carrying the visible label, platform
        `name` attributes on inputs.  Its option LABELS are the ones the filler
        assumes (inherited from AEF's vocabulary), because mylendingwallet.com
        fetches step content from the server at runtime rather than shipping it
        in its bundle.  A pass here proves the filler's mechanics work; it does
        NOT prove the real site uses these labels.  See README.

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
        print("\n  Mock offer forms running:")
        print(f"    American Emergency Fund   {base}/aef/index.html")
        print(f"    MyLendingWallet           {base}/mlw/index.html")
        print("\n  Point Settings -> Target URLs at one of these, then Start.")
        print("  Ctrl-C to stop.\n")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n  stopped")


if __name__ == "__main__":
    main()
