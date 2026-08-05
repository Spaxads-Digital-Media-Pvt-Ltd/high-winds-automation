"""
core/lead_platform.py — shared layer for the lead-funnel platform.

americanemergencyfund.com and mylendingwallet.com are two front-ends over the
same lead platform: identical 31-field vocabulary (confirmed from each site's
own JS), identical option semantics, and the same backend endpoints
(``/?cmd=ExtApplyV2`` progressive save, ``/?cmd=RenderResult`` on completion).

Everything that depends only on that shared data model lives here — reading a
sheet row, mapping its values onto the platform's option codes, validating
against the rules the sites enforce client-side, and the browser lifecycle.
Only the DOM interaction differs per site, so each filler subclasses
``BasePlatformFiller`` and implements the handful of abstract members below.

This split exists deliberately: the previous generation of this codebase had
five near-identical copies of one filler (8 415 lines) that had already drifted
into divergent bugs. Site-specific code belongs in a subclass; anything else
belongs here.
"""
from __future__ import annotations

import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog
from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

from utils.proxy_manager import ProxyManager
from utils.stealth import inject_stealth

log = structlog.get_logger(__name__)


def _digits(raw: str) -> str:
    return re.sub(r"\D", "", raw or "")


def _aba_checksum_ok(routing: str) -> bool:
    """Both sites run this exact check client-side before they will advance."""
    if len(routing) != 9 or not routing.isdigit():
        return False
    d = [int(c) for c in routing]
    total = (3 * (d[0] + d[3] + d[6])
             + 7 * (d[1] + d[4] + d[7])
             + 1 * (d[2] + d[5] + d[8]))
    return total % 10 == 0


class FormFillerError(Exception):
    """Base exception for form-filling errors."""

    def __init__(self, message: str, error_type: str = "unknown"):
        super().__init__(message)
        self.error_type = error_type


