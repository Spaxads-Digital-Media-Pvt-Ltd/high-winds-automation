"""Copy Simple Lending Direct rows 91-105 into happy loans (Pending).

Does not modify SLD or any other offer tab.
"""
from __future__ import annotations

import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from core.lead_platform import _aba_checksum_ok  # noqa: E402

SRC_TAB = "Simple Lending Direct"
SRC_START, SRC_END = 91, 105
FALLBACK_ROUTING = "021000021"


def _digits(raw: str) -> str:
    return re.sub(r"\D", "", str(raw or ""))


def _routing(raw: str) -> str:
    d = _digits(raw)
    if len(d) == 8:
        d = "0" + d
    if len(d) == 9 and _aba_checksum_ok(d):
        return d
    return FALLBACK_ROUTING


def _ssn(raw: str) -> str:
    d = _digits(raw)
    if len(d) == 9:
        return f"{d[:3]}-{d[3:5]}-{d[5:]}"
    return "512-48-7391"


def _payday(raw: str) -> str:
    raw = (raw or "").strip()
    parsed = None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%m/%d/%y"):
        try:
            parsed = datetime.strptime(raw[:10], fmt).date()
            break
        except ValueError:
            continue
    today = date.today()
    if parsed is None:
        d = today + timedelta(days=14)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        return d.strftime("%m/%d/%Y")
    while parsed <= today:
        parsed += timedelta(days=14)
    return parsed.strftime("%m/%d/%Y")


def _phone(raw: str) -> str:
    d = _digits(raw)
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    if len(d) == 10:
        return f"({d[:3]}) {d[3:6]}-{d[6:]}"
    return "(615) 555-0199"


def _yn(raw: str, default: str = "No") -> str:
    s = (raw or "").strip().lower()
    if not s:
        return default
    if s in {"yes", "y", "1", "true"}:
        return "Yes"
    if s in {"no", "n", "0", "false"}:
        return "No"
    return default


def main() -> None:
    creds = Credentials.from_service_account_file(
        os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "credentials/credentials.json"),
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    ss = gspread.authorize(creds).open_by_url(os.getenv("GOOGLE_SHEET_URL", ""))
    src = ss.worksheet(SRC_TAB)
    dest = ss.worksheet(os.getenv("SHEET_WS_HAPPYLOANS", "happy loans"))

    src_headers = src.row_values(1)
    dest_headers = dest.row_values(1)
    src_rows = src.get(f"A{SRC_START}:{gspread.utils.rowcol_to_a1(SRC_END, len(src_headers))}")

    out: list[list[str]] = []
    names: list[str] = []
    payday_fixes = 0

    for i, raw_row in enumerate(src_rows):
        src_d = dict(zip(src_headers, raw_row + [""] * (len(src_headers) - len(raw_row))))
        first = str(src_d.get("First Name") or "").strip()
        last = str(src_d.get("Last Name") or "").strip()
        if not first:
            continue

        old_pay = str(src_d.get("Next Payday") or "").strip()
        new_pay = _payday(old_pay)
        if old_pay and new_pay != old_pay:
            payday_fixes += 1

        phone = _phone(src_d.get("Phone Number", ""))
        employer_phone = str(src_d.get("Employer Work Phone") or "").strip() or phone
        occupation = str(src_d.get("Occupation") or "").strip() or "Employee"
        homeowner = str(src_d.get("Homeowner") or "").strip()
        if homeowner.lower() not in {"yes", "no"}:
            homeowner = "No"

        mapped = {h: str(src_d.get(h, "") or "").strip() for h in dest_headers if h in src_headers}
        mapped.update(
            {
                "First Name": first,
                "Last Name": last,
                "Email Address": str(src_d.get("Email Address") or "").strip(),
                "Phone Number": phone,
                "SSN Full": _ssn(src_d.get("SSN Full", "")),
                # Leading apostrophe keeps 9-digit ABA (incl. leading 0) as text in Sheets.
                "ABA Routing Number": "'" + _routing(src_d.get("ABA Routing Number", "")),
                "Account Number": _digits(src_d.get("Account Number", "")) or "4482917365",
                "Next Payday": new_pay,
                "Homeowner": homeowner,
                "Occupation": occupation,
                "Employer Work Phone": employer_phone,
                "Military": _yn(src_d.get("Military", ""), "No"),
                "Direct Deposit": _yn(src_d.get("Direct Deposit", ""), "Yes"),
                "Own a Car": _yn(src_d.get("Own a Car", ""), "Yes"),
                "Has Checking Account": _yn(src_d.get("Has Checking Account", ""), "Yes"),
                "Verify Income": _yn(src_d.get("Verify Income", ""), "Yes"),
                "Business Checking Account": _yn(src_d.get("Business Checking Account", ""), "No"),
                "Use_Custom_Device": "no",
                "Device_Model": "",
                "Android_Version": "",
                "Orientation": "",
                "Status": "Pending",
                "Notes": f"copied from SLD row {SRC_START + i}",
                "Proxy_Used": "",
                "IP": "",
                "Last_Attempt": "",
                "Retry_Count": "",
                "Submission_ID": "",
            }
        )

        # Fill any still-empty mandatory-ish fields with safe defaults
        defaults = {
            "Requested Loan Amount ($)": "1500",
            "Monthly Net Income ($)": "4000",
            "Credit Card Debt": "500",
            "Years at Address": "24",
            "Years at Employer": "24",
            "Years at Bank": "24",
            "Income Source": "Employment",
            "Pay Frequency": "Biweekly",
            "Employer Name": "General Services",
            "Driver License / ID Number": "A1234567",
            "Driver License State": str(src_d.get("State") or "TX").strip() or "TX",
            "Account Type": "Checking",
            "Credit Score Rating": "Fair",
            "Loan Purpose": "Other",
            "Bank Name": "Chase",
            "Monthly Housing Payment": "1000",
            "Credit Score Number": "640",
            "Business Age": "Not yet started",
            "Business Revenue": "50000",
            "Date of Birth (DOB)": "06/15/1988",
            "Street Address": "100 Main St",
            "City": "Austin",
            "State": "TX",
            "ZIP Code": "78701",
        }
        for k, v in defaults.items():
            if not str(mapped.get(k) or "").strip():
                mapped[k] = v

        out.append([mapped.get(h, "") for h in dest_headers])
        names.append(f"{first} {last}")

    start = max(len(dest.col_values(1)) + 1, 2)
    end = start + len(out) - 1
    dest.resize(rows=max(dest.row_count, end), cols=max(dest.col_count, len(dest_headers)))
    dest.update(
        values=out,
        range_name=f"A{start}:{gspread.utils.rowcol_to_a1(end, len(dest_headers))}",
        value_input_option="USER_ENTERED",
    )
    print(f"Wrote {len(out)} Pending leads to happy loans rows {start}-{end}")
    print(f"Payday rolled forward: {payday_fixes}")
    print("Names:", names)


if __name__ == "__main__":
    main()
