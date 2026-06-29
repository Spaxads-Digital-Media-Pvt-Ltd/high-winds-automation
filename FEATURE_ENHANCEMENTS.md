# UI Feature Enhancements — Detailed Specification

## Overview

Add four new configuration panels to the web UI to give users runtime control over critical automation parameters without requiring code edits or `.env` changes.

---

## Feature 1: Proxy Configuration Panel

### Current State
- Proxy is hardcoded in `.env` file: `ROTATING_PROXY_URL`
- Users must edit `.env` and restart Flask to change proxy
- No visibility into proxy test/validation

### Desired State
**Location:** New tab/section in the settings panel (or top-level sidebar)  
**Title:** "Proxy Settings"

#### UI Components

**1. Proxy Source Selector**
- Radio buttons or dropdown: `none`, `rotating`, `file`, `env`
- Default: current value from `.env`
- Label: "Proxy Source"
- Help text: "none = direct connection; rotating = single endpoint; file = proxies.txt list; env = comma-separated PROXY_LIST"

**2. If Proxy Source = `rotating`:**
- Text input field (large, monospace font)
  - Label: "Rotating Proxy URL"
  - Placeholder: `http://user:pass@host:port`
  - Default: current `ROTATING_PROXY_URL` from `.env`
  - Tooltip: "Format: http://username:password;mobile;us;;;@proxy.froxy.com:9000"
  - Optional: "Show/Hide" button to reveal/mask credentials

**3. If Proxy Source = `file`:**
- File upload input
  - Label: "Upload proxies.txt"
  - Accept: `.txt` files
  - Help: "One proxy per line, format: http://user:pass@host:port"
  - Current state display: Show count of loaded proxies from current proxies.txt

**4. If Proxy Source = `env`:**
- Textarea (monospace)
  - Label: "Proxy List (comma-separated)"
  - Placeholder: `http://user:pass@proxy1:8080,http://user:pass@proxy2:8080`
  - Rows: 4
  - Help: "Enter comma-separated proxies"

**5. Proxy Test Button**
- Button: "Test Proxy Connection"
- On click:
  - Show loading spinner
  - Try to connect through proxy to: `http://httpbin.org/ip`
  - Display result:
    - ✅ **Success:** "Proxy working. IP: 123.45.67.89"
    - ❌ **Failed:** "Proxy unreachable. Error: [error message]"
  - Timeout: 10 seconds
  - Log: Store test result with timestamp

**6. Apply Changes**
- Button: "Save & Apply Proxy"
- On click:
  - Validate the input (non-empty, correct format for selected source)
  - Update the in-memory config (do NOT write to `.env` file)
  - Restart affected browser contexts (or flag them to use new proxy on next engine start)
  - Show confirmation toast: "✅ Proxy settings updated"
  - On error: Show error toast with details

**7. Current Status Display**
- Card showing:
  - Active proxy source: `[rotating]`
  - Current URL/list summary: `http://***:***@proxy.froxy.com:9000` (mask credentials)
  - Last test: "Tested 2 hours ago — ✅ Working"

---

## Feature 2: Target URL Configuration Panel

### Current State
- Target URLs hardcoded in `app.py` OFFERS dict
- Different tracker URLs for each offer
- Users must edit Python code to change

### Desired State
**Location:** Same settings panel as Feature 1 (adjacent tab or section)  
**Title:** "Offer Settings" or "Target URLs"

#### UI Components

**1. Offer Cards (4 cards, one per offer)**

Each card contains:
- **Offer Name** (header): "50k Loans", "Low Credit Finance", "Super Personal Finder", "BorrowMoney"
- **Current URL** (read-only display or small badge): Shows first 60 chars of current URL
- **URL Input Field** (text input, monospace, large)
  - Label: "Target URL"
  - Placeholder: `https://example.com/track?campaign=123`
  - Current value: populated from app's OFFERS dict
  - Width: ~100% of card
  - Rows: 1 (single line)
  - Tooltip: "The tracking/redirect URL that leads are sent to"

