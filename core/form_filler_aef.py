"""
core/form_filler_aef.py — americanemergencyfund.com multi-step form automation.

Unlike the iframe-embedded funnels this project previously targeted, AEF hosts
its own Bootstrap 5 wizard directly on the landing page:

    <form id="applicantForm">   one step rendered at a time
    #nextBtn                    "Continue", becomes "Request Loan" on the last step
    #backBtn                    previous step
    #tcpa_acceptButton          consent gate shown before the form is usable

Step order is NOT fixed.  The server injects a `missingFields` array into the
page; `markFieldsWithErrors()` sets `step.display = true` only for steps that
carry a still-missing field, and `renderStep()` skips everything else.  A
returning applicant, or a resubmit after a validation bounce, therefore sees a
different and shorter sequence than a cold visitor.

Consequently this filler is **field-driven, not index-driven**: on every
iteration it reads the input/select names currently visible inside
#applicantForm and dispatches on those names.  Any step order works, steps may
be skipped or repeated, and a new step only needs a new entry in _HANDLERS.

Field reference (extracted from template/8735/js/fields.js):

  loanreqamt        radio 1000|3000|5000, or #customLoanAmount text (100-35000)
  fname lname dob   text; dob strictly MM/DD/YYYY, age must be 18..120
  email             email; #lastfourssn (maxlength 4) shares this step
  phhm              text, must match US phone regex (area code 2-9)
  hpostal haddress1 hcity hstate    zip exactly 5 digits; hstate is a <select>
  i_ad_ccDebtAmt    select 0|4999|9999|…|50000   (credit-card debt bracket)
  hmonthsat         radio 60|48|36|24|12  (months at address)
  ishowner          radio 1=Yes 0=No
  netim             select 11000|10000|…|1500    (net monthly income bracket)
  priincsrc         radio 1=employed 2=benefits
  payfreq           radio 1=weekly 2=bi-weekly 3=monthly 4=semi-monthly
  isactmil          radio 1=Yes 0=No
  ename             text (employer name)
  emonthsat         radio 60|48|36|24|12  (months at employer)
  phwrk             text, same phone regex as phhm
  licn licst        text + <select> (driver's licence number / state)
  ssn               text
  bacctype          radio 1=Checking 2=Savings
  bmonthsat         radio 60|48|36|24|12  (months at bank)
  isdd              radio 1=Yes 0=No (direct deposit)
  crscore           radio 2=700+ 3=600-700 4=500-600 5=below-500 1=not sure
  loanreason        radio 14=cc-debt-relief 1=debt-consolidation 13=other
  bname baba bacc   bank name is derived server-side from the routing number;
                    baba must be 9 digits AND pass the ABA checksum;
                    bacc must be 5..18 digits.  This is the final step.

Completion: submitFormData() POSTs to /?cmd=ExtApplyV2&skipscs=1&… then routes
to /?cmd=RenderResult&uuid=… (approved, or declined-with-offers) or off-site to
offer.requestedresults.com (declined / rejected / processing error).  Both count
as a delivered lead; the redirect target is recorded in the sheet notes.
"""
from __future__ import annotations

import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import structlog
from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

from utils.proxy_manager import ProxyManager
from utils.stealth import inject_stealth

log = structlog.get_logger(__name__)

_FORM = "#applicantForm"
_NEXT = "#nextBtn"


class FormFillerError(Exception):
    """Base exception for form-filling errors."""

    def __init__(self, message: str, error_type: str = "unknown"):
        super().__init__(message)
        self.error_type = error_type


def _digits(raw: str) -> str:
    return re.sub(r"\D", "", raw or "")


def _aba_checksum_ok(routing: str) -> bool:
    """The form runs this exact check client-side before it will advance."""
    if len(routing) != 9 or not routing.isdigit():
        return False
    d = [int(c) for c in routing]
    total = (
        3 * (d[0] + d[3] + d[6])
        + 7 * (d[1] + d[4] + d[7])
        + 1 * (d[2] + d[5] + d[8])
    )
    return total % 10 == 0


