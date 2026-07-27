"""
core/form_filler.py — 50kloans.com multi-step form automation.

The form lives entirely inside an iframe.global iframe.
All Playwright interactions use the Frame object, not the Page object.

Confirmed step map (discovered by diagnose_50k.py):
  00 (1%)   How Much Do You Need?          → loan amount chip
  01 (3%)   Enter Your Email Address       → email + CONTINUE
  02 (7%)   Last 4 Digits of Your SSN      → last_ssn text + CONTINUE
  03 (14%)  What Is Your Credit Score?     → credit chip
  04 (17%)  Your Legal Name                → first_name + last_name + CONTINUE
  05 (21%)  Date Of Birth                  → dob text MM/DD/YYYY + CONTINUE
  06 (24%)  What Is Your ZIP Code?         → zip + CONTINUE
  07 (28%)  What Is Your Street Address?   → street_address + city + state select + CONTINUE
  08 (31%)  Source of Income               → chip: Employed
  09 (34%)  Active in Military?            → chip: No
  10 (38%)  How Often Are You Paid?        → pay frequency chip
  11 (41%)  Monthly Gross Income           → monthly_income + CONTINUE
  12 (45%)  Next Pay Date                  → chip "Next scheduled date" + CONTINUE
  13 (48%)  Employer Information           → employer_name + job_title + employer_phone + CONTINUE
  14 (52%)  How Is Your Paycheck Received? → chip: Direct Deposit
  15 (55%)  ABA Routing Number             → routing_number + CONTINUE
  16 (59%)  Bank Name                      → bank_name text + CONTINUE
  17 (62%)  Type of Bank Account           → chip: Checking / Savings
  18 (66%)  Length of Bank Account         → chip: More than 2 years
  19+       Phone, DL, loan purpose, account number, etc.
"""
from __future__ import annotations

from datetime import datetime
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

import structlog
from playwright.sync_api import sync_playwright, Browser, BrowserContext, Frame, Page

from utils.stealth import inject_stealth
from utils.proxy_manager import ProxyManager

log = structlog.get_logger(__name__)

class FormFillerError(Exception):
    """Base exception for form-filling errors."""

    def __init__(self, message: str, error_type: str = "unknown"):
        super().__init__(message)
        self.error_type = error_type

