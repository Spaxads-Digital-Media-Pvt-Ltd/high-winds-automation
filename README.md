# 🚀 Lead Automation Engine

Reads leads from Google Sheets, rotates proxy + device fingerprint per attempt,
fills the **American Emergency Fund** loan application via Playwright, and writes
the result back to the sheet.

---

## 📁 Project Structure

```
lead-automation/
├── app.py                  # Flask multi-engine web UI (primary entry point)
├── main.py                 # Headless single-pass runner (used by run.sh / Docker)
├── config.yaml             # Delays, retry, pacing, screenshots, column mapping
├── .env                    # Secrets — git-ignored
├── devices_pool.py         # 27 real Android device fingerprints
├── credentials/
│   └── credentials.json    # Google Service Account key (git-ignored)
├── utils/
│   ├── sheet_handler.py    # Google Sheets read/write via gspread
│   ├── proxy_manager.py    # Proxy rotation (file / env / rotating gateway)
│   ├── device_manager.py   # Device fingerprint builder
│   ├── stealth.py          # Anti-detection JS patches + human-like helpers
│   └── lead_pacer.py       # Hour-by-hour lead release scheduler
├── core/
│   └── form_filler_aef.py  # americanemergencyfund.com form automation
├── logs/                   # Structured log files (git-ignored)
└── screenshots/            # Live preview + failure captures (git-ignored)
```

---

## ⚡ Quick Start

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
playwright install chromium

cp credentials.json credentials/credentials.json   # Google service account
# edit .env with your sheet URL + proxy settings

python app.py            # web UI on http://localhost:5000
# or
python main.py           # one headless pass over all pending rows
```

Share the Google Sheet with the service-account email (Editor access).

---

## 📋 Sheet Columns

Status columns are created automatically if absent. Lead columns are read with
tolerant fallbacks — the first name of each pair below is preferred.

| Column | Required | Notes |
|--------|----------|-------|
| `First Name` | ✅ | letters/spaces/apostrophes/hyphens only |
| `Last Name` | ✅ | generic values like "test" are rejected by the site |
| `Email Address` | ✅ | |
| `Phone Number` | ✅ | US number, area code must start 2–9 |
| `Date of Birth (DOB)` | ✅ | any common format; age must be 18–120 |
| `SSN Full` | ✅ | last 4 derived automatically |
| `Street Address` / `City` / `State` | ✅ | state name or 2-letter code |
| `ZIP Code` | ✅ | exactly 5 digits |
| `ABA Routing Number` | ✅ | 9 digits, **must pass the ABA checksum** |
| `Account Number` | ✅ | 5–18 digits |
| `Requested Loan Amount ($)` | | clamped to 100–35 000, default 5 000 |
| `Monthly Net Income ($)` | | mapped to the site's bracket, default 3 000 |
| `Credit Card Debt` | | mapped to the site's bracket, default "none" |
| `Years at Address` / `Years at Employer` / `Years at Bank` | | years or months; default "5 years or more" |
| `Homeowner` / `Military` | | yes/no, default no |
| `Direct Deposit` | | yes/no, default yes |
| `Income Source` | | "benefits"-like values → benefits, else employed |
| `Pay Frequency` | | weekly / bi-weekly / semi-monthly / monthly |
| `Employer Name` | | default "Employer" |
| `Driver License / ID Number`, `Driver License State` | | state falls back to `State` |
| `Account Type` | | checking (default) / savings |
| `Credit Score Rating` | | word or number; default "not sure" |
| `Loan Purpose` | | credit-card / debt-consolidation / other |
| `Bank Name` | | only used if the site ever renders the field; it normally derives this from the routing number |
| `Use_Custom_Device` | | `yes` to pin the fingerprint to the three columns below, else random |
| `Device_Model` / `Android_Version` / `Orientation` | | e.g. `Pixel 8` / `14` / `portrait`\|`landscape`\|`random` |
| `Status` | ✅ | set to **Pending** for new rows |

**A blank `Status` also counts as pending** — a pasted row with no status will be
picked up on the next run. Set it to anything else to park a row.

The engine writes back `Status`, `Notes`, `Proxy_Used`, `IP`, `Last_Attempt`,
`Retry_Count` and `Submission_ID`; these are created automatically if absent.

Rows failing a client-side rule (ABA checksum, age, phone shape, ZIP length) are
marked `Failed [missing_data]` **before** a browser is launched, so a bad row
costs no proxy traffic.

Keep `SSN Full`, `ABA Routing Number`, `Account Number`, `ZIP Code` and the phone
columns formatted as **plain text** in Sheets, or leading zeros are silently
dropped (`021000021` → `21000021`, which then fails the ABA checksum). The
provisioned sheet already has this formatting applied.

---

## 🎯 The Target Form

`americanemergencyfund.com` serves a native Bootstrap 5 wizard on the landing
page — no iframe. One step renders at a time inside `#applicantForm`, advanced
by `#nextBtn` (which becomes "Request Loan" on the last step).