class BasePlatformFiller:
    """Sheet row -> platform values -> browser session. DOM work is a subclass job."""

    # ---- subclass contract -------------------------------------------------
    #   default_url        : str                     fallback target URL
    #   _prepare(page, row_number)                   consent gates, etc. (optional)
    #   _fill_form(page, fields, row_number, stop)   drive the wizard; return outcome
    # ------------------------------------------------------------------------
    default_url = ""

    _STATE_CODES = {
        "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
        "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
        "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
        "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
        "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
        "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
        "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
        "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
        "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
        "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
        "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
        "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
        "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
    }

    # (upper bound exclusive, option value) — first bracket the income fits.
    _NETIM_BRACKETS = [
        (1500, "1500"), (2000, "2000"), (2500, "2500"), (3000, "3000"),
        (4000, "4000"), (5000, "5000"), (6000, "6000"), (7000, "7000"),
        (8000, "8000"), (9000, "9000"), (10000, "10000"),
    ]
    _NETIM_TOP = "11000"

    _DEBT_BRACKETS = [
        (1, "0"), (5000, "4999"), (10000, "9999"), (15000, "14999"),
        (20000, "19999"), (25000, "24999"), (30000, "29999"), (35000, "34999"),
        (40000, "39999"), (45000, "44999"), (50000, "49999"),
    ]
    _DEBT_TOP = "50000"

    _TENURE_STEPS = [(12, "12"), (24, "24"), (36, "36"), (48, "48")]
    _TENURE_TOP = "60"

    def __init__(self, config: dict) -> None:
        self._config = config
        self._target = config.get("target", {})
        self._delays = config.get("delays", {})
        self._ss_dir = Path(config.get("screenshots", {}).get("directory", "screenshots"))
        self._ss_dir.mkdir(parents=True, exist_ok=True)
        self._save_shots = bool(config.get("screenshots", {}).get("enabled", False))
        self._max_steps = int(config.get("form", {}).get("max_steps", 40))
        self._crashed = False   # per-row; reset at the top of process_row


    # ---------------------------------------------------------------- public

    def process_row(
        self,
        row: dict[str, Any],
        fingerprint: dict[str, Any],
        proxy_url: str | None,
        row_number: int,
        stop_event=None,
    ) -> dict[str, Any]:
        """Fill and submit the application for one sheet row."""
        headless = os.getenv("HEADLESS", "true").lower() == "true"
        fields = self._parse_fields(row)
        self._validate_required_fields(fields)

        url = (self._target.get("url") or self.default_url).strip()
        page: Page | None = None
        self._crashed = False

        with sync_playwright() as pw:
            launch_args: dict[str, Any] = {
                "headless": headless,
                "args": ["--no-sandbox", "--disable-dev-shm-usage"],
            }
            # Marker flag so the UI's Stop can force-kill this exact browser if it
            # blocks mid-call (e.g. a page that never renders).
            engine_tag = self._config.get("browser", {}).get("engine_tag")
            if engine_tag:
                launch_args["args"].append(f"--lead-engine-tag={engine_tag}")
            # Playwright's *bundled* Chromium reproducibly crashes its renderer
            # on these sites part-way through loading the third-party
            # fraud-detection script — headless and headed alike.  A stock Google
            # Chrome install runs the identical page without trouble, so we
            # launch that channel by default.  BROWSER_CHANNEL=chromium forces
            # the bundled build (expect crashes).
            # Per-offer channel comes from the engine's own config dict (so
            # concurrent engines can use different browsers without racing on a
            # shared env var); fall back to BROWSER_CHANNEL, then "chrome".
            channel = (self._config.get("browser", {}).get("channel")
                       or os.getenv("BROWSER_CHANNEL", "chrome")).strip().lower()
            if channel and channel not in ("chromium", "bundled", "default"):
                launch_args["channel"] = channel
            if proxy_url:
                launch_args["proxy"] = ProxyManager.to_playwright_proxy(proxy_url)

            try:
                browser: Browser = pw.chromium.launch(**launch_args)
            except Exception as exc:
                if "channel" not in launch_args:
                    raise
                raise FormFillerError(
                    f"Could not launch browser channel '{channel}': {exc}. "
                    f"Install Google Chrome, or set BROWSER_CHANNEL=chromium to use "
                    f"Playwright's bundled build (which crashes on this target).",
                    error_type="browser_launch",
                ) from exc
            try:
                ctx_args = self._clean_fingerprint(fingerprint)
                context: BrowserContext = browser.new_context(**ctx_args)
                page = context.new_page()

                def _on_crash(_p: Page) -> None:
                    self._crashed = True
                    log.error("form.page_crashed", row=row_number)

                page.on("crash", _on_crash)
                inject_stealth(page, fingerprint)

                log.info("form.navigating", url=url, row=row_number)
                self._goto(page, url, row_number, stop_event)
                self._live(page)

                self._prepare(page, row_number)
                outcome = self._fill_form(page, fields, row_number, stop_event)

                self._screenshot(page, row_number, "success")
                submission_id = str(uuid.uuid4())[:8].upper()
                log.info("form.success", row=row_number, submission_id=submission_id,
                         outcome=outcome)
                context.close()
                return {
                    "status": "Success",
                    "notes": f"Submitted — {outcome}",
                    "submission_id": submission_id,
                }

            except FormFillerError:
                if page:
                    try:
                        self._screenshot(page, row_number, "error")
                    except Exception:
                        pass
                raise
            except Exception as exc:
                error_type = self._classify_error(exc)
                if page:
                    try:
                        self._screenshot(page, row_number, error_type)
                    except Exception:
                        pass
                raise FormFillerError(str(exc), error_type=error_type) from exc
            finally:
                self._close_browser(browser)

    # --------------------------------------------------------------- navigation

    def _goto(self, page: Page, url: str, row_number: int, stop_event) -> None:
        """Load the landing page, retrying so a bad rotating-proxy exit IP or a
        renderer crash costs one reload rather than the whole lead."""
        last_err: Exception | None = None
        for attempt in range(1, 4):
            self._check_stop(stop_event)
            last_err = None
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                last_err = e
            landed = ""
            try:
                landed = page.url or ""
            except Exception as e:
                last_err = e
            if not last_err and not landed.startswith("chrome-error://"):
                return
            log.warning("form.nav_retry", attempt=attempt, row=row_number,
                        url=landed[:60], error=str(last_err)[:90] if last_err else "chrome-error")
            if attempt < 3:
                time.sleep(3)
        raise FormFillerError(
            f"Navigation failed: {last_err}" if last_err
            else "Page failed to load — proxy unreachable or blocked",
            error_type="proxy_error",
        )


    # ---------------------------------------------------------------- hooks

    def _prepare(self, page: Page, row_number: int) -> None:
        """Clear anything standing between the load and the first step
        (consent gates, splash screens). Default: nothing to do."""

    def _fill_form(self, page: Page, fields: dict, row_number: int, stop_event) -> str:
        raise NotImplementedError

    def _key_delay(self) -> float:
        import random
        lo = float(self._delays.get("min_typing_delay", 0.04))
        hi = float(self._delays.get("max_typing_delay", 0.12))
        return random.uniform(lo, hi) * 1000

    def _action_pause(self) -> None:
        import random
        lo = float(self._delays.get("min_action_delay", 0.5))
        hi = float(self._delays.get("max_action_delay", 2.0))
        time.sleep(random.uniform(lo, hi))

    def _pause_range(self, key: str, lo: float, hi: float) -> None:
        """Sleep a random time in a [lo, hi] window read from config, so the
        rhythm is tunable without code changes."""
        import random
        rng = self._delays.get(key)
        if isinstance(rng, (list, tuple)) and len(rng) == 2:
            lo, hi = float(rng[0]), float(rng[1])
        time.sleep(random.uniform(lo, hi))

    def _read_pause(self) -> None:
        """Human beat after a fresh step renders — as if reading the question
        before answering.  Keeps multi-step forms from feeling machine-paced."""
        self._pause_range("read_pause", 0.5, 1.2)

    def _field_pause(self) -> None:
        """Human beat between fields within one step (name -> surname -> DOB),
        which is what otherwise reads as a bot filling everything at once."""
        self._pause_range("field_pause", 0.35, 0.8)

    def _check_stop(self, stop_event) -> None:
        if stop_event is not None and stop_event.is_set():
            raise FormFillerError("Stopped by user", error_type="stopped")

    def _live(self, page: Page) -> None:
        """Refresh the UI's live-preview frame."""
        try:
            page.screenshot(path=str(self._ss_dir / "live_view.png"))
        except Exception:
            pass

    # ---------------------------------------------------------------- parsing

    def _parse_fields(self, row: dict) -> dict:
        # Case/space-insensitive header map — sheet tabs are sometimes re-cased
        # ('Zip Code' vs 'ZIP Code', 'Date Of Birth (Dob)', 'Ssn Full'), which
        # would otherwise make exact-key lookups miss and report missing data.
        _norm = {}
        for _k, _v in row.items():
            _nk = re.sub(r"\s+", " ", str(_k)).strip().lower()
            if _nk not in _norm or str(_v or "").strip():
                _norm[_nk] = _v

        def g(*keys: str) -> str:
            for k in keys:
                nk = re.sub(r"\s+", " ", str(k)).strip().lower()
                v = str(_norm.get(nk) or "").strip()
                if v:
                    return v
            return ""

        phone = _digits(g("Phone Number", "Phone"))
        employer_phone = _digits(g("Employer Work Phone", "Work Phone")) or phone

        full_ssn = _digits(g("SSN Full", "SSN"))
        last4 = _digits(g("SSN Last 4"))
        if full_ssn:
            last_ssn = full_ssn[-4:]
        else:
            last_ssn = last4[-4:] if last4 else ""
            full_ssn = last4

        zip_raw = _digits(g("ZIP Code", "Zip", "Zip_Code"))
        # ABA routing numbers are 9 digits; a spreadsheet that stored the value
        # as a number drops the leading zero (067014822 -> 67014822), which then
        # fails the checksum. Restore it before validating.
        routing = _digits(g("ABA Routing Number", "routingNumber", "Routing Number"))
        if 0 < len(routing) < 9:
            routing = routing.zfill(9)

        loan_raw = re.sub(r"[,$\s]", "", g("Requested Loan Amount ($)", "Loan_Amount"))
        try:
            loan_amount = int(float(loan_raw))
        except (ValueError, TypeError):
            loan_amount = 5000
        loan_amount = max(100, min(35000, loan_amount))

        income_raw = re.sub(r"[,$\s]", "", g("Monthly Net Income ($)", "Monthly_Income"))
        try:
            income = int(float(income_raw))
        except (ValueError, TypeError):
            income = 3000

        return {
            "first_name":     g("First Name", "First_Name"),
            "last_name":      g("Last Name", "Last_Name"),
            "email":          g("Email Address", "Email"),
            "phone":          self._fmt_phone(phone),
            "employer_phone": self._fmt_phone(employer_phone),
            "dob":            self._normalize_dob(g("Date of Birth (DOB)", "DOB", "dob")),
            "ssn":            full_ssn,
            "last_ssn":       last_ssn,
            "zip":            zip_raw.zfill(5) if zip_raw else "",
            "street_address": g("Street Address", "Address"),
            "city":           g("City"),
            "state":          self._normalize_state(g("State")),
            "loan_amount":    loan_amount,
            "income_bracket": self._bracket(income, self._NETIM_BRACKETS, self._NETIM_TOP),
            "debt_bracket":   self._debt_bracket(g("Credit Card Debt", "Debt Amount")),
            "address_months": self._tenure(g("Years at Address", "Months at Address")),
            "employer_months": self._tenure(g("Years at Employer", "Months at Employer")),
            "bank_months":    self._tenure(g("Years at Bank", "Months at Bank")),
            "is_homeowner":   self._yes_no(g("Homeowner", "Is Homeowner"), default="0"),
            "is_military":    self._yes_no(g("Military", "Active Military"), default="0"),
            "is_direct_deposit": self._yes_no(g("Direct Deposit"), default="1"),
            "income_source":  self._income_source(g("Income Source", "Primary Income Source")),
            "pay_freq":       self._pay_freq(g("Pay Frequency", "Pay_Frequency")),
            "employer_name":  g("Employer Name", "Employer_Name") or "Employer",
            "dl_number":      g("Driver License / ID Number", "driversLicenseNumber"),
            "dl_state":       self._normalize_state(
                                  g("Driver License State") or g("State")),
            "account_type":   "2" if g("Account Type", "bankAccountType").lower().startswith("sav") else "1",
            "credit_score":   self._credit_score(g("Credit Score Rating", "Credit_Score")),
            "loan_reason":    self._loan_reason(g("Loan Purpose", "Loan_Purpose")),
            "routing_number": routing,
            "account_number": _digits(g("Account Number", "accountNumber")),
            "bank_name":      g("Bank Name", "bankName"),
        }

    def _validate_required_fields(self, f: dict) -> None:
        required = [
            "first_name", "last_name", "email", "phone", "dob",
            "zip", "street_address", "city", "state",
            "ssn", "routing_number", "account_number",
        ]
        missing = [k for k in required if not f.get(k)]

        # Fail fast on rules the form enforces client-side — otherwise the lead
        # burns a full browser session only to stall on a step it can never pass.
        if f["routing_number"] and not _aba_checksum_ok(f["routing_number"]):
            missing.append("routing_number(failed ABA checksum)")
        acct = f["account_number"]
        if acct and not (5 <= len(acct) <= 18):
            missing.append(f"account_number(must be 5-18 digits, got {len(acct)})")
        if f["dob"] and not re.match(r"^(0[1-9]|1[0-2])/(0[1-9]|[12]\d|3[01])/(19|20)\d{2}$", f["dob"]):
            missing.append("dob(must be MM/DD/YYYY)")
        elif f["dob"]:
            age = self._age(f["dob"])
            if age is not None and not (18 <= age <= 120):
                missing.append(f"dob(age {age} outside 18-120)")
        if f["phone"] and not re.match(r"^\([2-9]\d{2}\) \d{3}-\d{4}$", f["phone"]):
            missing.append("phone(invalid US number)")
        if f["zip"] and len(f["zip"]) != 5:
            missing.append("zip(must be 5 digits)")

        if missing:
            raise FormFillerError(
                f"Missing or invalid fields: {missing}", error_type="missing_data"
            )

    # ------------------------------------------------------------- normalising

    def _fmt_phone(self, digits: str) -> str:
        """Render as (XXX) XXX-XXXX — the pattern the form's regex accepts."""
        d = digits[1:] if len(digits) == 11 and digits.startswith("1") else digits
        if len(d) != 10 or d[0] not in "23456789":
            return ""
        return f"({d[:3]}) {d[3:6]}-{d[6:]}"

    def _normalize_dob(self, raw: str) -> str:
        raw = (raw or "").strip()
        if not raw:
            return ""
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%d/%m/%Y", "%m/%d/%y"):
            try:
                return datetime.strptime(raw, fmt).strftime("%m/%d/%Y")
            except ValueError:
                pass
        return raw

    def _age(self, dob: str) -> int | None:
        try:
            d = datetime.strptime(dob, "%m/%d/%Y")
        except ValueError:
            return None
        today = datetime.now()
        return today.year - d.year - ((today.month, today.day) < (d.month, d.day))

    def _normalize_state(self, raw: str) -> str:
        raw = (raw or "").strip()
        if len(raw) == 2:
            return raw.upper()
        return self._STATE_CODES.get(raw.lower(), raw.upper()[:2])

    def _bracket(self, value: int, brackets: list[tuple[int, str]], top: str) -> str:
        for upper, option in brackets:
            if value < upper:
                return option
        return top

    def _debt_bracket(self, raw: str) -> str:
        digits = re.sub(r"[,$\s]", "", raw or "")
        try:
            amount = int(float(digits))
        except (ValueError, TypeError):
            return "0"
        return self._bracket(amount, self._DEBT_BRACKETS, self._DEBT_TOP)

    def _tenure(self, raw: str) -> str:
        """Sheet may hold years ("3") or months ("36"); both map to the radios."""
        raw = (raw or "").strip().lower()
        if not raw:
            return self._TENURE_TOP
        nums = re.findall(r"\d+", raw)
        if not nums:
            return self._TENURE_TOP
        n = int(nums[0])
        months = n if ("month" in raw or n > 12) else n * 12
        for upper, option in self._TENURE_STEPS:
            if months <= upper:
                return option
        return self._TENURE_TOP

    def _yes_no(self, raw: str, default: str = "0") -> str:
        raw = (raw or "").strip().lower()
        if not raw:
            return default
        return "1" if raw in {"yes", "y", "true", "1", "own", "owner"} else "0"

    def _income_source(self, raw: str) -> str:
        raw = (raw or "").strip().lower()
        if any(k in raw for k in ("benefit", "unemploy", "disab", "social", "pension", "retire")):
            return "2"
        return "1"

    def _pay_freq(self, raw: str) -> str:
        raw = (raw or "").strip().lower()
        if "week" in raw and ("bi" in raw or "every 2" in raw or "every two" in raw):
            return "2"
        if "semi" in raw or "twice" in raw:
            return "4"
        if "week" in raw:
            return "1"
        if "month" in raw:
            return "3"
        return "2"

    def _credit_score(self, raw: str) -> str:
        raw = (raw or "").strip().lower()
        if not raw:
            return "1"
        words = {"excellent": "2", "great": "2", "good": "3", "fair": "4", "poor": "5"}
        for word, value in words.items():
            if raw.startswith(word):
                return value
        try:
            score = int(re.sub(r"\D", "", raw)[:3])
        except (ValueError, TypeError):
            return "1"
        if score >= 700:
            return "2"
        if score >= 600:
            return "3"
        if score >= 500:
            return "4"
        return "5"

    def _loan_reason(self, raw: str) -> str:
        raw = (raw or "").strip().lower()
        if "card" in raw or "credit card" in raw:
            return "14"
        if "debt" in raw or "consol" in raw:
            return "1"
        return "13"

    # ---------------------------------------------------------------- utilities

    def _close_browser(self, browser: Browser) -> None:
        """Shut the browser down without ever blocking the engine thread.

        A Chromium renderer that has crashed never acknowledges close(), so the
        call would hang forever and the Stop button could not reach the thread.
        Playwright's sync API is greenlet-based and thread-affine, so closing on
        a watchdog thread is not an option either — it corrupts the event loop.
        Instead: skip close() entirely on a crashed target and let the
        sync_playwright() context exit reap the driver and its children.
        """
        if self._crashed:
            log.warning("form.skip_close", msg="renderer crashed; leaving teardown to the driver")
            return
        try:
            browser.close()
        except Exception:
            pass

    def _screenshot(self, page: Page, row: int, label: str) -> None:
        if not self._save_shots:
            return
        try:
            path = self._ss_dir / f"row_{row:04d}_{label}.png"
            page.screenshot(path=str(path), full_page=False)
        except Exception as e:
            log.warning("screenshot.failed", error=str(e)[:80])

    # ---------------------------------------------------------- post-offer flow
    # Shared by AEF and MyLendingWallet — same lender-match back-end.

    # Still working ("Thank you for your request / Connecting with our network of
    # trusted lenders") — wait, don't act.
    _JS_POST_STATE = r"""() => {
        const vis = e => e.offsetParent !== null && e.getClientRects().length > 0;
        const t = e => (e.innerText || e.value || '').replace(/\s+/g, ' ').trim();
        const body = (document.body ? document.body.innerText : '').toLowerCase();
        const processing = /(connecting with|trusted lenders|should only take|do not refresh|do not leave|please wait|one moment|processing your|matching you|finding you|searching for|finalis|finaliz)/.test(body);
        const fields = Array.from(document.querySelectorAll('input,select,textarea'))
            .filter(e => vis(e) && e.type !== 'hidden' && !e.disabled && !e.readOnly)
            .map(e => (e.name || e.id || ''));
        const buttons = Array.from(document.querySelectorAll(
                'button,input[type=submit],input[type=button],[role=button],a.btn,a.button'))
            .filter(e => vis(e) && !e.disabled).map(t).filter(x => x && x.length < 40);
        return { processing, fields, buttons, sig: fields.join(',') + '|' + buttons.join(',') };
    }"""

    # Fill any bank / routing / account fields the offer asks for, from the lead.
    # Skips fields that already carry a value; matches by name/id/placeholder/label.
    _JS_FILL_BANK = r"""(V) => {
        const vis = e => e.offsetParent !== null && e.getClientRects().length > 0;
        const setV = (el, val) => { const p = Object.getPrototypeOf(el);
            const d = Object.getOwnPropertyDescriptor(p, 'value') || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
            d.set.call(el, val); ['input','change','blur'].forEach(ev => el.dispatchEvent(new Event(ev, {bubbles:true}))); };
        const key = e => {
            let s = (e.name||'') + ' ' + (e.id||'') + ' ' + (e.placeholder||'') + ' ' + (e.getAttribute('aria-label')||'');
            const lf = e.id ? document.querySelector('label[for="' + e.id + '"]') : null;
            if (lf) s += ' ' + (lf.innerText || '');
            const wrap = e.closest('label');            // radios/checkboxes often wrap their label
            if (wrap) s += ' ' + (wrap.innerText || '');
            return s.toLowerCase();
        };
        let n = 0;
        document.querySelectorAll('input,select').forEach(e => {
            if (!vis(e) || e.disabled || e.readOnly || e.type === 'hidden' || e.type === 'radio' || e.type === 'checkbox') return;
            const k = key(e);
            if (e.tagName === 'SELECT') {
                if (/account type|acct type|type of account/.test(k)) {
                    const o = Array.from(e.options).find(o => new RegExp(V.account_type, 'i').test(o.text));
                    if (o) { e.value = o.value; e.dispatchEvent(new Event('change', {bubbles:true})); n++; }
                }
                return;
            }
            if (e.value && e.value.trim()) return;             // don't overwrite prefilled
            if (/routing|\baba\b|\brtn\b/.test(k))                  { setV(e, V.routing_number); n++; }
            else if (/account\s*(number|no|#)|acct\s*(number|no|#)/.test(k)
                     || (/account/.test(k) && !/type/.test(k)))    { setV(e, V.account_number); n++; }
            else if (/bank\s*name/.test(k) || (/bank/.test(k) && !/account/.test(k))) { setV(e, V.bank_name); n++; }
        });
        // account-type radios (checking / savings)
        const at = (V.account_type || '').toLowerCase();
        document.querySelectorAll('input[type=radio]').forEach(e => {
            if (!vis(e)) return; const k = key(e);
            if (/account type|acct|checking|savings/.test(k)) {
                if ((at.includes('sav') && /sav/.test(k)) || (at.includes('check') && /check/.test(k))) {
                    if (!e.checked) { e.click(); n++; }
                }
            }
        });
        return n;
    }"""

    # Find a Continue / Accept-style button (never Back / Decline / Cancel).
    # dryRun=true returns its text without clicking, so the caller can arm a
    # popup listener before the real click.
    _JS_CLICK_CONTINUE = r"""(dryRun) => {
        const vis = e => e.offsetParent !== null && e.getClientRects().length > 0;
        const t = e => (e.innerText || e.value || '').replace(/\s+/g, ' ').trim();
        const go = /^(continue|next|accept|agree|submit|proceed|confirm|finish|get started|start here|start now|start|see my|see offers?|see if|view|view my|view offer|view details|get my|get offer|get started now|claim|redeem|show me|complete|i agree|apply now|apply|yes\b.*|accept.*offer|get.*offer|see.*offer)/i;
        const no = /(back|cancel|decline|no thanks|edit|previous|return to|log ?in|sign ?in|sign ?up|español|terms|disclosure|privacy|conditions)/i;
        const b = Array.from(document.querySelectorAll(
                'button,input[type=submit],input[type=button],[role=button],a.btn,a.button,a'))
            .filter(vis).filter(e => !e.disabled)
            .find(e => { const s = t(e); return s && s.length < 40 && go.test(s) && !no.test(s); });
        if (b) { if (!dryRun) b.click(); return t(b).slice(0, 40); }
        return '';
    }"""

    def _handle_post_offer(self, page: Page, f: dict, row_number: int, stop_event) -> None:
        """After the application is routed to the lender-match flow ("Thank you
        for your request / Connecting with our network of trusted lenders"), the
        site may ask for bank/routing details to finalise an offer, then present
        one or more Continue buttons.  Work through it: wait out the processing,
        fill the bank fields, click Continue, and repeat until it settles."""
        log.info("form.post_offer_start", row=row_number)
        self._screenshot(page, row_number, "post_offer_arrived")
        # account_type is parsed as a code ("1" checking / "2" savings) — map it
        # to a word so it can match the offer page's Checking/Savings controls.
        at_raw = str(f.get("account_type", "") or "").strip().lower()
        account_type_word = "savings" if at_raw in ("2", "savings", "sav", "s") else "checking"
        # Accept both key conventions: AEF/MLW use routing_number/account_number,
        # CashUSA (which borrows this handler) uses routing/account.
        bank_vals = {
            "routing_number": f.get("routing_number") or f.get("routing", ""),
            "account_number": f.get("account_number") or f.get("account", ""),
            "bank_name":      f.get("bank_name", "") or "",
            "account_type":   account_type_word,
        }
        ctx = page.context
        cur = page                          # current surface — may switch to an offer tab
        # Let the arriving offers page begin loading before we poll it.
        for state in ("domcontentloaded", "load"):
            try:
                cur.wait_for_load_state(state, timeout=15000)
            except Exception:
                pass

        def _find_cta():
            """First frame carrying a Continue/View/Start-Here CTA, and its text.
            Offer cards are frequently rendered inside ad iframes."""
            for fr in cur.frames:
                try:
                    if fr.is_detached():
                        continue
                    txt = fr.evaluate(self._JS_CLICK_CONTINUE, True) or ""
                except Exception:
                    txt = ""
                if txt:
                    return fr, txt
            return None, ""

        deadline = time.time() + 240        # a few minutes, per the on-screen note
        clicks = 0
        max_clicks = 6                      # safety cap for in-place Continue chains
        proc_logged = 0.0
        last_progress = time.time()
        last_click = None                   # (url, cta) — detects a no-op re-click
        while time.time() < deadline:
            self._check_stop(stop_event)
            try:
                self._live(cur)
            except Exception:
                pass
            try:
                st = cur.evaluate(self._JS_POST_STATE)
            except Exception:
                time.sleep(2)
                continue

            # 1) Fill any bank/routing fields the offer is asking for.
            filled = 0
            try:
                filled = cur.evaluate(self._JS_FILL_BANK, bank_vals) or 0
            except Exception:
                filled = 0
            if filled:
                log.info("form.post_offer_bank_filled", count=filled, row=row_number)
                self._screenshot(cur, row_number, "post_offer_bank")
                last_progress = time.time()
                time.sleep(1)

            # 2) A Continue / View / "Start Here!" CTA (searched across frames).
            cta_fr, cta = (_find_cta() if clicks < max_clicks else (None, ""))
            if cta_fr is not None:
                here = ((cur.url or ""), cta)
                if here == last_click:
                    # Same button on the same page as last time — the previous
                    # click didn't advance, so stop rather than spam it.
                    log.info("form.post_offer_no_progress", button=cta, row=row_number)
                    break
                last_click = here
                clicks += 1
                last_progress = time.time()
                log.info("form.post_offer_continue", button=cta, row=row_number)
                new_tab = None
                try:
                    with ctx.expect_page(timeout=8000) as pinfo:
                        cta_fr.evaluate(self._JS_CLICK_CONTINUE, False)   # actually click
                    new_tab = pinfo.value
                except Exception:
                    new_tab = None           # no popup — navigated in place
                if new_tab is not None:
                    # The offer opened in its own window ("results will open in a
                    # new window") — that's the destination.  Follow it, give it
                    # time to load (it's often an interstitial that then redirects
                    # to the lender), and STOP; do not drill into the advertiser's
                    # own application funnel.
                    cur = new_tab
                    log.info("form.post_offer_tab", url=(cur.url or "")[:80], row=row_number)
                    settle = float(self._delays.get("offer_load_wait", 6))
                    for state in ("domcontentloaded", "load", "networkidle"):
                        try:
                            cur.wait_for_load_state(state, timeout=20000)
                        except Exception:
                            pass
                    try:
                        cur.bring_to_front()
                    except Exception:
                        pass
                    time.sleep(settle)
                    # In case it redirected to the lender during the settle, wait
                    # for that page to load too before finishing.
                    for state in ("domcontentloaded", "networkidle"):
                        try:
                            cur.wait_for_load_state(state, timeout=15000)
                        except Exception:
                            pass
                    log.info("form.post_offer_loaded", url=(cur.url or "")[:80], row=row_number)
                    break
                # In-place navigation (a Continue/Submit on the aggregator) —
                # wait for it and keep going through the flow.
                for state in ("domcontentloaded", "load"):
                    try:
                        cur.wait_for_load_state(state, timeout=15000)
                    except Exception:
                        pass
                time.sleep(2)
                continue

            # 3) No CTA yet.  If the offers page is still searching / connecting
            #    ("Searching for the best offers…"), keep waiting for the cards.
            if st.get("processing"):
                if time.time() - proc_logged > 15:
                    log.info("form.post_offer_processing", row=row_number)
                    proc_logged = time.time()
                time.sleep(3)
                continue

            # 4) Not processing and nothing to click — give ad-loaded cards time
            #    to render, then finish if still nothing after a while.
            if not filled and time.time() - last_progress > 45:
                break
            time.sleep(2)

        self._screenshot(cur, row_number, "post_offer_final")
        log.info("form.post_offer_done", clicks=clicks, row=row_number)

    def _classify_error(self, exc: Exception) -> str:
        msg = str(exc).lower()
        if "crash" in msg:
            return "browser_crashed"
        if "proxy" in msg or "net::err" in msg or "tunnel" in msg:
            return "proxy_error"
        if "closed" in msg or "target page" in msg:
            return "browser_closed"
        if "timeout" in msg:
            return "timeout"
        return "unknown"

    def _clean_fingerprint(self, fp: dict) -> dict:
        allowed = {
            "user_agent", "viewport", "locale", "timezone_id",
            "geolocation", "color_scheme", "device_scale_factor",
            "is_mobile", "has_touch", "java_script_enabled",
            "extra_http_headers",
        }
        return {k: v for k, v in fp.items() if k in allowed and v is not None}
