"""Fill one Happy Loans pending lead with a visible Chrome window."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")
os.environ["HEADLESS"] = "false"

from core.form_filler_happyloans import FormFiller, FormFillerError  # noqa: E402
from utils.device_manager import DeviceManager  # noqa: E402
from utils.proxy_manager import ProxyManager  # noqa: E402
from utils.sheet_handler import SheetHandler  # noqa: E402


def main() -> int:
    with open(ROOT / "config.yaml", encoding="utf-8") as fh:
        config = yaml.safe_load(fh)
    config["target"]["url"] = os.getenv(
        "HAPPY_LOANS_URL",
        "https://www.happyloans.net/submit-loan-request.php",
    )
    config["browser"] = {"channel": "chromium", "engine_tag": "happy_loans_chat"}
    config.setdefault("screenshots", {})["directory"] = "screenshots/happy_loans"
    config["screenshots"]["enabled"] = True
    Path("screenshots/happy_loans").mkdir(parents=True, exist_ok=True)

    sheet = SheetHandler(
        config,
        sheet_url=os.getenv("SHEET_URL_HAPPYLOANS") or os.getenv("GOOGLE_SHEET_URL", ""),
        worksheet_name=os.getenv("SHEET_WS_HAPPYLOANS", "happy loans"),
    )
    pending = sheet.get_pending_rows()
    only = (os.getenv("HAPPY_ONLY") or "").strip().lower()
    if only:
        pending = [
            r for r in pending
            if only in f"{r.get('First Name','')} {r.get('Last Name','')}".lower()
            or only in str(r.get("Notes") or "").lower()
            or only in str(r.get("Email Address") or "").lower()
        ]
    else:
        tests = [r for r in pending if "test lead" in str(r.get("Notes") or "").strip().lower()]
        if tests:
            pending = tests
    print(f"pending Happy Loans rows: {len(pending)}", flush=True)
    if not pending:
        print("No Pending leads on the happy loans tab.", flush=True)
        return 1

    row = pending[0]
    row_num = row["_row_number"]
    print(f"filling row {row_num} {row.get('First Name')} {row.get('Last Name')}", flush=True)
    sheet.mark_in_progress(row_num)

    proxy_mgr = ProxyManager()
    device_mgr = DeviceManager(config)
    filler = FormFiller(config)
    proxy_url = None
    if os.getenv("HAPPY_DIRECT", "1").lower() not in ("1", "true", "yes"):
        proxy_url = proxy_mgr.next_proxy()
    fingerprint = device_mgr.build_fingerprint(row)
    print(f"proxy={'yes' if proxy_url else 'direct'} headed Chromium", flush=True)

    try:
        result = filler.process_row(
            row=row,
            fingerprint=fingerprint,
            proxy_url=proxy_url,
            row_number=row_num,
            stop_event=None,
        )
        sheet.update_row(
            row_num,
            status="Success",
            notes=result.get("notes", ""),
            proxy_used=proxy_url or "direct",
            submission_id=result.get("submission_id", ""),
        )
        print("SUCCESS", result, flush=True)
        return 0
    except FormFillerError as exc:
        sheet.update_row(
            row_num,
            status="Failed",
            notes=f"[{exc.error_type}] {exc}",
            proxy_used=proxy_url or "direct",
        )
        print("STUCK/FAIL", exc.error_type, exc, flush=True)
        return 2
    except Exception as exc:
        sheet.update_row(
            row_num,
            status="Failed",
            notes=f"[unexpected] {exc}",
            proxy_used=proxy_url or "direct",
        )
        print("UNEXPECTED", type(exc).__name__, exc, flush=True)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
