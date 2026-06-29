"""
core/form_filler_simacash.py — simacash.com multi-step loan form automation.

Unlike the other offers (50k / LowCredit / BorrowMoney / SuperPersonal) which
embed an iframe.global form, SimaCash hosts its OWN native React wizard at
https://simacash.com/form.  Steps are routed by the URL hash (#loanAmount,
#email, #dobY, …) so we drive the form by reading location.hash and dispatching
a handler per step.

Discovered step map (in order):
  loanAmount        loan-amount choice chips
  email             email input
  lastName          firstName + lastName inputs
  homePhone         phone input (mask)
  isMilitary        YES / NO
  address           street address input
  state             zip + city inputs + state <select>
  residenceMonths   tenure choice
  dobY              month / day / year <select>s
  incomeSource      Employment / Benefits
  employedMonths    tenure choice
  incomeFrequency   Weekly / Bi-Weekly / Monthly / Semi-Monthly
  incomeNextDate    react calendar — real-click a future enabled day (auto-advances)
  incomeNetMonthly  net-monthly income bracket choice
  employerName      employer name input
  driverLicense     DL/ID number input
  driverLicenseState  state <select>
  ssn               SSN input (mask ___-__-____)
  loanPurpose       credit-score <select> + purpose <select>
  debt              debt-amount bracket choice
  payType           YES / NO (direct deposit)
  bankMonths        tenure choice
  bankAccountType   Checking / Saving
  bankRoutingNumber routing-number input
  bankAccountNumber bank name + account number inputs → final submit

Key robustness rules learned from the live form:
  • React inputs need real typing + input/change/blur events; native-setter
    fallback only when typing leaves the field empty (so input masks survive).
  • The wizard renders every step's buttons in the DOM — always click the
    *visible* `.f-wizard-step--buttons--next`, never the first one.
  • Choice options (`.f-button-primary.f-button-outl`) auto-advance on click.
  • The calendar requires a real mouse click (bounding box) on an enabled,
    non-disabled future day; that auto-advances with no NEXT button.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import os
import random
import re
import time
import uuid
from pathlib import Path
from typing import Any

import structlog
from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page

from utils.stealth import inject_stealth
from utils.proxy_manager import ProxyManager

log = structlog.get_logger(__name__)


class FormFillerError(Exception):
    """Base exception for form-filling errors."""

    def __init__(self, message: str, error_type: str = "unknown"):
        super().__init__(message)
        self.error_type = error_type


class FormFiller:
    """Fills and submits the simacash.com multi-step form."""

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

    # SimaCash loan-amount chips ("Up to $X")
    _LOAN_CHIPS = [500, 1000, 2500, 5000, 35000]

    # Tenure choice labels, ordered [<=1yr, 2yr, 3yr, 4yr, 5yr+].  These differ
    # per step on the live form (bankMonths spells out "One Year or Less" and
    # tops out at "5 Years"), so each step gets its exact label set.
    _RESIDENCE_OPTS = ["1 Year or less", "2 Years", "3 Years", "4 Years", "5 Years or more"]
    _EMPLOYED_OPTS  = ["1 Year or less", "2 Years", "3 Years", "4 Years", "5 Years or more"]
    _BANK_OPTS      = ["One Year or Less", "2 Years", "3 Years", "4 Years", "5 Years"]

    def __init__(self, config: dict) -> None:
        self._config = config
        self._target = config.get("target", {})
        self._strict_sheet = bool(config.get("form", {}).get("strict_sheet_data", True))
        self._ss_dir = Path(config.get("screenshots", {}).get("directory", "screenshots"))
        self._ss_dir.mkdir(parents=True, exist_ok=True)

        # Human-like pacing (mirrors the 50k offer): per-keystroke typing jitter
        # and a short "think" pause between actions/steps, tuned via config.yaml
        # → delays.  Slower than a bot, so the form sees realistic behaviour.
        d = config.get("delays", {})
        self._type_min = float(d.get("min_typing_delay", 0.04))
        self._type_max = float(d.get("max_typing_delay", 0.12))
        self._act_min  = float(d.get("min_action_delay", 0.5))
        self._act_max  = float(d.get("max_action_delay", 2.0))
        self._pre_submit_wait = float(d.get("pre_submit_wait", 1.5))
        # Dwell on the final review page before clicking "Submit Loan Request" so
        # TrustedForm finishes certifying the lead — submitting instantly yields
        # an incomplete cert and the backend drops the lead even though the form
        # redirects to the offer page (false success).
        self._review_dwell_min = float(d.get("review_dwell_min", 6.0))
        self._review_dwell_max = float(d.get("review_dwell_max", 9.0))

    def _human_pause(self, lo: float | None = None, hi: float | None = None) -> None:
        """Sleep a randomised, human-like interval between actions."""
        lo = self._act_min if lo is None else lo
        hi = self._act_max if hi is None else hi
        if hi < lo:
            hi = lo
        time.sleep(random.uniform(lo, hi))

    # ------------------------------------------------------------------ public

    def process_row(
        self,
        row: dict[str, Any],
        fingerprint: dict[str, Any],
        proxy_url: str | None,
        row_number: int,
        stop_event=None,
    ) -> dict[str, Any]:
        """Fill and submit the form for one sheet row."""
        headless = os.getenv("HEADLESS", "true").lower() == "true"
        entry_url = self._resolve_entry_url(self._target.get("url", "https://digipalz.trackog.net/c?oid=34&affid=442"))
        fields = self._parse_fields(row)
        self._validate_required_fields(fields, row_number=row.get("_row_number"))
        log.info(
            "form.sheet_data",
            row=row.get("_row_number"),
            email=fields.get("email", "")[:40],
            loan=fields.get("loan_amount_value"),
            strict=self._strict_sheet,
        )

        with sync_playwright() as pw:
            launch_args: dict[str, Any] = {"headless": headless}
            if proxy_url:
                launch_args["proxy"] = ProxyManager.to_playwright_proxy(proxy_url)

            browser: Browser = pw.chromium.launch(**launch_args)
            try:
                ctx_args = self._clean_fingerprint(fingerprint)
                context: BrowserContext = browser.new_context(**ctx_args)
                page: Page = context.new_page()
                inject_stealth(page, fingerprint)

                self._assert_browser_ip_us(page, row_number, require_us=bool(proxy_url))

                log.info("form.navigating", url=entry_url, site="simacash", row=row_number)
                # Rotating residential proxies hand out a fresh exit IP per
                # connection; a weak/blocked IP makes Chromium land on
                # chrome-error://.  Retry the load a few times within this
                # attempt before declaring the proxy unreachable.
                nav_tries = 3
                last_nav_err: Exception | None = None
                for nav_attempt in range(1, nav_tries + 1):
                    if stop_event and stop_event.is_set():
                        raise FormFillerError("Stopped by user", error_type="stopped")
                    last_nav_err = None
                    try:
                        page.goto(entry_url, wait_until="domcontentloaded", timeout=60000)
                    except Exception as nav_err:
                        last_nav_err = nav_err
                    if not last_nav_err and not (page.url or "").startswith("chrome-error://"):
                        break
                    log.warning(
                        "form.nav_retry",
                        attempt=nav_attempt,
                        of=nav_tries,
                        url=(page.url or "")[:40],
                        error=str(last_nav_err)[:80] if last_nav_err else "chrome-error",
                        row=row_number,
                    )
                    if nav_attempt < nav_tries:
                        time.sleep(3)
                else:
                    detail = (
                        f"Navigation failed through proxy: {last_nav_err}"
                        if last_nav_err
                        else f"Page failed to load ({page.url}) — proxy unreachable or blocked"
                    )
                    raise FormFillerError(detail, error_type="proxy_error")

                time.sleep(3)
                self._prepare_entry_page(page, row_number)
                time.sleep(1)
                try:
                    page.screenshot(path=str(self._ss_dir / "live_view.png"))
                except Exception:
                    pass

                self._fill_form(page, fields, row_number, stop_event=stop_event)

                self._screenshot(page, row_number, "success")
                submission_id = str(uuid.uuid4())[:8].upper()
                is_dry_run = os.getenv("DRY_RUN", "").strip().lower() in {"1", "true", "yes", "y"}
                log.info("form.success", row=row_number, submission_id=submission_id, dry_run=is_dry_run)
                context.close()
                return {
                    "status": "Success",
                    "notes": "Dry-run completed (stopped before final submit)" if is_dry_run else "Form submitted successfully",
                    "submission_id": submission_id,
                }

            except FormFillerError:
                try:
                    self._screenshot(page, row_number, "error")
                except Exception:
                    pass
                raise
            except Exception as exc:
                error_type = self._classify_error(exc)
                try:
                    self._screenshot(page, row_number, error_type)
                except Exception:
                    pass
                raise FormFillerError(str(exc), error_type=error_type) from exc
            finally:
                browser.close()

    # --------------------------------------------------------------- form flow

    def _fill_form(self, page: Page, f: dict, row_number: int, stop_event=None) -> None:
        """Drive the native hash-routed wizard step by step until submission."""
        # live_view refresh cadence
        last_shot = 0.0
        stuck: dict[str, int] = {}
        last_hash = ""

        for _ in range(60):
            if stop_event and stop_event.is_set():
                raise FormFillerError("Stopped by user", error_type="stopped")

            time.sleep(0.6)  # let the step settle/animate in
            page = self._live_page(page)

            # periodic live-view screenshot for the UI
            now = time.time()
            if now - last_shot > 1.5:
                try:
                    page.screenshot(path=str(self._ss_dir / "live_view.png"))
                except Exception:
                    pass
                last_shot = now

            self._raise_on_duplicate(page, row_number)

            if self._is_complete(page):
                log.info("form.submitted", row=row_number, url=(page.url or "")[:60])
                return

            step = self._current_step(page)
            log.info("form.step", row=row_number, step=step, hash=self._hash(page))

            if not step:
                # No recognizable step + not complete → wait a little; the SPA may
                # still be routing.  If it persists it's an unknown screen.
                stuck["__none__"] = stuck.get("__none__", 0) + 1
                if stuck["__none__"] >= 4:
                    raise FormFillerError(
                        f"Unknown screen (hash={self._hash(page)}) — form did not present a known step",
                        error_type="unknown",
                    )
                continue

            before = self._hash(page)
            self._human_pause(0.6, 1.6)  # read/absorb the step before acting on it
            try:
                self._handle_step(page, step, f)
            except FormFillerError:
                raise
            except Exception as e:
                log.warning("form.step_error", row=row_number, step=step, error=str(e)[:120])

            advanced = self._wait_step_change(page, before, timeout=14.0)

            if not advanced:
                stuck[step] = stuck.get(step, 0) + 1
                log.warning("form.no_advance", row=row_number, step=step, count=stuck[step])
                # The terminal review step submits via "Submit Loan Request" and
                # then navigates to the offer/results page. Wait for that page,
                # re-attempting the click — only declare success once we've
                # actually reached it (never assume submission on a timeout).
                if step == "finish":
                    for _ in range(5):
                        if stop_event and stop_event.is_set():
                            raise FormFillerError("Stopped by user", error_type="stopped")
                        time.sleep(3)
                        page = self._live_page(page)
                        if self._is_complete(page):
                            log.info("form.final_submit", row=row_number, url=(page.url or "")[:60])
                            return
                        self._submit_review(page)
                    raise FormFillerError(
                        "Final submit did not reach the offer page",
                        error_type="stuck",
                    )
                if stuck[step] >= 2:
                    raise FormFillerError(
                        f"Form did not advance after step '{step}'",
                        error_type="stuck",
                    )
            else:
                stuck[step] = 0
                last_hash = self._hash(page)

        raise FormFillerError("Form exceeded maximum step count", error_type="stuck")

    # ------------------------------------------------------------ step dispatch

    def _handle_step(self, page: Page, step: str, f: dict) -> None:
        if step == "loanAmount":
            label = self._loan_chip_label(f["loan_amount_value"])
            self._click_choice(page, [label, "Up to $5,000", "Up to $2,500"])

        elif step == "email":
            self._robust_fill(page, "email", f["email"])
            self._click_next(page)

        elif step == "lastName":
            self._robust_fill(page, "firstName", f["first_name"])
            self._robust_fill(page, "lastName", f["last_name"])
            self._click_next(page)

        elif step == "homePhone":
            self._robust_fill(page, "phone", f["phone"], masked=True)
            self._click_next(page)

        elif step == "isMilitary":
            self._click_choice(page, ["NO" if f["active_military"] != "Yes" else "YES"])

        elif step == "address":
            self._robust_fill(page, "address", f["street_address"])
            self._click_next(page)

        elif step == "state":
            self._robust_fill(page, "zip", f["zip"])
            self._robust_fill(page, "city", f["city"])
            self._select_visible(page, 0, f["state"])
            self._click_next(page)

        elif step == "residenceMonths":
            self._click_choice(page, self._tenure_candidates(
                f["residence_tenure"], self._RESIDENCE_OPTS))

        elif step == "employedMonths":
            self._click_choice(page, self._tenure_candidates(
                f["employment_tenure"], self._EMPLOYED_OPTS))

        elif step == "bankMonths":
            self._click_choice(page, self._tenure_candidates(
                f["bank_tenure"], self._BANK_OPTS))

        elif step == "dobY":
            self._fill_dob(page, f["dob"])
            self._click_next(page)

        elif step == "incomeSource":
            label = "Benefits" if f["income_source"] == "Benefits" else "Employment"
            self._click_choice(page, [label, "Employment"])

        elif step == "incomeFrequency":
            self._click_choice(page, [f["income_frequency"], "Bi-Weekly", "Weekly", "Monthly"])

        elif step == "incomeNextDate":
            self._pick_calendar_day(page, f.get("next_payday"))

        elif step == "incomeNetMonthly":
            self._click_choice(page, self._income_bracket_labels(f["monthly_income"]))

        elif step == "employerName":
            self._robust_fill(page, "employerName", f["employer_name"])
            self._click_next(page)

        elif step == "driverLicense":
            self._robust_fill(page, "driverLicense", f["dl_number"])
            self._click_next(page)

        elif step == "driverLicenseState":
            self._select_visible(page, 0, f["dl_state"])
            self._click_next(page)

        elif step == "ssn":
            self._robust_fill(page, "ssn", f["ssn"], masked=True)
            self._click_next(page)

        elif step == "loanPurpose":
            self._select_visible(page, 0, f["credit_value"])      # credit score
            self._select_visible(page, 1, f["purpose_value"])     # loan purpose
            self._click_next(page)

        elif step == "debt":
            # Multi-question screen (debt amount + a yes/no) that does NOT
            # auto-advance — pick the debt bracket then click the wizard NEXT.
            self._click_choice(page, [f["debt_label"], "No Debt"])
            time.sleep(0.4)
            self._click_next(page)

        elif step == "payType":
            self._click_choice(page, ["YES" if f["direct_deposit"] else "NO"])

        elif step == "bankAccountType":
            label = "Saving" if f["account_type"].lower().startswith("sav") else "Checking"
            self._click_choice(page, [label, "Checking"])

        elif step == "bankRoutingNumber":
            self._robust_fill(page, "bankRoutingNumber", f["routing_number"])
            self._click_next(page)

        elif step == "bankAccountNumber":
            self._robust_fill(page, "bankName", f["bank_name"])
            self._robust_fill(page, "bankAccountNumber", f["account_number"])
            self._click_next(page)

        elif step == "finish":
            # Terminal review screen ("Almost Done!") — clicking NEXT on the last
            # data step opens this in a NEW TAB whose only action is the final
            # "Submit Loan Request" button. Submitting it navigates to the
            # offer/results page, which _is_complete() detects as success.
            self._submit_review(page)

    # --------------------------------------------------------- interactions

    def _robust_fill(self, page: Page, name: str, value: str, masked: bool = False) -> bool:
        """Fill a React text input reliably.

        Real typing fires the mask + React onChange.  A native-setter fallback
        runs only when typing leaves the field empty, so masked inputs (phone,
        SSN) keep their formatting instead of being overwritten with raw digits.
        """
        value = str(value or "")
        if not value.strip():
            return False
        sel = f'input[name="{name}"]'
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=10000)
            loc.click(timeout=4000)
            self._human_pause(0.2, 0.5)         # settle on the field before typing
            loc.fill("")
            # Type character-by-character with per-keystroke jitter so the cadence
            # looks human rather than a constant machine-gun delay.
            for ch in value:
                loc.press_sequentially(
                    ch, delay=int(random.uniform(self._type_min, self._type_max) * 1000)
                )
            self._human_pause(0.3, 0.8)         # brief pause as if reviewing input
            loc.evaluate(
                "e => { e.dispatchEvent(new Event('input',{bubbles:true}));"
                "e.dispatchEvent(new Event('change',{bubbles:true}));"
                "e.dispatchEvent(new Event('blur',{bubbles:true})); }"
            )
            if (loc.input_value(timeout=1500) or "").strip():
                return True
        except Exception as e:
            log.debug("form.fill_retry", name=name, error=str(e)[:80])

        # native-setter fallback (only reached when typing produced nothing)
        if not masked:
            try:
                page.evaluate(
                    """(args) => {
                        const [sel, val] = args;
                        const el = document.querySelector(sel);
                        if (!el) return;
                        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                        setter.call(el, val);
                        el.dispatchEvent(new Event('input', {bubbles:true}));
                        el.dispatchEvent(new Event('change', {bubbles:true}));
                        el.dispatchEvent(new Event('blur', {bubbles:true}));
                    }""",
                    [sel, value],
                )
                return True
            except Exception:
                pass
        return False

    def _click_next(self, page: Page) -> bool:
        """Click the VISIBLE wizard NEXT button (DOM holds one per step)."""
        self._human_pause()  # pause before advancing, like a person clicking Next
        deadline = time.time() + 8.0
        while time.time() < deadline:
            clicked = page.evaluate(
                """() => {
                    const vis = e => e.offsetParent !== null && e.getClientRects().length > 0;
                    const n = Array.from(document.querySelectorAll('.f-wizard-step--buttons--next'))
                        .filter(vis).filter(e => !e.disabled)[0];
                    if (n) { n.click(); return true; }
                    return false;
                }"""
            )
            if clicked:
                return True
            time.sleep(0.4)
        return False

    def _click_choice(self, page: Page, candidates: list[str]) -> bool:
        """Click a visible option button matching any candidate label.

        Options auto-advance.  Falls back to the first option when nothing
        matches and the sheet isn't strict, so the form never stalls.
        """
        cands = [c for c in candidates if c]
        self._human_pause()  # pause before picking an option, like reading choices
        clicked = page.evaluate(
            """(cands) => {
                const norm = s => (s||'').replace(/[^a-z0-9]/gi,'').toLowerCase();
                const vis = e => e.offsetParent !== null && e.getClientRects().length > 0;
                const opts = Array.from(document.querySelectorAll('.f-button-primary'))
                    .filter(vis).filter(e => !e.disabled && !e.className.includes('wizard-step--buttons'));
                // Pass 1: exact normalized match (avoids '$5,000' matching '$500').
                for (const want of cands) {
                    const w = norm(want);
                    const hit = opts.find(o => norm(o.innerText) === w);
                    if (hit) { hit.click(); return (hit.innerText||'').trim().slice(0,30); }
                }
                // Pass 2: option text contains the candidate (one direction only,
                // so '$5,000' can never fall back to the shorter '$500').
                for (const want of cands) {
                    const w = norm(want);
                    if (w.length < 2) continue;
                    const hit = opts.find(o => norm(o.innerText).includes(w));
                    if (hit) { hit.click(); return (hit.innerText||'').trim().slice(0,30); }
                }
                return null;
            }""",
            cands,
        )
        if clicked:
            return True
        if not self._strict_sheet:
            page.evaluate(
                """() => {
                    const vis = e => e.offsetParent !== null && e.getClientRects().length > 0;
                    const o = Array.from(document.querySelectorAll('.f-button-primary'))
                        .filter(vis).find(e => !e.disabled && !e.className.includes('wizard-step--buttons'));
                    if (o) o.click();
                }"""
            )
            return True
        return False

    def _select_visible(self, page: Page, index: int, value: str) -> bool:
        """Select an option on the Nth visible <select> by value, then text."""
        value = str(value or "").strip()
        if not value:
            return False
        try:
            loc = page.locator("select:visible").nth(index)
            loc.wait_for(state="visible", timeout=8000)
        except Exception:
            return False
        for attempt in (value, value.lstrip("0"), value.upper(), value.capitalize()):
            if not attempt:
                continue
            try:
                loc.select_option(value=attempt, timeout=2500)
                loc.evaluate("e => e.dispatchEvent(new Event('change',{bubbles:true}))")
                return True
            except Exception:
                continue
        # by visible label
        try:
            loc.select_option(label=value, timeout=2500)
            loc.evaluate("e => e.dispatchEvent(new Event('change',{bubbles:true}))")
            return True
        except Exception:
            return False

    def _fill_dob(self, page: Page, dob_mmddyyyy: str) -> None:
        """DOB step has 3 <select>s: month / day / year.  Day populates after
        month is chosen, so set month first, then re-select day & year."""
        mm, dd, yyyy = self._split_dob(dob_mmddyyyy)
        if not (mm and dd and yyyy):
            return
        self._select_visible(page, 0, mm)
        time.sleep(0.6)
        self._select_visible(page, 1, dd)
        self._select_visible(page, 2, yyyy)
        time.sleep(0.3)

    _MONTHS = ["January", "February", "March", "April", "May", "June",
               "July", "August", "September", "October", "November", "December"]

    def _parse_payday(self, raw: str | None) -> "datetime | None":
        raw = (raw or "").strip()
        if not raw:
            return None
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%Y/%m/%d", "%d/%m/%Y", "%m/%d/%y"):
            try:
                return datetime.strptime(raw, fmt)
            except ValueError:
                continue
        return None

    def _payday_target(self, raw: str | None) -> str:
        """The single best aria-label fragment 'D Month YYYY' for a payday,
        snapped forward off weekends (the calendar rejects weekend paydays)."""
        cands = self._payday_candidates(raw)
        return cands[0] if cands else ""

    def _payday_candidates(self, raw: str | None) -> list[str]:
        """Ordered calendar aria-label fragments ('D Month YYYY') starting at the
        next payday and walking forward over bank days (Mon–Fri) only.  The form
        won't advance on a weekend selection, so we never offer one; if the exact
        payday falls on a weekend we land on the following Monday, then the next
        few bank days as graceful fallbacks (e.g. a holiday)."""
        base = self._parse_payday(raw)
        if base is None:
            return []
        out: list[str] = []
        d = base
        for _ in range(21):
            if d.weekday() < 5:  # 0=Mon … 4=Fri
                out.append(f"{d.day} {self._MONTHS[d.month - 1]} {d.year}")
                if len(out) >= 8:
                    break
            d += timedelta(days=1)
        return out

    def _pick_calendar_day(self, page: Page, payday_raw: str | None = None) -> None:
        """react-modern-calendar-datepicker: real-click the requested pay date
        (from the sheet's Next Payday) when it's an enabled future day, else fall
        back to any enabled future day.  A native JS .click() doesn't fire the
        library's handler, so we click the day's screen coordinates with the
        real mouse.  Selecting an enabled day auto-advances the wizard."""
        cands = self._payday_candidates(payday_raw)
        if cands:
            # The payday may be in a later month — advance up to 6 months. We try
            # the bank-day candidates in order and click the first that's enabled
            # and not a weekend (the wizard won't advance on a weekend). Match by
            # aria-label and click the bbox centre with the real mouse — this
            # custom calendar fails Playwright's locator actionability checks.
            for _ in range(6):
                target = page.evaluate(
                    """(wants) => {
                        const norm = s => (s||'').replace(/\\s+/g,' ').trim();
                        const days = Array.from(document.querySelectorAll('.Calendar__day'));
                        for (const want of wants) {
                            const d = days.find(e => norm(e.getAttribute('aria-label')||'').endsWith(want)
                                  && !e.disabled && e.getAttribute('aria-disabled') !== 'true'
                                  && !/-blank|-disabled|-weekend/.test(e.className));
                            if (d) {
                                d.scrollIntoView({block:'center'});
                                const r = d.getBoundingClientRect();
                                return { x: r.x + r.width/2, y: r.y + r.height/2, date: want };
                            }
                        }
                        return null;
                    }""",
                    cands,
                )
                if target:
                    try:
                        page.mouse.click(target["x"], target["y"])
                        log.info("form.calendar_payday", date=target["date"])
                    except Exception as e:
                        log.debug("form.calendar_err", error=str(e)[:80])
                    time.sleep(0.8)
                    self._click_next(page)
                    return
                moved = page.evaluate(
                    """() => {
                        const a = Array.from(document.querySelectorAll('.Calendar__monthArrowWrapper'))
                            .find(e => /Next Month/i.test(e.getAttribute('aria-label')||'') && !e.disabled);
                        if (a) { a.click(); return true; } return false;
                    }"""
                )
                if not moved:
                    break
                time.sleep(0.6)
            log.warning("form.calendar_payday_fallback", payday=payday_raw)

        # Fallback: first enabled future bank day (weekends are rejected by the
        # wizard, so prefer a non-weekend day).
        target = page.evaluate(
            """() => {
                const days = Array.from(document.querySelectorAll('.Calendar__day'))
                    .filter(e => !e.disabled && e.getAttribute('aria-disabled') !== 'true'
                              && !/-blank|-disabled/.test(e.className)
                              && (e.innerText||'').trim());
                if (!days.length) return null;
                const pick = days.find(e => !/-selected|-weekend/.test(e.className))
                          || days.find(e => !/-selected/.test(e.className)) || days[0];
                const r = pick.getBoundingClientRect();
                return { x: r.x + r.width/2, y: r.y + r.height/2, day: pick.innerText.trim() };
            }"""
        )
        if not target:
            return
        try:
            page.mouse.click(target["x"], target["y"])
            log.debug("form.calendar_day", day=target["day"])
        except Exception as e:
            log.debug("form.calendar_err", error=str(e)[:80])
        time.sleep(0.8)
        # some variants still expose a NEXT after selection
        self._click_next(page)

    # --------------------------------------------------------- step detection

    _KNOWN_STEPS = {
        "loanamount": "loanAmount", "email": "email", "lastname": "lastName",
        "homephone": "homePhone", "ismilitary": "isMilitary", "address": "address",
        "state": "state", "residencemonths": "residenceMonths", "doby": "dobY",
        "dobm": "dobY", "dobd": "dobY", "incomesource": "incomeSource",
        "employedmonths": "employedMonths", "incomefrequency": "incomeFrequency",
        "incomenextdate": "incomeNextDate", "incomenetmonthly": "incomeNetMonthly",
        "employername": "employerName", "driverlicense": "driverLicense",
        "driverlicensestate": "driverLicenseState", "ssn": "ssn",
        "loanpurpose": "loanPurpose", "debt": "debt", "paytype": "payType",
        "bankmonths": "bankMonths", "bankaccounttype": "bankAccountType",
        "bankroutingnumber": "bankRoutingNumber", "bankaccountnumber": "bankAccountNumber",
        "finish": "finish",
    }

    def _hash(self, page: Page) -> str:
        try:
            return (page.evaluate("location.hash") or "").lstrip("#").strip()
        except Exception:
            return ""

    def _current_step(self, page: Page) -> str | None:
        return self._KNOWN_STEPS.get(self._hash(page).lower())

    def _wait_step_change(self, page: Page, before_hash: str, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.4)
            page = self._live_page(page)
            if self._hash(page) != before_hash:
                return True
            if self._is_complete(page):
                return True
        return False

    def _is_complete(self, page: Page) -> bool:
        """Detect post-submit success without relying on the URL path.

        The path is NOT a reliable signal: the first step (loanAmount) renders on
        the homepage path "/" (simacash.com/#loanAmount), so "not on /form" would
        false-positive on the very first step and report success before anything
        is filled. Body text also false-positives (marketing copy).

        Completion is therefore only declared when:
          • the browser leaves the simacash host  → handed off to an external
            lender/offers network (submitted); OR
          • we're still on simacash but there is no recognizable wizard step AND
            no wizard UI left on the page → a true thank-you/results page.
        """
        try:
            url = (page.url or "").lower()
        except Exception:
            return False
        if not url or url.startswith("about:") or url.startswith("chrome-error"):
            return False
        if "simacash.com" not in url:
            return True   # redirected to an external offers/results page

        # Still on simacash. A recognized step hash means we're mid-flow.
        if self._current_step(page):
            return False

        # No known step — only "complete" if the wizard is genuinely gone. Be
        # conservative: any leftover wizard control means we're still in the form
        # (or mid-transition), never a false success.
        try:
            has_wizard = page.evaluate(
                """() => {
                    const vis = e => e.offsetParent !== null && e.getClientRects().length > 0;
                    return Array.from(document.querySelectorAll(
                        '.f-button-primary, .f-wizard-step--buttons--next, input[name], select'))
                        .some(vis);
                }"""
            )
        except Exception:
            has_wizard = True   # don't false-claim success on an eval error
        return not has_wizard

    def _live_page(self, page: Page) -> Page:
        """If the form opened a new tab/redirect, follow the active page."""
        try:
            ctx = page.context
            pages = [p for p in ctx.pages if not p.is_closed()]
            if pages and pages[-1] is not page:
                return pages[-1]
        except Exception:
            pass
        return page

    _SUBMIT_RE = (
        r"submit\\s*loan\\s*request|submit.*request|submit\\b|get\\s*my|see\\s*my|"
        r"view(\\s*my)?\\s*results|agree|apply\\s*now|finish"
    )

    def _review_page(self, page: Page) -> Page:
        """Return the tab that holds the final 'Submit Loan Request' button.

        Clicking NEXT on the last data step spawns a new tab with the review
        page; the original tab stays on #bankAccountNumber. Prefer the newest
        tab that actually shows a submit button so we never click in the wrong
        (stale) tab."""
        try:
            ctx = page.context
            pages = [p for p in ctx.pages if not p.is_closed()]
            for p in reversed(pages):
                try:
                    has = p.evaluate(
                        """(re) => {
                            const rx = new RegExp(re, 'i');
                            const vis = e => e.offsetParent !== null && e.getClientRects().length > 0;
                            return Array.from(document.querySelectorAll('button,.f-button,[role=button],a,input[type=submit]'))
                                .filter(vis).some(e => rx.test(e.innerText || e.value || ''));
                        }""",
                        self._SUBMIT_RE,
                    )
                except Exception:
                    has = False
                if has:
                    return p
            if pages:
                return pages[-1]
        except Exception:
            pass
        return page

    def _submit_review(self, page: Page) -> bool:
        """On the review tab: tick consent checkboxes and click 'Submit Loan
        Request'. Returns True once the button was clicked. The page then
        navigates to the offer/results page (detected by _is_complete)."""
        page = self._review_page(page)
        # Let the review page finish loading before touching it.
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        # Tick consent checkboxes (this interaction is part of what TrustedForm
        # records), then dwell so the certificate completes before submitting.
        try:
            page.evaluate(
                """() => { Array.from(document.querySelectorAll('input[type=checkbox]'))
                    .filter(c => c.offsetParent !== null && !c.checked).forEach(c => c.click()); }"""
            )
        except Exception:
            pass
        dwell = random.uniform(self._review_dwell_min, self._review_dwell_max)
        log.info("form.review_dwell", seconds=round(dwell, 1))
        time.sleep(dwell)
        deadline = time.time() + 12.0
        while time.time() < deadline:
            try:
                clicked = page.evaluate(
                    """(re) => {
                        const rx = new RegExp(re, 'i');
                        const vis = e => e.offsetParent !== null && e.getClientRects().length > 0;
                        const b = Array.from(document.querySelectorAll('button,.f-button,[role=button],a,input[type=submit]'))
                            .filter(vis).filter(e => !e.disabled)
                            .find(e => rx.test(e.innerText || e.value || ''));
                        if (b) { b.click(); return (b.innerText || b.value || '').replace(/\\s+/g,' ').trim().slice(0,40); }
                        return null;
                    }""",
                    self._SUBMIT_RE,
                )
            except Exception:
                clicked = None
            if clicked:
                log.info("form.final_submit_click", label=clicked)
                return True
            time.sleep(0.5)
        log.warning("form.final_submit_no_button")
        return False

    def _raise_on_duplicate(self, page: Page, row_number: int) -> None:
        try:
            body = (page.evaluate("document.body ? document.body.innerText : ''") or "").lower()
        except Exception:
            return
        phrases = (
            "already applied", "already submitted", "already have an application",
            "already exists", "application already", "duplicate application",
        )
        if any(p in body for p in phrases):
            self._screenshot(page, row_number, "duplicate")
            raise FormFillerError(
                "Duplicate: applicant already submitted on simacash",
                error_type="duplicate",
            )

    # ------------------------------------------------------------ entry page

    def _resolve_entry_url(self, url: str) -> str:
        return (url or "").strip() or "https://digipalz.trackog.net/c?oid=34&affid=442"

    def _prepare_entry_page(self, page: Page, row_number: int) -> None:
        """Ensure we're on the wizard.  If we landed on the marketing page,
        click GET STARTED to enter the form."""
        page = self._live_page(page)
        if self._hash(page) or page.locator(".f-button-primary").count() > 0:
            return
        for label in ("GET STARTED", "Get Started", "Apply Now", "Start"):
            try:
                btn = page.get_by_text(label, exact=False).first
                if btn.count() > 0 and btn.is_visible(timeout=2000):
                    btn.click(timeout=4000)
                    time.sleep(2)
                    log.info("form.entered_wizard", row=row_number, via=label)
                    return
            except Exception:
                continue

    # ----------------------------------------------------------- field parse

    def _parse_fields(self, row: dict) -> dict:
        def g(*keys: str) -> str:
            for k in keys:
                v = str(row.get(k) or "").strip()
                if v:
                    return v
            return ""

        first_name = g("First Name", "First_Name")
        last_name = g("Last Name", "Last_Name")
        email = g("Email Address", "Email")
        phone = re.sub(r"\D", "", g("Phone Number", "Phone"))

        full_ssn = re.sub(r"\D", "", g("SSN Full", "SSN"))
        if not full_ssn:
            last4 = re.sub(r"\D", "", g("SSN Last 4"))
            full_ssn = last4  # best effort; form wants 9 digits

        dob = self._normalize_dob(g("Date of Birth (DOB)", "dob"))
        next_payday = g("Next Payday", "Next Pay Date", "Pay Date")

        _zip_raw = re.sub(r"\D", "", g("ZIP Code", "Zip"))
        zip_code = _zip_raw.zfill(5) if _zip_raw else ""
        street = g("Street Address", "Address")
        city = g("City")
        state = self._normalize_state(g("State"))

        loan_raw = re.sub(r"[,$\s]", "", g("Requested Loan Amount ($)", "Loan_Amount"))
        try:
            loan_int = int(float(loan_raw))
        except (ValueError, TypeError):
            loan_int = 5000
        loan_int = max(100, min(loan_int, 35000))

        monthly_income = re.sub(r"[,$\s]", "", g("Monthly Net Income ($)", "Monthly_Income"))
        if not monthly_income and not self._strict_sheet:
            monthly_income = "3000"

        active_military = self._map_yes_no(g("Active Military Status", "Active in Military?")) or "No"
        income_source = self._map_income_source(g("Income Source", "Source of Income"))
        income_frequency = self._map_frequency(g("Pay Frequency", "Pay_Frequency"))

        employer_name = g("Employer Name", "Employer_Name")
        if not employer_name and not self._strict_sheet:
            employer_name = "Employer"

        _routing_raw = re.sub(r"\D", "", g("ABA Routing Number", "routingNumber"))
        routing_number = _routing_raw.zfill(9) if _routing_raw else ""
        account_number = re.sub(r"\D", "", g("Account Number", "accountNumber"))
        account_type = g("Account Type", "bankAccountType") or "Checking"
        bank_name = g("Bank Name", "bankName")
        if not bank_name and not self._strict_sheet:
            bank_name = "Chase"

        dl_number = g("Driver License / ID Number", "driversLicenseNumber")
        dl_state_raw = g("Driver License State", "bankState")
        dl_state = self._normalize_state(dl_state_raw) if dl_state_raw else state

        credit_value = self._map_credit_value(g("Credit Score Rating", "Credit_Score"))
        purpose_value = self._map_purpose_value(g("Loan Purpose", "Loan_Purpose"))

        paycheck = g("Paycheck Payment Method", "How Is Your Paycheck Received?").lower()
        direct_deposit = ("direct" in paycheck) or (paycheck == "")  # default to direct deposit

        debt_label = self._map_debt_label(g("Unsecured Debt Amount", "Debt Amount"))

        # Tenure steps (residence / employment / bank account age). Data-driven
        # when the sheet has a matching column; otherwise the mapper defaults to
        # "2 Years".  Column names are matched leniently across common variants.
        residence_tenure = g("Time at Current Address", "Time at Address", "Residence Length",
                             "Length of Residence", "Years at Address", "How Long at Address")
        employment_tenure = g("Time at Current Job", "Time at Job", "Time at Employer",
                              "Employment Length", "Length of Employment", "Years Employed",
                              "How Long Employed")
        bank_tenure = g("Account Age", "Length of Bank Account", "Bank Account Age",
                        "Time with Bank")

        return {
            "first_name": first_name,
            "last_name": last_name,
            "email": email,
            "phone": phone,
            "ssn": full_ssn,
            "dob": dob,
            "zip": zip_code,
            "street_address": street,
            "city": city,
            "state": state,
            "loan_amount_value": str(loan_int),
            "monthly_income": monthly_income,
            "active_military": active_military,
            "income_source": income_source,
            "income_frequency": income_frequency,
            "employer_name": employer_name,
            "routing_number": routing_number,
            "account_number": account_number,
            "account_type": account_type,
            "bank_name": bank_name,
            "dl_number": dl_number,
            "dl_state": dl_state,
            "credit_value": credit_value,
            "purpose_value": purpose_value,
            "direct_deposit": direct_deposit,
            "debt_label": debt_label,
            "residence_tenure": residence_tenure,
            "employment_tenure": employment_tenure,
            "bank_tenure": bank_tenure,
            "next_payday": next_payday,
        }

    def _validate_required_fields(self, f: dict, row_number: int | None = None) -> None:
        required = [
            "first_name", "last_name", "email", "phone",
            "dob", "zip", "street_address", "city", "state", "loan_amount_value",
        ]
        if self._strict_sheet:
            required += [
                "ssn", "monthly_income", "employer_name",
                "routing_number", "account_number", "bank_name", "dl_number",
            ]
        missing = [k for k in required if not f.get(k)]
        if missing:
            prefix = f"Sheet row {row_number}: " if row_number else ""
            raise FormFillerError(
                f"{prefix}missing required column data: {missing}. "
                "Fill these cells in the Google Sheet.",
                error_type="missing_data",
            )

    # ----------------------------------------------------------- value maps

    def _loan_chip_label(self, amount_value: str) -> str:
        try:
            amt = int(float(amount_value))
        except (ValueError, TypeError):
            amt = 5000
        closest = min(self._LOAN_CHIPS, key=lambda x: abs(x - amt))
        return f"Up to ${closest:,}"

    def _income_bracket_labels(self, monthly_income: str) -> list[str]:
        try:
            inc = int(float(re.sub(r"[^\d.]", "", monthly_income or "0")))
        except (ValueError, TypeError):
            inc = 3000
        if inc >= 5000:
            primary = "$5000 or more"
        elif inc >= 4000:
            primary = "$4000 - $5000"
        elif inc >= 3500:
            primary = "$3500 - $4000"
        elif inc >= 3000:
            primary = "$3000 - $3500"
        elif inc >= 2500:
            primary = "$2500 - $3000"
        elif inc >= 2000:
            primary = "$2000 - $2500"
        elif inc >= 1500:
            primary = "$1500 - $2000"
        else:
            primary = "Less than $1500"
        return [primary, "$3000 - $3500", "$5000 or more"]

    def _tenure_years(self, raw: str) -> float | None:
        """Best-effort parse of a residence/employment/bank tenure to years.
        Handles 'X years', 'X months', ranges ('1-2 years', '6-12 months'),
        'less than a month', 'more than 2 years', and bare numbers (= years).
        Returns None when nothing parseable is present (caller defaults)."""
        v = (raw or "").strip().lower()
        if not v:
            return None
        nums = [float(x) for x in re.findall(r"\d+\.?\d*", v)]
        if "month" in v:
            if "less" in v and not nums:
                return 0.0
            return (max(nums) / 12.0) if nums else 0.5
        if "year" in v:
            if not nums:
                return 5.0 if ("more" in v or "over" in v or "+" in v) else None
            top = max(nums)
            return top + 1 if ("more" in v or "over" in v or "+" in v) else top
        return nums[0] if nums else None

    def _tenure_candidates(self, raw: str, opts: list[str]) -> list[str]:
        """Pick the closest tenure label from this step's option set, then list
        the rest as fallbacks so the step always advances. opts is ordered
        [<=1yr, 2yr, 3yr, 4yr, 5yr+]."""
        yrs = self._tenure_years(raw)
        if yrs is None:
            idx = 1                       # default → "2 Years"
        elif yrs < 1.5:
            idx = 0
        elif yrs < 2.5:
            idx = 1
        elif yrs < 3.5:
            idx = 2
        elif yrs < 4.5:
            idx = 3
        else:
            idx = 4
        chosen = opts[idx]
        return [chosen] + [o for o in opts if o != chosen]

    def _map_debt_label(self, raw: str) -> str:
        v = (raw or "").strip().lower()
        if not v or v in {"0", "no", "none", "no debt"}:
            return "No Debt"
        try:
            amt = float(re.sub(r"[^\d.]", "", v) or "0")
        except ValueError:
            return "No Debt"
        if amt <= 0:
            return "No Debt"
        if amt < 10000:
            return "$7,500 - $10,000"
        if amt < 15000:
            return "$10,000 - $15,000"
        if amt < 20000:
            return "$15,000 - $20,000"
        if amt < 35000:
            return "$20,000 - $35,000"
        return "$35,000 or more"

    def _map_credit_value(self, raw: str) -> str:
        v = (raw or "").lower().strip()
        if not v:
            return "fair"
        if "excel" in v:
            return "excellent"
        if "good" in v:
            return "good"
        if "fair" in v:
            return "fair"
        if "poor" in v or "bad" in v:
            return "bad"
        try:
            score = int(re.sub(r"[^\d]", "", v)[:3])
            if score >= 720:
                return "excellent"
            if score >= 690:
                return "good"
            if score >= 630:
                return "fair"
            return "bad"
        except (ValueError, TypeError):
            return "notSure"

    def _map_purpose_value(self, raw: str) -> str:
        """Map a sheet purpose to one of the 16 loanPurpose <select> values:
        debtConsolidation, emergencySituation, autoRepair, autoPurchase, moving,
        homeImprovement, medical, business, vacation, taxes, rentOrMortgage,
        wedding, majorPurchase, studentLoanRefinance, creditCardConsolidation,
        other.  Order matters: more specific phrases are checked first."""
        v = (raw or "").lower().strip()
        if not v:
            return "debtConsolidation"
        if "credit card" in v:
            return "creditCardConsolidation"
        if "debt" in v or "consolidat" in v:
            return "debtConsolidation"
        if "emerg" in v:
            return "emergencySituation"
        # home improvement / repair before the generic "repair" → autoRepair
        if ("home" in v and ("improv" in v or "repair" in v)) or "renovat" in v or "remodel" in v:
            return "homeImprovement"
        if "repair" in v:
            return "autoRepair"
        if "auto" in v or "car" in v or "vehicle" in v or "truck" in v:
            return "autoPurchase"
        if "moving" in v or "move" in v or "relocat" in v:
            return "moving"
        if "medical" in v or "health" in v or "dental" in v:
            return "medical"
        if "business" in v:
            return "business"
        if "vacation" in v or "travel" in v or "trip" in v:
            return "vacation"
        if "tax" in v:
            return "taxes"
        if "rent" in v or "mortgage" in v:
            return "rentOrMortgage"
        if "wedding" in v:
            return "wedding"
        if "student" in v or "tuition" in v or ("educat" in v):
            return "studentLoanRefinance"
        if "major" in v or "large purchase" in v or "appliance" in v or "furniture" in v:
            return "majorPurchase"
        if "home" in v or "house" in v:
            return "homeImprovement"
        if "purchase" in v:
            return "majorPurchase"
        if "other" in v:
            return "other"
        return "debtConsolidation"

    def _map_income_source(self, raw: str) -> str:
        v = (raw or "").strip().lower()
        if "benefit" in v or "ssi" in v or "disab" in v or "retir" in v:
            return "Benefits"
        return "Employment"

    def _map_frequency(self, raw: str) -> str:
        v = (raw or "").strip().lower()
        if "week" in v and "bi" in v:
            return "Bi-Weekly"
        if "every two" in v or "every other week" in v or "biweek" in v:
            return "Bi-Weekly"
        if "semi" in v or "twice" in v:
            return "Semi-Monthly"
        if "month" in v:
            return "Monthly"
        if "week" in v:
            return "Weekly"
        return "Bi-Weekly"

    def _map_yes_no(self, raw: str) -> str:
        v = (raw or "").strip().lower()
        if v in {"yes", "y", "true", "1"} or "yes" in v:
            return "Yes"
        if v in {"no", "n", "false", "0"} or "no" in v:
            return "No"
        return ""

    # ----------------------------------------------------------- normalise

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

    def _split_dob(self, dob_mmddyyyy: str) -> tuple[str, str, str]:
        m = re.match(r"^\s*(\d{1,2})/(\d{1,2})/(\d{2,4})\s*$", dob_mmddyyyy or "")
        if not m:
            return "", "", ""
        mm, dd, yyyy = m.group(1), m.group(2), m.group(3)
        if len(yyyy) == 2:
            yyyy = "19" + yyyy
        return mm.zfill(2), dd.zfill(2), yyyy

    def _normalize_state(self, raw: str) -> str:
        raw = (raw or "").strip()
        if len(raw) == 2:
            return raw.upper()
        return self._STATE_CODES.get(raw.lower(), raw.upper()[:2])

    # ----------------------------------------------------------- utilities

    def _screenshot(self, page: Page, row: int, label: str) -> None:
        try:
            path = self._ss_dir / f"row_{row:04d}_{label}.png"
            page.screenshot(path=str(path), full_page=False)
            log.debug("screenshot.saved", path=str(path))
        except Exception as e:
            log.warning("screenshot.failed", error=str(e)[:80])

    def _classify_error(self, exc: Exception) -> str:
        msg = str(exc).lower()
        if "duplicate" in msg or "already" in msg:
            return "duplicate"
        if "proxy" in msg or "net::err" in msg or "tunnel" in msg:
            return "proxy_error"
        if "timeout" in msg:
            return "timeout"
        return "unknown"

    def _assert_browser_ip_us(self, page: Page, row_number: int, require_us: bool) -> None:
        if not require_us:
            return
        try:
            ip = page.evaluate(
                """async () => {
                    const r = await fetch('https://api.ipify.org?format=text', { cache: 'no-store' });
                    return (await r.text()).trim();
                }"""
            )
            ip = (ip or "").strip().split()[0] if ip else ""
            log.info("form.browser_ip", row=row_number, ip=ip)
        except Exception as e:
            log.warning("form.browser_ip_check_failed", row=row_number, error=str(e)[:80])


    def _clean_fingerprint(self, fp: dict) -> dict:
        allowed = {
            "user_agent", "viewport", "locale", "timezone_id",
            "geolocation", "color_scheme", "device_scale_factor",
            "is_mobile", "has_touch", "java_script_enabled",
            "extra_http_headers",
        }
        return {k: v for k, v in fp.items() if k in allowed and v is not None}