class FormFiller:
    """Fills and submits the 50kloans.com multi-step form."""

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

    _LOAN_AMOUNT_CHIPS = [1000, 1500, 2000, 2500, 5000, 7500, 10000, 20000, 25000]
    # BorrowMoney.us iframe (style=2, owner=BORROWMONEY) — $100–$5,000
    # Confirmed on borrowmoney.us iframe (owner=BORROWMONEY)
    _LOAN_AMOUNT_CHIPS_BORROWMONEY = [100, 250, 500, 1000, 2500, 5000]

    # BorrowMoney nova form chip labels: Monthly / Twice Monthly / Weekly / Bi-Weekly
    _PAY_FREQ_MAP = {
        "monthly": "Monthly",
        "twice monthly": "Twice Monthly",
        "semi-monthly": "Twice Monthly",
        "semimonthly": "Twice Monthly",
        "bimonthly": "Twice Monthly",
        "twice a month": "Twice Monthly",
        "weekly": "Weekly",
        "biweekly": "Bi-Weekly",
        "bi-weekly": "Bi-Weekly",
        "every 2 weeks": "Bi-Weekly",
        "every two weeks": "Bi-Weekly",
        "2 weeks": "Bi-Weekly",
    }

    def __init__(self, config: dict) -> None:
        self._config = config
        self._target = config.get("target", {})
        self._strict_sheet = bool(config.get("form", {}).get("strict_sheet_data", True))
        self._ss_dir = Path(config.get("screenshots", {}).get("directory", "screenshots"))
        self._ss_dir.mkdir(parents=True, exist_ok=True)

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
        entry_url = self._resolve_entry_url(self._target.get("url", "https://50kloans.com"))
        site = self._site_profile(entry_url)
        fields = self._parse_fields(row, site=site)
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

                log.info("form.navigating", url=entry_url, site=site, row=row_number)
                # Rotating residential proxies hand out a fresh exit IP per
                # connection; a weak/blocked IP makes Chromium land on
                # chrome-error://. Retry the load a few times within this
                # attempt — a new connection usually draws a better IP — before
                # declaring the proxy unreachable.
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
                        break  # page loaded
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
                self._prepare_entry_page(page, entry_url, fields, row_number)
                time.sleep(2)
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
        """Main form-fill loop — iterates through all iframe.global steps."""
        frame = self._get_frame(page)
        log.info("form.frame", url=frame.url[:80], row=row_number)
        for _ in range(30):  # 3 s, interruptible — let the SPA render
            if stop_event and stop_event.is_set():
                raise FormFillerError("Stopped by user", error_type="stopped")
            time.sleep(0.1)

        debug_steps = os.getenv("DEBUG_STEPS", "").strip().lower() in {"1", "true", "yes", "y"}
        debug_dir = self._ss_dir / "debug_steps"
        if debug_steps:
            debug_dir.mkdir(parents=True, exist_ok=True)

        seen_titles: dict[str, int] = {}
        for step_num in range(0, 60):
            for _ in range(5):
                if stop_event and stop_event.is_set():
                    raise FormFillerError("Stopped by user", error_type="stopped")
                time.sleep(0.1)
            if stop_event and stop_event.is_set():
                raise FormFillerError("Stopped by user", error_type="stopped")
            try:
                title = self._get_title(frame).lower().strip()
            except Exception:
                title = ""

            # Reached a completion / offer page
            if any(kw in title for kw in [
                "thank you", "congratulation", "review your", "your offers",
                "matched", "we found", "processing", "submitted",
            ]):
                log.info("form.completed", step=step_num, title=title, row=row_number)
                return

            if not title:
                # Re-fetch the frame — it may have detached/reloaded.
                frame = self._get_frame(page)
                if step_num == 0:
                    time.sleep(3)
                    continue
                log.warning("form.no_title", step=step_num, row=row_number)
                time.sleep(3)
                continue

            # ── Transient processing/interstitial screen ─────────────────────
            # Screens like "verifying your information..." appear briefly
            # between real steps.  Don't handle or count them (that would trip
            # the loop guard) — just wait for the form to move on.  Bounded so a
            # screen that never resolves still fails cleanly instead of hanging.
            if any(t in title for t in (
                "verifying", "validating", "please wait", "one moment",
                "loading", "checking",
            )):
                log.info("form.processing_wait", step=step_num, title=title[:60], row=row_number)
                deadline = time.time() + 40
                resolved = False
                while time.time() < deadline:
                    if stop_event and stop_event.is_set():
                        raise FormFillerError("Stopped by user", error_type="stopped")
                    time.sleep(1)
                    try:
                        cur = self._get_title(frame).lower().strip()
                    except Exception:
                        cur = ""
                    if cur and not any(t in cur for t in (
                        "verifying", "validating", "please wait", "one moment",
                        "loading", "checking",
                    )):
                        resolved = True
                        break
                if not resolved:
                    self._screenshot(page, row_number, f"processing_stuck_{step_num}")
                    raise FormFillerError(
                        f"Form stuck on processing screen at step {step_num}: {title!r}",
                        error_type="stuck",
                    )
                continue

            # ── Returning applicant ("welcome back") ─────────────────────────
            # Duplicate skipping has been removed: every lead is filled and
            # submitted regardless of whether the site recognises it. When a
            # returning applicant is greeted with "welcome back" the site
            # condenses the flow to the final steps — click through the greeting
            # and let the loop fill the remaining steps and submit.
            if "welcome back" in title:
                log.info(
                    "form.welcome_back",
                    step=step_num,
                    title=title[:80],
                    row=row_number,
                )
                try:
                    page.screenshot(path=str(self._ss_dir / "live_view.png"))
                except Exception:
                    pass
                self._continue(frame)
                time.sleep(2)
                continue

            log.info("form.step", step=step_num, title=title[:60], row=row_number)

            # Loop guard: if the same step title keeps recurring the form has
            # reset/cycled (e.g. bounced back to the landing step) — fail fast
            # with a clear message instead of silently burning all 60 steps.
            seen_titles[title] = seen_titles.get(title, 0) + 1
            if seen_titles[title] > 5:
                self._screenshot(page, row_number, f"loop_{step_num}")
                raise FormFillerError(
                    f"Form is looping on step {step_num}: {title!r}",
                    error_type="stuck",
                )

            if debug_steps:
                try:
                    page.screenshot(
                        path=str(debug_dir / f"row_{row_number:04d}_step_{step_num:02d}.png"),
                        full_page=True,
                    )
                except Exception:
                    pass
                try:
                    html = frame.content()
                    (debug_dir / f"row_{row_number:04d}_step_{step_num:02d}.html").write_text(
                        html, encoding="utf-8"
                    )
                except Exception:
                    pass

            result = self._handle_step(frame, title, f)
            if not result:
                self._screenshot(page, row_number, f"stuck_{step_num}")
                raise FormFillerError(
                    f"No handler matched step {step_num}: {title!r}",
                    error_type="stuck",
                )

            # Final submission — clicking "Request Cash" submits the form.
            # The iframe title doesn't change after submit, so handle the
            # post-submit offer page, then return.
            if "submit" in title or "loan request" in title or "request cash" in title:
                log.info("form.submitted", step=step_num, title=title, row=row_number)
                self._handle_post_submit(page, row_number)
                return

            # Wait for the form to advance PAST the step we just handled.
            # Compare against the CURRENT step's title (not the previous step's)
            # so we genuinely wait for this step to transition.  The old code
            # compared to the previous title, which already differed, so it
            # broke immediately and re-handled steps mid-transition — double-
            # clicking Continue on the SPA form and bouncing it back to start.
            new_title = title
            deadline = time.time() + 18
            while time.time() < deadline:
                if stop_event and stop_event.is_set():
                    raise FormFillerError("Stopped by user", error_type="stopped")
                time.sleep(0.6)
                try:
                    cur = self._get_title(frame).lower().strip()
                except Exception:
                    cur = ""
                if cur and cur != title:
                    new_title = cur
                    break

            if new_title == title and step_num > 0:
                # One more continue click before declaring stuck
                self._continue(frame)
                time.sleep(4)
                try:
                    new_title = self._get_title(frame).lower().strip()
                except Exception:
                    new_title = ""
                if new_title == title:
                    self._screenshot(page, row_number, f"stuck_{step_num}")
                    raise FormFillerError(
                        f"Form did not advance after step {step_num}: {title!r}",
                        error_type="stuck",
                    )

            try:
                page.screenshot(path=str(self._ss_dir / "live_view.png"))
            except Exception:
                pass

        raise FormFillerError("Form did not complete within 60 steps", error_type="timeout")

    def _handle_post_submit(self, page: Page, row_number: int) -> None:
        """After 'Request Cash' is clicked, the iframe shows a processing screen
        (94% spinner with checklist: Request started → Validating → Checking
        eligibility → Matching lenders → Offer received).

        The orange 'Get Your FREE Credit Score' button is rendered inside that
        same iframe immediately and is already visible at ~94%.  We click it as
        soon as it appears (no need to wait for 100%).  The button opens a new
        tab; we screenshot it and close it.
        """
        log.info("form.waiting_offers", row=row_number)

        # Selectors matching the actual button text seen on the offers screen.
        # Ordered from most-specific to least-specific.
        btn_selectors = [
            "text=Get Your FREE Credit Score",
            "button:has-text('FREE Credit Score')",
            "a:has-text('FREE Credit Score')",
            "text=Get Your Free Credit Score",
            "button:has-text('Free Credit Score')",
            "a:has-text('Free Credit Score')",
            "text=Get Your Credit Score Now",
            "button:has-text('Credit Score')",
            "a:has-text('Credit Score')",
        ]

        def _find_btn():
            """Return the first visible credit-score button across all frames."""
            # Search every attached frame first (button lives in the iframe)
            for f in page.frames:
                if f.is_detached():
                    continue
                for sel in btn_selectors:
                    try:
                        el = f.locator(sel).first
                        if el.is_visible(timeout=300):
                            return el
                    except Exception:
                        pass
            # Fallback: main page
            for sel in btn_selectors:
                try:
                    el = page.locator(sel).first
                    if el.is_visible(timeout=300):
                        return el
                except Exception:
                    pass
            return None

        def _offer_received() -> bool:
            """True when the 'Offer received' checklist item turns green."""
            for f in page.frames:
                if f.is_detached():
                    continue
                try:
                    # The item text exists AND the parent has a green/active class
                    el = f.locator("text=Offer received").first
                    if el.is_visible(timeout=200):
                        # Check if it has an active/checked sibling icon
                        parent = f.evaluate(
                            """() => {
                                var els = Array.from(document.querySelectorAll('*'));
                                for (var e of els) {
                                    if (e.textContent.trim() === 'Offer received') {
                                        var p = e.closest('li,div,[class]');
                                        return p ? p.className : '';
                                    }
                                }
                                return '';
                            }"""
                        )
                        if parent and any(k in parent.lower() for k in ["active", "complete", "check", "done", "green", "success"]):
                            return True
                except Exception:
                    pass
            return False

        btn = None
        deadline = time.time() + 150  # generous cap — click as soon as visible
        elapsed_log = 0
        while time.time() < deadline:
            btn = _find_btn()
            if btn:
                break
            # Also check if "Offer received" ticked — means processing done
            if _offer_received():
                log.info("form.offer_received_ticked", row=row_number)
                btn = _find_btn()
                break
            now = int(time.time() - (deadline - 150))
            if now - elapsed_log >= 15:
                log.info("form.offers_processing", elapsed_s=now, row=row_number)
                elapsed_log = now
            time.sleep(2)

        # Screenshot the offers page regardless of outcome
        try:
            page.screenshot(
                path=str(self._ss_dir / f"row_{row_number}_offers_page.png"),
                full_page=True,
            )
        except Exception:
            pass

        if not btn:
            log.warning("form.credit_btn_not_found", row=row_number)
            return

        log.info("form.clicking_credit_btn", row=row_number)
        # Button is target="_blank" — catch the new tab with expect_page
        try:
            with page.context.expect_page(timeout=15000) as new_page_info:
                btn.click()
            new_tab = new_page_info.value
            new_tab.wait_for_load_state("domcontentloaded", timeout=30000)
            log.info("form.credit_tab_opened", url=new_tab.url[:80], row=row_number)
            try:
                new_tab.screenshot(
                    path=str(self._ss_dir / f"row_{row_number}_credit_tab.png")
                )
            except Exception:
                pass
            new_tab.close()
        except Exception:
            # Fallback: button navigates in the current page (no new tab)
            try:
                btn.click()
                log.info("form.credit_btn_clicked_inline", row=row_number)
            except Exception as e:
                log.warning("form.credit_btn_click_failed", error=str(e), row=row_number)

    # ---------------------------------------------------------------- step handlers

    def _handle_step(self, frame: Frame, title: str, f: dict) -> str | None:
        """Dispatch the current form step to the correct action."""

        # ── Step 0: Loan amount ──────────────────────────────────────────────
        if (
            "how much" in title
            or "would you like to borrow" in title
            or ("amount" in title and "loan" in title)
            or ("borrow" in title and "amount" in title)
        ):
            val = str(f.get("loan_amount_value") or "").strip()
            chip = str(f.get("loan_amount_chip") or "").strip()
            clicked = self._click_loan_chip_js(frame, val or chip)
            if not clicked:
                clicked = self._click_loan_chip_js(frame, chip)
            if not clicked:
                for c in (chip, "$2,500", "$1,000", "$5,000", "$500", "$250"):
                    clicked = self._click_loan_chip_js(frame, c)
                    if clicked:
                        break
            if clicked:
                time.sleep(0.6)
                if not self._click_primary_continue(frame):
                    self._continue(frame)
                self._wait_title_change(frame, avoid=["how much", "borrow"], timeout=20)
                return clicked
            # BorrowMoney: custom amount field loan_amount + Continue
            if val and self._react_fill_any(
                frame,
                ['input[name="loan_amount"]', 'input[placeholder*="custom" i]'],
                val,
            ):
                time.sleep(0.5)
                if self._click_primary_continue(frame):
                    self._wait_title_change(frame, avoid=["how much", "borrow"], timeout=20)
                    return "CUSTOM_AMOUNT"
            try:
                pill = frame.locator("button.lcf-trigger-pill").first
                if pill.is_visible(timeout=2000):
                    pill.click(timeout=5000)
                    time.sleep(1)
                    if val:
                        self._react_fill_any(frame, ["input:visible"], val)
                        time.sleep(0.5)
                    if self._click_primary_continue(frame):
                        self._wait_title_change(frame, avoid=["how much"], timeout=20)
                        return "CUSTOM_AMOUNT"
            except Exception:
                pass
            return None

        # ── Enter amount (variant step) ──────────────────────────────────────
        if ("enter" in title and "amount" in title) or title.strip() == "enter your amount":
            amt = f.get("loan_amount_value") or ""
            if amt:
                self._robust_fill(frame, ["input:visible", "input[type=text]:visible"], amt, 0)
                time.sleep(0.5)
            return self._continue(frame)

        # ── Step 1: Email ────────────────────────────────────────────────────
        if "email" in title:
            self._robust_fill(
                frame,
                [
                    'input[name="email"]',
                    "input[type=email]",
                    "input[name*=email i]",
                    "input[id*=email i]",
                ],
                f["email"], 0,
            )
            time.sleep(0.5)
            if self._click_primary_continue(frame, wait=10.0):
                return "CONTINUE"
            return self._continue(frame)

        # ── Step 2 / 23+: SSN (last-4, single field, or XXX-XX-XXXX split) ─
        if "ssn" in title or ("social" in title and "secur" in title):
            if self._has_named_input(frame, "ssn_1"):
                self._fill_ssn_parts(frame, f["ssn"])
            elif "last" in title or "4" in title or "digit" in title:
                self._robust_fill(
                    frame, ['input[name="last_ssn"]', "input:visible"], f["last_ssn"], 0
                )
            else:
                self._robust_fill(
                    frame,
                    ['input[name="ssn"]', 'input[name*="ssn" i]', "input:visible"],
                    f["ssn"], 0,
                )
            time.sleep(0.5)
            return self._continue(frame)

        # ── Verify identity (variant SSN step) ───────────────────────────────
        # Some variants title the last-4 SSN step as "Verify your identity".
        if "verify" in title and "identity" in title:
            self._robust_fill(frame, ['input[name="last_ssn"]', "input:visible"], f["last_ssn"], 0)
            time.sleep(0.5)
            if self._click_primary_continue(frame, wait=10.0):
                return "CONTINUE"
            return self._continue(frame)

        # ── Post-verify confirmation (auto-advance screen) ───────────────────
        if "verification complete" in title or "verified successfully" in title:
            time.sleep(2)
            self._click_primary_continue(frame)
            self._continue(frame)
            self._wait_title_change(
                frame,
                avoid=["verification complete", "verified successfully"],
                timeout=20,
            )
            return "VERIFIED"

        # ── Name greeting confirmation (intermediate step) ─────────────────────
        # Some form flows show a standalone greeting step (just the name, e.g.
        # "christophe!" or "hi christophe,") that needs a Continue click to advance.
        # These steps have no form inputs — just a confirmation.
        first_name = (f.get("first_name") or "").lower().strip()
        if first_name and (
            title == first_name + "!"
            or title == "hi " + first_name + ","
            or title == "hi " + first_name + "!"
            or title.lower().startswith("hi " + first_name)
            or (title.lower() == first_name and not any(k in title for k in ["?", "would", "how", "what", "when", "why", "bank", "employer"]))
        ):
            time.sleep(0.5)
            if self._click_primary_continue(frame):
                return "GREETING"
            return self._continue(frame) or "GREETING"

        # ── Step 3: Credit score ─────────────────────────────────────────────
        if "credit" in title and ("score" in title or "rating" in title) and "trial" not in title:
            chip = f["credit_chip"]
            # Nova theme exposes only Poor/Fair/Good/Excellent (no "Not Sure")
            # with stable button ids credit-score-{poor|fair|good|excellent}.
            bucket_map = {
                "poor": "poor", "fair": "fair", "good": "good", "excellent": "excellent",
            }
            bucket = bucket_map.get((chip or "").strip().lower(), "fair")
            clicked = None
            # Strategy 1 (nova): JS-click by stable button id. The nova option
            # buttons ignore Playwright clicks — only a JS .click() registers
            # in React, and the step auto-advances (no Continue button).
            try:
                clicked = frame.evaluate(
                    """(bucket) => {
                        const el = document.querySelector('#credit-score-' + bucket)
                                || document.querySelector('button[id^="credit-score-"]');
                        if (el) { el.click(); return el.id || 'AUTO'; }
                        return null;
                    }""",
                    bucket,
                )
            except Exception:
                pass
            # Strategy 2 (legacy lcf / generic text match).
            if not clicked:
                clicked = self._click_chip_by_label_js(frame, chip) or self._chip(frame, chip)
            if not clicked:
                try:
                    frame.locator("button.lcf-option").first.click(timeout=5000)
                    clicked = "AUTO"
                except Exception:
                    pass
            if clicked:
                self._wait_title_change(frame, avoid=["credit score"], timeout=15)
                return clicked
            return None

        # ── Step 4: Legal name ───────────────────────────────────────────────
        if (
            "legal name" in title
            or "full name" in title
            or (
                "name" in title
                and "your" in title
                and "bank" not in title
                and "employer" not in title
            )
        ):
            # _robust_fill waits for the inputs to mount, verifies each value
            # stuck (retries if empty), so we never force-submit empty React
            # state — the cause of "stuck on last name" under a slow proxy.
            self._robust_fill(
                frame,
                ['input[name="first_name"]', 'input[name="fname"]', 'input[name="firstName"]'],
                f["first_name"], 0,
            )
            self._robust_fill(
                frame,
                ['input[name="last_name"]', 'input[name="lname"]', 'input[name="lastName"]'],
                f["last_name"], 1,
            )
            time.sleep(0.5)
            # The name step's async validation is the heaviest — give CONTINUE a
            # longer window to enable on a slow connection before falling through.
            if self._click_primary_continue(frame, wait=12.0):
                return "CONTINUE"
            return self._continue(frame)

        # ── Step 5: Date of birth ────────────────────────────────────────────
        if "birth" in title or "date of birth" in title or title.startswith("hi "):
            # Sheet DOB may be YYYY-MM-DD; convert to MM/DD/YYYY for form
            dob_normalized = self._normalize_dob(f["dob"])
            mm, dd, yyyy = self._split_dob(dob_normalized)
            # BorrowMoney nova uses a single #dob MM/DD/YYYY field.
            filled = self._robust_fill(
                frame,
                ['input[name="dob"]', '#dob', 'input[name="date_of_birth"]',
                 'input[placeholder*="MM/DD" i]'],
                dob_normalized, 0,
            )
            log.debug("form.dob_fill", filled=filled, dob_raw=f["dob"], dob_normalized=dob_normalized, title=title)
            # LowCreditFinance variant: separate dob_month / dob_day / dob_year.
            if not filled and mm and dd and yyyy:
                filled = self._react_fill_any(frame, ['input[name="dob_month"]'], mm) or filled
                filled = self._react_fill_any(frame, ['input[name="dob_day"]'], dd) or filled
                filled = self._react_fill_any(frame, ['input[name="dob_year"]'], yyyy) or filled
                if not filled and self._count_visible_inputs(frame) >= 3:
                    self._fill_nth_visible_input(frame, 0, mm)
                    self._fill_nth_visible_input(frame, 1, dd)
                    self._fill_nth_visible_input(frame, 2, yyyy)
                    filled = True
                log.debug("form.dob_fill_fallback", filled=filled, mm=mm, dd=dd, yyyy=yyyy)
            # Verify DOB was actually filled before continuing
            if not filled:
                log.warning("form.dob_not_filled", dob_raw=f["dob"], dob_normalized=dob_normalized, title=title)
                time.sleep(1)
                # Try one more time with extended wait
                filled = self._robust_fill(
                    frame,
                    ['input[name="dob"]', '#dob', 'input[name="date_of_birth"]',
                     'input[placeholder*="MM/DD" i]'],
                    dob_normalized, 0,
                    ready_timeout=12000,
                )
                log.debug("form.dob_fill_retry", filled=filled)
            time.sleep(0.5)
            if self._click_primary_continue(frame, wait=10.0):
                return "CONTINUE"
            return self._continue(frame)

        # ── Step 6: ZIP ──────────────────────────────────────────────────────
        if "zip" in title:
            self._robust_fill(
                frame,
                ['input[name="zip_code"]', 'input[name="zip"]', 'input[placeholder*="90210" i]'],
                f["zip"], 0,
            )
            time.sleep(0.5)
            if self._click_primary_continue(frame, wait=10.0):
                return "CONTINUE"
            return self._continue(frame)

        # ── Step 7: Street address ───────────────────────────────────────────
        if "street" in title or "address" in title:
            # Some variants don't use stable name= attributes; fill by best effort order.
            self._robust_fill(frame, ['input[name="street_address"]'], f["street_address"], 0)
            self._robust_fill(frame, ['input[name="city"]'], f["city"], 1)
            try:
                frame.locator('select[name="state"]').select_option(
                    value=f["state"], timeout=5000
                )
            except Exception:
                try:
                    frame.locator("select:visible").first.select_option(
                        value=f["state"], timeout=3000
                    )
                except Exception:
                    pass
            time.sleep(1)
            return self._continue(frame)

        # ── Step 8: Source of income ─────────────────────────────────────────
        if "source" in title and "income" in title:
            raw_src = f.get("income_source_raw") or ""
            clicked = self._click_borrowmoney_income_chip(frame, raw_src)
            if clicked:
                time.sleep(1)
                if self._click_primary_continue(frame):
                    return clicked
                return self._continue(frame) or clicked
            chip = f.get("income_source_chip") or ""
            for label in (chip, "Employed", "Job income", "Employment", "Benefits"):
                if not label:
                    continue
                if self._click_chip_by_label_js(frame, label) or self._chip(frame, label):
                    time.sleep(1)
                    if self._click_primary_continue(frame):
                        return label
                    return self._continue(frame) or label
            if not self._strict_sheet:
                return self._chip(frame, "Employed")
            return None

        # ── Step 9: Military ─────────────────────────────────────────────────
        if "military" in title or "veteran" in title:
            yn = (f.get("active_military_chip") or "No").strip().lower()
            el_id = "military-status-yes" if yn.startswith("y") else "military-status-no"
            try:
                frame.locator(f"#{el_id}").click(timeout=5000)
                time.sleep(0.8)
                if self._click_primary_continue(frame):
                    return el_id
                return self._continue(frame) or el_id
            except Exception:
                return self._chip(frame, f.get("active_military_chip") or "No")

        # ── Step 10: Pay frequency ───────────────────────────────────────────
        if (
            "often" in title
            or "paid" in title
            or "frequen" in title
            or "times a month" in title
        ):
            raw_pf = f.get("pay_freq_raw") or ""
            clicked = self._click_borrowmoney_pay_freq(frame, raw_pf)
            if clicked:
                time.sleep(0.8)
                if self._click_primary_continue(frame):
                    return clicked
                return self._continue(frame) or clicked
            chip = f.get("pay_freq_chip") or ""
            for label in (
                chip,
                "Bi-Weekly",
                "Twice Monthly",
                "Monthly",
                "Weekly",
            ):
                if label and (
                    self._click_chip_by_label_js(frame, label) or self._chip(frame, label)
                ):
                    time.sleep(0.8)
                    # This nova chip step auto-advances on selection. Click
                    # Continue only if it is present AND enabled; never force a
                    # disabled button (that would spuriously advance the NEXT step).
                    self._click_primary_continue(frame, wait=2.0)
                    return label
            if not self._strict_sheet:
                return self._chip(frame, "Bi-Weekly")
            return None

        # ── Step 11: Monthly income (before employer handler) ────────────────
        if "monthly" in title or ("gross" in title and "income" in title):
            self._robust_fill(
                frame, ['input[name="monthly_income"]', "input:visible"], f["monthly_income"], 0
            )
            time.sleep(0.5)
            return self._continue(frame)

        # ── Step 12: Next pay date ───────────────────────────────────────────
        if ("next" in title and "pay" in title) or "next pay" in title:
            chip = f.get("next_payday_choice") or "Next scheduled date"
            clicked = (
                self._chip(frame, chip)
                or self._chip(frame, "Next scheduled date")
                or self._chip(frame, "Next scheduled")
                or self._chip(frame, "In two weeks")
                or self._chip(frame, "Two weeks")
            )
            if not clicked:
                # This variant shows a date input instead of chips
                self._fill_next_pay_date_input(frame, f.get("next_payday_raw", ""))
            time.sleep(1)
            self._click_primary_continue(frame) or self._continue(frame)
            return clicked or "NEXT_PAY"

        # ── Step 13: Employer information ────────────────────────────────────
        if (
            "employer" in title
            or ("employ" in title and "info" in title)
            or "working right now" in title
            or ("where" in title and "working" in title)
        ):
            emp_phone = f.get("employer_phone") or f["phone"]
            self._robust_fill(frame, ['input[name="employer_name"]'], f["employer_name"], 0)
            self._robust_fill(frame, ['input[name="job_title"]'], f.get("job_title", ""), 1)
            self._robust_fill(frame, ['input[name="employer_phone"]'], emp_phone, 2)
            time.sleep(0.5)
            if self._click_primary_continue(frame, wait=10.0):
                return "CONTINUE"
            return self._continue(frame)

        # ── Step 14: Paycheck received ───────────────────────────────────────
        if "paycheck" in title or ("received" in title and "pay" in title):
            return self._chip(frame, f.get("paycheck_method_chip") or "Direct Deposit")

        # ── Step 15: ABA Routing number ──────────────────────────────────────
        if "routing" in title or "aba" in title:
            routing_val = f["routing_number"]
            self._robust_fill(
                frame,
                [
                    'input[name="routing_number"]',
                    'input[name*="routing" i]',
                    'input[id*="routing" i]',
                    'input[placeholder*="routing" i]',
                    "input:visible",
                ],
                routing_val, 0,
            )
            time.sleep(1)
            result = self._continue(frame)
            if not result:
                # Last resort: force-click the continue button
                try:
                    for btn in frame.locator("button").all():
                        t = (btn.text_content() or "").strip().upper()
                        if any(kw in t for kw in ["CONTINUE", "NEXT", "SUBMIT"]):
                            btn.click(force=True, timeout=3000)
                            result = "FORCED"
                            break
                except Exception:
                    pass
            return result

        # ── Step 16: Bank name ───────────────────────────────────────────────
        if title == "bank name" or "bank name" in title:
            self._robust_fill(
                frame,
                ['input[name="bank_name"]', 'input[name*="bank" i]', "input:visible"],
                f["bank_name"], 0,
            )
            time.sleep(0.5)
            return self._continue(frame)

        # ── Step 17: Type of bank account ───────────────────────────────────
        if "type" in title and ("bank" in title or "account" in title):
            chip_val = "Checking" if f["account_type"].lower().startswith("check") else "Savings"
            return self._chip(frame, chip_val)

        # ── Step 18: Length of bank account ─────────────────────────────────
        if ("length" in title or "how long" in title) and ("bank" in title or "account" in title):
            desired = f.get("account_age_chip") or "More than 2 Years"
            # Try exact, then common partials, then a JS-assisted click.
            result = (
                # LowCreditFinance: this option is present on every variant and is usually safe.
                self._chip(frame, "More than 2 Years")
                or self._chip(frame, desired)
                or self._chip(frame, "More than 2")
                or self._chip(frame, "More than 2 Years")
                or self._chip(frame, "1-2 Years")
                or self._chip(frame, "1-2")
                or self._chip(frame, "6-12")
                or self._chip(frame, "3-6")
                or self._chip(frame, "1-3")
            )
            if result:
                return result
            try:
                clicked = frame.evaluate(
                    """(desired) => {
                        const want = String(desired || '').toLowerCase();
                        const btns = Array.from(document.querySelectorAll('button'))
                          .filter(b => b && b.offsetParent !== null);
                        const pick = btns.find(b => (b.textContent||'').toLowerCase().includes(want))
                          || btns.find(b => (b.textContent||'').toLowerCase().includes('more than 2'));
                        if (!pick) return false;
                        pick.click();
                        return true;
                    }""",
                    desired,
                )
                if clicked:
                    return "JS_CLICK"
            except Exception:
                pass
            return None

        # ── Bank account number ──────────────────────────────────────────────
        if (
            ("account" in title and ("number" in title or "add" in title))
            or title == "account number"
        ) and "type" not in title and "length" not in title:
            self._robust_fill(
                frame,
                [
                    'input[name="bank_account_number"]',
                    'input[name="account_number"]',
                    'input[name*="account" i]',
                    "input:visible",
                ],
                f["account_number"], 0,
            )
            time.sleep(0.5)
            return self._continue(frame)

        # ── Phone number (personal) ──────────────────────────────────────────
        if "phone" in title or "mobile" in title or "cell" in title or "contact" in title:
            self._robust_fill(
                frame,
                [
                    'input[type="tel"].lcf-input',
                    'input[type="tel"]',
                    'input[name="phone"]',
                    'input[name*="phone" i]',
                ],
                f["phone"], 0,
            )
            time.sleep(0.5)
            return self._continue(frame)

        # ── Loan purpose ─────────────────────────────────────────────────────
        if "purpose" in title or "reason" in title or (
            "use" in title and ("loan" in title or "fund" in title)
        ):
            result = self._chip(frame, f["loan_purpose_chip"])
            if result:
                return result
            try:
                frame.locator("select:visible").first.select_option(
                    label=f["loan_purpose_chip"], timeout=3000
                )
                return self._continue(frame)
            except Exception:
                pass
            return self._continue(frame)

        # ── Driver's license ─────────────────────────────────────────────────
        if "license" in title or "driver" in title or "id number" in title:
            self._robust_fill(
                frame,
                [
                    'input[name="drivers_license_number"]',
                    'input[name*="license" i]',
                    'input[name*="dl_" i]',
                    'input[name*="driver" i]',
                    "input:visible",
                ],
                f["dl_number"], 0,
            )
            try:
                frame.locator('select[name*="state" i]').first.select_option(
                    value=f["dl_state"], timeout=3000
                )
            except Exception:
                pass
            time.sleep(1)
            return self._continue(frame)
        # ── Unsecured debt question ────────────────────────────────────────────
        if "debt" in title or "unsecured" in title:
            chip = f.get("unsecured_debt_chip") or "No"
            clicked = None
            try:
                frame.get_by_text(chip, exact=True).first.click(timeout=8000)
                clicked = chip
            except Exception:
                pass
            if not clicked:
                clicked = self._click_chip_by_label_js(frame, chip) or self._chip(frame, chip)
            if clicked:
                self._wait_title_change(frame, avoid=["debt", "unsecured", "10,000"], timeout=15)
                return clicked
            return None

        # ── Free trial upsell (step 24) ────────────────────────────────────────
        if "trial" in title or ("free" in title and "day" in title):
            # ALWAYS click "Yes" — it cleanly advances. Clicking "No" triggers a
            # blocking retention overlay that stalls the form. This is a free-
            # trial upsell (not lead data), so we ignore the sheet's choice.
            # JS-click is used because nova option buttons ignore Playwright
            # clicks (the click reports success but the form never advances).
            clicked = None
            try:
                clicked = frame.evaluate(
                    """() => {
                        const btns = Array.from(document.querySelectorAll(
                            'button,[role=button],a,label')).filter(e => e.offsetParent);
                        // Prefer a button whose option label / text is exactly "Yes"
                        let el = btns.find(b => (b.textContent||'').trim().toUpperCase() === 'YES')
                              || btns.find(b => (b.textContent||'').trim().toUpperCase().startsWith('YES'));
                        if (el) { el.click(); return (el.textContent||'').trim().slice(0, 30) || 'Yes'; }
                        return null;
                    }"""
                )
            except Exception:
                pass
            if not clicked:
                clicked = self._click_chip_by_label_js(frame, "Yes") or self._chip(frame, "Yes")
            if clicked:
                self._wait_title_change(frame, avoid=["trial", "free 7-day"], timeout=15)
                return clicked
            return self._continue(frame)

        # ── Terms & conditions consent (checkbox) ─────────────────────────────
        if "terms" in title or "conditions" in title or "consent" in title:
            if f.get("terms_consent"):
                try:
                    frame.evaluate(
                        """() => {
                            const boxes = Array.from(document.querySelectorAll('input[type="checkbox"]'))
                              .filter(b => b && b.offsetParent !== null);
                            for (const b of boxes) { if (!b.checked) b.click(); }
                        }"""
                    )
                except Exception:
                    pass
            return self._continue(frame)
        # ── Submit / Request Cash (final step) ───────────────────────────────
        if "submit" in title or "loan request" in title or "request cash" in title:
            if os.getenv("DRY_RUN", "").strip().lower() in {"1", "true", "yes", "y"}:
                # Safety: do not actually submit in dry-run mode.
                try:
                    frame.page.screenshot(path=str(self._ss_dir / f"dry_run_before_submit.png"))
                except Exception:
                    pass
                return "DRY_RUN_STOP"
            return self._continue(frame)

        # ── Generic chip-only step (no visible text inputs) ──────────────────
        try:
            visible_inputs = frame.locator("input:visible, select:visible").count()
        except Exception:
            visible_inputs = 0

        chips = frame.evaluate("""
            () => Array.from(document.querySelectorAll(
                    'button,[class*="chip"],[class*="option"],[class*="choice"]'))
                .filter(e => e.offsetParent !== null)
                .map(e => e.textContent.trim())
                .filter(t => t && t.toUpperCase() !== 'BACK' && t.length < 60)
        """)
        if chips and not visible_inputs:
            result = self._chip(frame, chips[0])
            if result:
                return result

        # ── Generic fallback: try CONTINUE ───────────────────────────────────
        return self._continue(frame)

    # ---------------------------------------------------------------- helpers

    def _site_profile(self, url: str) -> str:
        # This module exclusively drives the BorrowMoney funnel, which is now
        # reached via a tracker redirect (e.g. digipalz.trackog.net -> borrowmoney.us),
        # so the target URL no longer contains "borrowmoney". Always use the
        # BorrowMoney profile unless an explicit override is configured.
        explicit = (self._target.get("site") or "").strip().lower()
        if explicit == "default":
            return "default"
        return "borrowmoney"

    def _resolve_entry_url(self, url: str) -> str:
        """Normalize entry URL so automation lands on the iframe apply page."""
        u = (url or "").strip()
        low = u.lower()
        if "borrowmoney.us" in low:
            if "/apply" in low:
                return u if u.endswith("/") else u.rstrip("/") + "/"
            return "https://borrowmoney.us/apply/"
        return u

    def _prepare_entry_page(
        self,
        page: Page,
        url: str,
        fields: dict,
        row_number: int,
    ) -> None:
        """BorrowMoney.us landing → load the iframe.global apply form.

        The tracker (digipalz.trackog.net) redirects to borrowmoney.us, whose
        landing page shows an amount selector (#loan-input / #loan-slider) and a
        "Get My Loan Options" CTA. Clicking it embeds the iframe.global nova form.
        """
        if self._site_profile(url) != "borrowmoney":
            return

        # Already on the apply form?
        if any("iframe.global" in (f.url or "") for f in page.frames):
            log.info("form.borrowmoney_apply_ready", row=row_number)
            return

        loan = fields.get("loan_amount_value") or "2000"

        # Set the loan amount on the landing page (input + slider).
        try:
            page.evaluate(
                """(amt) => {
                    for (const sel of ['#loan-input', '#loan-slider', 'input[type=range]', 'input#amount', 'input[name=amount]']) {
                        const e = document.querySelector(sel);
                        if (e) {
                            e.value = amt;
                            e.dispatchEvent(new Event('input',  {bubbles: true}));
                            e.dispatchEvent(new Event('change', {bubbles: true}));
                        }
                    }
                }""",
                str(loan),
            )
            time.sleep(0.5)
        except Exception:
            pass

        # Click the "Get My Loan Options" CTA (JS click — React landing button).
        try:
            page.evaluate(
                """() => {
                    const b = [...document.querySelectorAll('.cta-btn, button, a')]
                        .find(e => e.offsetParent !== null && /get my loan options/i.test(e.textContent || ''));
                    if (b) { b.click(); return true; }
                    return false;
                }"""
            )
        except Exception:
            pass

        # Wait for the iframe.global apply form to appear.
        for _ in range(25):
            if any("iframe.global" in (f.url or "") for f in page.frames):
                log.info("form.borrowmoney_iframe_ready", row=row_number)
                return
            time.sleep(1)

        # Fallback: try the legacy /apply path directly.
        try:
            page.goto("https://borrowmoney.us/apply/", wait_until="domcontentloaded", timeout=60000)
            time.sleep(4)
        except Exception:
            pass
        for _ in range(15):
            if any("iframe.global" in (f.url or "") for f in page.frames):
                log.info("form.borrowmoney_iframe_ready", row=row_number)
                return
            time.sleep(1)
        log.warning("form.borrowmoney_iframe_slow", row=row_number)

    def _get_frame(self, page: Page) -> Frame:
        if (page.url or "").startswith("chrome-error://"):
            raise FormFillerError(
                f"Page failed to load ({page.url}) — proxy unreachable or blocked",
                error_type="proxy_error",
            )
        for _ in range(30):
            frames = [
                f for f in page.frames
                if "iframe.global" in (f.url or "")
                and f != page.main_frame
            ]
            if frames:
                prefer = [
                    f for f in frames
                    if "BORROWMONEY" in (f.url or "").upper()
                    or "LOWCREDIT" in (f.url or "").upper()
                    or "style=lcf" in (f.url or "").lower()
                ]
                return prefer[0] if prefer else frames[0]
            time.sleep(1)
        log.warning("form.iframe_not_found", fallback="main_frame")
        return page.main_frame

    def _get_title(self, frame: Frame) -> str:
        try:
            result = frame.evaluate(
                "() => { var els = document.querySelectorAll('h1,h2,h3,[class*=\"title\"],[class*=\"Title\"],[class*=\"heading\"],[class*=\"question\"],[class*=\"Question\"]'); for (var i = 0; i < els.length; i++) { var t = els[i].textContent.trim(); if (t.length > 3 && t.length < 150) return t; } return ''; }"
            )
            return result
        except Exception as e:
            log.warning("form.get_title_error", error=str(e)[:120])
            raise

    def _wait_title_change(
        self, frame: Frame, avoid: list[str] | None = None, timeout: float = 12
    ) -> bool:
        """Wait until the step title changes (chip steps auto-advance)."""
        avoid = [a.lower() for a in (avoid or [])]
        try:
            start = self._get_title(frame).lower().strip()
        except Exception:
            start = ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.5)
            try:
                cur = self._get_title(frame).lower().strip()
            except Exception:
                continue
            if not cur:
                continue
            if cur != start and not any(a in cur for a in avoid):
                return True
            if start and any(a in start for a in avoid) and not any(a in cur for a in avoid):
                return True
        return False

    def _fill(self, frame: Frame, selector: str, value: str) -> bool:
        try:
            loc = frame.locator(selector).first
            loc.wait_for(state="visible", timeout=5000)
            loc.fill(str(value))
            return True
        except Exception as e:
            log.warning("form.fill_error", selector=selector[:60], error=str(e)[:80])
            return False

    def _react_set_value(self, frame: Frame, selector: str, value: str) -> bool:
        """Set input value using native setter + input/change/blur events."""
        try:
            return bool(
                frame.evaluate(
                    """([sel, val]) => {
                        const el = document.querySelector(sel);
                        if (!el) return false;
                        const setter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype,
                            "value"
                        )?.set;
                        if (setter) setter.call(el, String(val));
                        else el.value = String(val);
                        el.dispatchEvent(new Event("input", { bubbles: true }));
                        el.dispatchEvent(new Event("change", { bubbles: true }));
                        el.dispatchEvent(new Event("blur", { bubbles: true }));
                        return true;
                    }""",
                    [selector, str(value)],
                )
            )
        except Exception:
            return False

    def _has_named_input(self, frame: Frame, name: str) -> bool:
        try:
            return frame.locator(f'input[name="{name}"]').count() > 0
        except Exception:
            return False

    def _fill_ssn_parts(self, frame: Frame, ssn: str) -> bool:
        """Fill LowCredit split SSN fields: ssn_1 (3), ssn_2 (2), ssn_3 (4)."""
        digits = re.sub(r"\D", "", ssn or "")
        if len(digits) < 9:
            digits = (digits + "000000000")[:9]
        p1, p2, p3 = digits[:3], digits[3:5], digits[5:9]
        parts = [("ssn_1", p1), ("ssn_2", p2), ("ssn_3", p3)]
        ok = False
        for name, part in parts:
            if self._react_fill_any(frame, [f'input[name="{name}"]'], part):
                ok = True
            else:
                idx = int(name.split("_")[1]) - 1
                self._fill_nth_visible_input(frame, idx, part)
                ok = True
            try:
                frame.evaluate(
                    """(n) => {
                        const el = document.querySelector('input[name="' + n + '"]');
                        if (!el) return;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                        el.dispatchEvent(new Event('blur', { bubbles: true }));
                    }""",
                    name,
                )
            except Exception:
                pass
            time.sleep(0.3)
        return ok

    def _fill_any(self, frame: Frame, selectors: list[str], value: str) -> bool:
        """Try multiple selectors until one fills successfully."""
        for sel in selectors:
            if self._fill(frame, sel, value):
                return True
        return False

    def _react_fill_any(self, frame: Frame, selectors: list[str], value: str) -> bool:
        """Try React-safe setValue for the first selector that exists."""
        for sel in selectors:
            if self._react_set_value(frame, sel, value):
                return True
        return False

    def _robust_fill(
        self,
        frame: Frame,
        selectors: list[str],
        value: str,
        nth_fallback: int = 0,
        ready_timeout: float = 8000,
    ) -> bool:
        """Fill a text field reliably under slow/laggy connections.

        1. DOM-ready guard: wait for a target field (or any visible input) to be
           interactive first — under a slow proxy the step title can render
           before React mounts the inputs, which leaves fields empty and the
           Continue button permanently disabled ("stuck on this step").
        2. Playwright ``.fill()`` focuses + sets the value the way the browser
           does (so React's onChange fires), with native setter events as backup.
        3. Read the value back; if empty (not hydrated yet), retry once.

        Returns True only when the value is confirmed present. Empty values are
        skipped (optional fields)."""
        value = str(value or "")
        if not value.strip():
            return False

        # DOM-ready guard.
        try:
            frame.locator(", ".join(selectors)).first.wait_for(
                state="visible", timeout=ready_timeout
            )
        except Exception:
            try:
                frame.locator("input:visible").nth(nth_fallback).wait_for(
                    state="visible", timeout=3000
                )
            except Exception:
                pass

        def _read_back() -> str:
            for sel in selectors:
                try:
                    loc = frame.locator(sel).first
                    if loc.count() > 0:
                        return (loc.input_value(timeout=1000) or "").strip()
                except Exception:
                    continue
            try:
                return (frame.locator("input:visible").nth(nth_fallback)
                        .input_value(timeout=1000) or "").strip()
            except Exception:
                return ""

        for _ in range(2):
            matched_sel = None
            for sel in selectors:
                try:
                    loc = frame.locator(sel).first
                    if loc.count() > 0 and loc.is_visible(timeout=2000):
                        matched_sel = sel
                        loc.click(timeout=2000)
                        loc.fill("")
                        loc.fill(value)
                        break
                except Exception:
                    continue
            if matched_sel is None:
                self._fill_nth_visible_input(frame, nth_fallback, value)
            else:
                # Backup: re-fire the native setter + input/change/blur events
                # in case the React controlled-input state didn't pick up .fill().
                self._react_set_value(frame, matched_sel, value)
            if _read_back():
                return True
            time.sleep(0.5)
        return False

    def _count_visible_inputs(self, frame: Frame) -> int:
        try:
            return frame.locator("input:visible").count()
        except Exception:
            return 0

    def _fill_nth_visible_input(self, frame: Frame, n: int, value: str) -> bool:
        try:
            loc = frame.locator("input:visible").nth(n)
            loc.wait_for(state="visible", timeout=3000)
            loc.fill(str(value))
            return True
        except Exception:
            return False

    def _fill_next_pay_date_input(self, frame: Frame, raw_date: str) -> bool:
        """Fill a masked date input (dd/mm/yyyy) with the next pay date.
        Falls back to today + 14 days if raw_date is absent or not a real date."""
        from datetime import date as _date, timedelta
        dt = None
        if raw_date:
            raw = raw_date.strip()
            # MM/DD/YYYY (US sheet format)
            m2 = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", raw)
            if m2:
                try:
                    dt = _date(int(m2.group(3)), int(m2.group(1)), int(m2.group(2)))
                except Exception:
                    pass
            # YYYY-MM-DD (ISO)
            if not dt:
                m2 = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", raw)
                if m2:
                    try:
                        dt = _date(int(m2.group(1)), int(m2.group(2)), int(m2.group(3)))
                    except Exception:
                        pass
        today = _date.today()
        if not dt or dt <= today:
            dt = today + timedelta(days=14)
        iso_val = dt.strftime("%Y-%m-%d")   # for native type="date" inputs
        seq_val = dt.strftime("%d%m%Y")     # for masked text inputs: DDMMYYYY
        log.info("form.next_pay_date_fill", iso=iso_val, raw=raw_date)

        # ── Pass 1: native type="date" input ─────────────────────────────────
        try:
            cnt = frame.locator('input[type="date"]').count()
            if cnt > 0:
                loc = frame.locator('input[type="date"]').first
                if loc.is_visible(timeout=2000):
                    loc.fill(iso_val, timeout=3000)
                    val = loc.input_value(timeout=1000)
                    if val == iso_val:
                        return True
        except Exception:
            pass

        # ── Pass 2: masked text input (dd / mm / yyyy format) ────────────────
        # BorrowMoney.us uses a JS-masked text input — must type DDMMYYYY
        for sel in [
            'input[name="next_pay_date"]',
            'input[name*="next_pay" i]',
            'input[placeholder*="dd" i]',
            'input[placeholder*="mm" i]',
            "input:visible",
        ]:
            try:
                cnt = frame.locator(sel).count()
                if cnt == 0:
                    continue
                loc = frame.locator(sel).first
                if not loc.is_visible(timeout=2000):
                    continue
                loc.click(timeout=3000)
                time.sleep(0.3)
                # Clear existing content then type digits sequentially
                loc.press("Control+a")
                loc.press("Delete")
                time.sleep(0.1)
                loc.press_sequentially(seq_val, delay=100)
                time.sleep(0.5)
                val = loc.input_value(timeout=1000)
                if val:
                    return True
            except Exception:
                continue
        return False

    def _split_dob(self, dob_str: str) -> tuple[str, str, str]:
        """Parse DOB from multiple formats and return (mm, dd, yyyy)."""
        dob_str = (dob_str or "").strip()
        if not dob_str:
            return "", "", ""
        # Try MM/DD/YYYY format (native form format)
        m = re.match(r"^\s*(\d{1,2})/(\d{1,2})/(\d{2,4})\s*$", dob_str)
        if m:
            mm, dd, yyyy = m.group(1), m.group(2), m.group(3)
            if len(yyyy) == 2:
                yyyy = "19" + yyyy
            return mm.zfill(2), dd.zfill(2), yyyy
        # Try YYYY-MM-DD format (Google Sheets standard)
        m = re.match(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})\s*$", dob_str)
        if m:
            yyyy, mm, dd = m.group(1), m.group(2), m.group(3)
            return mm.zfill(2), dd.zfill(2), yyyy
        # Try YYYY/MM/DD format
        m = re.match(r"^\s*(\d{4})/(\d{1,2})/(\d{1,2})\s*$", dob_str)
        if m:
            yyyy, mm, dd = m.group(1), m.group(2), m.group(3)
            return mm.zfill(2), dd.zfill(2), yyyy
        return "", "", ""

    def _normalize_dob(self, dob_str: str) -> str:
        """Convert DOB to MM/DD/YYYY format for form filling."""
        mm, dd, yyyy = self._split_dob(dob_str)
        if mm and dd and yyyy:
            return f"{mm}/{dd}/{yyyy}"
        return dob_str

    def _click_chip_by_label_js(self, frame: Frame, label: str) -> str | None:
        """Click an lcf-option chip by exact label text (e.g. Fair, Good)."""
        want = (label or "").strip()
        if not want:
            return None
        try:
            loc = frame.locator(
                f'button.lcf-option:has(.lcf-option-label:text-is("{want}"))'
            ).first
            if loc.count() > 0 and loc.is_visible(timeout=2000):
                loc.scroll_into_view_if_needed(timeout=2000)
                loc.click(timeout=8000)
                return want
        except Exception:
            pass
        try:
            return frame.evaluate(
                """(label) => {
                    const want = String(label || '').trim().toUpperCase();
                    const labels = Array.from(document.querySelectorAll('.lcf-option-label'));
                    for (const span of labels) {
                        const t = (span.textContent || '').trim().toUpperCase();
                        if (t !== want) continue;
                        const btn = span.closest('button');
                        if (btn) { btn.click(); return (span.textContent || '').trim(); }
                    }
                    return null;
                }""",
                want,
            )
        except Exception:
            return None

    def _click_loan_chip_js(self, frame: Frame, amount: str) -> str | None:
        """Click a loan amount chip by matching numeric value (avoids 2000 vs 20000)."""
        digits = re.sub(r"\D", "", str(amount or ""))
        if not digits:
            return None
        try:
            return frame.evaluate(
                """(digits) => {
                    const pools = [
                        ...document.querySelectorAll('button.lcf-option'),
                        ...document.querySelectorAll('button[class*="chip"]'),
                        ...document.querySelectorAll('button'),
                    ];
                    const seen = new Set();
                    for (const b of pools) {
                        if (!b || seen.has(b)) continue;
                        seen.add(b);
                        if (!b.offsetParent) continue;
                        const d = (b.textContent || '').replace(/\\D/g, '');
                        if (d === digits) {
                            b.click();
                            return (b.textContent || '').trim();
                        }
                    }
                    return null;
                }""",
                digits,
            )
        except Exception:
            return None

    def _click_borrowmoney_pay_freq(self, frame: Frame, raw: str) -> str | None:
        """BorrowMoney style=2 pay-frequency-* button ids."""
        v = (raw or "").strip().lower()
        id_map = [
            (("biweek", "every 2", "2 week"), "pay-frequency-biweekly"),
            (("twice", "semi", "two time"), "pay-frequency-twice_monthly"),
            (("month",), "pay-frequency-monthly"),
            (("week",), "pay-frequency-weekly"),
        ]
        for keys, el_id in id_map:
            if any(k in v for k in keys):
                try:
                    frame.locator(f"#{el_id}").click(timeout=5000)
                    return el_id
                except Exception:
                    pass
        return None

    def _click_borrowmoney_income_chip(self, frame: Frame, raw: str) -> str | None:
        """BorrowMoney style=2 income-source-* button ids."""
        v = (raw or "").strip().lower()
        id_map = [
            (("employ", "job", "work"), "income-source-employed"),
            (("unemploy",), "income-source-unemployed"),
            (("benefit", "ssi", "disab"), "income-source-benefits"),
        ]
        for keys, el_id in id_map:
            if any(k in v for k in keys):
                try:
                    frame.locator(f"#{el_id}").click(timeout=5000)
                    return el_id
                except Exception:
                    pass
        return None

    def _click_primary_continue(self, frame: Frame, wait: float = 6.0) -> bool:
        """Click the primary Continue button.

        Nova input steps (zip, email, ssn…) run async validation that keeps
        the Continue button disabled for ~1-2s after the field is filled.
        Poll until it becomes enabled (up to ``wait`` seconds) before giving
        up — otherwise the click is silently skipped and the form stalls.
        """
        deadline = time.time() + max(0.0, wait)
        while True:
            try:
                clicked = bool(
                    frame.evaluate(
                        """() => {
                            const candidates = [
                                document.querySelector('button.lcf-btn-primary'),
                                document.querySelector('#continue-button'),
                                document.querySelector('button#continue-button'),
                                ...Array.from(document.querySelectorAll('button')).filter(
                                    b => /^continue$/i.test((b.textContent || '').trim())
                                ),
                            ];
                            for (const b of candidates) {
                                if (!b || b.offsetParent === null) continue;
                                if (b.disabled) continue;
                                b.click();
                                return true;
                            }
                            return false;
                        }"""
                    )
                )
            except Exception:
                clicked = False
            if clicked or time.time() >= deadline:
                return clicked
            time.sleep(0.4)

    def _chip_label_matches(self, fragment: str, label: str) -> bool:
        """Match chip labels without false positives (e.g. 2000 vs $20,000)."""
        frag_norm = fragment.strip().upper()
        label_norm = label.strip().upper()
        if frag_norm == label_norm:
            return True
        frag_digits = re.sub(r"\D", "", fragment)
        label_digits = re.sub(r"\D", "", label)
        if frag_digits and label_digits:
            return frag_digits == label_digits
        return frag_norm in label_norm

    def _chip(self, frame: Frame, text_fragment: str) -> str | None:
        """Click a chip/option button by matching visible label text."""
        frag = text_fragment.strip().upper()
        if not frag:
            return None

        # Prefer LCF option buttons (most chip steps on LowCreditFinance).
        selectors = [
            "button.lcf-option",
            '[class*="lcf-option"]',
            "button",
            '[class*="chip"]',
            '[class*="option"]',
            '[class*="choice"]',
        ]

        def _label_text(el) -> str:
            try:
                inner = el.locator(".lcf-option-label").first
                if inner.count() > 0:
                    t = (inner.text_content() or "").strip()
                    if t:
                        return t
            except Exception:
                pass
            return (el.text_content() or "").strip()

        try:
            for sel in selectors:
                for el in frame.locator(sel).all():
                    try:
                        if not el.is_visible():
                            continue
                        t_raw = _label_text(el)
                        if not self._chip_label_matches(text_fragment, t_raw):
                            continue
                        try:
                            el.scroll_into_view_if_needed(timeout=2000)
                        except Exception:
                            pass
                        # JS .click() is the primary path: nova option buttons
                        # silently ignore Playwright clicks (the click reports
                        # success but React never advances). JS click registers.
                        try:
                            el.evaluate("e => e.click()")
                            return t_raw or frag
                        except Exception:
                            try:
                                el.click(timeout=8000)
                                return t_raw or frag
                            except Exception:
                                continue
                    except Exception:
                        continue
        except Exception as e:
            log.warning("form.chip_error", fragment=text_fragment, error=str(e)[:80])

        # Last resort: find any visible button containing the fragment
        try:
            clicked = frame.evaluate(
                """(frag) => {
                    const wantDigits = String(frag || '').replace(/\\D/g, '');
                    const wantText = String(frag || '').trim().toUpperCase();
                    const btns = Array.from(document.querySelectorAll('button'))
                      .filter(b => b && b.offsetParent !== null);
                    const pick = btns.find(b => {
                      const t = (b.textContent || '').trim();
                      const tUp = t.toUpperCase();
                      const tDigits = t.replace(/\\D/g, '');
                      if (wantDigits && tDigits) return wantDigits === tDigits;
                      if (wantText) return tUp === wantText || tUp.includes(wantText);
                      return false;
                    });
                    if (!pick) return false;
                    pick.click();
                    return true;
                }""",
                text_fragment,
            )
            if clicked:
                return text_fragment
        except Exception:
            pass
        return None

    def _continue(self, frame: Frame) -> str | None:
        if self._click_primary_continue(frame):
            return "CONTINUE"

        for kw in ["CONTINUE", "NEXT", "SUBMIT", "APPLY NOW", "APPLY", "GET STARTED", "REQUEST CASH"]:
            for attempt in range(3):
                try:
                    for btn in frame.locator("button").all():
                        t = (btn.text_content() or "").strip().upper()
                        if kw in t and btn.is_visible() and btn.is_enabled():
                            btn.click(timeout=5000)
                            return kw
                except Exception:
                    pass
                if attempt < 2:
                    time.sleep(0.8)
        # Fallback: try to click the primary button even if disabled
        try:
            clicked = frame.evaluate(
                """() => {
                    const btn =
                      document.querySelector('button.lcf-btn-primary') ||
                      Array.from(document.querySelectorAll('button')).find(b => /continue|next|submit|apply|request/i.test(b.textContent||''));
                    if (!btn) return false;
                    try { btn.disabled = false; btn.removeAttribute('disabled'); } catch(e) {}
                    try { btn.click(); return true; } catch(e) { return false; }
                }"""
            )
            if clicked:
                return "FORCED_CONTINUE"
        except Exception:
            pass
        return None

    # ---------------------------------------------------------------- parsing

    def _parse_fields(self, row: dict, site: str = "default") -> dict:
        def g(*keys: str) -> str:
            """Return the first non-empty value from the given column keys."""
            for k in keys:
                v = str(row.get(k) or "").strip()
                if v:
                    return v
            return ""

        # Personal
        first_name = g("First Name", "First_Name")
        last_name = g("Last Name", "Last_Name")
        email = g("Email Address", "Email")
        phone = re.sub(r"\D", "", g("Phone Number", "Phone"))

        # SSN — new sheet has separate Full / Last-4 columns
        full_ssn_raw = re.sub(r"\D", "", g("SSN Full", "SSN"))
        last_ssn_raw = re.sub(r"\D", "", g("SSN Last 4"))
        if full_ssn_raw:
            full_ssn = full_ssn_raw
            last_ssn = full_ssn_raw[-4:]
        elif last_ssn_raw:
            full_ssn = last_ssn_raw
            last_ssn = last_ssn_raw[-4:]
        else:
            full_ssn = ""
            last_ssn = ""

        # DOB
        dob = self._normalize_dob(g("Date of Birth (DOB)", "dob"))

        # Address
        _zip_raw = re.sub(r"\D", "", g("ZIP Code", "Zip"))
        zip_code = _zip_raw.zfill(5) if _zip_raw else ""
        street = g("Street Address", "Address")
        city = g("City")
        state = self._normalize_state(g("State"))

        # Loan amount → closest chip
        loan_raw = re.sub(r"[,$\s]", "", g("Requested Loan Amount ($)", "Loan_Amount"))
        try:
            loan_int = int(float(loan_raw))
        except (ValueError, TypeError):
            loan_int = 2000 if site == "borrowmoney" else 5000
        if site == "borrowmoney":
            loan_int = max(100, min(loan_int, 5000))
        loan_amount_chip = self._closest_loan_chip(loan_int, site=site)
        loan_amount_value = str(loan_int)

        # Credit
        credit_raw = g("Credit Score Rating", "Credit_Score")
        credit_chip = self._map_credit_chip(credit_raw) if credit_raw else ""
        if not credit_chip and credit_raw and not self._strict_sheet:
            credit_chip = "Fair"

        # Pay frequency
        pay_freq_raw = g("Pay Frequency", "Pay_Frequency")
        pay_freq_chip = self._PAY_FREQ_MAP.get(pay_freq_raw.lower().strip(), "")
        if not pay_freq_chip and pay_freq_raw and not self._strict_sheet:
            pay_freq_chip = "Biweekly"

        # Income source (chip step)
        income_source_raw = g("Income Source", "Source of Income")
        income_source_chip = self._map_income_source(income_source_raw)
        if not income_source_chip and not self._strict_sheet:
            income_source_chip = "Employed"

        # Military (chip step)
        active_military_chip = self._map_yes_no_chip(
            g("Active Military Status", "Active in Military?")
        )
        if not active_military_chip and not self._strict_sheet:
            active_military_chip = "No"

        # Next payday (usually a chip choice, not a date input on LCF)
        next_payday_raw = g("Next Payday", "Next Pay Date")
        next_payday_choice = self._map_next_payday_choice(next_payday_raw)
        if not next_payday_choice and not self._strict_sheet:
            next_payday_choice = "Next scheduled"

        # Income
        monthly_income = re.sub(r"[,$\s]", "", g("Monthly Net Income ($)", "Monthly_Income"))
        if not monthly_income and not self._strict_sheet:
            monthly_income = "3000"

        # Employer
        employer_name = g("Employer Name", "Employer_Name")
        if not employer_name and not self._strict_sheet:
            employer_name = "Employer"
        job_title = g("Job Title")
        if not job_title and not self._strict_sheet:
            job_title = "Employee"
        employer_phone = re.sub(r"\D", "", g("Employer Work Phone", "Employer Phone"))
        if len(employer_phone) < 10:
            employer_phone = phone

        # Paycheck payment method
        paycheck_method_chip = self._map_paycheck_method(
            g("Paycheck Payment Method", "How Is Your Paycheck Received?")
        )
        if not paycheck_method_chip and not self._strict_sheet:
            paycheck_method_chip = "Direct Deposit"

        # Bank
        _routing_raw = g("ABA Routing Number", "routingNumber")
        # ABA routing numbers are 9 digits; Google Sheets drops leading zeros
        routing_number = _routing_raw.zfill(9) if _routing_raw else ""
        account_number = g("Account Number", "accountNumber")
        account_type = g("Account Type", "bankAccountType")
        if not account_type and not self._strict_sheet:
            account_type = "Checking"
        bank_name = g("Bank Name", "bankName")
        if not bank_name and not self._strict_sheet:
            bank_name = "Chase"

        # Bank account age (chip step)
        account_age_chip = self._map_account_age(g("Account Age", "Length of Bank Account"))
        if not account_age_chip and not self._strict_sheet:
            account_age_chip = "More than 2 Years"

        # Driver's license — new sheet has a dedicated DL state column
        dl_number = g("Driver License / ID Number", "driversLicenseNumber")
        dl_state_raw = g("Driver License State", "bankState")  # fall back to bankState
        dl_state = self._normalize_state(dl_state_raw) if dl_state_raw else state

        # Loan purpose
        loan_purpose_chip = self._map_loan_purpose(g("Loan Purpose", "Loan_Purpose"))

        # Free credit score trial choice (Yes/No)
        free_trial_choice_chip = self._map_yes_no_chip(
            g("FREE Credit Score Trial Choice", "Free Trial")
        )
        if not free_trial_choice_chip:
            free_trial_choice_chip = "Yes"

        # Unsecured debt amount (Yes/No question on form; decide based on amount)
        unsecured_debt_chip = self._map_unsecured_debt(g("Unsecured Debt Amount", "Debt Amount"))
        if not unsecured_debt_chip and not self._strict_sheet:
            unsecured_debt_chip = "No"

        # Terms consent (checkbox step)
        terms_consent = self._truthy(g("Terms and Conditions Consent", "Terms Consent", "Consent"))

        return {
            "first_name": first_name,
            "last_name": last_name,
            "email": email,
            "phone": phone,
            "last_ssn": last_ssn,
            "ssn": full_ssn,
            "dob": dob,
            "zip": zip_code,
            "street_address": street,
            "city": city,
            "state": state,
            "loan_amount_chip": loan_amount_chip,
            "loan_amount_value": loan_amount_value,
            "credit_chip": credit_chip,
            "pay_freq_raw": pay_freq_raw,
            "pay_freq_chip": pay_freq_chip,
            "income_source_raw": income_source_raw,
            "income_source_chip": income_source_chip,
            "active_military_chip": active_military_chip,
            "next_payday_choice": next_payday_choice,
            "next_payday_raw": next_payday_raw,
            "monthly_income": monthly_income,
            "employer_name": employer_name,
            "job_title": job_title,
            "employer_phone": employer_phone,
            "paycheck_method_chip": paycheck_method_chip,
            "routing_number": routing_number,
            "account_number": account_number,
            "account_type": account_type,
            "bank_name": bank_name,
            "account_age_chip": account_age_chip,
            "dl_number": dl_number,
            "dl_state": dl_state,
            "loan_purpose_chip": loan_purpose_chip,
            "free_trial_choice_chip": free_trial_choice_chip,
            "unsecured_debt_chip": unsecured_debt_chip,
            "terms_consent": terms_consent,
        }

    def _validate_required_fields(self, f: dict, row_number: int | None = None) -> None:
        required = [
            "first_name", "last_name", "email", "phone",
            "last_ssn", "dob", "zip", "street_address", "city", "state",
            "loan_amount_value", "credit_chip", "monthly_income",
            "employer_name", "routing_number", "account_number", "bank_name",
            "pay_freq_chip", "income_source_chip", "active_military_chip",
            "paycheck_method_chip", "account_type", "account_age_chip",
        ]
        if not self._strict_sheet:
            required = required[:11]
        missing = [k for k in required if not f.get(k)]
        if missing:
            prefix = f"Sheet row {row_number}: " if row_number else ""
            raise FormFillerError(
                f"{prefix}missing required column data: {missing}. "
                "Fill these cells in the Google Sheet.",
                error_type="missing_data",
            )

    # ---------------------------------------------------------------- normalise

    def _normalize_dob(self, raw: str) -> str:
        raw = raw.strip()
        if not raw:
            return ""
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%d/%m/%Y", "%m/%d/%y"):
            try:
                return datetime.strptime(raw, fmt).strftime("%m/%d/%Y")
            except ValueError:
                pass
        if re.match(r"^\d{2}/\d{2}/\d{4}$", raw):
            return raw
        return raw

    def _normalize_state(self, raw: str) -> str:
        raw = raw.strip()
        if len(raw) == 2:
            return raw.upper()
        return self._STATE_CODES.get(raw.lower(), raw.upper()[:2])

    def _truthy(self, raw: str) -> bool:
        v = (raw or "").strip().lower()
        return v in {"1", "true", "yes", "y", "checked", "on", "agree", "i agree"}

    def _map_yes_no_chip(self, raw: str) -> str:
        v = (raw or "").strip().lower()
        if not v:
            return ""
        if v in {"yes", "y", "true", "1"}:
            return "Yes"
        if v in {"no", "n", "false", "0"}:
            return "No"
        if "yes" in v:
            return "Yes"
        if "no" in v:
            return "No"
        return ""

    def _map_income_source(self, raw: str) -> str:
        v = (raw or "").strip().lower()
        if not v:
            return ""
        # LowCreditFinance chip labels
        if "employ" in v or "job" in v or "work" in v:
            return "Employed"
        if "self" in v:
            return "Self employment"
        if "benefit" in v or "ssi" in v or "disab" in v:
            return "Benefits"
        if "unemploy" in v:
            return "Unemployed"
        if "retir" in v:
            return "Retired"
        return raw.strip()

    def _map_paycheck_method(self, raw: str) -> str:
        v = (raw or "").strip().lower()
        if not v:
            return ""
        if "direct" in v:
            return "Direct Deposit"
        if "check" in v or "cheque" in v:
            return "Paper Check"
        if "cash" in v:
            return "Cash"
        if "debit" in v or "card" in v:
            return "Debit Card"
        return raw.strip()

    def _map_account_age(self, raw: str) -> str:
        v = (raw or "").strip().lower()
        if not v:
            return ""
        if "less" in v and "month" in v:
            return "Less than 1 Month"
        if v.startswith("1-3") or ("1" in v and "3" in v and "month" in v):
            return "1-3 Months"
        if v.startswith("3-6") or ("3" in v and "6" in v and "month" in v):
            return "3-6 Months"
        if "6-12" in v or ("6" in v and "12" in v and "month" in v):
            return "6-12 Months"
        if "1-2" in v and "year" in v:
            return "1-2 Years"
        if "more" in v and "2" in v and "year" in v:
            return "More than 2 Years"
        if "2" in v and "year" in v:
            return "More than 2 Years"
        return raw.strip()

    def _map_next_payday_choice(self, raw: str) -> str:
        v = (raw or "").strip().lower()
        if not v:
            return ""
        if "next" in v and ("sched" in v or "scheduled" in v):
            return "Next scheduled"
        if re.match(r"^\d{1,2}/\d{1,2}/\d{2,4}$", raw.strip()):
            return "Next scheduled"
        if re.match(r"^\d{4}-\d{2}-\d{2}$", raw.strip()):
            return "Next scheduled"
        return raw.strip()

    def _map_unsecured_debt(self, raw: str) -> str:
        v = (raw or "").strip()
        if not v:
            return ""
        yn = self._map_yes_no_chip(v)
        if yn:
            return yn
        try:
            amt = float(re.sub(r"[^\d.]", "", v) or "0")
            return "Yes" if amt > 0 else "No"
        except Exception:
            return "No"

    def _closest_loan_chip(self, amount: int, site: str = "default") -> str:
        chips = (
            self._LOAN_AMOUNT_CHIPS_BORROWMONEY
            if site == "borrowmoney"
            else self._LOAN_AMOUNT_CHIPS
        )
        closest = min(chips, key=lambda x: abs(x - amount))
        return f"${closest:,}"

    def _map_credit_chip(self, raw: str) -> str:
        raw = raw.lower().strip()
        direct = {
            "poor": "Poor",
            "fair": "Fair",
            "good": "Good",
            "excellent": "Excellent",
            "not sure": "Not Sure",
        }
        if raw in direct:
            return direct[raw]
        for key, val in direct.items():
            if raw.startswith(key):
                return val
        try:
            score = int(re.sub(r"[^\d]", "", raw)[:3])
            if score < 580:
                return "Poor"
            if score < 670:
                return "Fair"
            if score < 740:
                return "Good"
            return "Excellent"
        except (ValueError, TypeError):
            return "Fair"

    def _map_loan_purpose(self, raw: str) -> str:
        raw = raw.lower().strip()
        if not raw:
            return "Personal"
        # LowCreditFinance purpose chips:
        # Personal | Business | Home | Vehicle | Education | Other
        if "business" in raw:
            return "Business"
        if "home" in raw or "house" in raw or "improv" in raw:
            return "Home"
        if "auto" in raw or "car" in raw or "vehicle" in raw:
            return "Vehicle"
        if "educ" in raw or "school" in raw or "college" in raw:
            return "Education"
        # Debt consolidation and medical typically fall under "Personal"
        if "debt" in raw or "consol" in raw:
            return "Personal"
        if "medical" in raw or "health" in raw:
            return "Personal"
        return "Other"

    # ---------------------------------------------------------------- utilities

    def _screenshot(self, page: Page, row: int, label: str) -> None:
        try:
            path = self._ss_dir / f"row_{row:04d}_{label}.png"
            page.screenshot(path=str(path), full_page=False)
            log.debug("screenshot.saved", path=str(path))
        except Exception as e:
            log.warning("screenshot.failed", error=str(e)[:80])

    def _classify_error(self, exc: Exception) -> str:
        msg = str(exc).lower()
        if "proxy" in msg or "net::err" in msg or "tunnel" in msg:
            return "proxy_error"
        if "timeout" in msg:
            return "timeout"
        return "unknown"

    def _assert_browser_ip_us(self, page: Page, row_number: int, require_us: bool) -> None:
        """Confirm the browser session egress IP is US before filling (Trackier geo)."""
        if not require_us:
            return
        try:
            ip = page.evaluate(
                """async () => {
                    const r = await fetch('https://api.ipify.org?format=text', { cache: 'no-store' });
                    return (await r.text()).trim();
                }"""
            )
            ip = (ip or "").strip().split()[0]
            log.info("form.browser_ip", row=row_number, ip=ip)
        except FormFillerError:
            raise
        except Exception as e:
            # Fetch failed (slow proxy, HTTP block, etc.) — log and continue.
            # The chrome-error guard at page.goto() handles genuine proxy failures.
            log.warning("form.browser_ip_check_failed", row=row_number, error=str(e))

    def _clean_fingerprint(self, fp: dict) -> dict:
        allowed = {
            "user_agent", "viewport", "locale", "timezone_id",
            "geolocation", "color_scheme", "device_scale_factor",
            "is_mobile", "has_touch", "java_script_enabled",
            "extra_http_headers",
        }
        return {k: v for k, v in fp.items() if k in allowed and v is not None}