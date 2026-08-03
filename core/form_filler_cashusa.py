"""
core/form_filler_cashusa.py — cashusa.com (Round Sky SmartForm) automation.

CashUSA embeds a Vue "SmartForm" (class ``sf-form``) on a WordPress page — a
distinct platform from the other offers (not iframe.global, not ExtApplyV2).
Fields are keyed by ``id`` (not ``name``) and the form is one long multi-step
wizard: one question per screen, advanced by a "Continue" button.

The landing page opens with a mini form (amount / zip / last-4 SSN / DOB) whose
answers feed a backend identity lookup.  We skip that entirely: navigate to
``/get-started`` and click **Skip lookup**, which routes to the full manual
form.  Since the sheet already holds the full lead, the lookup adds nothing and
only risks hanging.

Step map (confirmed live via the skip-lookup path):
  fName / lName            text
  bMonth / bDay / bYear    DOB (MM / DD / YYYY, tel)
  loanReason               select: debtConsolidation | debtRelief |
                           creditCardRefinance | emergencySituation | autoRepair
                           | autoPurchase | moving | medical | business |
                           vacation | taxes | rentOrMortgage | specialOccasion |
                           majorPurchase | education | other
  amount                   tel (loan amount)
  address / address2 / zip / city / state(2-letter select)
  email / smsCellphone     text / tel (phone mask)
  lengthAtAddress          select: 1..10+ (years)
  "Do you own your home?"  Yes / No
  incomeSource             select: EMPLOYMENT | SELFEMPLOYMENT | BENEFITS | UNEMPLOYED
  timeEmployed             select: years
  paidEvery                select: weekly | biweekly | twicemonthly | monthly
  "Are you in the Military…"  Yes / No
  monthlyNetIncome         tel
  nextPayday               Duet date-picker (month/year selects + day grid)
  … bank details + final submit (see the note below)

Dispatch is field-driven: each screen is read for its visible controls and the
question text, filled/answered, then advanced.  Yes/No screens are answered from
the lead's data (homeowner / military / direct-deposit) by matching the question
text; unknown Yes/No default to "No".

⚠️  VERIFIED through the monthly-income / next-payday step via the skip-lookup
walk.  The bank-detail and final-submit screens after the payday step were not
reached during mapping (the walk stopped at the date picker), so their field ids
are handled best-effort from the SmartForm's conventional bank vocabulary
(bankName / routingNumber / accountNumber / accountType) and logged.  This
filler needs ONE real-run validation of the bank + submit tail.  See README →
"Validating the CashUSA filler".
"""
from __future__ import annotations

import os
import re
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import structlog
from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

