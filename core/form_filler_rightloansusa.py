"""
core/form_filler_rightloansusa.py — rightloansusa.com (rndframe.com) automation.

RightLoansUSA embeds an **rndframe.com** form in an iframe (``name="rsIframe"``,
``installmentStep.php``).  It is a single ``RSform`` whose ~50 fields are revealed
progressively — a quick prequalify (``h_``-prefixed helper fields: last-4 SSN,
birth year, zip, name, email, phone) followed by the full application, submitting
to ``process.php``.  A different platform from the other offers (not ExtApplyV2,
not iframe.global, not the Round Sky SmartForm), so this filler is standalone —
interface-compatible with the engine (same FormFiller / FormFillerError /
process_row contract).

Because every field lives in the DOM from the start, the strategy is: on each
screen, (re)apply ALL mapped field values by name (idempotent — a native value
setter + input/change events, which the form validates on Continue), tick the
terms checkbox when present, then advance (click the loan-amount button on the
first screen, else Continue/Submit).  This survives the progressive reveal and
the occasional re-render without tracking every individual step.

Field / value reference (rndframe RSform):
  requestedLoanAmount   300 | 600 | 1000 | 3000 | 10000 | 30000  (range buckets)
  firstName lastName    text (+ h_firstName / h_lastName mirrors)
  email                 text
  home_phone1/2/3       phone split 3 / 3 / 4
  birthdate_month/day/year  DOB selects (+ h_birthdate_year)
  ssn1/2/3              SSN split 3 / 2 / 4 (+ h_ssn3 last-4)
  address zip           (+ h_zip);  monthsAtResidence 12|24|36|48
  housing               own | rent
  activeMilitary        true | false
  hasCarTitle           true | false
  incomeType            employment | benefits | self_employed
  monthlyIncome         1500 | 2000 | 2500 | 3000 | 3500 | 4000 | 5000 | 5001
  payPeriod             weekly | biweekly | twice_monthly | monthly
  monthsEmployed        12 | 24 | 36 | 48
  payMonth payDay1      next-payday month / day
  employer occupation   text ;  work_phone1/2/3  phone split
  drivingLicenseNumber  text ;  drivingLicenseState  2-letter select
  routingNumber(9) accountNumber bankName
  bankAccountType       checking | savings
  directDeposit         true | false ;  monthsWithBank 12|24|36|48
  creditScore           750 | 690 | 630 | 590   (excellent/good/fair/poor)
  loanPurpose           other | debt | debtSettlement | auto | creditCard | …
  highDebt              1 | 0 ;  termsField  checkbox (consent)
"""
from __future__ import annotations

import os
import re
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import structlog
from playwright.sync_api import Browser, BrowserContext, Frame, Page, sync_playwright

from core.lead_platform import BasePlatformFiller, _aba_checksum_ok, _digits
from utils.proxy_manager import ProxyManager
from utils.stealth import inject_stealth

log = structlog.get_logger(__name__)

__all__ = ["FormFiller", "FormFillerError"]


class FormFillerError(Exception):
    def __init__(self, message: str, error_type: str = "unknown"):
        super().__init__(message)
        self.error_type = error_type


_STATE_CODES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV", "new hampshire": "NH",
    "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN",
    "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
}