**Step order is decided server-side per session.** The page injects a
`missingFields` array; only steps carrying a still-missing field are displayed.
A returning applicant or a post-validation retry therefore sees a shorter,
different sequence. `core/form_filler_aef.py` is written accordingly: each
iteration reads the field names currently rendered and dispatches on those, so
any order works and skipped or repeated steps are handled naturally. Adding a
new step means adding one entry to the handler map.

Full field/value reference is in the module docstring of
[`core/form_filler_aef.py`](core/form_filler_aef.py).

**Completion** is detected by URL: `/?cmd=RenderResult&uuid=…` (approved, or
declined with offers) or a redirect to `offer.requestedresults.com` (declined /
rejected / processing error). Both are recorded as delivered, with the specific
outcome written to the sheet's `Notes` column.

---

## ⚠️ Known Blocker: Anura Bot Detection

The site loads `script.anura.io`, a commercial ad-fraud detection service.
**With that script active, the headless Chromium renderer crashes reproducibly**
part-way through page load — the form never renders and no lead can be filled.

Verified by isolation:

| Condition | Result |
|-----------|--------|
| Headless, `anura.io` reachable | renderer crash, form never renders |
| Headless, `anura.io` blocked at the network layer | loads normally, step 0 renders |

The crash is caused by the fraud-detection script, not by the site's own code,
the proxy configuration, or the device fingerprint.

Anura exists specifically to identify non-human form submissions, so suppressing
it is not a supported configuration of this tool and is deliberately not
implemented. If you have a commercial relationship with this advertiser, the
practical routes forward are a server-to-server posting agreement or an
allow-listed integration — ask your affiliate manager. The filler itself is
complete and will work as soon as the page renders.

---

## 🛡️ Anti-Detection Features

- Stealth JS injection — hides `navigator.webdriver`, spoofs WebGL / canvas / plugins
- 27 real Android device fingerprints (Pixel, Galaxy, OnePlus, Xiaomi…)
- Human-like typing with variable inter-key delays
- Per-attempt proxy + fingerprint rotation
- Randomised viewport, locale, timezone, colour scheme, carrier

---

## ⚙️ Configuration

### `.env`

| Variable | Default | Description |
|----------|---------|-------------|
| `GOOGLE_SERVICE_ACCOUNT_FILE` | `credentials/credentials.json` | SA key path |
| `GOOGLE_SHEET_URL` | — | Sheet URL or ID |
| `GOOGLE_SHEET_WORKSHEET` | `Sheet1` | Tab name |
| `SHEET_URL_AEF` / `SHEET_WS_AEF` | — | Per-offer override; blank falls back to the two above |
| `PROXY_SOURCE` | `file` | `file`, `env`, `rotating`, or `none` |
| `PROXY_LIST` | — | Comma-separated proxies (source=env) |
| `ROTATING_PROXY_URL` | — | Single rotating endpoint |
| `HEADLESS` | `true` | The web UI always forces this to `true` |
| `LOG_LEVEL` | `INFO` | DEBUG / INFO / WARNING / ERROR |

### `config.yaml`

`target` (URL, timeout) · `form.max_steps` · `retry` · `pacing` · `delays` ·
`device_defaults` · `screenshots` · `logging` · `sheet_columns`

---

## 🔄 Status Flow

```
Pending → In Progress → Success
                      → Failed   (missing_data, or retries exhausted)
                      → Retry    (intermediate, will be retried)
                      → Stopped  (user pressed Stop mid-lead)
```

---

## 🔐 Security Note

`app.py` binds `0.0.0.0:5000` with the Werkzeug development server and has **no
authentication**. The sheets behind it hold SSNs, dates of birth, driver's
licence numbers and bank account details, and `/api/config` returns proxy
credentials. Bind it to localhost, or put authentication and a production WSGI
server in front of it, before exposing it anywhere.

---

## 📝 License

For authorized use only. Ensure you have permission to automate submissions on
any target website, and that every lead you submit has consented to the
application being made on their behalf.
