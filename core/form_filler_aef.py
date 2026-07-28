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

import re
import time
from typing import Callable

import structlog
from playwright.sync_api import Page

from core.lead_platform import BasePlatformFiller, FormFillerError, _digits

log = structlog.get_logger(__name__)

_FORM = "#applicantForm"
_NEXT = "#nextBtn"

__all__ = ["FormFiller", "FormFillerError"]


class FormFiller(BasePlatformFiller):
    """americanemergencyfund.com — server-rendered Bootstrap wizard."""

    default_url = "https://www.americanemergencyfund.com/"

    def _prepare(self, page: Page, row_number: int) -> None:
        self._accept_tcpa(page, row_number)

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
            self._read_pause()          # human beat: read the step before filling

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
            if known:                       # not the first field on this step
                self._field_pause()         # human beat between fields
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
