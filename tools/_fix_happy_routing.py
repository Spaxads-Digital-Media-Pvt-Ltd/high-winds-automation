"""Fix ABA routing on happy loans rows 2-16 (preserve leading zeros as text)."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from core.lead_platform import _aba_checksum_ok  # noqa: E402

FALLBACK = "021000021"


def _digits(raw: str) -> str:
    return re.sub(r"\D", "", str(raw or ""))


def _routing(raw: str) -> str:
    d = _digits(raw)
    if len(d) == 8:
        d = "0" + d
    if len(d) == 9 and _aba_checksum_ok(d):
        return d
    return FALLBACK


def main() -> None:
    creds = Credentials.from_service_account_file(
        os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "credentials/credentials.json"),
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    ss = gspread.authorize(creds).open_by_url(os.getenv("GOOGLE_SHEET_URL", ""))
    ws = ss.worksheet(os.getenv("SHEET_WS_HAPPYLOANS", "happy loans"))
    headers = ws.row_values(1)
    aba_col = headers.index("ABA Routing Number") + 1
    updates = []
    for r in range(2, 17):
        val = ws.cell(r, aba_col).value
        fixed = _routing(val)
        # Apostrophe forces text so Sheets keeps leading zero.
        updates.append(
            {
                "range": gspread.utils.rowcol_to_a1(r, aba_col),
                "values": [["'" + fixed]],
            }
        )
        print(f"{r}: {val} -> {fixed}")
    ws.batch_update(updates, value_input_option="USER_ENTERED")
    print("fixed routing on rows 2-16")


if __name__ == "__main__":
    main()
