"""Add three synthetic Pending test leads to happy loans rows 69-71 only."""
from __future__ import annotations

import os
import re
from pathlib import Path

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

LEADS = [
    {
        "First Name": "Avery",
        "Last Name": "Mitchell",
        "Email Address": "avery.mitchell.hltest14@gmail.com",
        "Phone Number": "(720) 481-6392",
        "Date of Birth (DOB)": "01/19/1986",
        "SSN Full": "417-62-8395",
        "Street Address": "1540 Larimer St",
        "City": "Denver",
        "State": "CO",
        "ZIP Code": "80202",
        "ABA Routing Number": "'021000021",
        "Account Number": "4738291056",
        "Requested Loan Amount ($)": "1500",
        "Monthly Net Income ($)": "4600",
        "Credit Card Debt": "700",
        "Years at Address": "24",
        "Years at Employer": "36",
        "Years at Bank": "24",
        "Homeowner": "No",
        "Military": "No",
        "Direct Deposit": "Yes",
        "Income Source": "Employment",
        "Pay Frequency": "Biweekly",
        "Next Payday": "09/11/2026",
        "Employer Name": "Front Range Distribution",
        "Occupation": "Coordinator",
        "Employer Work Phone": "(720) 481-6393",
        "Driver License / ID Number": "CO47382910",
        "Driver License State": "CO",
        "Account Type": "Checking",
        "Credit Score Rating": "Good",
        "Loan Purpose": "Other",
        "Bank Name": "Chase",
        "Use_Custom_Device": "no",
        "Status": "Pending",
        "Notes": "manual flask test row 69",
        "Monthly Housing Payment": "1250",
        "Own a Car": "Yes",
        "Has Checking Account": "Yes",
        "Verify Income": "Yes",
        "Credit Score Number": "675",
        "Business Checking Account": "No",
        "Business Age": "Not yet started",
        "Business Revenue": "50000",
    },
    {
        "First Name": "Jordan",
        "Last Name": "Ellis",
        "Email Address": "jordan.ellis.hltest15@gmail.com",
        "Phone Number": "(614) 392-7481",
        "Date of Birth (DOB)": "06/08/1990",
        "SSN Full": "528-41-7396",
        "Street Address": "88 E Broad St",
        "City": "Columbus",
        "State": "OH",
        "ZIP Code": "43215",
        "ABA Routing Number": "'021000021",
        "Account Number": "5829371046",
        "Requested Loan Amount ($)": "2000",
        "Monthly Net Income ($)": "4300",
        "Credit Card Debt": "900",
        "Years at Address": "18",
        "Years at Employer": "30",
        "Years at Bank": "18",
        "Homeowner": "No",
        "Military": "No",
        "Direct Deposit": "Yes",
        "Income Source": "Employment",
        "Pay Frequency": "Semimonthly",
        "Next Payday": "09/15/2026",
        "Employer Name": "Buckeye Office Supply",
        "Occupation": "Specialist",
        "Employer Work Phone": "(614) 392-7482",
        "Driver License / ID Number": "OH58293710",
        "Driver License State": "OH",
        "Account Type": "Checking",
        "Credit Score Rating": "Fair",
        "Loan Purpose": "Debt Consolidation",
        "Bank Name": "Wells Fargo",
        "Use_Custom_Device": "no",
        "Status": "Pending",
        "Notes": "manual flask test row 70",
        "Monthly Housing Payment": "1150",
        "Own a Car": "Yes",
        "Has Checking Account": "Yes",
        "Verify Income": "Yes",
        "Credit Score Number": "635",
        "Business Checking Account": "No",
        "Business Age": "Not yet started",
        "Business Revenue": "50000",
    },
    {
        "First Name": "Maya",
        "Last Name": "Lawson",
        "Email Address": "maya.lawson.hltest16@gmail.com",
        "Phone Number": "(404) 583-2176",
        "Date of Birth (DOB)": "10/27/1984",
        "SSN Full": "319-57-8246",
        "Street Address": "675 Peachtree St NE",
        "City": "Atlanta",
        "State": "GA",
        "ZIP Code": "30308",
        "ABA Routing Number": "'021000021",
        "Account Number": "6948210375",
        "Requested Loan Amount ($)": "2500",
        "Monthly Net Income ($)": "5500",
        "Credit Card Debt": "1200",
        "Years at Address": "30",
        "Years at Employer": "48",
        "Years at Bank": "30",
        "Homeowner": "Yes",
        "Military": "No",
        "Direct Deposit": "Yes",
        "Income Source": "Employment",
        "Pay Frequency": "Biweekly",
        "Next Payday": "09/18/2026",
        "Employer Name": "Peachtree Health Services",
        "Occupation": "Supervisor",
        "Employer Work Phone": "(404) 583-2177",
        "Driver License / ID Number": "GA69482103",
        "Driver License State": "GA",
        "Account Type": "Checking",
        "Credit Score Rating": "Good",
        "Loan Purpose": "Other",
        "Bank Name": "Bank of America",
        "Use_Custom_Device": "no",
        "Status": "Pending",
        "Notes": "manual flask test row 71",
        "Monthly Housing Payment": "1450",
        "Own a Car": "Yes",
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
        ROOT / os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "credentials/credentials.json"),
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    ss = gspread.authorize(creds).open_by_url(os.getenv("GOOGLE_SHEET_URL", ""))
    ws = ss.worksheet(os.getenv("SHEET_WS_HAPPYLOANS", "happy loans"))
    headers = ws.row_values(1)

    existing = ws.get("A69:AZ71")
    if any(any(str(cell).strip() for cell in row) for row in existing):
        raise RuntimeError("Rows 69-71 are not empty; refusing to overwrite them.")

    rows = [[lead.get(header, "") for header in headers] for lead in LEADS]
    ws.resize(rows=max(ws.row_count, 71), cols=max(ws.col_count, len(headers)))
    ws.update(
        values=rows,
        range_name=f"A69:{gspread.utils.rowcol_to_a1(71, len(headers))}",
        value_input_option="USER_ENTERED",
    )
    print("Wrote 3 Pending leads to happy loans rows 69-71")
    print("Names:", [f"{lead['First Name']} {lead['Last Name']}" for lead in LEADS])


if __name__ == "__main__":
    main()
