"""Add three synthetic Pending test leads to happy loans rows 72-74."""
from __future__ import annotations

import os
from pathlib import Path

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

LEADS = [
    {
        "First Name": "Noah",
        "Last Name": "Prescott",
        "Email Address": "noah.prescott.hltest17@gmail.com",
        "Phone Number": "(602) 471-8352",
        "Date of Birth (DOB)": "02/11/1988",
        "SSN Full": "428-61-7395",
        "Street Address": "210 W Washington St",
        "City": "Phoenix",
        "State": "AZ",
        "ZIP Code": "85003",
        "ABA Routing Number": "'021000021",
        "Account Number": "5839271046",
        "Requested Loan Amount ($)": "1500",
        "Monthly Net Income ($)": "4400",
        "Credit Card Debt": "650",
        "Years at Address": "24",
        "Years at Employer": "36",
        "Years at Bank": "24",
        "Homeowner": "No",
        "Military": "No",
        "Direct Deposit": "Yes",
        "Income Source": "Employment",
        "Pay Frequency": "Biweekly",
        "Next Payday": "09/11/2026",
        "Employer Name": "Sonoran Business Services",
        "Occupation": "Coordinator",
        "Employer Work Phone": "(602) 471-8353",
        "Driver License / ID Number": "AZ58392710",
        "Driver License State": "AZ",
        "Account Type": "Checking",
        "Credit Score Rating": "Good",
        "Loan Purpose": "Other",
        "Bank Name": "Chase",
        "Use_Custom_Device": "no",
        "Status": "Pending",
        "Notes": "manual flask test row 72",
        "Monthly Housing Payment": "1200",
        "Own a Car": "Yes",
        "Has Checking Account": "Yes",
        "Verify Income": "Yes",
        "Credit Score Number": "675",
        "Business Checking Account": "No",
        "Business Age": "Not yet started",
        "Business Revenue": "50000",
    },
    {
        "First Name": "Grace",
        "Last Name": "Holloway",
        "Email Address": "grace.holloway.hltest18@gmail.com",
        "Phone Number": "(214) 583-7461",
        "Date of Birth (DOB)": "07/24/1991",
        "SSN Full": "537-28-6941",
        "Street Address": "1700 Pacific Ave",
        "City": "Dallas",
        "State": "TX",
        "ZIP Code": "75201",
        "ABA Routing Number": "'021000021",
        "Account Number": "6948210375",
        "Requested Loan Amount ($)": "2000",
        "Monthly Net Income ($)": "5100",
        "Credit Card Debt": "1000",
        "Years at Address": "18",
        "Years at Employer": "30",
        "Years at Bank": "18",
        "Homeowner": "No",
        "Military": "No",
        "Direct Deposit": "Yes",
        "Income Source": "Employment",
        "Pay Frequency": "Semimonthly",
        "Next Payday": "09/15/2026",
        "Employer Name": "Lone Star Medical Supply",
        "Occupation": "Specialist",
        "Employer Work Phone": "(214) 583-7462",
        "Driver License / ID Number": "TX69482103",
        "Driver License State": "TX",
        "Account Type": "Checking",
        "Credit Score Rating": "Fair",
        "Loan Purpose": "Debt Consolidation",
        "Bank Name": "Wells Fargo",
        "Use_Custom_Device": "no",
        "Status": "Pending",
        "Notes": "manual flask test row 73",
        "Monthly Housing Payment": "1350",
        "Own a Car": "Yes",
        "Has Checking Account": "Yes",
        "Verify Income": "Yes",
        "Credit Score Number": "640",
        "Business Checking Account": "No",
        "Business Age": "Not yet started",
        "Business Revenue": "50000",
    },
    {
        "First Name": "Ethan",
        "Last Name": "McCall",
        "Email Address": "ethan.mccall.hltest19@gmail.com",
        "Phone Number": "(704) 392-6817",
        "Date of Birth (DOB)": "11/16/1985",
        "SSN Full": "614-39-8275",
        "Street Address": "301 S Tryon St",
        "City": "Charlotte",
        "State": "NC",
        "ZIP Code": "28202",
        "ABA Routing Number": "'021000021",
        "Account Number": "8273941056",
        "Requested Loan Amount ($)": "2500",
        "Monthly Net Income ($)": "5700",
        "Credit Card Debt": "1300",
        "Years at Address": "30",
        "Years at Employer": "48",
        "Years at Bank": "30",
        "Homeowner": "Yes",
        "Military": "No",
        "Direct Deposit": "Yes",
        "Income Source": "Employment",
        "Pay Frequency": "Biweekly",
        "Next Payday": "09/18/2026",
        "Employer Name": "Carolina Tech Partners",
        "Occupation": "Supervisor",
        "Employer Work Phone": "(704) 392-6818",
        "Driver License / ID Number": "NC82739410",
        "Driver License State": "NC",
        "Account Type": "Checking",
        "Credit Score Rating": "Good",
        "Loan Purpose": "Other",
        "Bank Name": "Bank of America",
        "Use_Custom_Device": "no",
        "Status": "Pending",
        "Notes": "manual flask test row 74",
        "Monthly Housing Payment": "1500",
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

    existing = ws.get("A72:AZ74")
    if any(any(str(cell).strip() for cell in row) for row in existing):
        raise RuntimeError("Rows 72-74 are not empty; refusing to overwrite them.")

    rows = [[lead.get(header, "") for header in headers] for lead in LEADS]
    ws.resize(rows=max(ws.row_count, 74), cols=max(ws.col_count, len(headers)))
    ws.update(
        values=rows,
        range_name=f"A72:{gspread.utils.rowcol_to_a1(74, len(headers))}",
        value_input_option="USER_ENTERED",
    )
    print("Wrote 3 Pending leads to happy loans rows 72-74")
    print("Names:", [f"{lead['First Name']} {lead['Last Name']}" for lead in LEADS])


if __name__ == "__main__":
    main()