**2. URL Validation**
- As user types, show icon:
  - 🟢 Valid URL (starts with http:// or https://)
  - 🔴 Invalid URL
- On blur, validate and show tooltip if invalid

**3. Reset to Default Button** (optional, per card)
- Button: "Reset to Default"
- On click: Restore to hardcoded default in OFFERS
- Confirmation: "Reset URL to default for this offer?"

**4. Save All Changes Button**
- Button: "Save All Target URLs"
- Location: Below the 4 cards (sticky or at bottom of panel)
- On click:
  - Validate all 4 URLs
  - Update in-memory OFFERS dict
  - Show confirmation: "✅ All target URLs updated"
  - Update displayed URLs on cards

**5. Status/Summary**
- Show how many offers have non-default URLs
- Last updated timestamp: "Saved at 2:45 PM"

---

## Feature 3: Lead Scheduling

### Current State
- Leads are filled immediately when user clicks "Start" on an offer
- No scheduling/delayed start functionality
- All processing is real-time

### Desired State
**Location:** New tab/section in the UI (could be in main dashboard or settings)  
**Title:** "Schedule" or "Lead Scheduling"

#### UI Components

**1. Schedule Enable Toggle**
- Toggle switch: "Enable Scheduling"
- Default: OFF
- Label: "Run leads on a schedule instead of immediately"

**2. (If Toggle = ON) Start Time Picker**
- Input type: datetime-local or two separate inputs (date + time)
- Label: "Start Time"
- Placeholder: "2026-06-12 14:30"
- Default: Current time + 1 hour
- Help: "First lead will start processing at this time"

**3. (If Toggle = ON) Offer Selection**
- Checkboxes (or multi-select dropdown) for each offer:
  - ☑ 50k Loans
  - ☑ Low Credit Finance
  - ☑ Super Personal Finder
  - ☑ BorrowMoney
- Label: "Which offers to schedule?"
- Help: "Only selected offers will run on schedule"
- Default: All checked

**4. (If Toggle = ON) Lead Count Picker** (for scheduling mode)
- Number input or spinner
- Label: "Number of leads to process"
- Min: 1, Max: no limit
- Placeholder: "10"
- Help: "Leave blank to process all available leads"

**5. Schedule Preview**
- Read-only text block showing:
  - "Scheduled to start: Thursday, June 12 at 2:30 PM"
  - "Offers: 50k Loans, Low Credit Finance"
  - "Total leads: 10"
- Updates live as user adjusts inputs

**6. Confirm & Schedule Button**
- Button: "Schedule & Start"
- On click:
  - Validate inputs (start time not in past, at least one offer selected)
  - Queue the job for the scheduled time
  - Redirect to Dashboard/Home
  - Show confirmation: "✅ Leads scheduled to start at 2:30 PM"
  - Optionally show: "View scheduled jobs" link

**7. Scheduled Jobs List** (new section on Dashboard)
- Table/list showing all pending scheduled jobs:
  - Offer(s): "50k Loans, Low Credit Finance"
  - Scheduled for: "2026-06-12 14:30"
  - Status: "Pending" or "Running"
  - Actions: "Cancel" button (before it starts)
- Can scroll through and cancel any scheduled job

---

## Feature 4: Batch Processing with Time Intervals

### Current State
- All available leads are processed sequentially without batch grouping
- No concept of "process N leads, then wait M minutes, then continue"

### Desired State
**Location:** Same "Schedule" tab as Feature 3 (could be a separate section or integrated)  
**Title:** "Batch & Interval Settings" or part of scheduling section

#### UI Components

**1. Batch Processing Enable Toggle**
- Toggle switch: "Enable Batch Processing"
- Default: OFF
- Label: "Process leads in batches with time intervals between batches"

**2. (If Toggle = ON) Batch Size Selector**
- Number input or dropdown/slider
- Label: "Leads per batch"
- Options: Dropdown with preset values: `1`, `2`, `3`, `4`, `5`, `10`, `Custom`
- Or: Single text input (number only, min 1)
- Placeholder: "4"
- Help: "Process this many leads, then pause before starting next batch"

**3. (If Toggle = ON) Interval Selector**
- Dropdown: "Time interval between batches"
- Options (preset):
  - 30 seconds
  - 1 minute
  - 2 minutes
  - 5 minutes
  - 10 minutes
  - 15 minutes
  - 30 minutes
  - 1 hour
  - Custom
- If "Custom" selected: Text input (number) + Unit dropdown (seconds/minutes/hours)
- Label: "Wait between batches"
- Help: "After completing a batch, wait this long before starting the next batch"

**4. Batch Preview / Summary Card**
- Read-only display:
  - "Processing plan: 4 leads → wait 5 minutes → next batch"
  - "Estimated time: [total leads / batch size × interval] = ~1 hour 15 minutes for 15 leads"
  - "Total leads available: 15"
- Updates live as user adjusts batch size and interval

**5. Integration with Scheduling**
- If both Scheduling (Feature 3) and Batch Processing (Feature 4) are enabled:
  - Scheduling time applies to **first batch**
  - Subsequent batches follow the interval pattern
  - Example: "Start at 2:30 PM, batch 4 leads, then every 5 minutes"
  - Preview shows full timeline:
    - "Batch 1 (4 leads): 2:30 PM — 2:45 PM"
    - "Wait: 2:45 PM — 2:50 PM"
    - "Batch 2 (4 leads): 2:50 PM — 3:05 PM"
    - "..."

**6. Pause Between Batches Display** (during execution)
- On the Dashboard, while a scheduled batch is running:
  - Show countdown timer: "Next batch starts in: 4:32"
  - Option: "Start Now" button to skip the wait
  - Option: "Cancel Next Batch" to stop processing
  - Log entry: "Batch 1 complete (4 leads). Waiting 5 minutes until Batch 2."

**7. Batch Processing Status Card**
- During execution, show:
  - "Batch 2 of 4 | 8 leads completed of 15 total"
  - Progress bar: [████████░░] 53%
  - Current status: "Waiting 2 minutes 15 seconds before next batch"
  - Paused state (if user paused): "Batch processing paused. Click 'Resume' to continue."

---

## Technical Implementation Notes

### Backend (Flask/Python)

1. **Settings Storage:**
   - Store proxy, URLs, scheduling config in **in-memory config object** (don't persist to `.env`)
   - Optionally: Add a `config.json` file for persistence across Flask restarts
   - On startup, load from `.env` as defaults, then overlay any saved `config.json`

2. **Proxy Testing:**
   - Create async function `test_proxy_connection(proxy_url: str) -> dict`
   - Returns: `{"success": bool, "ip": str, "error": str}`
   - Called from `/api/proxy/test` endpoint

3. **Scheduling:**
   - Use `APScheduler` (Python job scheduler) or similar
   - Store jobs in a queue with scheduled trigger times
   - `GET /api/scheduled-jobs` — list all pending jobs
   - `POST /api/schedule-leads` — create new scheduled job
   - `DELETE /api/scheduled-jobs/{job_id}` — cancel job

4. **Batch Processing:**
   - Modify the engine start logic to respect batch config
   - After each batch completes, schedule next batch with interval delay
   - Store batch state: `{batch_num, total_batches, next_start_time}`

5. **API Endpoints:**
   - `POST /api/config/proxy` — update proxy settings
   - `POST /api/config/urls` — update target URLs (all 4 offers)
   - `GET /api/config` — get current proxy & URL settings
   - `POST /api/proxy/test` — test proxy connection
   - `POST /api/schedule-leads` — schedule leads
   - `GET /api/scheduled-jobs` — list scheduled jobs
   - `DELETE /api/scheduled-jobs/{job_id}` — cancel job
   - `POST /api/batch/pause` — pause current batch processing
   - `POST /api/batch/resume` — resume batch processing

### Frontend (Jinja2 / JavaScript)

1. **New Tabs/Panels:**
   - Add "Settings" tab with sub-tabs: Proxy, Offers, Schedule, Batch
   - Or: Sidebar menu with separate pages for each

2. **Form Handling:**
   - Validate inputs before submitting
   - Show loading spinners on async calls
   - Toast notifications for success/error
   - Live preview updates

3. **Dashboard Updates:**
   - Show "Scheduled Jobs" widget
   - Show "Batch Status" widget during execution
   - Real-time countdown timer for next batch

---

## User Flow Example

### Scenario: User wants to fill 20 leads starting at 2 PM, in batches of 5 leads, with 10 minutes between batches

1. User opens "Schedule" tab
2. Toggles "Enable Scheduling" → ON
3. Sets Start Time: 2026-06-12 14:00
4. Selects offers: 50k Loans, Low Credit Finance
5. Leaves "Number of leads" blank (process all)
6. Toggles "Enable Batch Processing" → ON
7. Sets Batch Size: 5
8. Sets Interval: 10 minutes
9. Sees preview:
   - "Batch 1 (5 leads): 2:00 PM — 2:15 PM"
   - "Wait 10 minutes"
   - "Batch 2 (5 leads): 2:25 PM — 2:40 PM"
   - "..." (4 total batches)
   - "Total estimated time: ~50 minutes"
10. Clicks "Schedule & Start"
11. Redirected to Dashboard
12. Toast: "✅ Leads scheduled. First batch starts at 2:00 PM"
13. At 2:00 PM, first batch auto-starts
14. User sees:
    - "Batch 1 of 4 | 5 leads complete"
    - "Next batch in: 9:45..."
    - Countdown timer ticks down
15. At 2:10 PM, next batch auto-starts

---

## Priority & Phasing (Optional)

**Phase 1 (MVP):**
- Feature 1: Proxy Configuration Panel ✅
- Feature 2: Target URL Configuration Panel ✅

**Phase 2:**
- Feature 3: Lead Scheduling (simple: start time + offer selection)
- Feature 4: Batch Processing (batch size + interval)

**Phase 3 (Nice to have):**
- Persistence to `config.json`
- Batch timeline preview with interactive drag
- Scheduling recurring jobs (daily, weekly)

---

## Notes

- **Do NOT modify `.env` file** — all changes are in-memory or optional persistent JSON
- **Do NOT require Flask restart** — proxy and URL changes take effect immediately for next run
- **Human behavior:** Batch intervals simulate real gaps between application submissions (don't look like bot spam)
- **Error handling:** Show clear user-friendly error messages, not stack traces
- **Accessibility:** Use labels, help text, tooltips throughout

