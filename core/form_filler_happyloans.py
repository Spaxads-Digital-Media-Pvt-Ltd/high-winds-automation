"""
core/form_filler_happyloans.py — happyloans.net (Round Sky / rndframe).

The apply wizard is Round Sky ``INSTALLMENT_STEP`` (STYLE5) loaded in an
iframe from ``rndframe.com``. Field names were dumped from the live form:

  page1   requestedLoanAmount / h_requestedLoanAmount
  page2   firstName, lastName (+ h_firstName, h_lastName)
  page3   h_ssn3, h_birthdate_year, h_zip
  page4   email
  page5   home_phone1/2/3
  page6   birthdate_month/day/year
  page7   activeMilitary
  page8   address, zip
  page9   monthsAtResidence
  page10  housing
  page30  hasCarTitle
  page11  incomeType
  page12  monthsEmployed
  page13  payPeriod
  page14  monthlyIncome
  page15  payMonth, payDay1
  page16  employer, occupation
  page17  work_phone1/2/3
  page18  drivingLicenseNumber, drivingLicenseState
  page19  ssn1/2/3
  page20  routingNumber, accountNumber, bankName
  page21  directDeposit
  page22  monthsWithBank
  page23  bankAccountType
  page24  creditScore
  page25  loanPurpose
  page26  highDebt
  page28  retrySubmit
  page29  termsField, #RSsubmit
  page40  h_special_ssn3, h_specialRequestedLoanAmount, h_loanPurpose,
          h_creditScore, h_highDebt, h_retrySubmit, h_hasCarTitle,
          h_termsField   (returning-applicant express path)
  page41  rp_requestedLoanAmount  (short-term fallback re-price)

After submit, the shared 247 Lending Group chase in lead_platform runs.
"""
from __future__ import annotations

import re
import time

import structlog
from playwright.sync_api import Frame, Page

from core.lead_platform import BasePlatformFiller, FormFillerError, _digits
log = structlog.get_logger(__name__)

__all__ = ["FormFiller", "FormFillerError"]

# Round Sky honeypot inputs (e.g. uh-sbq) must stay empty — never fill them.
_HONEYPOT_RE = re.compile(r"^uh[-_]", re.I)

_AMOUNT_OPTIONS = (
    (500, "$100 - $500"),
    (1000, "$500 - $1,000"),
    (2500, "$1,000 - $2,500"),
    (5000, "$2,500 - $5,000"),
    (20000, "$5,000 - $20,000"),
    (50000, "$20,000 - $50,000"),
)

# page41 re-price select: plain $100..$1,000 in $100 steps.
_RETRY_AMOUNT_OPTIONS = (
    (100, "$100"), (200, "$200"), (300, "$300"), (400, "$400"), (500, "$500"),
    (600, "$600"), (700, "$700"), (800, "$800"), (900, "$900"), (1000, "$1,000"),
)

_INCOME_OPTIONS = (
    (1499, "Less than $1500"),
    (2000, "$1500-$2000"),
    (2500, "$2000-$2500"),
    (3000, "$2500-$3000"),
    (3500, "$3000-$3500"),
    (4000, "$3500-$4000"),
    (5000, "$4000-$5000"),
    (10**9, "$5000 or more"),
)

_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

# STYLE5 renders every single-choice step as a row of chip buttons rather
# than the <select> that is in the markup, so those steps report zero visible
# inputs. Map the step's heading to the field whose label to click.
_CHOICE_STEPS = (
    (r"how much do you need|let'?s get started",        "requestedLoanAmount"),
    (r"lived at your current address",                  "monthsAtResidence"),
    (r"are you a homeowner|own your home",              "housing"),
    (r"car with a clear title",                         "hasCarTitle"),
    (r"employment status",                              "incomeType"),
    (r"been with your employer",                        "monthsEmployed"),
    (r"how often are you paid",                         "payPeriod"),
    (r"your monthly income",                            "monthlyIncome"),
    (r"in the military|active military",                "activeMilitary"),
    (r"how do you get paid",                            "directDeposit"),
    (r"had this bank account",                          "monthsWithBank"),
    (r"checking or a savings",                          "bankAccountType"),
    (r"approximate credit score",                       "creditScore"),
    (r"primary reason for the loan",                    "loanPurpose"),
    (r"credit card debt",                               "highDebt"),
    (r"expand the search|loan of at least",             "retrySubmit"),
    (r"choose a loan amount",                           "rp_requestedLoanAmount"),
)

_CLICK_NEXT_JS = r"""() => {
    const vis = e => {
        const r = e.getBoundingClientRect();
        const st = getComputedStyle(e);
        return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
    };
    const t = e => (e.innerText || e.value || '').replace(/\s+/g, ' ').trim();
    const no = /prev|back|previous|español/i;
    const go = /^(next|continue|submit|start here|get started)$/i;
    const nodes = Array.from(document.querySelectorAll(
        'a.next-step, input#RSsubmit, #RSsubmit, input[type=button], button, a'
    )).filter(vis);
    const rs = nodes.find(e => (e.id || '') === 'RSsubmit');
    if (rs) { rs.click(); return t(rs) || 'RSsubmit'; }
    const b = nodes.find(e => {
        const s = t(e);
        return s && s.length < 28 && go.test(s) && !no.test(s);
    });
    if (b) { b.click(); return t(b); }
    return '';
}"""

_VISIBLE_NAMES_JS = r"""() => {
    const vis = e => {
        const r = e.getBoundingClientRect();
        const st = getComputedStyle(e);
        return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
    };
    return Array.from(document.querySelectorAll('input,select,textarea'))
        .filter(e => vis(e) && e.type !== 'hidden' && !e.disabled)
        .map(e => e.name || e.id || '')
        .filter(Boolean);
}"""

_DONE_JS = r"""() => {
    const b = ((document.body && document.body.innerText) || '').toLowerCase();
    // page40 greets returning applicants with "Congratulations!" but is still
    // a step to fill (h_special_ssn3 + h_specialRequestedLoanAmount), so the
    // express path must not be mistaken for a submitted state.
    if (/previous request on file|verify the last 4/.test(b)) return false;
    return /(thank you|congratulations|request has been submitted|connecting with|finding you|please wait|do not close|processing your)/.test(b);
}"""

_SET_FIELDS_JS = r"""(map) => {
    const vis = e => {
        const r = e.getBoundingClientRect();
        const st = getComputedStyle(e);
        return r.width > 2 && r.height > 2
            && st.visibility !== 'hidden' && st.display !== 'none';
    };
    const setNative = (el, val) => {
        const v = String(val);
        if (el.tagName === 'SELECT') {
            const opt = Array.from(el.options).find(o =>
                o.value === v
                || (o.text || '').trim() === v
                || (o.text || '').includes(v)
                || o.value.endsWith(v)
            );
            if (!opt) return false;
            const proto = HTMLSelectElement.prototype;
            const d = Object.getOwnPropertyDescriptor(proto, 'value');
            if (d && d.set) d.set.call(el, opt.value); else el.value = opt.value;
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
            try { if (window.jQuery) window.jQuery(el).val(opt.value).trigger('change'); } catch (e) {}
            return true;
        }
        if (el.type === 'checkbox' || el.type === 'radio') {
            const on = /^(1|true|yes)$/i.test(v);
            if (el.checked !== on) el.click();
            return true;
        }
        const proto = el.tagName === 'TEXTAREA'
            ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        const d = Object.getOwnPropertyDescriptor(proto, 'value');
        el.focus();
        if (d && d.set) d.set.call(el, v); else el.value = v;
        ['input','change','blur','keyup'].forEach(ev =>
            el.dispatchEvent(new Event(ev, {bubbles: true})));
        return true;
    };
    let n = 0;
    for (const [name, val] of Object.entries(map || {})) {
        if (val === undefined || val === null || String(val) === '') continue;
        const els = Array.from(document.querySelectorAll(
            '[name="' + name + '"], #' + name
        )).filter(e => e.type !== 'hidden' && !e.disabled);
        for (const el of els) {
            if (el.tagName !== 'SELECT' && !vis(el)) continue;
            if (setNative(el, val)) n++;
        }
    }
    return n;
}"""


