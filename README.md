# 🚀 Lead Automation Engine

Reads leads from Google Sheets, rotates proxy + device fingerprint per attempt,
fills a loan application via Playwright, and writes the result back to the sheet.

Two offers, both on the same lead platform:

| Offer | Front-end | Sheet tab |
|-------|-----------|-----------|
| American Emergency Fund | server-rendered Bootstrap wizard | `Sheet1` |
| MyLendingWallet | React SPA (react-hook-form) | `Sheet2` |

`core/lead_platform.py` holds everything the two share — the 31-field
vocabulary, sheet parsing, value mapping and validation, and the browser
lifecycle. Each filler subclasses it and implements only its own DOM layer.

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
│   ├── lead_platform.py    # shared platform layer (parsing, mapping, browser lifecycle)
│   ├── form_filler_aef.py  # americanemergencyfund.com — Bootstrap wizard DOM
│   └── form_filler_mlw.py  # mylendingwallet.com — React SPA DOM
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

## ⚠️ Validating the MyLendingWallet filler

**Verified against the live form** by walking every step (stopping before the
final submit — no application was completed). All steps now fill and advance:
loan amount · name/DOB · email/SSN-4 · phone · address+state · debt · homeowner ·
income · pay frequency · military · employer · employment length · work phone ·
driver's licence + state · SSN · account type.

Two bugs were found and fixed this way, both of which had been failing in
production:

* **Choice steps rejected every value.** The filler matched options by their
  visible label using AEF's wording, but this site words them differently
  ("5+ years" not "5 years or more", "Under 1 year" not "1 year or less",
  "self-employed" hyphenated). It turns out these are real
  `<input type=radio name=X value=Y>` groups carrying the platform's own
  values — the same values as AEF — so they are now set **by value** and the
  wording is irrelevant.
* **State dropdowns never took, and one failed silently.** `hstate` and `licst`
  are HeroUI `<Select>` components: the element holding the `name` is a
  visually-hidden a11y mirror, and writing it leaves React's state untouched.
  `licst` surfaced as "stuck at licn,licst"; `hstate` failed *quietly* because
  the address step advanced on its text fields alone — meaning **leads
  submitted before this fix had an empty home state**. Both now drive the real
  listbox.

Also confirmed, against the `mlw/` mock: the filler drives all 23 steps and
sets all 31 fields correctly, including choice-only steps that expose no named
input — those are identified from their option labels, falling back to the
question heading for the fields whose options are identical (`ishowner` /
`isactmil` / `isdd` are all Yes/No; the three tenure questions share one set).

What is *inferred*, not verified: the option labels and question headings for
steps 1+. They are taken
from AEF's vocabulary because both sites share the platform's copy, and step 0's
three labels match exactly — but the site fetches step content from the server
at runtime, so it is not in the bundle and could not be read statically.

Walking further requires submitting to the live advertiser, which has not been
done. To finish validation, either:

* run one real, consented lead through it and read the run log — every
  unmatched label is logged with `form.choice_not_found` including the labels
  actually on screen, so one pass identifies any gap; or
* walk the form manually in a browser and capture each step's button labels.

The filler fails loudly rather than silently on a mismatch: `unhandled_step` for
an unrecognised field, `field_rejected` for a label it cannot find.

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

---

## 🧪 Testing Without Submitting Real Applications

Rows land in the sheet as **Pending**, so pressing Start submits them as real
loan applications. To exercise the pipeline without that, run the bundled mock:

```bash
python devtools/serve_mock.py
```

| Offer | Mock URL |
|-------|----------|
| American Emergency Fund | `http://127.0.0.1:8799/aef/index.html` |
| MyLendingWallet | `http://127.0.0.1:8799/mlw/index.html` |

Set **Settings → Target URLs** to the relevant one and press Start. Everything
else is real: sheet read, engine, retries, live preview, status write-back.

**The two mocks are not equally trustworthy, and the difference matters:**

* `aef/` is built from the live site's own JS (`fields.js` + `funnel.js`) — same
  field names, same option values, same `validateStep()` semantics. A pass here
  is strong evidence the real thing will work.
* `mlw/` is built from the live site's *observed DOM contract only* —
  regenerated form id, `<button>` choices, platform `name` attributes. Its
  option labels are the ones the filler assumes. A pass proves the filler's
  mechanics; it does **not** prove the real site uses those labels.

Both sheets ship with five synthetic sample rows covering different mapping
branches (income and debt brackets, credit bands, pay frequencies, account
types, homeowner/military flags). SSNs are in the 900-999 range the SSA never
issues, phones use the 555-01xx block reserved for fiction, emails are
`@example.com`, and routing numbers are real published bank ABAs because they
must pass the checksum. **They are for the mocks — do not point them at a live
site.**

Verified: AEF 5/5 Success against `aef/` (~72 s per lead); MLW 31/31 field
values and all 23 steps against `mlw/`, with statuses written back to Sheet2.

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