class FormFiller:
    """Fills the rightloansusa.com (rndframe) application inside its rsIframe."""

    default_url = "https://www.rightloansusa.com/"

    # Reuse the shared bank-fill + CTA-find JS; the post-offer handler itself is
    # RightLoansUSA-specific (its offers are short secondary forms — select
    # loan-option checkboxes, maybe a field, then a CTA — that must be walked).
    _JS_POST_STATE = BasePlatformFiller._JS_POST_STATE
    _JS_FILL_BANK = BasePlatformFiller._JS_FILL_BANK
    _JS_CLICK_CONTINUE = BasePlatformFiller._JS_CLICK_CONTINUE

    def __init__(self, config: dict) -> None:
        self._config = config
        self._target = config.get("target", {})
        cd = config.get("delays", {})
        self._delays = {
            "min_typing_delay": cd.get("rl_min_typing", 0.02),
            "max_typing_delay": cd.get("rl_max_typing", 0.06),
            "min_action_delay": cd.get("rl_min_action", 0.25),
            "max_action_delay": cd.get("rl_max_action", 0.7),
            "read_pause":       cd.get("rl_read_pause", [0.2, 0.5]),
            "offer_load_wait":  cd.get("rl_offer_load_wait", 12),
        }
        self._ss_dir = Path(config.get("screenshots", {}).get("directory", "screenshots"))
        self._ss_dir.mkdir(parents=True, exist_ok=True)
        self._save_shots = bool(config.get("screenshots", {}).get("enabled", False))
        self._max_steps = int(config.get("form", {}).get("max_steps", 55))

    # ------------------------------------------------------------------ public

    def process_row(self, row: dict[str, Any], fingerprint: dict[str, Any],
                    proxy_url: str | None, row_number: int, stop_event=None) -> dict[str, Any]:
        headless = os.getenv("HEADLESS", "true").lower() == "true"
        f = self._parse_fields(row)
        self._validate_required_fields(f)
        url = (self._target.get("url") or self.default_url).strip() or self.default_url
        page: Page | None = None

        with sync_playwright() as pw:
            launch_args: dict[str, Any] = {"headless": headless, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
            engine_tag = self._config.get("browser", {}).get("engine_tag")
            if engine_tag:
                launch_args["args"].append(f"--lead-engine-tag={engine_tag}")
            channel = (self._config.get("browser", {}).get("channel")
                       or os.getenv("BROWSER_CHANNEL", "chrome")).strip().lower()
            if channel and channel not in ("chromium", "bundled", "default"):
                launch_args["channel"] = channel
            if proxy_url:
                launch_args["proxy"] = ProxyManager.to_playwright_proxy(proxy_url)
            browser: Browser = pw.chromium.launch(**launch_args)
            self._duplicate = False   # set if the site recognises a returning lead
            try:
                ctx: BrowserContext = browser.new_context(**self._clean_fingerprint(fingerprint))
                page = ctx.new_page()
                inject_stealth(page, fingerprint)
                self._goto(page, url, row_number, stop_event)
                outcome = self._fill_form(page, f, row_number, stop_event)
                self._screenshot(page, row_number, "success")
                sid = str(uuid.uuid4())[:8].upper()
                log.info("form.success", row=row_number, submission_id=sid,
                         outcome=outcome, duplicate=self._duplicate)
                ctx.close()
                note = f"Submitted — {outcome}"
                if self._duplicate:
                    note = ("[duplicate] Lead already submitted (returning applicant) — "
                            "completed the condensed flow through to the offer/redirect. " + note)
                return {"status": "Success", "notes": note, "submission_id": sid}
            except FormFillerError:
                if page:
                    try: self._screenshot(page, row_number, "error")
                    except Exception: pass
                raise
            except Exception as exc:
                et = self._classify_error(exc)
                if page:
                    try: self._screenshot(page, row_number, et)
                    except Exception: pass
                raise FormFillerError(str(exc), error_type=et) from exc
            finally:
                try: browser.close()
                except Exception: pass

    # --------------------------------------------------------------- navigation

    def _goto(self, page: Page, url: str, row_number: int, stop_event) -> None:
        last: Exception | None = None
        for attempt in range(1, 4):
            self._check_stop(stop_event)
            last = None
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                last = e
            landed = ""
            try: landed = page.url or ""
            except Exception as e: last = e
            if not last and not landed.startswith("chrome-error://"):
                time.sleep(4)
                return
            log.warning("form.nav_retry", attempt=attempt, row=row_number,
                        error=str(last)[:80] if last else "chrome-error")
            if attempt < 3: time.sleep(3)
        raise FormFillerError(f"Navigation failed: {last}" if last else "Page failed to load",
                              error_type="proxy_error")

    def _get_frame(self, page: Page) -> Frame | None:
        """The rndframe application frame — found by CONTENT (the RSform / its
        fields / the 'LET'S GET STARTED' heading), since the cross-origin iframe's
        name and url are not reliable.  Falls back to name/url."""
        for fr in page.frames:
            try:
                hit = fr.evaluate(r"""() => !!(
                    document.getElementById('RSform')
                    || document.querySelector('[name=requestedLoanAmount],[name=h_firstName],[name=firstName],[name=h_ssn3]')
                    || /let'?s get started/i.test(document.body ? document.body.innerText : '')
                )""")
                if hit:
                    return fr
            except Exception:
                pass
        # Fallback: name / url match (frame may be mid-load and un-evaluable).
        for fr in page.frames:
            try:
                if fr.name == "rsIframe" or "rndframe.com" in (fr.url or "") \
                        or "installmentStep" in (fr.url or ""):
                    return fr
            except Exception:
                pass
        return None

    def _wait_frame(self, page: Page, stop_event, timeout: float = 45.0) -> Frame:
        """Wait for the rndframe form to attach AND render its controls."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._check_stop(stop_event)
            fr = self._get_frame(page)
            if fr is not None:
                try:
                    if fr.evaluate("() => document.querySelectorAll('input,select,button,[class*=option]').length") > 3:
                        return fr
                except Exception:
                    pass
            time.sleep(1)
        self._screenshot(page, 0, "no_frame")
        raise FormFillerError("rndframe application form never rendered", error_type="stuck")

    # --------------------------------------------------------------- form flow

    def _fill_form(self, page: Page, f: dict, row_number: int, stop_event) -> str:
        frame = self._wait_frame(page, stop_event)
        log.info("form.frame", url=(frame.url or "")[:70], row=row_number)
        values = self._field_values(f)
        seen: dict[str, int] = {}

        for step in range(self._max_steps):
            self._check_stop(stop_event)

            done = self._completion_state(page, frame)
            if done:
                log.info("form.offers_reached", step=step, outcome=done, row=row_number)
                self._handle_post_offer(page, f, row_number, stop_event)
                return done

            if self._is_captcha(page):
                self._screenshot(page, row_number, "captcha")
                if getattr(self, "_duplicate", False):
                    return "duplicate (returning applicant — captcha on re-submit)"
                raise FormFillerError(
                    "reCAPTCHA challenge presented — it cannot be solved by automation. "
                    "This is usually triggered by re-submitting the same lead or a flagged "
                    "IP; a fresh, not-yet-submitted lead on a clean US proxy typically "
                    "does not hit it.", error_type="captcha")

            frame = self._get_frame(page) or frame
            try:
                st = self._state(frame)
            except Exception:
                time.sleep(1.5)
                continue
            if st["loading"] and not st["fields"] and not st["buttons"]:
                time.sleep(2)
                continue
            if not st["fields"] and not st["buttons"]:
                # Interstitial / navigating — give it a beat, then re-check.
                time.sleep(1.5)
                if self._completion_state(page, frame):
                    continue
                continue

            # Returning applicant: the site recognises an already-submitted lead
            # and jumps straight to a condensed "Congratulations!" step (with
            # h_special_* fields) instead of the full form.  Flag it as a
            # duplicate but keep going — fill it, submit, follow the redirect.
            if not getattr(self, "_duplicate", False) \
                    and re.search(r"congratulat", st["heading"], re.I) \
                    and any(str(x["name"]).startswith("h_special") for x in st["fields"]):
                self._duplicate = True
                log.info("form.duplicate_detected", step=step, row=row_number)

            sig = st["sig"]
            seen[sig] = seen.get(sig, 0) + 1
            if seen[sig] > 4:
                if getattr(self, "_duplicate", False):
                    # The site won't advance a recognised returning lead past its
                    # condensed confirmation — it was already delivered.  Done.
                    log.info("form.duplicate_stopped", step=step, row=row_number)
                    return "duplicate (returning applicant)"
                self._screenshot(page, row_number, f"stuck_{step}")
                bad = self._invalid_fields(frame)
                hint = (f" — rejected/invalid: {bad}. This offer verifies the phone/data "
                        f"against real-number checks, so fictitious (e.g. 555) values fail."
                        if bad else "")
                raise FormFillerError(
                    f"Form stopped advancing at step {step}: {st['heading'][:60]!r} "
                    f"(fields={[x['name'] for x in st['fields']][:8]}){hint}",
                    error_type="field_rejected" if bad else "stuck")

            log.info("form.step", step=step, heading=st["heading"][:55],
                     fields=[x["name"] for x in st["fields"]][:8], row=row_number)
            self._live(page)
            self._read_pause()

            self._apply_values(frame, values)     # (re)fill every mapped field
            # "Next pay date" — payDay1 is a dependent dropdown that repopulates
            # when payMonth changes, so it must be sequenced (month → wait → day).
            if any(x["name"] in ("payMonth", "payDay1") for x in st["fields"]):
                self._set_paydate(frame, values.get("payMonth", ""), values.get("payDay1", ""))
            self._tick_terms(frame)                # consent checkbox when present
            self._action_pause()
            self._advance(frame, values, st)       # click amount button / Continue / Submit
            self._await_change(page, frame, sig)
            self._live(page)

        if getattr(self, "_duplicate", False):
            return "duplicate (returning applicant — no further progress)"
        raise FormFillerError(f"Form did not complete within {self._max_steps} steps",
                              error_type="timeout")

    _JS_VIS = "const vis=e=>e.offsetParent!==null&&e.getClientRects().length>0;"

    def _state(self, frame: Frame) -> dict:
        return frame.evaluate("() => {" + self._JS_VIS + r"""
            const t = e => (e.innerText||e.textContent||'').replace(/\s+/g,' ').trim();
            const fields = Array.from(document.querySelectorAll('input,select,textarea'))
                .filter(e => vis(e) && e.type !== 'hidden')
                .map(e => ({name:e.name||e.id||'', type:e.type}));
            const heading = (() => {
                for (const e of document.querySelectorAll('h1,h2,h3,h4,legend,label,[class*=question],[class*=title],[class*=head],p')) {
                    if (vis(e)) { const s=t(e); if (s.length>3 && s.length<130) return s; } }
                return ''; })();
            const buttons = [...new Set(Array.from(document.querySelectorAll(
                'button,[role=button],input[type=submit],input[type=button],a.btn,.btn,[class*=option],[class*=choice],[class*=answer]'))
                .filter(vis).map(t).filter(x => x && x.length < 46))];
            const loading = /loading|verifying|please wait|processing|one moment|searching/i.test(document.body.innerText||'');
            return {fields, heading, buttons, loading,
                    sig: fields.map(f=>f.name).sort().join(',') + '|' + heading.slice(0,50)};
        }""") or {"fields": [], "heading": "", "buttons": [], "loading": False, "sig": ""}

    def _apply_values(self, frame: Frame, values: dict) -> None:
        """Set every mapped RSform field (visible or not) via a native setter so
        the form's on-Continue validation sees a valid value.  Idempotent."""
        try:
            frame.evaluate(r"""(V) => {
                const setText = (el, val) => {
                    if (val === undefined || val === null || val === '') return;
                    if (el.value && el.value === String(val)) return;
                    const proto = Object.getPrototypeOf(el);
                    const d = Object.getOwnPropertyDescriptor(proto,'value')
                           || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value');
                    d.set.call(el, String(val));
                    ['input','keyup','change','blur'].forEach(ev => el.dispatchEvent(new Event(ev,{bubbles:true})));
                };
                const setSelect = (el, val) => {
                    if (val === undefined || val === null || val === '') return;
                    const want = String(val).toLowerCase();
                    let hit = Array.from(el.options).find(o => o.value.toLowerCase() === want)
                           || Array.from(el.options).find(o => o.text.trim().toLowerCase() === want)
                           || Array.from(el.options).find(o => o.text.trim().toLowerCase().includes(want) && want.length>1);
                    if (hit) { el.value = hit.value; ['input','change'].forEach(ev => el.dispatchEvent(new Event(ev,{bubbles:true}))); }
                };
                for (const [name, val] of Object.entries(V)) {
                    const els = document.getElementsByName(name);
                    if (!els || !els.length) continue;
                    for (const el of els) {
                        if (el.tagName === 'SELECT') setSelect(el, val);
                        else if (el.type === 'checkbox') { if (!el.checked) el.click(); }
                        else if (el.type === 'radio') { /* handled elsewhere */ }
                        else setText(el, val);
                    }
                }
            }""", values)
        except Exception as e:
            log.warning("form.apply_failed", error=str(e)[:80])

    def _is_captcha(self, page: Page) -> bool:
        """A visible reCAPTCHA / 'verify you're human' challenge is present."""
        for fr in list(page.frames):
            try:
                hit = fr.evaluate(r"""() => {
                    const b = (document.body ? document.body.innerText : '').toLowerCase();
                    if (/verify you'?re human|solving the captcha|i'?m not a robot|n[aã]o sou um rob[oô]|complete the captcha|prove you'?re (not a robot|human)/.test(b)) return true;
                    const el = document.querySelector('.g-recaptcha, #recaptcha, iframe[src*="recaptcha/api2/anchor"], iframe[title*="reCAPTCHA"]');
                    return !!(el && el.offsetParent !== null);
                }""")
                if hit:
                    return True
            except Exception:
                pass
        return False

    def _invalid_fields(self, frame: Frame) -> list:
        """Visible fields the form has flagged invalid (red border / error class)."""
        try:
            return frame.evaluate("() => {" + self._JS_VIS + r"""
                const out = [];
                document.querySelectorAll('input,select,textarea').forEach(e => {
                    if (!vis(e) || e.type === 'hidden') return;
                    const cls = (e.className || '').toLowerCase();
                    const bad = /invalid|error|red|danger/.test(cls)
                        || e.getAttribute('aria-invalid') === 'true'
                        || /(2[0-9]{2},\s*0,\s*0)|rgb\(2[0-9]{2}, ?0, ?0\)/.test(getComputedStyle(e).borderColor || '');
                    if (bad && (e.name || e.id)) out.push(e.name || e.id);
                });
                return [...new Set(out)];
            }""") or []
        except Exception:
            return []

    def _tick_terms(self, frame: Frame) -> None:
        try:
            frame.evaluate("() => {" + self._JS_VIS + r"""
                document.querySelectorAll('input[type=checkbox]').forEach(cb => {
                    if (!cb.checked) {
                        const lab = cb.id ? document.querySelector('label[for="'+cb.id+'"]') : null;
                        const wrap = cb.closest('label');
                        if (lab) lab.click(); else if (wrap) wrap.click(); else cb.click();
                        if (!cb.checked) { cb.checked = true; cb.dispatchEvent(new Event('change',{bubbles:true})); }
                    }
                });
            }""")
        except Exception:
            pass

    def _advance(self, frame: Frame, values: dict, st: dict) -> None:
        """Advance the form: the loan-amount screen uses $range buttons; every
        other screen uses Continue / Submit."""
        target_amt = values.get("requestedLoanAmount", "")
        try:
            frame.evaluate("([amt]) => {" + self._JS_VIS + r"""
                const t = e => (e.innerText||e.value||'').replace(/\s+/g,' ').trim();
                const enabled = e => vis(e) && !e.disabled && !/back|previous|español/i.test(t(e));
                // 1) Continue / Next / Submit
                let b = Array.from(document.querySelectorAll('button,input[type=submit],input[type=button],[role=button],a.btn,.btn'))
                    .filter(enabled).find(e => /^(continue|next|submit|proceed|get started|see if|go)$/i.test(t(e)));
                // 2) loan-amount range buttons ($100 - $500 …) — pick by target bucket
                if (!b) {
                    const ranges = Array.from(document.querySelectorAll('button,[role=button],a.btn,.btn,[class*=option],[class*=choice]'))
                        .filter(enabled).filter(e => /\$[\d,]+\s*-\s*\$[\d,]+/.test(t(e)));
                    if (ranges.length) {
                        const bucket = { '300':0, '600':1, '1000':2, '3000':3, '10000':4, '30000':5 };
                        const idx = bucket[amt];
                        b = (idx !== undefined && ranges[idx]) ? ranges[idx] : ranges[Math.min(3, ranges.length-1)];
                    }
                }
                // 3) any single option / primary button
                if (!b) {
                    b = Array.from(document.querySelectorAll('[class*=option],[class*=choice],[class*=answer],button,input[type=submit],input[type=button],a.btn,.btn'))
                        .filter(enabled)[0];
                }
                if (b) b.click();
            """ + "}", [str(target_amt)])
        except Exception as e:
            log.warning("form.advance_failed", error=str(e)[:80])

    def _set_paydate(self, frame: Frame, target_month: str, target_day: str) -> None:
        """Fill the 'next pay date' step reliably.  payDay1 is a dependent select
        that repopulates when payMonth changes, so: set the month (matching the
        lead's, else the earliest offered), let the days refresh, then set the day
        (matching, else the nearest available).  Adapts a real pay date to the
        form's allowed window instead of failing."""
        try:
            chose = frame.evaluate(r"""([m]) => {
                const s = document.querySelector('[name=payMonth]'); if (!s) return '';
                const opts = Array.from(s.options).filter(o => o.value);
                if (!opts.length) return '';
                const hit = opts.find(o => o.value === String(m)) || opts[0];
                s.value = hit.value; ['input','change'].forEach(e => s.dispatchEvent(new Event(e,{bubbles:true})));
                return hit.value;
            }""", [str(target_month or "")])
            if not chose:
                return
            # Poll until payDay1 has repopulated, set the day, and confirm it
            # stuck (guards the async repopulate race that cleared it before).
            for _ in range(12):
                time.sleep(0.3)
                stuck = frame.evaluate(r"""([d]) => {
                    const s = document.querySelector('[name=payDay1]'); if (!s) return null;
                    const opts = Array.from(s.options).filter(o => o.value);
                    if (!opts.length) return false;
                    const want = parseInt(d, 10);
                    let hit = opts.find(o => o.value === String(d));
                    if (!hit && !isNaN(want)) {
                        hit = opts.reduce((a, b) =>
                            Math.abs(parseInt(b.value,10) - want) < Math.abs(parseInt(a.value,10) - want) ? b : a);
                    }
                    if (!hit) hit = opts[0];
                    s.value = hit.value; ['input','change'].forEach(e => s.dispatchEvent(new Event(e,{bubbles:true})));
                    return s.value !== '';
                }""", [str(target_day or "")])
                if stuck:
                    time.sleep(0.3)   # ensure no later repopulate wipes it
                    if frame.evaluate("() => { const s=document.querySelector('[name=payDay1]'); return !!(s && s.value); }"):
                        return
        except Exception as e:
            log.warning("form.paydate_failed", error=str(e)[:80])

    def _await_change(self, page: Page, frame: Frame, prev_sig: str, timeout: float = 16.0) -> None:
        end = time.time() + timeout
        while time.time() < end:
            time.sleep(0.6)
            if self._completion_state(page, frame):
                return
            fr = self._get_frame(page)
            if fr is None:
                return
            try:
                st = self._state(fr)
            except Exception:
                continue
            if st["sig"] and st["sig"] != prev_sig and not st["loading"]:
                return

    def _completion_state(self, page: Page, frame: Frame) -> str:
        """Non-empty once the application has been submitted and routed to an
        offers / results page (rndframe posts to process.php then shows offers)."""
        # Still filling: if the rndframe form still shows any fillable field
        # (text/tel/email/select), we are inside the application — including the
        # "Congratulations, {name}!" confirmation step — NOT on the offers page.
        try:
            fr = self._get_frame(page)
            if fr is not None:
                still = fr.evaluate("() => {" + self._JS_VIS + r"""
                    return Array.from(document.querySelectorAll('input,select,textarea'))
                        .some(e => vis(e) && ['text','tel','email','number','password',
                            'select-one','select-multiple','textarea'].includes(e.type)); }""")
                if still:
                    return ""
        except Exception:
            pass
        # Parent page URL / body
        try:
            purl = page.url or ""
        except Exception:
            purl = ""
        for host_hint in ("process.php", "results", "offers", "thank"):
            if host_hint in purl.lower():
                return f"submitted ({purl[:60]})"
        # Parent navigated off rightloansusa/rndframe entirely — it was routed to
        # an offer / advertiser page (this is where the post-offer handler takes
        # over; it also stops a duplicate lead from looping on an ad portal).
        try:
            from urllib.parse import urlparse
            phost = urlparse(purl).netloc.lower()
        except Exception:
            phost = ""
        if phost and not any(h in phost for h in ("rightloansusa.com", "rndframe.com")):
            return f"redirected ({phost})"
        try:
            pbody = page.evaluate("() => (document.body ? document.body.innerText : '').slice(0,1200)") or ""
        except Exception:
            pbody = ""
        # iframe body / url
        fbody, furl = "", ""
        try:
            fr = self._get_frame(page) or frame
            furl = fr.url or ""
            fbody = fr.evaluate("() => (document.body ? document.body.innerText : '').slice(0,1200)") or ""
        except Exception:
            pass
        if "process.php" in (furl or "").lower():
            return "submitted (process.php)"
        blob = (pbody + " " + fbody).lower()
        # Still inside the application — not complete.
        if re.search(r"(let'?s get started|basic information|what is your|how much|do you|please (enter|select))", blob):
            return ""
        if re.search(r"(congratulat|you were matched|these offers|the following offers|thank you for|your request (is )?complete|connecting with|searching for (the )?best|approved|you'?re connected|matched you|top ?five)", blob):
            return "results / offers page"
        return ""

    # ------------------------------------------------------------ post-offer flow

    # Tick the offer's selectable option checkboxes (loan-purpose / "review these
    # options" / consent) — never negative opt-outs.  Returns how many it ticked.
    _JS_TICK_OPTIONS = r"""() => {
        const vis = e => e.offsetParent !== null && e.getClientRects().length > 0;
        const labelOf = cb => {
            const lf = cb.id ? document.querySelector('label[for="'+cb.id+'"]') : null;
            const w = cb.closest('label,[class*=option],[class*=card],[class*=choice],li,div');
            return ((lf ? lf.innerText : '') + ' ' + (w ? w.innerText : '')).toLowerCase();
        };
        let n = 0;
        document.querySelectorAll('input[type=checkbox]').forEach(cb => {
            if (!vis(cb) || cb.checked) return;
            const t = labelOf(cb);
            if (/unsubscribe|do not|opt.?out|no thanks|decline|not interested/.test(t)) return;
            const lf = cb.id ? document.querySelector('label[for="'+cb.id+'"]') : null;
            const w = cb.closest('label');
            if (lf) lf.click(); else if (w) w.click(); else cb.click();
            if (!cb.checked) { cb.checked = true; cb.dispatchEvent(new Event('change',{bubbles:true})); }
            if (cb.checked) n++;
        });
        return n;
    }"""

    # Fill any lead fields the offer re-asks (name / email / phone / zip / …),
    # matching by name / id / placeholder / label.  Skips prefilled fields.
    _JS_FILL_LEAD = r"""(L) => {
        const vis = e => e.offsetParent !== null && e.getClientRects().length > 0;
        const setV = (el, v) => { if (!v) return;
            const p = Object.getPrototypeOf(el);
            const d = Object.getOwnPropertyDescriptor(p,'value') || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value');
            d.set.call(el, v); ['input','keyup','change','blur'].forEach(ev => el.dispatchEvent(new Event(ev,{bubbles:true}))); };
        const key = el => ((el.name||'')+' '+(el.id||'')+' '+(el.placeholder||'')+' '+(el.getAttribute('aria-label')||'')+' '
            + ((el.id?(document.querySelector('label[for="'+el.id+'"]')||{}):{}).innerText||'')).toLowerCase();
        let n = 0;
        document.querySelectorAll('input,select,textarea').forEach(el => {
            if (!vis(el) || el.disabled || el.readOnly || el.type==='hidden' || el.type==='checkbox' || el.type==='radio') return;
            if (el.value && el.value.trim()) return;
            const k = key(el);
            let v = '';
            if (/first ?name|fname|given/.test(k)) v = L.first;
            else if (/last ?name|lname|surname|family/.test(k)) v = L.last;
            else if (/full ?name|your name|^name/.test(k)) v = (L.first+' '+L.last).trim();
            else if (/e-?mail/.test(k)) v = L.email;
            else if (/phone|mobile|cell|tel/.test(k)) v = L.phone;
            else if (/\bzip|postal/.test(k)) v = L.zip;
            else if (/address|street/.test(k)) v = L.address;
            else if (/city|town/.test(k)) v = L.city;
            if (v) { setV(el, v); n++; }
        });
        return n;
    }"""

    def _offer_sig(self, surface) -> str:
        try:
            return surface.evaluate("() => {" + self._JS_VIS + r"""
                const t = e => (e.innerText||e.value||'').replace(/\s+/g,' ').trim();
                const heading = (document.querySelector('h1,h2,h3,legend') || {}).innerText || '';
                const fields = Array.from(document.querySelectorAll('input,select,textarea'))
                    .filter(e => vis(e) && e.type !== 'hidden')
                    .map(e => (e.name||e.id||e.type) + (e.type==='checkbox' ? (e.checked?':1':':0') : ''));
                const btns = Array.from(document.querySelectorAll('button,[role=button],input[type=submit],a.btn'))
                    .filter(vis).map(t).filter(x => x && x.length < 40);
                return (heading.slice(0,40) + '|' + fields.sort().join(',') + '|' + btns.join(',')).slice(0, 300);
            }""") or ""
        except Exception:
            return ""

    def _surfaces(self, cur) -> list:
        """Every live frame of the current page/tab — the offer ('Friendly Loans')
        renders inside the rndframe iframe, so actions must reach all frames."""
        out = []
        try:
            for fr in cur.frames:
                if not fr.is_detached():
                    out.append(fr)
        except Exception:
            pass
        return out or [cur]

    def _handle_post_offer(self, page: Page, f: dict, row_number: int, stop_event) -> None:
        """After submit an offer loads (e.g. 'Friendly Loans', inside the iframe)
        — usually a short secondary form: pick loan-option checkbox(es), maybe a
        field, then a CTA.  Walk it across all frames — wait out processing, tick
        options, fill any lead fields, click Continue/CTA — following the offer
        into its own window, and stop at a terminal / when it can't progress."""
        log.info("form.post_offer_start", row=row_number)
        self._screenshot(page, row_number, "post_offer_arrived")
        ctx = page.context
        cur = page
        for state in ("domcontentloaded", "load"):
            try: cur.wait_for_load_state(state, timeout=15000)
            except Exception: pass

        ph = re.sub(r"\D", "", f.get("phone", ""))
        lead = {"first": f.get("first_name", ""), "last": f.get("last_name", ""),
                "email": f.get("email", ""), "phone": ph, "zip": f.get("zip", ""),
                "address": f.get("street", ""), "city": f.get("city", "")}
        bank_vals = {"routing_number": f.get("routing", ""), "account_number": f.get("account", ""),
                     "bank_name": f.get("bank_name", ""), "account_type": f.get("account_type", "checking")}

        def _sig() -> str:
            parts = []
            for fr in self._surfaces(cur):
                s = self._offer_sig(fr)
                if s:
                    parts.append(s)
            return "||".join(parts)

        deadline = time.time() + 180
        clicks = 0
        max_clicks = 8
        proc_logged = 0.0
        last_progress = time.time()
        last_click = None
        while time.time() < deadline:
            self._check_stop(stop_event)
            try: self._live(cur)
            except Exception: pass
            frames = self._surfaces(cur)

            processing = False
            for fr in frames:
                try:
                    if (fr.evaluate(self._JS_POST_STATE) or {}).get("processing"):
                        processing = True
                except Exception:
                    pass

            # Fill / select whatever this offer step exposes, in every frame.
            acted = 0
            for fr in frames:
                for js, arg in ((self._JS_FILL_BANK, bank_vals), (self._JS_FILL_LEAD, lead)):
                    try: acted += fr.evaluate(js, arg) or 0
                    except Exception: pass
                try: acted += fr.evaluate(self._JS_TICK_OPTIONS) or 0
                except Exception: pass
            if acted:
                log.info("form.post_offer_fill", acted=acted, row=row_number)
                self._screenshot(cur, row_number, "post_offer_fill")
                last_progress = time.time()
                time.sleep(0.6)
                frames = self._surfaces(cur)

            # Find a Continue / CTA in any frame.
            cta_fr, cta = None, ""
            if clicks < max_clicks:
                for fr in frames:
                    try: t = fr.evaluate(self._JS_CLICK_CONTINUE, True) or ""
                    except Exception: t = ""
                    if t:
                        cta_fr, cta = fr, t
                        break
            if cta_fr is not None:
                here = (_sig(), cta)
                if here == last_click:
                    log.info("form.post_offer_no_progress", button=cta, row=row_number)
                    break
                last_click = here
                clicks += 1
                last_progress = time.time()
                log.info("form.post_offer_continue", button=cta, row=row_number)
                new_tab = None
                try:
                    with ctx.expect_page(timeout=8000) as pinfo:
                        cta_fr.evaluate(self._JS_CLICK_CONTINUE, False)
                    new_tab = pinfo.value
                except Exception:
                    new_tab = None
                if new_tab is not None:
                    cur = new_tab
                    log.info("form.post_offer_tab", url=(cur.url or "")[:80], row=row_number)
                    settle = float(self._delays.get("offer_load_wait", 12))
                    for state in ("domcontentloaded", "load", "networkidle"):
                        try: cur.wait_for_load_state(state, timeout=20000)
                        except Exception: pass
                    try: cur.bring_to_front()
                    except Exception: pass
                    time.sleep(settle)
                    for state in ("domcontentloaded", "networkidle"):
                        try: cur.wait_for_load_state(state, timeout=15000)
                        except Exception: pass
                    log.info("form.post_offer_loaded", url=(cur.url or "")[:80], row=row_number)
                    break
                for state in ("domcontentloaded", "load"):
                    try: cur.wait_for_load_state(state, timeout=12000)
                    except Exception: pass
                time.sleep(1.5)
                continue

            if processing:
                if time.time() - proc_logged > 15:
                    log.info("form.post_offer_processing", row=row_number)
                    proc_logged = time.time()
                time.sleep(3)
                continue

            if time.time() - last_progress > 40:
                break
            time.sleep(2)

        self._screenshot(cur, row_number, "post_offer_final")
        log.info("form.post_offer_done", clicks=clicks, row=row_number)

    # ------------------------------------------------------------ value mapping

    def _field_values(self, f: dict) -> dict:
        """The full RSform name -> value map for this lead (helpers included)."""
        ph = re.sub(r"\D", "", f["phone"])
        wph = re.sub(r"\D", "", f["employer_phone"] or f["phone"])
        ssn = f["ssn"]
        m, d, y = f["dob_m"], f["dob_d"], f["dob_y"]
        v = {
            "requestedLoanAmount":   f["loan_bucket"],
            "h_requestedLoanAmount": f["loan_bucket"],
            "firstName": f["first_name"], "lastName": f["last_name"],
            "h_firstName": f["first_name"], "h_lastName": f["last_name"],
            "email": f["email"],
            "home_phone1": ph[0:3], "home_phone2": ph[3:6], "home_phone3": ph[6:10],
            "birthdate_month": str(int(m)) if m else "", "birthdate_day": str(int(d)) if d else "",
            "birthdate_year": y, "h_birthdate_year": y,
            "ssn1": ssn[0:3], "ssn2": ssn[3:5], "ssn3": ssn[5:9], "h_ssn3": ssn[5:9],
            "address": f["street"], "zip": f["zip"], "h_zip": f["zip"],
            "monthsAtResidence": f["months_at_address"],
            "housing": f["housing"],
            "activeMilitary": f["military"],
            "hasCarTitle": "false",
            "incomeType": f["income_type"],
            "monthlyIncome": f["income_bucket"],
            "payPeriod": f["pay_period"],
            "monthsEmployed": f["months_employed"],
            "payMonth": f["pay_month"], "payDay1": f["pay_day"],
            "employer": f["employer_name"], "occupation": f["job_title"],
            "work_phone1": wph[0:3], "work_phone2": wph[3:6], "work_phone3": wph[6:10],
            "drivingLicenseNumber": f["dl_number"], "drivingLicenseState": f["dl_state"],
            "routingNumber": f["routing"], "accountNumber": f["account"], "bankName": f["bank_name"],
            "bankAccountType": f["account_type"],
            "directDeposit": f["direct_deposit"],
            "monthsWithBank": f["months_at_bank"],
            "creditScore": f["credit_score"],
            "loanPurpose": f["loan_purpose"],
            "highDebt": f["high_debt"],
            "hasCarTitle": "false",
            # ── "Congratulations, {name}!" confirmation / retry-submit step ──────
            # After the main form, rndframe shows a final step that re-collects
            # data through h_special_* / h_* mirror fields and a retrySubmit flag
            # (submit to the extended lender network).  Fill them all so it
            # advances to the offers page.
            "h_special_ssn3": ssn[5:9],
            "h_specialRequestedLoanAmount": f["loan_bucket"],
            "h_loanPurpose": f["loan_purpose"],
            "h_creditScore": f["credit_score"],
            "h_highDebt": f["high_debt"],
            "h_hasCarTitle": "false",
            "retrySubmit": "true", "h_retrySubmit": "true",
        }
        return {k: val for k, val in v.items() if val not in (None, "")}

    def _parse_fields(self, row: dict) -> dict:
        _norm = {}
        for _k, _v in row.items():
            _nk = re.sub(r"\s+", " ", str(_k)).strip().lower()
            if _nk not in _norm or str(_v or "").strip():
                _norm[_nk] = _v

        def g(*keys: str) -> str:
            for k in keys:
                nk = re.sub(r"\s+", " ", str(k)).strip().lower()
                val = str(_norm.get(nk) or "").strip()
                if val:
                    return val
            return ""

        phone = _digits(g("Phone Number", "Phone"))
        phone = phone[1:] if len(phone) == 11 and phone.startswith("1") else phone
        ssn = _digits(g("SSN Full", "SSN"))
        dob = self._norm_dob(g("Date of Birth (DOB)", "DOB", "dob"))
        mm, dd, yy = (dob.split("/") + ["", "", ""])[:3] if dob else ("", "", "")
        routing = _digits(g("ABA Routing Number", "routingNumber", "Routing Number"))
        if 0 < len(routing) < 9:
            routing = routing.zfill(9)
        loan = re.sub(r"[,$\s]", "", g("Requested Loan Amount ($)", "Loan_Amount")) or "5000"

        return {
            "first_name": g("First Name", "First_Name"), "last_name": g("Last Name", "Last_Name"),
            "email": g("Email Address", "Email"), "phone": phone,
            "dob_m": mm, "dob_d": dd, "dob_y": yy, "ssn": ssn,
            "street": g("Street Address", "Address"), "city": g("City"),
            "state": self._state_code(g("State")),
            "zip": _digits(g("ZIP Code", "Zip"))[:5],
            "loan_bucket": self._loan_bucket(loan),
            "months_at_address": self._months_bucket(g("Years at Address", "Months at Address")),
            "housing": "own" if self._truthy(g("Homeowner")) else "rent",
            "military": "true" if self._truthy(g("Military", "Active Military")) else "false",
            "income_type": self._income_type(g("Income Source", "Primary Income Source")),
            "income_bucket": self._income_bucket(g("Monthly Net Income ($)", "Monthly_Income")),
            "pay_period": self._pay_period(g("Pay Frequency", "Pay_Frequency")),
            "months_employed": self._months_bucket(g("Years at Employer", "Months at Employer")),
            "employer_name": g("Employer Name", "Employer_Name") or "Employer",
            "employer_phone": _digits(g("Employer Work Phone", "Employer Phone", "Work Phone")),
            "job_title": g("Job Title") or "Employee",
            "dl_number": g("Driver License / ID Number", "Driver License / Id Number", "driversLicenseNumber"),
            "dl_state": self._state_code(g("Driver License State") or g("State")),
            "routing": routing, "account": _digits(g("Account Number", "accountNumber")),
            "bank_name": g("Bank Name", "bankName") or "Chase",
            "account_type": "savings" if g("Account Type").lower().startswith("sav") else "checking",
            "direct_deposit": "false" if g("Direct Deposit") and not self._truthy(g("Direct Deposit")) else "true",
            "months_at_bank": self._months_bucket(g("Years at Bank", "Months at Bank")),
            "credit_score": self._credit(g("Credit Score Rating", "Credit Score", "Credit Rating")),
            "loan_purpose": self._loan_purpose(g("Loan Purpose", "Loan_Purpose")),
            "high_debt": "1" if self._digits_int(g("Credit Card Debt", "Unsecured Debt")) >= 10000 else "0",
            "pay_month": self._pay_month(g("Next Payday")),
            "pay_day": self._pay_day(g("Next Payday")),
        }

    def _validate_required_fields(self, f: dict) -> None:
        req = ["first_name", "last_name", "email", "phone", "dob_y", "zip", "street", "state"]
        missing = [k for k in req if not f.get(k)]
        if len(f["ssn"]) != 9:
            missing.append("ssn(need 9 digits)")
        if len(re.sub(r"\D", "", f["phone"])) != 10:
            missing.append("phone(need 10 digits)")
        if not (f["dob_m"] and f["dob_d"] and f["dob_y"]):
            missing.append("dob")
        if f["routing"] and not _aba_checksum_ok(f["routing"]):
            missing.append("routing_number(failed ABA checksum)")
        if missing:
            raise FormFillerError(f"Missing or invalid fields: {missing}", error_type="missing_data")

    # ---------------------------------------------------------------- mappers

    def _digits_int(self, raw: str) -> int:
        d = re.sub(r"\D", "", raw or "")
        return int(d) if d else 0

    def _loan_bucket(self, raw: str) -> str:
        try:
            n = int(float(re.sub(r"[,$\s]", "", raw)))
        except (ValueError, TypeError):
            n = 5000
        for hi, val in ((500, "300"), (1000, "600"), (2500, "1000"),
                        (5000, "3000"), (20000, "10000")):
            if n <= hi:
                return val
        return "30000"

    def _income_bucket(self, raw: str) -> str:
        opts = [1500, 2000, 2500, 3000, 3500, 4000, 5000]
        n = self._digits_int(raw)
        if n <= 0:
            return "3000"
        if n > 5000:
            return "5001"
        return str(min(opts, key=lambda o: abs(o - n)))

    def _months_bucket(self, raw: str) -> str:
        raw = (raw or "").strip().lower()
        nums = re.findall(r"\d+", raw)
        if not nums:
            return "24"
        n = int(nums[0])
        months = n if "month" in raw else n * 12
        return str(min([12, 24, 36, 48], key=lambda o: abs(o - months)))

    def _income_type(self, raw: str) -> str:
        r = (raw or "").lower()
        if "self" in r:
            return "self_employed"
        if "benefit" in r or "disab" in r or "social" in r or "unemploy" in r or "retire" in r or "ssi" in r:
            return "benefits"
        return "employment"

    def _pay_period(self, raw: str) -> str:
        r = (raw or "").lower()
        if "week" in r and "bi" in r:
            return "biweekly"
        if "biweek" in r or "every two" in r or "every other" in r:
            return "biweekly"
        if "twice" in r or "semi" in r or "1st and 15" in r or "15th" in r:
            return "twice_monthly"
        if "month" in r:
            return "monthly"
        if "week" in r:
            return "weekly"
        return "biweekly"

    def _credit(self, raw: str) -> str:
        raw = (raw or "").strip().lower()
        nums = re.findall(r"\d{3}", raw)
        if nums:
            n = int(nums[0])
            if n >= 720: return "750"
            if n >= 660: return "690"
            if n >= 600: return "630"
            return "590"
        if "excellent" in raw: return "750"
        if "good" in raw: return "690"
        if "fair" in raw: return "630"
        if "poor" in raw or "bad" in raw: return "590"
        return "630"

    def _loan_purpose(self, raw: str) -> str:
        r = (raw or "").lower()
        table = [
            ("debt consolid", "debt"), ("consolid", "debt"), ("debt settle", "debtSettlement"),
            ("tax", "taxSettlement"), ("auto", "auto"), ("car", "auto"), ("vehicle", "auto"),
            ("credit card", "creditCard"), ("education", "education"), ("student", "education"),
            ("home", "home"), ("house", "home"), ("mortgage", "home"), ("rent", "home"),
            ("medical", "medical"), ("emergen", "other"), ("debt", "debt"),
        ]
        for key, val in table:
            if key in r:
                return val
        return "other"

    def _pay_month(self, raw: str) -> str:
        d = self._next_payday(raw)
        return str(d.month)

    def _pay_day(self, raw: str) -> str:
        d = self._next_payday(raw)
        return str(d.day)

    def _next_payday(self, raw: str) -> datetime:
        raw = (raw or "").strip()
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
            try:
                dt = datetime.strptime(raw, fmt)
                if dt.date() <= datetime.now().date():
                    dt = datetime.now() + timedelta(days=14)
                return dt
            except ValueError:
                pass
        return datetime.now() + timedelta(days=14)

    def _norm_dob(self, raw: str) -> str:
        raw = (raw or "").strip()
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%d/%m/%Y", "%m/%d/%y"):
            try:
                return datetime.strptime(raw, fmt).strftime("%m/%d/%Y")
            except ValueError:
                pass
        return raw

    def _state_code(self, raw: str) -> str:
        raw = (raw or "").strip()
        if len(raw) == 2 and raw.isalpha():
            return raw.upper()
        return _STATE_CODES.get(raw.lower(), raw.upper()[:2])

    def _truthy(self, raw: str) -> bool:
        return (raw or "").strip().lower() in {"yes", "y", "true", "1", "own", "owner"}

    # ---------------------------------------------------------------- utilities

    _US_TZ = ("America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles")

    def _clean_fingerprint(self, fp: dict) -> dict:
        allowed = {"user_agent", "viewport", "locale", "timezone_id", "geolocation",
                   "color_scheme", "device_scale_factor", "is_mobile", "has_touch",
                   "java_script_enabled", "extra_http_headers"}
        clean = {k: v for k, v in fp.items() if k in allowed and v is not None}
        # This is a US loan lead — present a US-English locale (and a US timezone
        # if a non-US one was rolled) so the fingerprint is consistent with the
        # applicant.  A random pt-BR / es-ES / Asia-TZ profile looks fraudulent
        # and helps trigger the bot-check reCAPTCHA.
        clean["locale"] = "en-US"
        if str(clean.get("timezone_id", "")) not in self._US_TZ:
            import random
            clean["timezone_id"] = random.choice(self._US_TZ)
        return clean

    def _screenshot(self, page: Page, row: int, label: str) -> None:
        if not self._save_shots:
            return
        try:
            page.screenshot(path=str(self._ss_dir / f"row_{row:04d}_{label}.png"), full_page=False)
        except Exception as e:
            log.warning("screenshot.failed", error=str(e)[:60])

    def _live(self, page: Page) -> None:
        try:
            page.screenshot(path=str(self._ss_dir / "live_view.png"))
        except Exception:
            pass

    def _check_stop(self, stop_event) -> None:
        if stop_event is not None and stop_event.is_set():
            raise FormFillerError("Stopped by user", error_type="stopped")

    def _classify_error(self, exc: Exception) -> str:
        msg = str(exc).lower()
        if "crash" in msg: return "browser_crashed"
        if "proxy" in msg or "net::err" in msg or "tunnel" in msg: return "proxy_error"
        if "closed" in msg or "target page" in msg: return "browser_closed"
        if "timeout" in msg: return "timeout"
        return "unknown"

    # ------------------------------------------------------------------ timing

    def _key_delay(self) -> float:
        import random
        return random.uniform(float(self._delays.get("min_typing_delay", 0.02)),
                              float(self._delays.get("max_typing_delay", 0.06))) * 1000

    def _action_pause(self) -> None:
        import random
        time.sleep(random.uniform(float(self._delays.get("min_action_delay", 0.25)),
                                  float(self._delays.get("max_action_delay", 0.7))))

    def _read_pause(self) -> None:
        import random
        rng = self._delays.get("read_pause", [0.2, 0.5])
        time.sleep(random.uniform(float(rng[0]), float(rng[1])))