from core.lead_platform import _aba_checksum_ok, _digits
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
    """Fills the cashusa.com SmartForm via the skip-lookup manual path."""

    default_url = "https://www.cashusa.com/get-started"

    def __init__(self, config: dict) -> None:
        self._config = config
        self._target = config.get("target", {})
        self._delays = config.get("delays", {})
        self._ss_dir = Path(config.get("screenshots", {}).get("directory", "screenshots"))
        self._ss_dir.mkdir(parents=True, exist_ok=True)
        self._max_steps = int(config.get("form", {}).get("max_steps", 45))

    # ------------------------------------------------------------------ public

    def process_row(self, row: dict[str, Any], fingerprint: dict[str, Any],
                    proxy_url: str | None, row_number: int, stop_event=None) -> dict[str, Any]:
        headless = os.getenv("HEADLESS", "true").lower() == "true"
        f = self._parse_fields(row)
        self._validate_required_fields(f)
        url = (self._target.get("url") or self.default_url).strip()
        # The mini landing form and the /get-started page share a host; always
        # drive the skip-lookup page so we fill the full form directly.
        if "get-started" not in url:
            url = url.rstrip("/") + "/get-started" if "cashusa.com" in url else "https://www.cashusa.com/get-started"
        page: Page | None = None

        with sync_playwright() as pw:
            launch_args: dict[str, Any] = {"headless": headless, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
            # Marker flag so the UI's Stop can force-kill this exact browser.
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
            try:
                ctx: BrowserContext = browser.new_context(**self._clean_fingerprint(fingerprint))
                page = ctx.new_page()
                inject_stealth(page, fingerprint)
                self._goto(page, url, row_number, stop_event)
                self._skip_lookup(page, row_number)
                outcome = self._fill_form(page, f, row_number, stop_event)
                self._screenshot(page, row_number, "success")
                sid = str(uuid.uuid4())[:8].upper()
                log.info("form.success", row=row_number, submission_id=sid, outcome=outcome)
                ctx.close()
                return {"status": "Success", "notes": f"Submitted — {outcome}", "submission_id": sid}
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
                time.sleep(4)   # SmartForm JS boot
                return
            log.warning("form.nav_retry", attempt=attempt, row=row_number, error=str(last)[:80] if last else "chrome-error")
            if attempt < 3: time.sleep(3)
        raise FormFillerError(f"Navigation failed: {last}" if last else "Page failed to load", error_type="proxy_error")

    def _skip_lookup(self, page: Page, row_number: int) -> None:
        """Click 'Skip lookup' to bypass the identity-verification lookup and go
        straight to the full manual form."""
        deadline = time.time() + 25
        while time.time() < deadline:
            clicked = page.evaluate(
                r"""() => {
                    const vis = e => e && e.offsetParent !== null && e.getClientRects().length > 0;
                    const el = Array.from(document.querySelectorAll('button,a,[role=button],span'))
                        .filter(vis).find(e => /skip lookup/i.test((e.innerText || '').trim()));
                    if (el) { el.click(); return true; }
                    return false;
                }"""
            )
            if clicked:
                log.info("form.skip_lookup", row=row_number)
                time.sleep(3)
                return
            if self._visible_fields(page):
                return   # already on the form
            time.sleep(1)
        log.info("form.skip_lookup_absent", row=row_number)

    # --------------------------------------------------------------- form flow

    def _fill_form(self, page: Page, f: dict, row_number: int, stop_event) -> str:
        seen: dict[str, int] = {}
        for step in range(self._max_steps):
            self._check_stop(stop_event)
            done = self._completion_state(page)
            if done:
                log.info("form.completed", step=step, outcome=done, row=row_number)
                return done

            st = self._state(page)
            if not st["fields"] and not st["yesno"] and not st["choices"]:
                if st["loading"]:
                    time.sleep(2); continue
                time.sleep(1.5)
                if self._completion_state(page):
                    return self._completion_state(page) or "submitted"
                continue

            sig = st["sig"]
            seen[sig] = seen.get(sig, 0) + 1
            if seen[sig] > 3:
                self._screenshot(page, row_number, f"stuck_{step}")
                raise FormFillerError(
                    f"Form stopped advancing at step {step}: {st['question'][:60]!r} "
                    f"(fields={[x['id'] for x in st['fields']]})", error_type="stuck")

            log.info("form.step", step=step, q=st["question"][:60],
                     fields=[x["id"] for x in st["fields"]], yesno=st["yesno"], row=row_number)
            self._live(page)
            self._read_pause()

            handled = self._handle_step(page, st, f, row_number)
            if not handled:
                self._screenshot(page, row_number, f"unhandled_{step}")
                raise FormFillerError(
                    f"Could not handle step {step}: {st['question'][:60]!r} "
                    f"fields={[x['id'] for x in st['fields']]} choices={st['choices'][:6]}",
                    error_type="unhandled_step")

            # Click Continue promptly after filling.  On this SmartForm a masked
            # phone field runs an async re-validation ~1s after blur that briefly
            # blocks Continue, so pausing *before* the click (human pacing) can
            # trap the step — advance first, then take the human beat.
            self._continue(page)
            if not self._await_change(page, sig, timeout=8):
                self._continue(page)
                self._await_change(page, sig, timeout=14)
            self._action_pause()
            self._live(page)

        raise FormFillerError(f"Form did not complete within {self._max_steps} steps", error_type="timeout")

    def _handle_step(self, page: Page, st: dict, f: dict, row_number: int) -> bool:
        # Date-picker step (next payday) — handled specially.
        if any(x["id"] == "nextPayday" for x in st["fields"]):
            return self._set_payday(page, f["next_payday"])
        # Yes/No qualifier step.
        if st["yesno"] and not st["fields"]:
            ans = self._answer_yesno(st["question"], f)
            ok = self._click_yesno(page, ans)
            log.info("form.yesno", q=st["question"][:50], answer=ans, ok=ok, row=row_number)
            return ok
        # Choice step (no input fields, not strictly Yes/No) — e.g. the
        # Checking/Savings bank-account-type selector.
        if st["choices"] and not st["fields"]:
            ans = self._answer_choice(st["question"], st["choices"], f)
            if ans:
                ok = self._click_choice(page, re.escape(ans))
                log.info("form.choice", q=st["question"][:50], answer=ans, ok=ok, row=row_number)
                return ok
        # Field step: fill every recognised control.
        any_ok = False
        for fld in st["fields"]:
            fid = fld["id"]
            val = self._value_for(fid, f)
            if val is None:
                log.warning("form.unmapped_field", id=fid, row=row_number)
                continue
            if fld["type"] == "select-one" or fld.get("opts"):
                any_ok = self._set_select(page, fid, val) or any_ok
            else:
                any_ok = self._set_text(page, fid, val) or any_ok
        # Let blur/validation settle before the loop clicks Continue — masked
        # phone fields in particular reject a Continue that lands mid-keystroke.
        try:
            page.evaluate("() => { if (document.activeElement) document.activeElement.blur(); }")
        except Exception:
            pass
        time.sleep(0.5)
        return any_ok

    # ------------------------------------------------------------- lead -> field

    def _value_for(self, fid: str, f: dict) -> str | None:
        return {
            "fName": f["first_name"], "lName": f["last_name"],
            "bMonth": f["dob_m"], "bDay": f["dob_d"], "bYear": f["dob_y"],
            "loanReason": f["loan_reason"], "amount": f["loan_amount"],
            "address": f["street"], "address2": "", "zip": f["zip"], "city": f["city"],
            "state": f["state"], "email": f["email"], "smsCellphone": f["phone"],
            "lengthAtAddress": f["years_at_address"],
            "incomeSource": f["income_source"], "timeEmployed": f["years_employed"],
            "paidEvery": f["pay_freq"], "monthlyNetIncome": f["income"],
            "employerName": f["employer_name"], "employerPhone": f["employer_phone"],
            "jobTitle": f["job_title"], "workPhone": f["employer_phone"],
            "monthsBank": f["months_bank"], "creditType": f["credit_type"],
            "unsecuredDebt": f["unsecured_debt"],
            "ssn": f["ssn"], "ssnLast4": f["ssn_last4"],
            "license": f["dl_number"], "licenseNumber": f["dl_number"],
            "licenseState": f["dl_state"], "driverLicense": f["dl_number"],
            # bank vocabulary — abaNumber/accountNumber are the live ids (step 20);
            # the rest are conventional aliases kept for resilience.
            "abaNumber": f["routing"], "bankName": f["bank_name"],
            "routingNumber": f["routing"], "aba": f["routing"], "routingNo": f["routing"],
            "accountNumber": f["account"], "accountNo": f["account"],
            "accountType": f["account_type"], "bankAccountType": f["account_type"],
        }.get(fid)

    def _answer_choice(self, question: str, choices: list, f: dict) -> str | None:
        """Pick the option matching the lead on a non-Yes/No choice screen."""
        q = question.lower()
        picks = [c for c in choices if c.strip().lower() not in ("back", "continue", "next", "")]

        def match(want: str) -> str | None:
            want = (want or "").lower()
            return next((c for c in picks if want and want in c.lower()), None)

        joined = " ".join(picks).lower()
        if "account" in q and ("type" in q or "bank" in q or "checking" in joined):
            return match(f["account_type"]) or match("checking")
        if "direct deposit" in q or "paper check" in q or "paid with" in q:
            if f["direct_deposit"] == "Yes":
                return match("direct deposit") or match("direct")
            return match("paper") or match("check")
        return None

    def _answer_yesno(self, question: str, f: dict) -> str:
        q = question.lower()
        if "own your home" in q or "homeowner" in q or "own or rent" in q:
            return f["homeowner"]
        if "military" in q:
            return f["military"]
        if "direct deposit" in q:
            return f["direct_deposit"]
        if "bank account" in q or ("account" in q and "have" in q):
            return "Yes"
        if "unsecured debt" in q or "10,000" in q:
            return "No"
        if "own a car" in q or "title loan" in q or "vehicle" in q or "car that is paid" in q:
            return "No"
        return "No"

    # ------------------------------------------------------------ interactions

    _JS_VIS = "const vis=e=>e.offsetParent!==null&&e.getClientRects().length>0;"

    def _visible_fields(self, page: Page) -> list:
        try:
            return page.evaluate("() => {" + self._JS_VIS + """
                return Array.from(document.querySelectorAll('input,select,textarea'))
                    .filter(e => vis(e) && e.type !== 'hidden').map(e => e.id).filter(x => x);
            }""") or []
        except Exception:
            return []

    def _state(self, page: Page) -> dict:
        try:
            st = page.evaluate("() => {" + self._JS_VIS + r"""
                const t = e => (e.innerText||e.textContent||'').replace(/\s+/g,' ').trim();
                const fields = Array.from(document.querySelectorAll('input,select,textarea'))
                    .filter(e => vis(e) && e.type !== 'hidden')
                    .map(e => ({id:e.id, type:e.type, ml:e.maxLength>0?e.maxLength:null,
                                opts:e.tagName==='SELECT'?Array.from(e.options).map(o=>o.value).filter(x=>x):null}));
                const cands = Array.from(document.querySelectorAll('.sf-page *')).filter(vis)
                    .map(t).filter(x => x && x.length>4 && x.length<160);
                const question = (cands.find(x=>x.includes('?')) || cands[0] || '').replace(/^.*loan request\.\s*/i,'');
                const choices = [...new Set(Array.from(document.querySelectorAll('button,[role=button],a.btn,[class*=option],[class*=choice],label'))
                    .filter(vis).map(t).filter(x => x && x.length<40))];
                const low = choices.map(c => c.trim().toLowerCase());
                const hasYes = low.some(x => x==='yes' || x.startsWith('yes ') || x.startsWith('yes,'));
                const hasNo  = low.some(x => x==='no'  || x.startsWith('no ')  || x.startsWith('no,'));
                const yesno = hasYes && hasNo;
                const loading = /loading|verifying|please wait|processing|one moment/i.test(document.body.innerText||'');
                return {fields, question, choices, yesno, loading,
                        sig: fields.map(f=>f.id).sort().join(',') + '|' + question.slice(0,50)};
            }""")
            return st or {"fields": [], "question": "", "choices": [], "yesno": False, "loading": False, "sig": ""}
        except Exception:
            return {"fields": [], "question": "", "choices": [], "yesno": False, "loading": False, "sig": ""}

    def _set_text(self, page: Page, fid: str, value: str) -> bool:
        value = str(value or "")
        if not value:
            return True   # blank optional field (e.g. address2) counts as handled
        sel = f'#{self._css(fid)}'
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=6000)
            loc.click()
            loc.fill("")
            loc.press_sequentially(value, delay=self._key_delay())
            loc.blur()
        except Exception as e:
            log.warning("form.type_failed", id=fid, error=str(e)[:70])
        got = self._read(page, fid)
        if _digits(got) == _digits(value) or got.strip() == value.strip():
            return True
        # native setter fallback (masks / framework-controlled)
        try:
            page.evaluate("""([id, v]) => {
                const el = document.getElementById(id); if (!el) return;
                const proto = Object.getPrototypeOf(el);
                (Object.getOwnPropertyDescriptor(proto,'value')||Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value')).set.call(el, v);
                ['input','change','blur'].forEach(ev => el.dispatchEvent(new Event(ev,{bubbles:true})));
            }""", [fid, value])
        except Exception:
            pass
        got = self._read(page, fid)
        ok = _digits(got) == _digits(value) or got.strip() == value.strip()
        if not ok:
            log.warning("form.value_mismatch", id=fid, wanted=value[:16], got=got[:16])
        return ok

    def _set_select(self, page: Page, fid: str, value: str) -> bool:
        if not value:
            return False
        loc = page.locator(f'#{self._css(fid)}').first
        for kwargs in ({"value": value}, {"label": value}):
            try:
                loc.select_option(timeout=6000, **kwargs)
                return True
            except Exception:
                pass
        try:
            ok = page.evaluate("""([id, v]) => {
                const s = document.getElementById(id); if (!s) return false;
                const want = String(v).trim().toLowerCase();
                const hit = Array.from(s.options).find(o => o.value.trim().toLowerCase()===want)
                         || Array.from(s.options).find(o => o.text.trim().toLowerCase()===want)
                         || Array.from(s.options).find(o => o.text.trim().toLowerCase().includes(want) && want.length>1);
                if (!hit) return false;
                s.value = hit.value; ['input','change'].forEach(ev=>s.dispatchEvent(new Event(ev,{bubbles:true})));
                return true;
            }""", [fid, value])
            if ok:
                return True
            # Numeric selects (timeEmployed, monthsBank, lengthAtAddress) use
            # bucketed option values (e.g. months = 9|18|30, years = 1..5) that
            # rarely equal our exact figure — snap to the closest available.
            chosen = page.evaluate("""([id, v]) => {
                const s = document.getElementById(id); if (!s) return null;
                const want = parseFloat(String(v).replace(/[^0-9.]/g,''));
                if (isNaN(want)) return null;
                let best = null, bestDiff = Infinity;
                for (const o of s.options) {
                    const raw = o.value.trim(); if (raw === '') continue;
                    const n = parseFloat(raw.replace(/[^0-9.]/g,'')); if (isNaN(n)) continue;
                    const d = Math.abs(n - want);
                    if (d < bestDiff) { bestDiff = d; best = o.value; }
                }
                if (best === null) return null;
                s.value = best; ['input','change'].forEach(ev => s.dispatchEvent(new Event(ev,{bubbles:true})));
                return best;
            }""", [fid, value])
            if chosen is not None:
                log.info("form.select_nearest", id=fid, wanted=value, chose=chosen)
                return True
            avail = page.evaluate("(id)=>{const s=document.getElementById(id);return s?Array.from(s.options).map(o=>o.value).slice(0,25):[];}", fid)
            log.warning("form.select_no_option", id=fid, wanted=value, available=avail)
        except Exception as e:
            log.warning("form.select_failed", id=fid, error=str(e)[:70])
        return False

    def _click_yesno(self, page: Page, answer: str) -> bool:
        """Click a Yes/No option, tolerating descriptive labels such as
        'No, I don't' or 'Yes, I do'."""
        try:
            return bool(page.evaluate("([ans]) => {" + self._JS_VIS + r"""
                const w = ans.trim().toLowerCase();
                const el = Array.from(document.querySelectorAll('button,[role=button],a.btn,input[type=submit],[class*=option],label'))
                    .filter(vis).filter(e=>!e.disabled).find(e => {
                        const s = (e.innerText||e.value||'').replace(/\s+/g,' ').trim().toLowerCase();
                        return s===w || s.startsWith(w+' ') || s.startsWith(w+',');
                    });
                if (el) { el.click(); return true; }
                return false;
            }""", [answer]))
        except Exception:
            return False

    def _click_choice(self, page: Page, label: str) -> bool:
        try:
            return bool(page.evaluate("([lbl]) => {" + self._JS_VIS + r"""
                const rx = new RegExp('^'+lbl+'$','i');
                const el = Array.from(document.querySelectorAll('button,[role=button],a.btn,input[type=submit],[class*=option],label'))
                    .filter(vis).filter(e=>!e.disabled).find(e => rx.test((e.innerText||e.value||'').replace(/\s+/g,' ').trim()));
                if (el) { el.click(); return true; }
                return false;
            }""", [label]))
        except Exception:
            return False

    def _set_payday(self, page: Page, iso_date: str) -> bool:
        """Set the Duet date-picker (readonly input) to the next payday.

        Duet: a ``.duet-date__toggle`` button opens a calendar with month
        (0-indexed) + year <select>s and a ``button.duet-date__day`` grid; days
        from adjacent months carry ``is-outside``.  Open it, set month/year, then
        click the in-month day button matching the day number."""
        try:
            y, m, d = iso_date.split("-")
        except Exception:
            dt = datetime.now() + timedelta(days=14)
            y, m, d = str(dt.year), f"{dt.month:02d}", f"{dt.day:02d}"
        month0 = str(int(m) - 1)
        day = str(int(d))
        try:
            # 1. open the calendar
            page.evaluate("() => {" + self._JS_VIS + r"""
                const b = document.querySelector('.duet-date__toggle');
                if (b && vis(b)) b.click();
            }""")
            time.sleep(0.7)
            # 2. set month + year selects (fire native events for the web component)
            page.evaluate("""([m0, yr]) => {
                const setSel = (el, v) => { if(!el) return; el.value=v; ['input','change'].forEach(ev=>el.dispatchEvent(new Event(ev,{bubbles:true}))); };
                document.querySelectorAll('select').forEach(s => {
                    if (/DuetDateMonth/i.test(s.id)) setSel(s, m0);
                    if (/DuetDateYear/i.test(s.id)) setSel(s, yr);
                });
            }""", [month0, y])
            time.sleep(0.7)
            # 3. click the in-month day. Duet renders the number in an
            #    aria-hidden <span>; current-month days carry the ``is-month``
            #    class (adjacent-month days do not), which disambiguates a repeated
            #    day number at the grid edges.
            clicked = page.evaluate("([day]) => {" + self._JS_VIS + r"""
                const btn = Array.from(document.querySelectorAll('button.duet-date__day'))
                    .filter(vis).filter(e => !e.disabled && /is-month/.test(e.className))
                    .find(e => { const s = e.querySelector('[aria-hidden]');
                                 return s && s.textContent.trim() === day; });
                if (btn) { btn.click(); return true; }
                return false;
            }""", [day])
            time.sleep(0.5)
            got = self._read(page, "nextPayday")
            ok = clicked or bool(got.strip())
            log.info("form.payday_set", date=iso_date, readback=got[:16], clicked=clicked, ok=ok)
            return ok
        except Exception as e:
            log.warning("form.payday_failed", error=str(e)[:80])
            return False

    def _read(self, page: Page, fid: str) -> str:
        try:
            return page.evaluate("(id)=>{const e=document.getElementById(id);return e?(e.value||''):'';}", fid) or ""
        except Exception:
            return ""

    def _continue(self, page: Page) -> None:
        try:
            page.evaluate("() => {" + self._JS_VIS + r"""
                const pick = rx => Array.from(document.querySelectorAll('button,input[type=submit],[role=button],a.btn'))
                    .filter(vis).filter(e=>!e.disabled)
                    .find(e => rx.test((e.innerText||e.value||'').replace(/\s+/g,' ').trim()));
                const b = pick(/^(continue|next|submit|see if|get started)$/i)
                       || Array.from(document.querySelectorAll('button,input[type=submit]'))
                            .filter(vis).filter(e=>!e.disabled && !/back/i.test(e.innerText||''))[0];
                if (b) b.click();
            }""")
        except Exception as e:
            log.warning("form.continue_failed", error=str(e)[:70])

    def _await_change(self, page: Page, prev_sig: str, timeout: float = 20.0) -> bool:
        """Wait for the step to change (or the form to complete).  Returns True
        if it changed, False on timeout (caller may re-click Continue)."""
        end = time.time() + timeout
        while time.time() < end:
            time.sleep(0.7)
            if self._completion_state(page):
                return True
            st = self._state(page)
            if st["sig"] and st["sig"] != prev_sig and not st["loading"]:
                return True
        return False

    def _completion_state(self, page: Page) -> str:
        try:
            url = page.url or ""
            body = page.evaluate("() => (document.body ? document.body.innerText : '').slice(0, 400)") or ""
        except Exception:
            return ""
        if re.search(r"(offers?|results|congratulat|you'?re connected|matching you|thank you for)", body, re.I) \
                and not re.search(r"loan request|what is your|do you", body, re.I):
            return "results / offers page"
        if any(k in url for k in ("results", "offers", "thank", "confirm")):
            return f"redirected ({url[:60]})"
        return ""

    @staticmethod
    def _css(fid: str) -> str:
        # ids may contain special chars (Duet uuids) — escape for a selector.
        return re.sub(r'([^a-zA-Z0-9_-])', r'\\\1', fid)

    # ------------------------------------------------------------------ timing

    def _key_delay(self) -> float:
        import random
        return random.uniform(float(self._delays.get("min_typing_delay", 0.05)),
                              float(self._delays.get("max_typing_delay", 0.14))) * 1000

    def _action_pause(self) -> None:
        import random
        time.sleep(random.uniform(float(self._delays.get("min_action_delay", 0.5)),
                                  float(self._delays.get("max_action_delay", 1.8))))

    def _read_pause(self) -> None:
        import random
        rng = self._delays.get("read_pause", [0.5, 1.2])
        time.sleep(random.uniform(float(rng[0]), float(rng[1])))

    def _check_stop(self, stop_event) -> None:
        if stop_event is not None and stop_event.is_set():
            raise FormFillerError("Stopped by user", error_type="stopped")

    def _live(self, page: Page) -> None:
        try: page.screenshot(path=str(self._ss_dir / "live_view.png"))
        except Exception: pass

    # ---------------------------------------------------------------- parsing

    def _parse_fields(self, row: dict) -> dict:
        def g(*keys: str) -> str:
            for k in keys:
                v = str(row.get(k) or "").strip()
                if v: return v
            return ""

        phone = _digits(g("Phone Number", "Phone"))
        phone = phone[1:] if len(phone) == 11 and phone.startswith("1") else phone
        ssn = _digits(g("SSN Full", "SSN")) or _digits(g("SSN Last 4"))
        dob = self._norm_dob(g("Date of Birth (DOB)", "DOB", "dob"))
        m, d, y = (dob.split("/") + ["", "", ""])[:3] if dob else ("", "", "")
        routing = _digits(g("ABA Routing Number", "routingNumber"))
        if 0 < len(routing) < 9: routing = routing.zfill(9)
        loan_raw = re.sub(r"[,$\s]", "", g("Requested Loan Amount ($)", "Loan_Amount"))
        try: loan = str(max(100, min(35000, int(float(loan_raw)))))
        except (ValueError, TypeError): loan = "5000"

        return {
            "first_name": g("First Name", "First_Name"), "last_name": g("Last Name", "Last_Name"),
            "email": g("Email Address", "Email"), "phone": self._fmt_phone(phone),
            "dob_m": m, "dob_d": d, "dob_y": y, "ssn": ssn, "ssn_last4": ssn[-4:] if ssn else "",
            "street": g("Street Address", "Address"), "city": g("City"),
            "state": self._norm_state(g("State")), "zip": _digits(g("ZIP Code", "Zip")).zfill(5) if g("ZIP Code", "Zip") else "",
            "loan_amount": loan, "loan_reason": self._loan_reason(g("Loan Purpose", "Loan_Purpose")),
            "income": re.sub(r"[,$\s]", "", g("Monthly Net Income ($)", "Monthly_Income")) or "3000",
            "income_source": self._income_source(g("Income Source", "Primary Income Source")),
            "pay_freq": self._pay_freq(g("Pay Frequency", "Pay_Frequency")),
            "years_at_address": self._years(g("Years at Address", "Months at Address")),
            "years_employed": self._years(g("Years at Employer", "Months at Employer")),
            "homeowner": "Yes" if self._truthy(g("Homeowner")) else "No",
            "military": "Yes" if self._truthy(g("Military", "Active Military")) else "No",
            "direct_deposit": "No" if g("Direct Deposit") and not self._truthy(g("Direct Deposit")) else "Yes",
            "next_payday": self._next_payday(g("Next Payday")),
            "employer_name": g("Employer Name", "Employer_Name") or "Employer",
            "employer_phone": self._employer_phone(
                _digits(g("Employer Work Phone", "Employer Phone", "Work Phone")), phone),
            "job_title": g("Job Title") or "Employee",
            "months_bank": (self._months(g("Months at Bank", "Bank Tenure"))
                            or self._years_to_months(g("Years at Bank")) or "24"),
            "credit_type": self._credit(g("Credit Score Rating", "Credit Score", "Credit Rating", "Credit")),
            "unsecured_debt": g("Unsecured Debt", "Credit Card Debt", "Debt Amount") or "less than $10,000",
            "bank_name": g("Bank Name", "bankName") or "Chase",
            "routing": routing, "account": _digits(g("Account Number", "accountNumber")),
            "account_type": "savings" if g("Account Type").lower().startswith("sav") else "checking",
            "dl_number": g("Driver License / ID Number", "driversLicenseNumber"),
            "dl_state": self._norm_state(g("Driver License State") or g("State")),
        }

    def _validate_required_fields(self, f: dict) -> None:
        req = ["first_name", "last_name", "email", "phone", "dob_y", "zip", "street", "city", "state"]
        missing = [k for k in req if not f.get(k)]
        if f["routing"] and not _aba_checksum_ok(f["routing"]):
            missing.append("routing_number(failed ABA checksum)")
        if f["phone"] and not re.match(r"^\(\d{3}\) \d{3}-\d{4}$", f["phone"]):
            missing.append("phone(invalid US number)")
        if not (f["dob_m"] and f["dob_d"] and f["dob_y"]):
            missing.append("dob")
        if missing:
            raise FormFillerError(f"Missing or invalid fields: {missing}", error_type="missing_data")

    # ---------------------------------------------------------------- mappers

    def _fmt_phone(self, digits: str) -> str:
        d = digits
        if len(d) != 10 or d[0] not in "23456789":
            return ""
        return f"({d[:3]}) {d[3:6]}-{d[6:]}"

    def _employer_phone(self, emp_digits: str, applicant_digits: str) -> str:
        """CashUSA rejects an employer phone that equals the applicant's own phone
        ("INVALID EMPLOYER PHONE").  Use the lead's employer phone when it is
        present and distinct; otherwise derive a distinct, valid US number from
        the applicant's so the required field passes without duplicating it."""
        emp = self._fmt_phone(emp_digits)
        if emp and emp_digits != applicant_digits:
            return emp
        d = applicant_digits
        if len(d) != 10:
            return "(212) 555-0100"
        new_last4 = f"{(int(d[6:]) + 1234) % 10000:04d}"
        if new_last4 == d[6:]:
            new_last4 = f"{(int(d[6:]) + 1) % 10000:04d}"
        return self._fmt_phone(d[:6] + new_last4)

    def _norm_dob(self, raw: str) -> str:
        raw = (raw or "").strip()
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%d/%m/%Y", "%m/%d/%y"):
            try: return datetime.strptime(raw, fmt).strftime("%m/%d/%Y")
            except ValueError: pass
        return raw

    def _norm_state(self, raw: str) -> str:
        raw = (raw or "").strip()
        return raw.upper() if len(raw) == 2 else _STATE_CODES.get(raw.lower(), raw.upper()[:2])

    def _loan_reason(self, raw: str) -> str:
        r = (raw or "").lower()
        table = [
            (("debt consol", "consolidat"), "debtConsolidation"),
            (("debt relief",), "debtRelief"),
            (("credit card",), "creditCardRefinance"),
            (("emergency",), "emergencySituation"),
            (("auto repair", "car repair", "vehicle repair"), "autoRepair"),
            (("auto purchase", "buy a car", "car purchase"), "autoPurchase"),
            (("moving", "relocat"), "moving"),
            (("medical", "dental", "health"), "medical"),
            (("business",), "business"),
            (("vacation", "travel"), "vacation"),
            (("tax",), "taxes"),
            (("rent", "mortgage"), "rentOrMortgage"),
            (("home improv", "renovat", "major purchase", "appliance"), "majorPurchase"),
            (("wedding", "special"), "specialOccasion"),
            (("education", "school", "tuition"), "education"),
        ]
        for keys, val in table:
            if any(k in r for k in keys):
                return val
        return "other"

    def _income_source(self, raw: str) -> str:
        r = (raw or "").lower()
        if "self" in r: return "SELFEMPLOYMENT"
        if any(k in r for k in ("benefit", "disab", "social", "pension", "retire", "unemploy")):
            return "UNEMPLOYED" if "unemploy" in r else "BENEFITS"
        return "EMPLOYMENT"

    def _pay_freq(self, raw: str) -> str:
        r = (raw or "").lower()
        if "week" in r and ("bi" in r or "every 2" in r or "every two" in r): return "biweekly"
        if "twice" in r or "semi" in r or "1st" in r: return "twicemonthly"
        if "week" in r: return "weekly"
        return "monthly"

    def _years(self, raw: str) -> str:
        raw = (raw or "").strip().lower()
        nums = re.findall(r"\d+", raw)
        if not nums: return "3"
        n = int(nums[0])
        yrs = n if ("year" in raw or n <= 12 and "month" not in raw) else max(1, round(n / 12))
        return str(min(max(yrs, 1), 10))

    def _credit(self, raw: str) -> str:
        """Map a numeric FICO score or a rating word to CashUSA's creditType
        vocabulary (excellent / good / fair / poor).  Defaults to 'fair'."""
        raw = (raw or "").strip().lower()
        nums = re.findall(r"\d{3}", raw)
        if nums:
            n = int(nums[0])
            if n >= 720: return "excellent"
            if n >= 660: return "good"
            if n >= 600: return "fair"
            return "poor"
        for word in ("excellent", "good", "fair", "poor"):
            if word in raw:
                return word
        return "fair"

    def _months(self, raw: str) -> str:
        raw = (raw or "").strip().lower()
        nums = re.findall(r"\d+", raw)
        if not nums: return ""
        n = int(nums[0])
        months = n * 12 if "year" in raw else n
        return str(max(1, min(months, 480)))

    def _years_to_months(self, raw: str) -> str:
        """Convert a value from a 'Years at Bank' column to months."""
        nums = re.findall(r"\d+", raw or "")
        if not nums: return ""
        return str(max(1, min(int(nums[0]) * 12, 480)))

    def _truthy(self, raw: str) -> bool:
        return (raw or "").strip().lower() in {"yes", "y", "true", "1", "own", "owner"}

    def _next_payday(self, raw: str) -> str:
        raw = (raw or "").strip()
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
            try:
                dt = datetime.strptime(raw, fmt)
                if dt.date() <= datetime.now().date():
                    dt = datetime.now() + timedelta(days=14)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                pass
        return (datetime.now() + timedelta(days=14)).strftime("%Y-%m-%d")

    # ---------------------------------------------------------------- utilities

    def _screenshot(self, page: Page, row: int, label: str) -> None:
        try:
            page.screenshot(path=str(self._ss_dir / f"row_{row:04d}_{label}.png"), full_page=False)
        except Exception as e:
            log.warning("screenshot.failed", error=str(e)[:60])

    def _classify_error(self, exc: Exception) -> str:
        m = str(exc).lower()
        if "crash" in m: return "browser_crashed"
        if "proxy" in m or "net::err" in m or "tunnel" in m: return "proxy_error"
        if "timeout" in m: return "timeout"
        return "unknown"

    def _clean_fingerprint(self, fp: dict) -> dict:
        allowed = {"user_agent", "viewport", "locale", "timezone_id", "geolocation",
                   "color_scheme", "device_scale_factor", "is_mobile", "has_touch",
                   "java_script_enabled", "extra_http_headers"}
        return {k: v for k, v in fp.items() if k in allowed and v is not None}
