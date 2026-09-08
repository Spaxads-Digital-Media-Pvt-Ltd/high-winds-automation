# 🚀 Lead Automation Engine

Reads leads from Google Sheets, rotates proxy + device fingerprint per attempt,
fills a loan application via Playwright, and writes the result back to the sheet.

Three offers:

| Offer | Front-end | Sheet tab | Filler |
|-------|-----------|-----------|--------|
| Simple Lending Direct | `ef-` wizard on the homepage | `Simple Lending Direct` | `form_filler_simplelending` |
| ExaBucks | same widget on `/form` | `ExaBucks` | `form_filler_exabucks` |
| SimaCash | same widget on `/form` | `SimaCash` | `form_filler_simacash` |

Each offer carries an `enabled` flag in `ALL_OFFERS` ([app.py](app.py)); setting
it to `False` takes an offer out of the UI without deleting anything.

`core/lead_platform.py` holds the platform layer — the 31-field vocabulary,
sheet parsing, value mapping and validation, and the browser lifecycle — and
each offer's filler subclasses it with only the site's own DOM layer. Additional
offers plug in the same way: a filler that is interface-compatible with the
engine (same `FormFiller`/`FormFillerError`/`process_row` contract) plus one
entry in `ALL_OFFERS`.

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
│   ├── lead_platform.py              # shared platform layer
│   ├── form_filler_simplelending.py  # simplelendingdirect.com
│   ├── form_filler_exabucks.py       # exabucks.com/form
│   └── form_filler_simacash.py       # simacash.com/form
├── logs/                   # Structured log files (git-ignored)
└── screenshots/            # Live preview + failure captures (git-ignored)
```

---

## ⚡ Quick Start

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
playwright install chrome     # required — bundled Chromium crashes on this target

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

Simple Lending Direct, ExaBucks and SimaCash all load the same `ef-` widget
(`dynamicformrequest.com/form-loader.js` into `#ef-container`). SLD embeds it
on the homepage; ExaBucks and SimaCash host it at `/form`.

Each filler dispatches on the field names and chip headings currently
rendered, so skipped or reordered steps are handled naturally. Completion
routes into the shared lender-match chase in `core/lead_platform.py`.
Both approved and declined outcomes are recorded as delivered, with the
specific result written to the sheet's `Notes` column.

---

## ⚠️ Browser Engine: use Google Chrome, not bundled Chromium

**Playwright's bundled Chromium crashes its renderer on this site** part-way
through loading `script.anura.io` (the advertiser's fraud-detection vendor). The
form never renders. A stock **Google Chrome** install loads the identical page
without trouble, headless included.

Verified by isolation:

| Browser | Mode | Result |
|---------|------|--------|
| Google Chrome (`channel=chrome`) | headless | ✅ form renders |
| Google Chrome (`channel=chrome`) | headed | ✅ form renders |
| Bundled Chromium | headless | ❌ renderer crash |
| Bundled Chromium | headed | ❌ renderer crash |
| Bundled Chromium, `anura.io` blocked | headless | ✅ form renders |

The last row shows the script is what *triggers* the crash — but since real
Chrome executes that same script fine, this is a **Chromium build bug, not bot
detection**. Nothing needs to be suppressed or worked around; just run Chrome.

**Requirement:** Google Chrome must be installed on the host. The engine
defaults to `BROWSER_CHANNEL=chrome`; Settings → Browser exposes the choice and
a *Test Against Offer* button that loads the real page and reports whether it
rendered or crashed. In Docker, add Chrome to the image (`playwright install
chrome`) — the bundled Chromium alone is not sufficient for this target.

If the browser crashes anyway, the lead fails with `browser_crashed` /
`stuck` rather than hanging the engine: a crashed renderer never acknowledges
`close()`, so teardown is left to the Playwright driver instead.

## 🧪 Testing Without Submitting Real Applications

Rows land in the sheet as **Pending**, so pressing Start submits them as real
loan applications. Use a small synthetic batch (SSNs in the 900-999 range the
SSA never issues, phones in the 555-01xx fiction block, `@example.com` emails,
and published bank ABAs that pass the checksum) if you only want to exercise
the pipeline. **Do not point those rows at a live site unless you intend to
submit them.**

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
| `SHEET_URL_SLD` / `SHEET_WS_SLD` | — | Simple Lending Direct sheet override |
| `SHEET_URL_EXABUCKS` / `SHEET_WS_EXABUCKS` | — | ExaBucks sheet override |
| `SHEET_URL_SIMACASH` / `SHEET_WS_SIMACASH` | — | SimaCash sheet override |
| `SHEET_URL_HAPPYLOANS` / `SHEET_WS_HAPPYLOANS` | — | Happy Loans (Round Sky) tab, named **happy loans** |
| `BROWSER_CHANNEL` | `chrome` | `chrome` \| `chromium` \| `msedge`. Bundled `chromium` crashes on this target |
| `PROXY_SOURCE` | `file` | `file`, `env`, `rotating`, or `none` |
| `PROXY_LIST` | — | Comma-separated proxies (source=env) |
| `ROTATING_PROXY_URL` | — | Single rotating endpoint |
| `HEADLESS` | `true` | Set from Settings → Browser; headed needs a display |
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
