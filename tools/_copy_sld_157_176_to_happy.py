"""Copy Simple Lending Direct rows 157-176 into happy loans rows 70-89.

Only the Happy Loans tab is written. Source rows and all other offer tabs
remain unchanged.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from tools._copy_sld_106_156_to_happy import (  # noqa: E402
    _digits,
    _payday,
    _routing,
    _ssn,
    _yn,
)

SRC_TAB = "Simple Lending Direct"
SRC_START, SRC_END = 157, 176
DEST_START, DEST_END = 70, 89


def _phone(raw: str) -> str:
    digits = _digits(raw)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return "(615) 555-0199"


def _distinct_work_phone(phone: str) -> str:
    """Use a valid-looking distinct number when SLD has no work-phone column."""
    digits = _digits(phone)
    if len(digits) != 10:
        return "(615) 555-0198"
    last = "0" if digits[-1] == "9" else str(int(digits[-1]) + 1)
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:-1]}{last}"


def main() -> None:
    creds = Credentials.from_service_account_file(
        ROOT / os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "credentials/credentials.json"),
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
    target = dest.get(f"A{DEST_START}:{gspread.utils.rowcol_to_a1(DEST_END, len(dest_headers))}")
    if any(any(str(cell).strip() for cell in row) for row in target):
        raise RuntimeError(f"Happy Loans rows {DEST_START}-{DEST_END} are not empty.")

    src_rows = src.get(
        f"A{SRC_START}:{gspread.utils.rowcol_to_a1(SRC_END, len(src_headers))}"
    )
    out: list[list[str]] = []
    names: list[str] = []
    payday_fixes = 0

    for offset, raw_row in enumerate(src_rows):
        source_row = SRC_START + offset
        src_d = dict(zip(src_headers, raw_row + [""] * (len(src_headers) - len(raw_row))))
        first = str(src_d.get("First Name") or "").strip()
        last = str(src_d.get("Last Name") or "").strip()
        if not first:
            continue

        phone = _phone(src_d.get("Phone Number", ""))
        old_payday = str(src_d.get("Next Payday") or "").strip()
        payday = _payday(old_payday)
        payday_fixes += int(bool(old_payday) and payday != old_payday)

        mapped = {
            header: str(src_d.get(header, "") or "").strip()
            for header in dest_headers
            if header in src_headers
        }
        mapped.update(
            {
                "First Name": first,
                "Last Name": last,
                "Email Address": str(src_d.get("Email Address") or "").strip(),
                "Phone Number": phone,
                "SSN Full": _ssn(src_d.get("SSN Full", "")),
                "ABA Routing Number": "'" + _routing(src_d.get("ABA Routing Number", "")),
                "Account Number": _digits(src_d.get("Account Number", "")) or "4482917365",
                "Next Payday": payday,
                "Homeowner": str(src_d.get("Homeowner") or "").strip() or "No",
                "Occupation": str(src_d.get("Occupation") or "").strip() or "Employee",
                "Employer Work Phone": _distinct_work_phone(phone),
                "Military": _yn(src_d.get("Military", ""), "No"),
                "Direct Deposit": _yn(src_d.get("Direct Deposit", ""), "Yes"),
                "Own a Car": _yn(src_d.get("Own a Car", ""), "Yes"),
                "Has Checking Account": _yn(src_d.get("Has Checking Account", ""), "Yes"),
                "Verify Income": _yn(src_d.get("Verify Income", ""), "Yes"),
                "Business Checking Account": _yn(
                    src_d.get("Business Checking Account", ""), "No"
                ),
                "Use_Custom_Device": "no",
                "Device_Model": "",
                "Android_Version": "",
                "Orientation": "",
                "Status": "Pending",
                "Notes": f"copied from SLD row {source_row}",
                "Proxy_Used": "",
                "IP": "",
                "Last_Attempt": "",
                "Retry_Count": "",
                "Submission_ID": "",
            }
        )
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
        for key, value in defaults.items():
            if not str(mapped.get(key) or "").strip():
                mapped[key] = value

        out.append([mapped.get(header, "") for header in dest_headers])
        names.append(f"{first} {last}")

    if len(out) != DEST_END - DEST_START + 1:
        raise RuntimeError(f"Expected 20 source leads, found {len(out)}.")

    dest.resize(rows=max(dest.row_count, DEST_END), cols=max(dest.col_count, len(dest_headers)))
    dest.update(
        values=out,
        range_name=f"A{DEST_START}:{gspread.utils.rowcol_to_a1(DEST_END, len(dest_headers))}",
        value_input_option="USER_ENTERED",
    )
    print(f"Wrote {len(out)} Pending leads to happy loans rows {DEST_START}-{DEST_END}")
    print(f"Payday rolled forward: {payday_fixes}")
    print("First 3:", names[:3])
    print("Last 3:", names[-3:])


if __name__ == "__main__":
    main()
