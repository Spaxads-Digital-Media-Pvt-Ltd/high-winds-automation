"""Append 3 valid test leads to the happy loans tab. Does not touch other sheets."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

LEADS = [
    {
        "First Name": "Natalie",
        "Last Name": "Quinn",
        "Email Address": "natalie.quinn.hltest11@gmail.com",
        "Phone Number": "(615) 382-7451",
        "Date of Birth (DOB)": "03/21/1989",
        "SSN Full": "412-58-7391",
        "Street Address": "1207 Demonbreun St",
        "City": "Nashville",
        "State": "TN",
        "ZIP Code": "37203",
        "ABA Routing Number": "021000021",
        "Account Number": "4619283750",
        "Requested Loan Amount ($)": "1500",
        "Monthly Net Income ($)": "4100",
        "Credit Card Debt": "700",
        "Years at Address": "24",
        "Years at Employer": "30",
        "Years at Bank": "24",
        "Homeowner": "No",
        "Military": "No",
        "Direct Deposit": "Yes",
        "Income Source": "Employment",
        "Pay Frequency": "Biweekly",
        "Next Payday": "09/18/2026",
        "Employer Name": "Music City Logistics",
        "Occupation": "Dispatcher",
        "Employer Work Phone": "(615) 382-7452",
        "Driver License / ID Number": "TN46192837",
        "Driver License State": "TN",
        "Account Type": "Checking",
        "Credit Score Rating": "Fair",
        "Loan Purpose": "Other",
        "Bank Name": "Chase",
        "Own a Car": "Yes",
        "Monthly Housing Payment": "1200",
        "Has Checking Account": "Yes",
        "Verify Income": "Yes",
        "Credit Score Number": "640",
        "Business Checking Account": "No",
        "Business Age": "Not yet started",
        "Business Revenue": "50000",
    },
    {
        "First Name": "Omar",
        "Last Name": "Hassan",
        "Email Address": "omar.hassan.hltest12@gmail.com",
        "Phone Number": "(919) 473-6280",
        "Date of Birth (DOB)": "08/12/1987",
        "SSN Full": "245-69-3814",
        "Street Address": "301 Fayetteville St",
        "City": "Raleigh",
        "State": "NC",
        "ZIP Code": "27601",
        "ABA Routing Number": "021000021",
        "Account Number": "7382910465",
        "Requested Loan Amount ($)": "2000",
        "Monthly Net Income ($)": "4800",
        "Credit Card Debt": "1100",
        "Years at Address": "30",
        "Years at Employer": "36",
        "Years at Bank": "30",
        "Homeowner": "No",
        "Military": "No",
        "Direct Deposit": "Yes",
        "Income Source": "Employment",
        "Pay Frequency": "Semimonthly",
        "Next Payday": "09/15/2026",
        "Employer Name": "Triangle Medical Group",
        "Occupation": "Analyst",
        "Employer Work Phone": "(919) 473-6281",
        "Driver License / ID Number": "NC73829104",
        "Driver License State": "NC",
        "Account Type": "Checking",
        "Credit Score Rating": "Good",
        "Loan Purpose": "Debt Consolidation",
        "Bank Name": "Wells Fargo",
        "Own a Car": "Yes",
        "Monthly Housing Payment": "1350",
        "Has Checking Account": "Yes",
        "Verify Income": "Yes",
        "Credit Score Number": "675",
        "Business Checking Account": "No",
        "Business Age": "Not yet started",
        "Business Revenue": "50000",
    },
    {
        "First Name": "Chloe",
        "Last Name": "Bennett",
        "Email Address": "chloe.bennett.hltest13@gmail.com",
        "Phone Number": "(503) 291-8473",
        "Date of Birth (DOB)": "12/04/1991",
        "SSN Full": "541-27-6938",
        "Street Address": "1120 SW 5th Ave",
        "City": "Portland",
        "State": "OR",
        "ZIP Code": "97204",
        "ABA Routing Number": "021000021",
        "Account Number": "9203847561",
        "Requested Loan Amount ($)": "2500",
        "Monthly Net Income ($)": "5300",
        "Credit Card Debt": "1400",
        "Years at Address": "18",
        "Years at Employer": "24",
        "Years at Bank": "18",
        "Homeowner": "Yes",
        "Military": "No",
        "Direct Deposit": "Yes",
        "Income Source": "Employment",
        "Pay Frequency": "Biweekly",
        "Next Payday": "09/18/2026",
        "Employer Name": "Cascade Retail Ops",
        "Occupation": "Manager",
        "Employer Work Phone": "(503) 291-8474",
        "Driver License / ID Number": "OR92038475",
        "Driver License State": "OR",
        "Account Type": "Checking",
        "Credit Score Rating": "Good",
        "Loan Purpose": "Other",
        "Bank Name": "Bank of America",
        "Own a Car": "Yes",
        "Monthly Housing Payment": "1550",
        "Has Checking Account": "Yes",
        "Verify Income": "Yes",
        "Credit Score Number": "690",
        "Business Checking Account": "No",
        "Business Age": "Not yet started",
        "Business Revenue": "50000",
    },
]


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
    start = max(len(ws.col_values(1)) + 1, 2)
    rows = []
    for lead in LEADS:
        row = {h: lead.get(h, "") for h in headers}
        row["Status"] = "Pending"
        row["Notes"] = "flask test"
        row["Use_Custom_Device"] = "no"
        rows.append([row.get(h, "") for h in headers])
    end = start + len(rows) - 1
    ws.resize(rows=max(ws.row_count, end), cols=max(ws.col_count, len(headers)))
    ws.update(
        values=rows,
        range_name=f"A{start}:{gspread.utils.rowcol_to_a1(end, len(headers))}",
        value_input_option="USER_ENTERED",
    )
    print(f"Wrote {len(rows)} Pending test leads to happy loans rows {start}-{end}")
    print("Names:", [l["First Name"] for l in LEADS])


if __name__ == "__main__":
    main()