class FormFiller(BasePlatformFiller):
    """happyloans.net — Round Sky installment wizard."""

    default_url = "https://www.happyloans.net/submit-loan-request.php"
    default_url = default_url
    site_label = "Happy Loans"

    def _prepare(self, page: Page, row_number: int) -> None:
        self._dismiss_spanish(page)
        self._open_apply_form(page)
        if not self._wait_rs_frame(page, 50):
            log.warning("form.rs_frames",
                        frames=[(fr.url or "")[:90] for fr in page.frames],
                        row=row_number)
            self._screenshot(page, row_number, "no_rs_form")
            raise FormFillerError(
                "Happy Loans Round Sky form iframe did not load",
                error_type="stuck",
            )
        try:
            page.locator("#rsForm, #landeriframe, iframe").first.scroll_into_view_if_needed(
                timeout=3000
            )
        except Exception:
            pass
        self._live(page)

    def _open_apply_form(self, page: Page) -> None:
        """The homepage widget often never injects; the apply URL hosts #rsForm."""
        url = (page.url or "").lower()
        if "submit-loan-request" not in url:
            page.goto(self.default_url, wait_until="domcontentloaded", timeout=45000)
        self._dismiss_spanish(page)

    def _fill_form(self, page: Page, f: dict, row_number: int, stop_event) -> str:
        vals = self._rs_values(f)
        self._click_amount(page, vals["amount_label"], row_number)
        deadline = time.time() + 8
        while time.time() < deadline:
            fr = self._rs_frame(page)
            names = self._visible_names(fr) if fr else []
            if any(n in names for n in ("firstName", "email", "h_firstName", "zip", "address")):
                break
            time.sleep(0.35)
        self._action_pause()
        self._live(page)

        seen: dict[str, int] = {}
        submitted = False
        for step in range(40):
            self._check_stop(stop_event)
            if self._is_done(page):
                submitted = True
                break
            fr = self._active_rs_frame(page)
            if fr is None:
                submitted = True
                break
            names = self._visible_names(fr)
            if not names:
                wait_until = time.time() + 10
                while time.time() < wait_until and not names:
                    self._wait_overlay(page, fr)
                    if self._is_done(page):
                        submitted = True
                        break
                    if self._click_choice_step(page, vals):
                        time.sleep(0.9)
                    elif self._click_continue(page, fr) or self._click_next(fr):
                        time.sleep(0.9)
                    fr = self._active_rs_frame(page) or fr
                    names = self._visible_names(fr) if fr else []
                    if names:
                        break
                    time.sleep(0.35)
                if submitted:
                    break
            if not names:
                # Chip-only steps have no inputs, and there are a dozen of them
                # in a row. Key the stall counter on the heading so advancing
                # through them is not mistaken for being stuck on one.
                sig = "empty:" + (self._heading(fr) or (fr.url or "")[:60] if fr else "noframe")
                seen[sig] = seen.get(sig, 0) + 1
                log.info("form.empty_step", step=step, head=self._heading(fr)[:60],
                         row=row_number)
                if seen[sig] > 6:
                    self._screenshot(page, row_number, f"stuck_{step}")
                    raise FormFillerError(
                        f"Happy Loans form stopped advancing at step {step}: {sig[:80]}",
                        error_type="stuck",
                    )
                continue
            sig = ",".join(names)
            seen[sig] = seen.get(sig, 0) + 1
            if seen[sig] > 4:
                self._screenshot(page, row_number, f"stuck_{step}")
                raise FormFillerError(
                    f"Happy Loans form stopped advancing at step {step}: {sig[:80]}",
                    error_type="stuck",
                )
            log.info("form.step", step=step, fields=sig[:80], row=row_number)
            if any(n in names for n in ("h_special_ssn3", "h_specialRequestedLoanAmount")):
                self._leave_returning_shortcut(page)
                fr = self._active_rs_frame(page) or fr
                names = self._visible_names(fr) if fr else []
                sig = ",".join(names) if names else sig
                if any(n in names for n in ("h_special_ssn3", "h_specialRequestedLoanAmount")):
                    filled = self._fill_returning_page(page, fr, vals)
                    self._check_terms(page, fr)
                    time.sleep(0.35)
                    clicked = self._click_continue(page, fr) or self._click_next(fr)
                    log.info("form.rs_step", step=step, filled=filled, cta=clicked or "",
                             row=row_number)
                    self._live(page)
                    time.sleep(1.0)
                    self._wait_overlay(page, fr)
                    if self._is_done(page):
                        submitted = True
                        break
                    # Still on returning page after Submit — treat as progress
                    # only if the field set changed; otherwise keep looping.
                    continue
            self._wait_overlay(page, fr)
            self._read_pause()
            filled = self._fill_named_visible(page, fr, vals)
            filled += self._type_visible_inputs(fr, vals)
            filled += self._fill_current_visible(page, fr, vals)
            if any(n in names for n in ("work_phone1", "work_phone2", "work_phone3")):
                # Round Sky can show the segmented values while its validator
                # still has stale state after a native JS value assignment.
                # Re-enter each employer-phone segment as real keystrokes.
                filled += self._type_phone_segments(fr, vals, "work_phone")
            if any(n in names for n in ("h_ssn3", "ssn3", "h_special_ssn3")):
                filled += self._fill_ssn_last4(page, fr, vals)
            self._check_terms(page, fr)
            self._field_pause()
            fillable = self._fillable_names(names)
            if filled == 0 and fillable:
                log.warning("form.rs_unfilled", fields=sig[:80], row=row_number)
                time.sleep(0.4)
                filled = self._fill_named_visible(page, fr, vals)
                filled += self._type_visible_inputs(fr, vals)
                filled += self._fill_current_visible(page, fr, vals)
                if any(n in names for n in ("work_phone1", "work_phone2", "work_phone3")):
                    filled += self._type_phone_segments(fr, vals, "work_phone")
                if any(n in names for n in ("h_ssn3", "ssn3", "h_special_ssn3")):
                    filled += self._fill_ssn_last4(page, fr, vals)
            elif filled == 0 and names and not fillable:
                log.info("form.honeypot_skip", fields=sig[:80], row=row_number)
            if any(n in names for n in ("h_ssn3", "ssn3", "h_special_ssn3")):
                last4 = self._ssn_last4(vals)
                if not self._ssn4_ok(page, last4):
                    self._fill_ssn_last4(page, fr, vals)
                if not self._ssn4_ok(page, last4):
                    self._screenshot(page, row_number, "ssn4_empty")
                    raise FormFillerError(
                        f"Happy Loans last-4 SSN did not stick (tried {last4})",
                        error_type="field_rejected",
                    )
            if filled == 0 and fillable:
                self._screenshot(page, row_number, f"unfilled_{step}")
                raise FormFillerError(
                    f"Happy Loans could not fill step {step} ({sig[:80]})",
                    error_type="field_rejected",
                )
            time.sleep(0.35)
            clicked = self._click_continue(page, fr)
            if not clicked:
                clicked = self._click_next(fr)
            log.info("form.rs_step", step=step, filled=filled, cta=clicked or "",
                     row=row_number)
            self._live(page)
            time.sleep(0.7)
            self._wait_overlay(page, fr)
            if self._is_done(page):
                submitted = True
                break
            if clicked.lower() in ("rsubmit", "rssubmit", "submit") or clicked == "RSsubmit":
                deadline = time.time() + 12
                while time.time() < deadline:
                    if self._is_done(page):
                        submitted = True
                        break
                    time.sleep(0.5)
                if submitted:
                    break

        if not submitted and not self._is_done(page):
            self._screenshot(page, row_number, "not_submitted")
            raise FormFillerError(
                "Happy Loans form did not reach a submitted / thank-you state",
                error_type="timeout",
            )

        self._handle_post_offer(page, f, row_number, stop_event)
        return "submitted"

    # ---------------------------------------------------------------- frame

    def _rs_frame(self, page: Page) -> Frame | None:
        return self._active_rs_frame(page)

    def _active_rs_frame(self, page: Page) -> Frame | None:
        hosts = ("rndframe.com", "rndframe.com", "roundsky", "installmentstep")
        cands: list[Frame] = []
        for fr in page.frames:
            u = (fr.url or "").lower()
            if any(h in u for h in hosts):
                cands.append(fr)
        for fr in cands:
            if self._visible_names(fr):
                return fr
        for fr in page.frames:
            try:
                if self._visible_names(fr):
                    return fr
            except Exception:
                continue
        for fr in page.frames:
            try:
                if fr.locator("#requestedLoanAmount, #RSsubmit").count() > 0:
                    return fr
            except Exception:
                continue
        return cands[0] if cands else None

    def _click_choice_step(self, page: Page, vals: dict) -> str:
        """Click the chip for a STYLE5 single-choice step.

        These steps have no visible input at all — the <select> in the markup
        is hidden and the options are rendered as buttons — so the step is
        driven by matching the heading to a field and clicking the chip whose
        text equals that field's already-resolved option label.
        """
        fields = (vals or {}).get("fields") or {}
        js_list = r"""() => {
            const vis = e => {
                const r = e.getBoundingClientRect();
                const st = getComputedStyle(e);
                return r.width > 16 && r.height > 16
                    && st.visibility !== 'hidden' && st.display !== 'none'
                    && Number(st.opacity) !== 0;
            };
            const t = e => (e.innerText || e.value || '').replace(/\s+/g, ' ').trim();
            const body = ((document.body && document.body.innerText) || '').toLowerCase();
            const head = Array.from(document.querySelectorAll('h1,h2,legend'))
                .filter(vis).map(t).filter(Boolean)[0] || '';
            const opts = Array.from(document.querySelectorAll(
                'a,button,label,div[role=button],span,li'
            )).filter(vis).map(e => t(e)).filter(s => s && s.length < 40);
            return {body: body.slice(0, 400), head: head, opts: opts.slice(0, 24)};
        }"""
        for fr in page.frames:
            try:
                info = fr.evaluate(js_list) or {}
            except Exception:
                continue
            opts = [str(x) for x in (info.get("opts") or [])]
            body = str(info.get("body") or "").lower()
            head = str(info.get("head") or "").lower()
            if not opts:
                continue

            # Preferred path: heading names the field, the chip text is the
            # option label _rs_values already resolved.
            want = ""
            for pattern, field in _CHOICE_STEPS:
                if not re.search(pattern, head or body):
                    continue
                want = str(fields.get(field) or "")
                # "How do you get paid?" offers Direct Deposit / Paper Check,
                # but the later employer question is a plain Yes/No.
                if field == "directDeposit" and not any(
                    o.lower().startswith("direct deposit") for o in opts
                ):
                    want = "Yes"
                break
            if want:
                low = [o.lower() for o in opts]
                if want.lower() not in low:
                    # e.g. "Poor (< 600)" rendered as "Poor (&lt; 600)"
                    stem = re.split(r"[\s(]", want)[0].lower()
                    match = next(
                        (o for o in opts if o.lower().startswith(stem) and len(o) < 40),
                        "",
                    )
                    want = match or want
                hit = self._click_chip(fr, want)
                if hit:
                    log.info("form.chip", head=head[:52], pick=hit)
                    return hit
                log.warning("form.chip_missing", head=head[:52], want=want,
                            opts=",".join(opts)[:110])
            low = [o.lower() for o in opts]
            pick = ""
            if any(x in low for x in ("yes", "no")):
                if "military" in body or "armed" in body:
                    pick = "No"
                elif "own" in body and "home" in body:
                    pick = "Yes" if str(fields.get("housing") or "").lower() == "yes" else "No"
                elif "car" in body or "title" in body or "vehicle" in body:
                    pick = "Yes" if str(fields.get("hasCarTitle") or "").lower() == "yes" else "No"
                elif "direct deposit" in body:
                    pick = "Yes"
                elif "expand the search" in body or "is not available" in body:
                    pick = "Yes"
                else:
                    pick = "No"
            if not pick:
                continue
            hit = self._click_chip(fr, pick)
            if hit:
                log.info("form.choice_chip", pick=hit, q=body[:80])
                return hit
        return ""

    def _leave_returning_shortcut(self, page: Page) -> None:
        """Returning-applicant shortcut often rejects a new test SSN. Prefer the full form."""
        js = r"""() => {
            const vis = e => {
                const r = e.getBoundingClientRect();
                const st = getComputedStyle(e);
                return r.width > 8 && r.height > 8
                    && st.visibility !== 'hidden' && st.display !== 'none';
            };
            const t = e => (e.innerText || e.value || '').replace(/\s+/g, ' ').trim();
            const el = Array.from(document.querySelectorAll('a,button,span,div,label'))
                .find(e => vis(e) && /new customer|new applicant|not (you|me)|start over|full application|apply as new|different (person|applicant)/i.test(t(e))
                    && t(e).length < 60);
            if (!el) return '';
            el.click();
            return t(el);
        }"""
        for fr in page.frames:
            try:
                hit = fr.evaluate(js)
            except Exception:
                hit = ""
            if hit:
                log.info("form.leave_returning", cta=hit)
                time.sleep(0.8)
                return

    def _fill_returning_page(self, page: Page, fr: Frame, vals: dict) -> int:
        """Fill page40 returning-applicant fields using live select options."""
        fields = (vals or {}).get("fields") or {}
        last4 = self._ssn_last4(vals)
        want_amt = (
            fields.get("h_specialRequestedLoanAmount")
            or fields.get("requestedLoanAmount")
            or fields.get("rp_requestedLoanAmount")
            or ""
        )
        want_debt = fields.get("h_highDebt") or fields.get("highDebt") or "No"
        n = 0
        n += self._fill_ssn_last4(page, fr, vals)
        js = r"""(P) => {
            const vis = e => {
                const r = e.getBoundingClientRect();
                const st = getComputedStyle(e);
                return r.width > 2 && r.height > 2
                    && st.visibility !== 'hidden' && st.display !== 'none';
            };
            const setSel = (el, want) => {
                const opts = Array.from(el.options).filter(o => (o.value || '') && !/select/i.test(o.text || ''));
                if (!opts.length) return false;
                const w = String(want || '').toLowerCase();
                let opt = opts.find(o => (o.text || '').trim().toLowerCase() === w)
                    || opts.find(o => (o.text || '').toLowerCase().includes(w) || w.includes((o.text || '').trim().toLowerCase()))
                    || opts.find(o => /\d/.test(o.text || ''))
                    || opts[0];
                const proto = HTMLSelectElement.prototype;
                const d = Object.getOwnPropertyDescriptor(proto, 'value');
                if (d && d.set) d.set.call(el, opt.value); else el.value = opt.value;
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                try { if (window.jQuery) window.jQuery(el).val(opt.value).trigger('change'); } catch (e) {}
                return true;
            };
            const setInp = (el, v) => {
                const proto = HTMLInputElement.prototype;
                const d = Object.getOwnPropertyDescriptor(proto, 'value');
                el.focus();
                if (d && d.set) d.set.call(el, v); else el.value = v;
                ['input','change','blur','keyup'].forEach(ev =>
                    el.dispatchEvent(new Event(ev, {bubbles: true})));
            };
            let n = 0;
            const amt = document.querySelector(
                '[name="h_specialRequestedLoanAmount"], #h_specialRequestedLoanAmount'
            );
            if (amt && amt.tagName === 'SELECT' && setSel(amt, P.amt)) n++;
            const debt = document.querySelector('[name="h_highDebt"], #h_highDebt, [name="highDebt"]');
            if (debt && debt.tagName === 'SELECT' && setSel(debt, P.debt)) n++;
            for (const name of ['h_special_ssn3', 'h_ssn3', 'ssn3']) {
                const el = document.querySelector('[name="' + name + '"], #' + name);
                if (el && vis(el) && P.ssn4) { setInp(el, P.ssn4); n++; }
            }
            const terms = document.querySelector(
                '#h_termsField, [name="h_termsField"], #termsField, [name="termsField"]'
            );
            if (terms) {
                if (terms.type === 'checkbox' && !terms.checked) { terms.click(); n++; }
                else if (terms.tagName === 'SELECT') { setSel(terms, 'true') || setSel(terms, 'Yes'); n++; }
            }
            return {n, amtOpts: amt ? Array.from(amt.options).map(o => (o.text||'').trim()).slice(0,12) : []};
        }"""
        for frame in [fr, *list(page.frames)]:
            try:
                out = frame.evaluate(js, {"amt": want_amt, "debt": want_debt, "ssn4": last4}) or {}
                got = int(out.get("n") or 0)
                if out.get("amtOpts"):
                    log.info("form.returning_opts", options=out.get("amtOpts")[:8])
                n += got
            except Exception as e:
                log.warning("form.returning_js", error=str(e)[:70])
        self._check_terms(page, fr)
        log.info("form.returning_filled", filled=n, amt=want_amt, debt=want_debt)
        return n

    def _heading(self, fr: Frame | None) -> str:
        if fr is None:
            return ""
        try:
            return str(fr.evaluate(
                r"""() => {
                    const vis = e => {
                        const r = e.getBoundingClientRect();
                        const st = getComputedStyle(e);
                        return r.width > 0 && r.height > 0
                            && st.visibility !== 'hidden' && st.display !== 'none';
                    };
                    const h = Array.from(document.querySelectorAll('h1,h2,legend'))
                        .filter(vis)
                        .map(e => (e.innerText || '').replace(/\s+/g, ' ').trim())
                        .filter(Boolean)[0] || '';
                    return h;
                }"""
            ) or "")
        except Exception:
            return ""

    def _click_chip(self, fr: Frame, want: str) -> str:
        """Click the visible option button whose text is exactly ``want``."""
        if not want:
            return ""
        try:
            return str(fr.evaluate(
                r"""(want) => {
                    const vis = e => {
                        const r = e.getBoundingClientRect();
                        const st = getComputedStyle(e);
                        return r.width > 16 && r.height > 16
                            && st.visibility !== 'hidden' && st.display !== 'none'
                            && Number(st.opacity) !== 0;
                    };
                    const t = e => (e.innerText || e.value || '')
                        .replace(/\s+/g, ' ').replace(/ /g, ' ').trim();
                    const w = String(want).toLowerCase();
                    // Innermost match wins so a wrapper div is not clicked in
                    // place of the button it contains.
                    const hits = Array.from(document.querySelectorAll(
                        'a,button,input[type=button],label,div,span,li'
                    )).filter(e => vis(e) && t(e).toLowerCase() === w && t(e).length < 44);
                    const el = hits[hits.length - 1];
                    if (!el) return '';
                    el.click();
                    return t(el);
                }""",
                want,
            ) or "")
        except Exception:
            return ""

    def _form_ready(self, page: Page) -> bool:
        for fr in page.frames:
            url = (fr.url or "").lower()
            if not any(h in url for h in ("rndframe.com", "rndframe.com", "roundsky")):
                continue
            try:
                if fr.get_by_text("How much do you need?", exact=False).count() > 0:
                    return True
                if fr.locator("#requestedLoanAmount").count() > 0:
                    return True
            except Exception:
                return True
        try:
            if page.frame_locator("iframe").get_by_text(
                "How much do you need?", exact=False
            ).count() > 0:
                return True
        except Exception:
            pass
        return False

    def _wait_rs_frame(self, page: Page, seconds: float) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            self._dismiss_spanish(page)
            if self._form_ready(page):
                return True
            time.sleep(0.4)
        return self._form_ready(page)

    def _dismiss_spanish(self, page: Page) -> None:
        for sel in ("button.no", "button:has-text('No')"):
            try:
                loc = page.locator(sel).first
                if loc.count() and loc.is_visible():
                    loc.click(timeout=1500)
                    time.sleep(0.3)
                    return
            except Exception:
                continue

    def _wait_overlay(self, page: Page, fr: Frame | None) -> None:
        deadline = time.time() + 18
        while time.time() < deadline:
            txt = ""
            for target in (fr, page):
                if target is None:
                    continue
                try:
                    txt = (target.evaluate(
                        "() => (document.body && document.body.innerText) || ''"
                    ) or "").lower()
                    if txt:
                        break
                except Exception:
                    continue
            if "loading next page" not in txt:
                return
            time.sleep(0.35)

    def _is_done(self, page: Page) -> bool:
        try:
            if page.evaluate(_DONE_JS):
                return True
        except Exception:
            pass
        for fr in page.frames:
            try:
                url = (fr.url or "").lower()
                if "rndframe.com" in url and re.search(
                    r"thank|complete|finish|result|offer", url
                ):
                    return True
                if fr.evaluate(_DONE_JS):
                    return True
            except Exception:
                continue
        return False

    def _visible_names(self, fr: Frame) -> list[str]:
        try:
            return list(fr.evaluate(_VISIBLE_NAMES_JS) or [])
        except Exception:
            return []

    # ---------------------------------------------------------------- fill

    def _is_honeypot(self, name: str) -> bool:
        return bool(_HONEYPOT_RE.match(str(name or "").strip()))

    def _fillable_names(self, names: list[str]) -> list[str]:
        return [n for n in names if not self._is_honeypot(n)]

    def _lookup_value(self, fields: dict, name: str) -> str:
        if not name:
            return ""
        if fields.get(name):
            return str(fields[name])
        low = {str(k).lower(): v for k, v in fields.items() if v not in (None, "")}
        hit = low.get(name.lower())
        if hit:
            return str(hit)
        alt = name[2:] if name.lower().startswith("h_") else "h_" + name
        hit = fields.get(alt) or low.get(alt.lower())
        return str(hit) if hit else ""

    def _type_visible_inputs(self, fr: Frame, vals: dict) -> int:
        """Type into the currently visible boxes so Round Sky validation sees keystrokes."""
        fields = (vals or {}).get("fields") or {}
        n = 0
        for name in self._visible_names(fr):
            if self._is_honeypot(name):
                continue
            if re.search(r"ssn3|ssn_3|special_ssn", name, re.I):
                continue
            value = self._lookup_value(fields, name)
            if not value:
                continue
            loc = fr.locator(f'[name="{name}"], #{name}').first
            try:
                if loc.count() == 0:
                    continue
                tag = (loc.evaluate("e => e.tagName") or "").upper()
                if tag == "SELECT":
                    try:
                        loc.select_option(label=str(value), timeout=1500)
                    except Exception:
                        loc.select_option(value=str(value), timeout=1500)
                    n += 1
                    continue
                typ = (loc.get_attribute("type") or "").lower()
                if typ in ("checkbox", "radio", "hidden"):
                    continue
                current = ""
                try:
                    current = loc.input_value() or ""
                except Exception:
                    pass
                if current.strip() == str(value).strip():
                    n += 1
                    continue
                loc.click(timeout=1500, force=True)
                loc.fill("")
                loc.press_sequentially(str(value), delay=35)
                n += 1
            except Exception as e:
                log.warning("form.rs_type_failed", field=name, error=str(e)[:70])
        return n

    def _type_phone_segments(self, fr: Frame, vals: dict, prefix: str) -> int:
        """Re-enter segmented phone fields so Round Sky's mask sees key events."""
        fields = (vals or {}).get("fields") or {}
        n = 0
        for name in (f"{prefix}1", f"{prefix}2", f"{prefix}3"):
            value = self._lookup_value(fields, name)
            if not value:
                continue
            loc = fr.locator(f'[name="{name}"], #{name}').first
            try:
                if loc.count() == 0 or not loc.is_visible():
                    continue
                loc.click(timeout=1500, force=True)
                loc.fill("")
                loc.press_sequentially(str(value), delay=self._key_delay())
                n += 1
            except Exception as e:
                log.warning("form.phone_type_failed", field=name, error=str(e)[:70])
        return n

    def _ssn_last4(self, vals: dict) -> str:
        fields = (vals or {}).get("fields") or {}
        raw = (
            fields.get("h_ssn3")
            or fields.get("ssn3")
            or fields.get("ssn")
            or ""
        )
        digits = re.sub(r"\D", "", str(raw))
        if len(digits) >= 4:
            return digits[-4:]
        return "3729"

    def _ssn4_ok(self, page: Page, last4: str) -> bool:
        js = r"""(v) => {
            const vis = e => {
                const r = e.getBoundingClientRect();
                const st = getComputedStyle(e);
                return r.width > 8 && r.height > 8
                    && st.visibility !== 'hidden' && st.display !== 'none'
                    && Number(st.opacity) !== 0;
            };
            const inputs = Array.from(document.querySelectorAll('input')).filter(vis);
            return inputs.some(e =>
                /ssn3|ssn_3|ssnLast|special_ssn/i.test((e.name || '') + (e.id || ''))
                && String(e.value || '') === v && v.length === 4
            );
        }"""
        for fr in page.frames:
            try:
                if fr.evaluate(js, last4):
                    return True
            except Exception:
                continue
        return False

    def _fill_ssn_last4(self, page: Page, fr: Frame, vals: dict) -> int:
        """The last-4 SSN box is a masked input; JS value-set often does not stick."""
        last4 = self._ssn_last4(vals)
        n = 0
        typed = False
        label_re = re.compile(r"last\s*4.*ssn|ssn.*last\s*4|xxx-xx", re.I)
        for frame in [fr, *list(page.frames)]:
            for loc in (
                frame.get_by_label(label_re),
                frame.get_by_placeholder(label_re),
                frame.locator(
                    'input[name="h_ssn3"], input[name="ssn3"], input[id="h_ssn3"], '
                    'input[name="h_special_ssn3"], input[id="h_special_ssn3"]'
                ),
            ):
                try:
                    box = loc.first
                    if box.count() == 0:
                        continue
                    box.scroll_into_view_if_needed(timeout=1500)
                    box.click(timeout=2000, force=True)
                    box.fill("")
                    box.press_sequentially(last4, delay=70)
                    typed = True
                    n += 1
                    break
                except Exception:
                    continue
            if typed:
                break
        js = r"""(v) => {
            const vis = e => {
                const r = e.getBoundingClientRect();
                const st = getComputedStyle(e);
                return r.width > 8 && r.height > 8
                    && st.visibility !== 'hidden' && st.display !== 'none'
                    && Number(st.opacity) !== 0;
            };
            const labs = Array.from(document.querySelectorAll('label,div,span,p,td,th,li,strong'));
            let el = null;
            for (const lab of labs) {
                const tx = (lab.innerText || '').replace(/\s+/g, ' ').trim();
                if (!/last\s*4|ssn\s*#|xxx-xx/i.test(tx) || tx.length > 90) continue;
                const forId = lab.getAttribute && lab.getAttribute('for');
                if (forId) el = document.getElementById(forId);
                if (!el) el = lab.querySelector && lab.querySelector('input');
                if (!el && lab.parentElement) el = lab.parentElement.querySelector('input');
                if (!el) {
                    let n = lab.nextElementSibling;
                    for (let i = 0; i < 5 && n; i++, n = n.nextElementSibling) {
                        if (n.tagName === 'INPUT') { el = n; break; }
                        const inner = n.querySelector && n.querySelector('input');
                        if (inner) { el = inner; break; }
                    }
                }
                if (el) break;
            }
            const inputs = Array.from(document.querySelectorAll('input')).filter(vis);
            if (!el || !vis(el)) {
                el = inputs.find(e => /ssn3|ssn_3|ssnLast|special_ssn/i.test((e.name || '') + (e.id || '')));
            }
            if (!el) return 0;
            el.scrollIntoView({block: 'center'});
            el.focus();
            el.click();
            const proto = HTMLInputElement.prototype;
            const desc = Object.getOwnPropertyDescriptor(proto, 'value');
            const setV = x => { if (desc && desc.set) desc.set.call(el, x); else el.value = x; };
            setV('');
            el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'deleteContentBackward'}));
            for (const ch of v) {
                setV(String(el.value || '') + ch);
                el.dispatchEvent(new InputEvent('input', {bubbles: true, data: ch, inputType: 'insertText'}));
            }
            el.dispatchEvent(new Event('change', {bubbles: true}));
            el.dispatchEvent(new Event('blur', {bubbles: true}));
            try { if (window.jQuery) window.jQuery(el).val(v).trigger('input').trigger('change'); } catch (e) {}
            return String(el.value || '') === v ? 1 : 0;
        }"""
        for frame in page.frames:
            try:
                n += int(frame.evaluate(js, last4) or 0)
            except Exception:
                continue
        log.info("form.ssn4", last4_len=len(last4), filled=n)
        return n

    def _fill_named_visible(self, page: Page, fr: Frame, vals: dict) -> int:
        """Fill visible Round Sky fields by name (h_ssn3, h_zip, h_birthdate_year, …)."""
        fields = vals.get("fields") or {}
        names: list[str] = []
        targets: list[Frame] = []
        for frame in [fr, *list(page.frames)]:
            if frame in targets:
                continue
            found = self._visible_names(frame)
            if found:
                targets.append(frame)
                for n in found:
                    if n not in names:
                        names.append(n)
        if not names:
            targets = [fr]
        payload = {}
        for name in names or fields:
            value = self._lookup_value(fields, name)
            if value:
                payload[name] = value
        n = 0
        for frame in targets:
            try:
                n += int(frame.evaluate(_SET_FIELDS_JS, payload) or 0)
            except Exception as e:
                log.warning("form.rs_js_fill", error=str(e)[:70])
        if n:
            return n
        fl = page.frame_locator("iframe")
        for name, value in payload.items():
            for loc in (
                fl.locator(f'[name="{name}"]').first,
                fr.locator(f'[name="{name}"]').first,
            ):
                try:
                    if loc.count() == 0:
                        continue
                    tag = (loc.evaluate("e => e.tagName") or "").upper()
                    if tag == "SELECT":
                        try:
                            loc.select_option(label=str(value), timeout=2000)
                        except Exception:
                            loc.select_option(value=str(value), timeout=2000)
                    else:
                        loc.fill(str(value), timeout=2000, force=True)
                    n += 1
                    break
                except Exception as e:
                    log.warning("form.rs_named_failed", field=name, error=str(e)[:70])
        return n

    def _click_amount(self, page: Page, label: str, row_number: int) -> None:
        last_err = ""
        # iframe on the apply page — don't rely on a single frame handle
        try:
            loc = page.frame_locator("iframe").get_by_text(label, exact=True).first
            loc.click(timeout=8000, force=True)
            log.info("form.amount_chip", label=label, how="frame_locator", row=row_number)
            time.sleep(0.8)
            return
        except Exception as e:
            last_err = str(e)[:100]
        for fr in page.frames:
            try:
                loc = fr.get_by_text(label, exact=True).first
                if loc.count() == 0:
                    continue
                loc.scroll_into_view_if_needed(timeout=2000)
                loc.click(timeout=4000, force=True)
                log.info("form.amount_chip", label=label, how="frame", row=row_number)
                time.sleep(0.8)
                return
            except Exception as e:
                last_err = str(e)[:100]
                continue
        fr = self._rs_frame(page)
        if fr is not None:
            try:
                how = fr.evaluate(
                    r"""(label) => {
                        const vis = e => {
                            const r = e.getBoundingClientRect();
                            return r.width > 8 && r.height > 8;
                        };
                        const norm = s => (s || '').replace(/\s+/g, ' ').trim();
                        const el = Array.from(document.querySelectorAll(
                            'a,button,label,div,li,span'
                        )).find(e => vis(e) && norm(e.innerText) === label
                            && norm(e.innerText).length < 48);
                        if (el) { el.click(); return 'js'; }
                        return '';
                    }""",
                    label,
                )
                if how:
                    log.info("form.amount_chip", label=label, how=how, row=row_number)
                    time.sleep(0.8)
                    return
            except Exception as e:
                last_err = str(e)[:100]
        self._screenshot(page, row_number, "no_amount")
        raise FormFillerError(
            f"Could not pick loan amount '{label}': {last_err}",
            error_type="stuck",
        )

    def _fill_current_visible(self, page: Page, fr: Frame, vals: dict) -> int:
        """Fill whatever inputs are actually on screen, matched by label text."""
        payload = {
            "firstName": vals["fields"].get("firstName") or "",
            "lastName": vals["fields"].get("lastName") or "",
            "email": vals["fields"].get("email") or "",
            "zip": vals["fields"].get("zip") or "",
            "address": vals["fields"].get("address") or "",
            "employer": vals["fields"].get("employer") or "",
            "occupation": vals["fields"].get("occupation") or "",
            "ssn": (vals["fields"].get("ssn1") or "")
            + (vals["fields"].get("ssn2") or "")
            + (vals["fields"].get("ssn3") or ""),
            "phone": (vals["fields"].get("home_phone1") or "")
            + (vals["fields"].get("home_phone2") or "")
            + (vals["fields"].get("home_phone3") or ""),
            "ssn4": vals["fields"].get("ssn3") or "",
            # Split-box segments. The generic label sweep below matches on
            # e.name, so home_phone1/2/3, work_phone1/2/3 and ssn1/2/3 all
            # look like "phone"/"ssn" fields; without these it writes the
            # whole number into each box and Round Sky's own input handler
            # truncates it to that box's maxlength (614 / 614 / 6145).
            "seg": {
                "home_phone1": vals["fields"].get("home_phone1") or "",
                "home_phone2": vals["fields"].get("home_phone2") or "",
                "home_phone3": vals["fields"].get("home_phone3") or "",
                "work_phone1": vals["fields"].get("work_phone1") or "",
                "work_phone2": vals["fields"].get("work_phone2") or "",
                "work_phone3": vals["fields"].get("work_phone3") or "",
                "ssn1": vals["fields"].get("ssn1") or "",
                "ssn2": vals["fields"].get("ssn2") or "",
                "ssn3": vals["fields"].get("ssn3") or "",
            },
            "birthYear": vals["fields"].get("birthdate_year") or "",
            "birthMonth": vals["fields"].get("birthdate_month") or "",
            "birthDay": vals["fields"].get("birthdate_day") or "",
            "highDebt": vals["fields"].get("highDebt") or "No",
        }
        n = 0
        fl = page.frame_locator("iframe")
        names = self._visible_names(fr)
        try:
            q = (fr.evaluate("() => (document.body && document.body.innerText) || ''") or "").lower()
        except Exception:
            q = ""
        for name in ("h_ssn3", "ssn3"):
            for loc in (
                fl.locator(f'[name="{name}"]').first,
                fr.locator(f'[name="{name}"]').first,
            ):
                try:
                    if loc.count() == 0:
                        continue
                    if not loc.is_visible():
                        continue
                    loc.fill(payload.get("ssn4") or "", timeout=1500)
                    n += 1
                    break
                except Exception:
                    continue
        phone = re.sub(r"\D", "", payload.get("phone") or "")
        if len(phone) >= 10 and ("phone" in q or "cell" in q):
            parts = [phone[:3], phone[3:6], phone[6:10]]
            for name, part in (
                ("home_phone1", parts[0]),
                ("home_phone2", parts[1]),
                ("home_phone3", parts[2]),
            ):
                filled_part = False
                for loc in (
                    fl.locator(f'[name="{name}"]').first,
                    fr.locator(f'[name="{name}"]').first,
                ):
                    try:
                        if loc.count() == 0:
                            continue
                        try:
                            if (loc.input_value() or "").strip() == part:
                                n += 1
                                filled_part = True
                                break
                        except Exception:
                            pass
                        loc.fill(part, timeout=1500, force=True)
                        n += 1
                        filled_part = True
                        break
                    except Exception:
                        continue
                if filled_part:
                    continue
            if n < 3:
                for loc_root in (fl, None):
                    try:
                        tels = (loc_root.locator('input[type="tel"]') if loc_root is not None
                                else fr.locator('input[type="tel"]'))
                        shown = 0
                        for i in range(tels.count()):
                            if shown >= 3:
                                break
                            box = tels.nth(i)
                            try:
                                if not box.is_visible():
                                    continue
                            except Exception:
                                continue
                            box.fill(parts[shown], timeout=1500)
                            shown += 1
                            n += 1
                        if shown:
                            break
                    except Exception:
                        continue
        for label, key, kind, need in (
            ("First name", "firstName", "text", "name"),
            ("Last name", "lastName", "text", "name"),
            ("Email", "email", "text", "email"),
            ("Zip Code", "zip", "text", "zip"),
            ("Zip", "zip", "text", "zip"),
            ("Last 4 digits of SSN", "ssn4", "text", "ssn"),
            ("Birth Year", "birthYear", "select", "year"),
            ("Year", "birthYear", "select", "year"),
            ("Month", "birthMonth", "select", "month"),
            ("Day", "birthDay", "select", "day"),
            ("Do you have $10,000", "highDebt", "select", "debt"),
        ):
            if need == "name" and "name" not in q:
                continue
            if need == "email" and "email" not in q:
                continue
            if need == "zip" and "zip" not in q and "basic information" not in q:
                if not any(n in names for n in ("h_zip", "zip")):
                    continue
            if need == "ssn" and "ssn" not in q and "social" not in q:
                if not any(n in names for n in ("h_ssn3", "ssn3", "ssn1")):
                    continue
            if need in ("year", "month", "day") and "birth" not in q and "birthday" not in q:
                if not any(
                    n in names
                    for n in ("h_birthdate_year", "birthdate_year", "birthdate_month", "birthdate_day")
                ):
                    continue
            if need == "debt" and "debt" not in q:
                continue
            val = payload.get(key) or ""
            if not val:
                continue
            filled_one = False
            for loc in (
                fl.get_by_label(re.compile(label, re.I)).first,
                fr.get_by_label(re.compile(label, re.I)).first,
            ):
                try:
                    if loc.count() == 0:
                        continue
                    if kind == "select":
                        try:
                            loc.select_option(label=str(val), timeout=1500)
                        except Exception:
                            loc.select_option(value=str(val), timeout=1500)
                    else:
                        loc.fill(val, timeout=1500)
                    n += 1
                    filled_one = True
                    break
                except Exception:
                    continue
            if filled_one:
                continue
        try:
            js_n = int(fr.evaluate(
                r"""(V) => {
                    const vis = e => {
                        const r = e.getBoundingClientRect();
                        const st = getComputedStyle(e);
                        return r.width > 8 && r.height > 8
                            && st.visibility !== 'hidden' && st.display !== 'none'
                            && st.opacity !== '0';
                    };
                    const lab = e => {
                        let s = ((e.labels && e.labels[0] && e.labels[0].innerText) || '')
                            + ' ' + (e.placeholder || '') + ' ' + (e.name || '')
                            + ' ' + (e.id || '') + ' ' + (e.getAttribute('aria-label') || '');
                        const prev = e.previousElementSibling;
                        if (prev && (prev.innerText || '').length < 40)
                            s += ' ' + (prev.innerText || '');
                        return s.toLowerCase();
                    };
                    const setV = (el, val) => {
                        el.focus();
                        const proto = el.tagName === 'SELECT' ? HTMLSelectElement.prototype
                            : HTMLInputElement.prototype;
                        const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                        if (desc && desc.set) desc.set.call(el, val); else el.value = val;
                        ['input','change','blur'].forEach(ev =>
                            el.dispatchEvent(new Event(ev, {bubbles: true})));
                    };
                    let n = 0;
                    const body = (document.body.innerText || '').toLowerCase();
                    if (V.ssn4 && /xxx-xx|last 4/.test(body)) {
                        const inp = Array.from(document.querySelectorAll('input')).find(e =>
                            vis(e) && e.type !== 'checkbox' && e.type !== 'radio'
                            && e.type !== 'hidden' && (e.value || '').length <= 4
                            && e.getBoundingClientRect().width < 140);
                        if (inp) { setV(inp, V.ssn4); n++; }
                    }
                    if (V.highDebt && /credit card debt/.test(body)) {
                        const sel = Array.from(document.querySelectorAll('select')).find(e => vis(e)
                            && /debt|10,000|-select-/.test(((e.labels && e.labels[0] && e.labels[0].innerText) || '') + e.options[0].text));
                        if (sel) {
                            const opt = Array.from(sel.options).find(o =>
                                (o.text || '').toLowerCase() === V.highDebt.toLowerCase());
                            if (opt) { sel.value = opt.value;
                                sel.dispatchEvent(new Event('change', {bubbles: true})); n++; }
                        }
                    }
                    document.querySelectorAll('input,select,textarea').forEach(e => {
                        if (!vis(e) || e.disabled || e.type === 'hidden'
                            || e.type === 'checkbox' || e.type === 'radio') return;
                        // Split boxes are addressed by exact name first, so a
                        // three-part phone/SSN never receives the whole value.
                        const nm = (e.name || e.id || '').replace(/^h_/, '');
                        if ((V.seg || {}).hasOwnProperty(nm)) {
                            const seg = V.seg[nm];
                            if (seg && e.value !== seg) { setV(e, seg); n++; }
                            return;
                        }
                        const k = lab(e);
                        let v = '';
                        if (/first/.test(k) && !/last/.test(k)) v = V.firstName;
                        else if (/last/.test(k)) v = V.lastName;
                        else if (/email/.test(k)) v = V.email;
                        else if (/zip/.test(k)) v = V.zip;
                        else if (/address|street/.test(k)) v = V.address;
                        else if (/employer|company/.test(k)) v = V.employer;
                        else if (/occupat|job title/.test(k)) v = V.occupation;
                        else if (/ssn|social/.test(k)) v = V.ssn4 || V.ssn;
                        else if (/birth/.test(k) && /year/.test(k) || (k.includes('year') && V.birthYear && e.tagName==='SELECT' && e.options.length > 50)) v = V.birthYear;
                        else if (/month/.test(k) && e.tagName==='SELECT') v = V.birthMonth;
                        else if (/^day\b/.test(k) && e.tagName==='SELECT') v = V.birthDay;
                        else if (/phone|cell/.test(k)) v = V.phone;
                        if (!v) return;
                        if (e.tagName === 'SELECT') {
                            const opt = Array.from(e.options).find(o =>
                                (o.text || '').toLowerCase().includes(v.toLowerCase())
                                || o.value === v);
                            if (opt) { e.value = opt.value;
                                e.dispatchEvent(new Event('change', {bubbles: true})); n++; }
                            return;
                        }
                        setV(e, v); n++;
                    });
                    return n;
                }""",
                payload,
            ) or 0)
            return n + js_n
        except Exception as e:
            log.warning("form.rs_visible_js", error=str(e)[:80])
        for frame in page.frames:
            if frame == fr:
                continue
            try:
                extra = int(frame.evaluate(
                    r"""(V) => {
                        const vis = e => {
                            const r = e.getBoundingClientRect();
                            const st = getComputedStyle(e);
                            return r.width > 8 && r.height > 8
                                && st.visibility !== 'hidden' && st.display !== 'none';
                        };
                        const setV = (el, val) => {
                            el.focus();
                            const proto = HTMLInputElement.prototype;
                            const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                            if (desc && desc.set) desc.set.call(el, val); else el.value = val;
                            ['input','change','blur'].forEach(ev =>
                                el.dispatchEvent(new Event(ev, {bubbles: true})));
                        };
                        const body = (document.body.innerText || '').toLowerCase();
                        let n = 0;
                        if (V.ssn4 && /xxx-xx|last 4/.test(body)) {
                            const inp = Array.from(document.querySelectorAll('input')).find(e =>
                                vis(e) && e.type !== 'checkbox' && e.type !== 'hidden'
                                && (e.value || '').length <= 4
                                && e.getBoundingClientRect().width < 140);
                            if (inp) { setV(inp, V.ssn4); n++; }
                        }
                        return n;
                    }""",
                    payload,
                ) or 0)
                n += extra
            except Exception:
                continue
        return n

    def _fill_visible(self, fr: Frame, vals: dict) -> int:
        n = 0
        for name, value in vals["fields"].items():
            if not value:
                continue
            loc = fr.locator(f'[name="{name}"]').first
            try:
                if loc.count() == 0:
                    continue
                typ = (loc.get_attribute("type") or "").lower()
                if typ == "hidden":
                    continue
            except Exception:
                continue
            try:
                visible = loc.is_visible()
            except Exception:
                visible = False
            try:
                tag = (loc.evaluate("e => e.tagName") or "").upper()
                if tag == "SELECT":
                    try:
                        loc.select_option(label=str(value), timeout=2500)
                    except Exception:
                        loc.select_option(value=str(value), timeout=2500)
                    n += 1
                    continue
                if typ == "checkbox":
                    if str(value).lower() in ("1", "true", "yes"):
                        if not loc.is_checked():
                            loc.check(force=True)
                        n += 1
                    continue
                if visible:
                    loc.click(timeout=2000)
                    loc.fill("")
                    loc.press_sequentially(str(value), delay=self._key_delay())
                else:
                    loc.evaluate(
                        """(e, v) => {
                            e.value = v;
                            ['input','change','blur'].forEach(ev =>
                                e.dispatchEvent(new Event(ev, {bubbles: true})));
                        }""",
                        str(value),
                    )
                n += 1
                time.sleep(0.08)
            except Exception as e:
                log.warning("form.rs_field_failed", field=name, error=str(e)[:80])
        return n

    def _check_terms(self, page: Page, fr: Frame) -> None:
        for loc in (
            page.frame_locator("iframe").locator("#termsField, [name='termsField']"),
            fr.locator("[name='termsField'], #termsField"),
            page.frame_locator("iframe").get_by_role("checkbox"),
        ):
            try:
                box = loc.first
                if box.count() == 0:
                    continue
                box.check(force=True, timeout=1500)
                return
            except Exception:
                try:
                    loc.first.click(force=True, timeout=1500)
                    return
                except Exception:
                    continue

    def _click_continue(self, page: Page, fr: Frame | None) -> str:
        for frame in page.frames:
            try:
                hit = frame.evaluate(
                    r"""() => {
                        const t = e => (e.innerText || e.value || '').replace(/\s+/g,' ').trim();
                        const vis = e => {
                            const r = e.getBoundingClientRect();
                            const st = getComputedStyle(e);
                            return r.width > 20 && r.height > 20
                                && st.visibility !== 'hidden'
                                && st.display !== 'none'
                                && Number(st.opacity) !== 0;
                        };
                        const el = Array.from(document.querySelectorAll(
                            'a,button,input[type=button],input[type=submit]'
                        )).find(e => vis(e) && /continue|submit|next|request cash|get cash|see offers/i.test(t(e))
                            && t(e).length < 32 && !/prev|back|spanish|español/i.test(t(e)));
                        if (!el) return '';
                        el.click();
                        return t(el);
                    }"""
                )
                if hit:
                    return str(hit)
            except Exception:
                continue
        fl = page.frame_locator("iframe")
        candidates = [
            fl.get_by_text("Continue", exact=True),
            fl.get_by_role("button", name=re.compile(r"^continue$", re.I)),
        ]
        if fr is not None:
            candidates.extend([
                fr.get_by_text("Continue", exact=True),
                fr.get_by_role("button", name=re.compile(r"^continue$", re.I)),
            ])
        for loc in candidates:
            try:
                box = loc.first
                if box.count() == 0:
                    continue
                if not box.is_visible():
                    continue
                box.scroll_into_view_if_needed(timeout=1000)
                box.click(timeout=2000)
                return "Continue"
            except Exception:
                continue
        return ""

    def _click_next(self, fr: Frame) -> str:
        try:
            return str(fr.evaluate(_CLICK_NEXT_JS) or "")
        except Exception:
            return ""

    # ---------------------------------------------------------------- mapping

    def _rs_values(self, f: dict) -> dict:
        raw = getattr(self, "_raw_row", {}) or {}
        phone = _digits(f.get("phone") or "")
        work = _digits(f.get("employer_phone") or phone)
        ssn = _digits(f.get("ssn") or "")
        dob = f.get("dob") or ""
        mm = dd = yyyy = ""
        m = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", dob)
        if m:
            mm, dd, yyyy = m.group(1), m.group(2), m.group(3)
        payday = f.get("next_payday") or ""
        pmm = pdd = ""
        pm = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", payday)
        if pm:
            pmm, pdd = pm.group(1), pm.group(2)

        income = f.get("monthly_income") or 3000
        try:
            income = int(income)
        except (TypeError, ValueError):
            income = 3000
        loan = int(f.get("loan_amount") or 1000)
        amount_label = _AMOUNT_OPTIONS[-1][1]
        for cap, lab in _AMOUNT_OPTIONS:
            if loan <= cap:
                amount_label = lab
                break
        income_label = _INCOME_OPTIONS[-1][1]
        for cap, lab in _INCOME_OPTIONS:
            if income <= cap:
                income_label = lab
                break
        retry_label = _RETRY_AMOUNT_OPTIONS[-1][1]
        for cap, lab in _RETRY_AMOUNT_OPTIONS:
            if loan <= cap:
                retry_label = lab
                break

        occ = self._raw_val(raw, "Occupation") or "Employee"
        homeowner = str(f.get("is_homeowner") or "0") == "1" or self._raw_val(
            raw, "Homeowner"
        ).lower() in ("yes", "own", "1")
        car = self._raw_val(raw, "Own a Car").lower() in ("yes", "1", "true")
        debt_raw = self._raw_val(raw, "Credit Card Debt", "Debt Amount")
        try:
            debt = int(float(re.sub(r"[,$\s]", "", debt_raw) or "0"))
        except (TypeError, ValueError):
            debt = 0

        last4 = (ssn[-4:] if len(ssn) >= 4 else ssn[5:9]) or "3729"

        fields = {
            "requestedLoanAmount": amount_label,
            "h_requestedLoanAmount": amount_label,
            "firstName": f.get("first_name") or "",
            "lastName": f.get("last_name") or "",
            "email": f.get("email") or "",
            "home_phone1": phone[:3],
            "home_phone2": phone[3:6],
            "home_phone3": phone[6:10],
            "birthdate_month": _MONTHS[int(mm) - 1] if mm.isdigit() and 1 <= int(mm) <= 12 else "",
            "birthdate_day": str(int(dd)) if dd.isdigit() else "",
            "birthdate_year": yyyy,
            "activeMilitary": "Yes" if str(f.get("is_military")) == "1" else "No",
            "address": f.get("street_address") or "",
            "zip": f.get("zip") or "",
            "monthsAtResidence": self._years_opt(
                self._raw_val(raw, "Years at Address", "Months at Address")
            ),
            "housing": "Yes" if homeowner else "No",
            "hasCarTitle": "Yes" if car else "No",
            "incomeType": self._income_opt(self._raw_val(raw, "Income Source")),
            "monthsEmployed": self._years_opt(
                self._raw_val(raw, "Years at Employer", "Months at Employer")
            ),
            "payPeriod": self._pay_opt(self._raw_val(raw, "Pay Frequency")),
            "monthlyIncome": income_label,
            "payMonth": _MONTHS[int(pmm) - 1] if pmm.isdigit() and 1 <= int(pmm) <= 12 else "",
            "payDay1": str(int(pdd)) if pdd.isdigit() else "",
            "employer": f.get("employer_name") or "Employer",
            "occupation": occ,
            "work_phone1": work[:3],
            "work_phone2": work[3:6],
            "work_phone3": work[6:10],
            "drivingLicenseNumber": f.get("dl_number") or "",
            "drivingLicenseState": f.get("dl_state") or f.get("state") or "",
            "ssn1": ssn[:3],
            "ssn2": ssn[3:5],
            "ssn3": last4,
            "routingNumber": f.get("routing_number") or "",
            "accountNumber": f.get("account_number") or "",
            "bankName": f.get("bank_name") or "",
            "directDeposit": "Direct Deposit" if str(f.get("is_direct_deposit") or "1") != "0" else "Paper Check",
            "monthsWithBank": self._years_opt(
                self._raw_val(raw, "Years at Bank", "Months at Bank")
            ),
            "bankAccountType": "Savings" if str(f.get("account_type")) == "2" else "Checking",
            "creditScore": self._credit_opt(self._raw_val(raw, "Credit Score Rating")),
            "loanPurpose": self._purpose_opt(self._raw_val(raw, "Loan Purpose")),
            "highDebt": "Yes" if debt >= 2500 else "No",
            # page28 — widen the search rather than dead-end the lead.
            "retrySubmit": "Yes",
            "termsField": "true",
            # page41 — short-term fallback re-price select.
            "rp_requestedLoanAmount": retry_label,
            # page40 — returning-applicant express path.
            "h_special_ssn3": last4,
            "h_specialRequestedLoanAmount": amount_label,
            "h_highDebt": "Yes" if debt >= 2500 else "No",
            "h_termsField": "true",
            "h_loanPurpose": self._purpose_opt(self._raw_val(raw, "Loan Purpose")),
            "h_creditScore": self._credit_opt(self._raw_val(raw, "Credit Score Rating")),
            "h_hasCarTitle": "Yes" if car else "No",
            "h_retrySubmit": "Yes",
            "h_firstName": f.get("first_name") or "",
            "h_lastName": f.get("last_name") or "",
            "h_zip": f.get("zip") or "",
            "h_ssn1": ssn[:3],
            "h_ssn2": ssn[3:5],
            "h_ssn3": last4,
            "ssn": ssn,
            "h_birthdate_month": _MONTHS[int(mm) - 1] if mm.isdigit() and 1 <= int(mm) <= 12 else "",
            "h_birthdate_day": str(int(dd)) if dd.isdigit() else "",
            "h_birthdate_year": yyyy,
            "h_email": f.get("email") or "",
            "h_address": f.get("street_address") or "",
            "h_home_phone1": phone[:3],
            "h_home_phone2": phone[3:6],
            "h_home_phone3": phone[6:10],
        }
        aliased = dict(fields)
        for key, val in list(fields.items()):
            if not val:
                continue
            if key.startswith("h_"):
                aliased.setdefault(key[2:], val)
            else:
                aliased.setdefault("h_" + key, val)
        return {"amount_label": amount_label, "fields": aliased}

    def _raw_val(self, raw: dict, *keys: str) -> str:
        norm = {}
        for k, v in (raw or {}).items():
            nk = re.sub(r"\s+", " ", str(k)).strip().lower()
            if nk not in norm or str(v or "").strip():
                norm[nk] = v
        for k in keys:
            v = str(norm.get(re.sub(r"\s+", " ", k).strip().lower()) or "").strip()
            if v:
                return v
        return ""

    def _years_opt(self, raw: str) -> str:
        s = (raw or "").lower()
        nums = [int(x) for x in re.findall(r"\d+", s)]
        n = nums[0] if nums else 2
        if "month" in s and "year" not in s:
            n = 1 if n <= 12 else 2
        if n <= 1 or "or less" in s:
            return "1 year or less"
        if n == 2:
            return "2 years"
        if n == 3:
            return "3 years"
        return "More than 4 years"

    def _income_opt(self, raw: str) -> str:
        s = (raw or "").lower()
        if "benefit" in s or "disab" in s or "social" in s:
            return "Benefits"
        if "self" in s:
            return "Self-Employed"
        return "Employed"

    def _pay_opt(self, raw: str) -> str:
        s = (raw or "").lower()
        if "week" in s and ("bi" in s or "every 2" in s or "every two" in s):
            return "Every 2 Weeks"
        if "semi" in s or "twice" in s:
            return "Twice a Month"
        if "week" in s:
            return "Weekly"
        if "month" in s:
            return "Monthly"
        return "Every 2 Weeks"

    def _credit_opt(self, raw: str) -> str:
        s = (raw or "").lower()
        if s.startswith("excellent") or s.startswith("great"):
            return "Excellent (720+)"
        if s.startswith("good"):
            return "Good (660-719)"
        if s.startswith("poor"):
            return "Poor (< 600)"
        return "Fair (600-659)"

    def _purpose_opt(self, raw: str) -> str:
        s = (raw or "").lower()
        options = (
            "Need Cash", "Debt Settlement", "Debt Consolidation",
            "IRS Tax Debt Settlement", "Auto/Car Related", "Credit Card",
            "Education", "Home Improvement", "Medical", "Relocation",
            "Renewable Energy", "Small Business", "Travel", "Wedding",
        )
        for o in options:
            if o.lower() in s or s in o.lower():
                return o
        if "card" in s:
            return "Credit Card"
        if "debt" in s or "consol" in s:
            return "Debt Consolidation"
        return "Need Cash"