class FormFiller:
    """Fills and submits the americanemergencyfund.com application."""

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
        self._max_steps = int(config.get("form", {}).get("max_steps", 40))
        self._crashed = False   # per-row; reset at the top of process_row

    # ------------------------------------------------------------------ public

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

        url = (self._target.get("url") or "https://www.americanemergencyfund.com/").strip()
        page: Page | None = None
        self._crashed = False

        with sync_playwright() as pw:
            launch_args: dict[str, Any] = {
                "headless": headless,
                "args": ["--no-sandbox", "--disable-dev-shm-usage"],
            }
            # Playwright's *bundled* Chromium reproducibly crashes its renderer on
            # this site part-way through loading the third-party fraud-detection
            # script — headless and headed alike.  A stock Google Chrome install
            # runs the identical page without trouble, so we launch that channel
            # by default.  Set BROWSER_CHANNEL=chromium to force the bundled
            # build (expect crashes on this target).
            channel = os.getenv("BROWSER_CHANNEL", "chrome").strip().lower()
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

                self._accept_tcpa(page, row_number)
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

    def _accept_tcpa(self, page: Page, row_number: int) -> None:
        """The consent gate hides #nextBtn until accepted.  Absent on some
        campaign variants, so a miss here is not an error."""
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                clicked = page.evaluate(
                    """() => {
                        const vis = e => e && e.offsetParent !== null;
                        const a = document.getElementById('tcpa_acceptButton');
                        if (vis(a)) { a.click(); return 'accept'; }
                        const c = document.getElementById('tcpa_continueButton');
                        if (vis(c) && !c.disabled) { c.click(); return 'continue'; }
                        return null;
                    }"""
                )
            except Exception:
                clicked = None
            if clicked:
                log.info("form.tcpa", action=clicked, row=row_number)
                time.sleep(1.2)
                continue
            if self._form_ready(page):
                return
            time.sleep(1)
        log.info("form.tcpa_absent", row=row_number)

    def _form_ready(self, page: Page) -> bool:
        try:
            return bool(page.evaluate(
                """() => {
                    const v = e => e && e.offsetParent !== null;
                    const f = document.getElementById('applicantForm');
                    if (!v(f)) return false;
                    return f.querySelectorAll('input,select,textarea').length > 0;
                }"""
            ))
        except Exception:
            return False

    # --------------------------------------------------------------- form flow

    def _fill_form(self, page: Page, f: dict, row_number: int, stop_event) -> str:
        """Drive the wizard until it submits.  Returns a short outcome label."""
        for _ in range(40):
            self._check_stop(stop_event)
            if self._form_ready(page):
                break
            time.sleep(0.5)
        else:
            self._screenshot(page, row_number, "no_form")
            raise FormFillerError("Application form never rendered", error_type="stuck")

        seen: dict[str, int] = {}
        for step_num in range(self._max_steps):
            self._check_stop(stop_event)

            done = self._completion_state(page)
            if done:
                log.info("form.completed", step=step_num, outcome=done, row=row_number)
                return done

            names = self._visible_field_names(page)
            if not names:
                # Between renders, or a non-field interstitial — give it a beat.
                time.sleep(1.5)
                if self._completion_state(page):
                    return self._completion_state(page) or "submitted"
                continue

            sig = ",".join(sorted(names))
            seen[sig] = seen.get(sig, 0) + 1
            if seen[sig] > 3:
                self._screenshot(page, row_number, f"stuck_{step_num}")
                raise FormFillerError(
                    f"Form stopped advancing at step {step_num} (fields: {sig})",
                    error_type="stuck",
                )

            log.info("form.step", step=step_num, fields=sig[:70], row=row_number)
            self._live(page)

            res = self._handle_step(page, names, f)
            if not res["known"]:
                self._screenshot(page, row_number, f"unhandled_{step_num}")
                raise FormFillerError(
                    f"Unrecognised step {step_num} — the site is asking for "
                    f"fields this filler does not handle: {sig}",
                    error_type="unhandled_step",
                )
            if not res["filled"]:
                # Every field was recognised but none would take its value —
                # usually a mapped option the site does not offer for this lead.
                self._screenshot(page, row_number, f"unfilled_{step_num}")
                raise FormFillerError(
                    f"Could not set any field on step {step_num}: {res['failed']} "
                    f"(value rejected or option missing)",
                    error_type="field_rejected",
                )
            if res["failed"]:
                log.warning("form.partial_step", step=step_num,
                            filled=res["filled"], failed=res["failed"], row=row_number)

            self._action_pause()
            self._click_next(page)
            self._await_change(page, sig)
            self._live(page)

        raise FormFillerError(
            f"Form did not complete within {self._max_steps} steps", error_type="timeout"
        )

    def _handle_step(self, page: Page, names: list[str], f: dict) -> bool:
        """Fill every field the current step exposes.  True if at least one
        known field was handled."""
        handlers: dict[str, Callable[[], bool]] = {
            "loanreqamt":     lambda: self._set_loan_amount(page, f),
            "fname":          lambda: self._text(page, "fname", f["first_name"]),
            "lname":          lambda: self._text(page, "lname", f["last_name"]),
            "dob":            lambda: self._text(page, "dob", f["dob"]),
            "email":          lambda: self._text(page, "email", f["email"]),
            "lastfourssn":    lambda: self._text(page, "lastfourssn", f["last_ssn"]),
            "phhm":           lambda: self._text(page, "phhm", f["phone"]),
            "phwrk":          lambda: self._text(page, "phwrk", f["employer_phone"]),
            "hpostal":        lambda: self._text(page, "hpostal", f["zip"]),
            "haddress1":      lambda: self._text(page, "haddress1", f["street_address"]),
            "hcity":          lambda: self._text(page, "hcity", f["city"]),
            "hstate":         lambda: self._select(page, "hstate", f["state"]),
            "i_ad_ccDebtAmt": lambda: self._select(page, "i_ad_ccDebtAmt", f["debt_bracket"]),
            "hmonthsat":      lambda: self._radio(page, "hmonthsat", f["address_months"]),
            "ishowner":       lambda: self._radio(page, "ishowner", f["is_homeowner"]),
            "netim":          lambda: self._select(page, "netim", f["income_bracket"]),
            "priincsrc":      lambda: self._radio(page, "priincsrc", f["income_source"]),
            "payfreq":        lambda: self._radio(page, "payfreq", f["pay_freq"]),
            "isactmil":       lambda: self._radio(page, "isactmil", f["is_military"]),
            "ename":          lambda: self._text(page, "ename", f["employer_name"]),
            "emonthsat":      lambda: self._radio(page, "emonthsat", f["employer_months"]),
            "licn":           lambda: self._text(page, "licn", f["dl_number"]),
            "licst":          lambda: self._select(page, "licst", f["dl_state"]),
            "ssn":            lambda: self._text(page, "ssn", f["ssn"]),
            "bacctype":       lambda: self._radio(page, "bacctype", f["account_type"]),
            "bmonthsat":      lambda: self._radio(page, "bmonthsat", f["bank_months"]),
            "isdd":           lambda: self._radio(page, "isdd", f["is_direct_deposit"]),
            "crscore":        lambda: self._radio(page, "crscore", f["credit_score"]),
            "loanreason":     lambda: self._radio(page, "loanreason", f["loan_reason"]),
            "baba":           lambda: self._text(page, "baba", f["routing_number"]),
            "bacc":           lambda: self._text(page, "bacc", f["account_number"]),
            "bname":          lambda: self._text(page, "bname", f["bank_name"]),
        }

        known, filled, failed = [], [], []
        for name in names:
            fn = handlers.get(name)
            if fn is None:
                # customLoanAmount is driven by the loanreqamt handler; anything
                # else unknown means the site added a step we do not cover.
                if name != "customLoanAmount":
                    log.warning("form.unknown_field", field=name)
                continue
            known.append(name)
            try:
                (filled if fn() else failed).append(name)
            except Exception as e:
                failed.append(name)
                log.warning("form.field_error", field=name, error=str(e)[:90])
        return {"known": known, "filled": filled, "failed": failed}

    # ------------------------------------------------------------- interactions

    def _visible_field_names(self, page: Page) -> list[str]:
        try:
            return page.evaluate(
                """() => {
                    const v = e => e.offsetParent !== null && e.getClientRects().length > 0;
                    const f = document.getElementById('applicantForm');
                    if (!f) return [];
                    const out = [];
                    f.querySelectorAll('input,select,textarea').forEach(el => {
                        if (!el.name) return;
                        // radios: the styled label is visible, the input often is not
                        const visible = el.type === 'radio'
                            ? !!document.querySelector('label[for="' + el.id + '"]')
                            : v(el);
                        if (visible && !out.includes(el.name)) out.push(el.name);
                    });
                    return out;
                }"""
            ) or []
        except Exception:
            return []

    def _text(self, page: Page, name: str, value: str) -> bool:
        """Type into a field, honouring input masks, then verify by read-back."""
        value = str(value or "")
        if not value:
            return False
        sel = f'{_FORM} [name="{name}"]'
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=8000)
            loc.click()
            loc.fill("")
            # Masked fields (phone, dob, ssn) rewrite the value on each keypress,
            # so type rather than set: a bulk fill() leaves them malformed.
            loc.press_sequentially(value, delay=self._key_delay())
        except Exception as e:
            log.warning("form.type_failed", field=name, error=str(e)[:80])

        got = self._read_back(page, name)
        if _digits(got) == _digits(value) or got.strip() == value.strip():
            return True

        # Mask rejected the typed form (or the field is React-controlled) —
        # fall back to the native setter plus the events listeners expect.
        try:
            page.evaluate(
                """([n, v]) => {
                    const el = document.querySelector('#applicantForm [name="' + n + '"]');
                    if (!el) return;
                    const proto = el instanceof HTMLTextAreaElement
                        ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                    Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, v);
                    el.dispatchEvent(new Event('input',  { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.dispatchEvent(new Event('blur',   { bubbles: true }));
                }""",
                [name, value],
            )
        except Exception as e:
            log.warning("form.set_failed", field=name, error=str(e)[:80])
        got = self._read_back(page, name)
        ok = _digits(got) == _digits(value) or got.strip() == value.strip()
        if not ok:
            log.warning("form.value_mismatch", field=name,
                        wanted=value[:20], got=got[:20])
        return ok

    def _read_back(self, page: Page, name: str) -> str:
        try:
            return page.evaluate(
                """(n) => {
                    const el = document.querySelector('#applicantForm [name="' + n + '"]');
                    return el ? (el.value || '') : '';
                }""",
                name,
            ) or ""
        except Exception:
            return ""

    def _select(self, page: Page, name: str, value: str) -> bool:
        """Choose an <option> by value, falling back to its visible label.

        select_option() takes plain strings only — passing a compiled regex
        raises "'re.Pattern' object is not iterable" — so label matching is done
        in the page instead, where we can be lenient about case and whitespace.
        """
        if not value:
            return False
        loc = page.locator(f'{_FORM} select[name="{name}"]').first
        for kwargs in ({"value": value}, {"label": value}):
            try:
                loc.select_option(timeout=6000, **kwargs)
                return True
            except Exception:
                pass
        try:
            matched = page.evaluate(
                """([n, v]) => {
                    const sel = document.querySelector('#applicantForm select[name="' + n + '"]');
                    if (!sel) return null;
                    const want = String(v).trim().toLowerCase();
                    const opts = Array.from(sel.options);
                    const hit =
                        opts.find(o => o.value.trim().toLowerCase() === want) ||
                        opts.find(o => o.text.trim().toLowerCase() === want) ||
                        opts.find(o => o.text.trim().toLowerCase().includes(want) && want.length > 1);
                    if (!hit) return null;
                    sel.value = hit.value;
                    sel.dispatchEvent(new Event('input',  { bubbles: true }));
                    sel.dispatchEvent(new Event('change', { bubbles: true }));
                    return hit.value;
                }""",
                [name, value],
            )
            if matched is not None:
                return True
            available = page.evaluate(
                """(n) => { const s = document.querySelector('#applicantForm select[name="' + n + '"]');
                            return s ? Array.from(s.options).map(o => o.value).slice(0, 20) : []; }""",
                name,
            )
            log.warning("form.select_no_option", field=name, wanted=value, available=available)
        except Exception as e:
            log.warning("form.select_failed", field=name, value=value, error=str(e)[:80])
        return False

    def _radio(self, page: Page, name: str, value: str) -> bool:
        """Bootstrap hides the radio itself and styles its <label>; click the
        label so the site's own change handlers fire."""
        if not value:
            return False
        try:
            ok = page.evaluate(
                """([n, v]) => {
                    const el = document.querySelector(
                        '#applicantForm input[type=radio][name="' + n + '"][value="' + v + '"]');
                    if (!el) return false;
                    const lbl = el.id ? document.querySelector('label[for="' + el.id + '"]') : null;
                    (lbl || el).click();
                    if (!el.checked) {
                        el.checked = true;
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                    return el.checked;
                }""",
                [name, value],
            )
            if ok:
                return True
            log.warning("form.radio_missing", field=name, value=value)
        except Exception as e:
            log.warning("form.radio_failed", field=name, value=value, error=str(e)[:80])
        return False

    def _set_loan_amount(self, page: Page, f: dict) -> bool:
        """Radio wins over the free-text box when both are present, so use the
        radio when the request maps cleanly and the text box otherwise."""
        amount = f["loan_amount"]
        radio_value = "1000" if amount <= 1000 else "3000" if amount <= 3000 else "5000"
        has_custom = bool(page.locator(f"{_FORM} #customLoanAmount").count())
        if has_custom and 100 <= amount <= 35000 and amount not in (1000, 3000, 5000):
            if self._text(page, "customLoanAmount", str(amount)):
                return True
        return self._radio(page, "loanreqamt", radio_value)

    def _click_next(self, page: Page) -> None:
        try:
            page.evaluate(
                """() => {
                    const b = document.getElementById('nextBtn');
                    if (b && b.offsetParent !== null && !b.disabled) { b.click(); return; }
                    const r = document.getElementById('returnSubmit');
                    if (r && r.offsetParent !== null && !r.disabled) r.click();
                }"""
            )
        except Exception as e:
            log.warning("form.next_failed", error=str(e)[:80])

    def _await_change(self, page: Page, prev_sig: str, timeout: float = 20.0) -> None:
        """Wait for the rendered field set to change, the page to navigate, or a
        validation error to surface."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.6)
            if self._completion_state(page):
                return
            names = self._visible_field_names(page)
            if names and ",".join(sorted(names)) != prev_sig:
                return
            err = self._validation_error(page)
            if err:
                log.warning("form.validation_error", error=err[:110])
                return

    def _validation_error(self, page: Page) -> str:
        try:
            return page.evaluate(
                """() => {
                    const v = e => e.offsetParent !== null;
                    const el = Array.from(document.querySelectorAll(
                        '.invalid-feedback,.is-invalid,.custom-error,.text-danger')).filter(v)[0];
                    return el ? (el.innerText || '').trim().slice(0, 160) : '';
                }"""
            ) or ""
        except Exception:
            return ""

    def _completion_state(self, page: Page) -> str:
        """Non-empty once the application has been submitted and routed."""
        try:
            url = page.url or ""
        except Exception:
            return ""
        if "cmd=RenderResult" in url:
            return "offers page (RenderResult)"
        if "offer.requestedresults.com" in url:
            m = re.search(r"subid=([\w]+)", url)
            return f"redirected to offers ({m.group(1)})" if m else "redirected to offers"
        return ""

    # ------------------------------------------------------------------ timing

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
        def g(*keys: str) -> str:
            for k in keys:
                v = str(row.get(k) or "").strip()
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
        routing = _digits(g("ABA Routing Number", "routingNumber", "Routing Number"))

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
        try:
            path = self._ss_dir / f"row_{row:04d}_{label}.png"
            page.screenshot(path=str(path), full_page=False)
        except Exception as e:
            log.warning("screenshot.failed", error=str(e)[:80])

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
