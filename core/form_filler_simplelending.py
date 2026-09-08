"""
core/form_filler_simplelending.py — simplelendingdirect.com multi-step form
automation.

Same lead-platform family as ExaBucks and SimaCash (identical wizard
copy — "What Is Your Email Address? We use your email to send request
updates and connect you with lenders" is verbatim shared text), skinned
with an "ef-" component library
(ef-btn, ef-nav__btn--next, ef-input-wrapper, …).

Live-confirmed step order (landing -> wizard), walked end-to-end against the
real site using this module's own dispatch code (stopping short of actually
submitting SSN/bank details, to avoid completing a real submission during
development):
  [landing]   loan-amount chips ($500/$1,000/$2,000/$3,000) -- picking one
              enters the wizard.
  email       input[name=email]
  name        input[name=firstName] + input[name=lastName]
  phone       input[name=cellPhone]
  military    Yes/No chips, no <input> ("Are You Active Military?")
  address     input[name=address] + input[name=zip] (home address, one step)
  homeowner   Yes/No chips ("Are You a Home Owner?")
  dob         input[name=dob]
  income src  chips: Employment / Benefits / Self-Employed / Unemployed
  pay freq    chips: Semimonthly / Biweekly / Weekly / Monthly
  next payday calendar grid (`.ef-calendar__day-btn`) -- see `_pick_payday`
  gross inc   dollar-bracket chips ("$3,001 - $5,000", "$8,500 or more", …)
  employer    input[name=employerName]
  DL / ID     input[name=driverLicense] + select[name=driverLicenseState]
  credit score chips: "Excellent Credit (720+)" … "Not Sure"
  loan purpose chips: Debt Consolidation / Credit Card Consolidation / Other
  debt gate   dollar-bracket chips incl. a "No debt" zero-option
  car title   Yes/No chip, no sheet column -- defaults to "No"
  direct dep. Yes/No chip ("Do You Get Paid by Direct Deposit?")
  account type chips: Checking / Savings

  Not walked live (same input mechanism as every text step above, already
  proven): SSN, routing number, account number, bank name.

A "Welcome Back, <name>!" screen can interrupt at any point once a phone
number the platform has already seen is entered (returning-applicant
recognition); "Continue filling full form" bypasses it.

Anything this filler doesn't recognise raises a clear ``unhandled_step`` /
``field_rejected`` error (with a screenshot), so a real run's log pinpoints
exactly what to extend rather than silently guessing. See
`_handle_text_step` / `_pick_choice` below.

Completion routes into the shared lender-match flow (`_handle_post_offer`
in core/lead_platform.py) — the same "Continue" chase across popups / new
tabs / in-place navigation used by ExaBucks and SimaCash. When that flow
lands on 247LendingGroup.com, the second apply form is filled and submitted
from the same sheet row (`core/form_filler_247lending.py`).
"""
from __future__ import annotations

import re
import time
from datetime import date, timedelta

import structlog
from playwright.sync_api import Page

from core.lead_platform import BasePlatformFiller, FormFillerError

log = structlog.get_logger(__name__)

__all__ = ["FormFiller", "FormFillerError"]

_LOAN_CHIPS = (500, 1000, 2000, 3000)

# (regex over name+id+placeholder+label, fields-dict key) -- first match wins.
_TEXT_FIELD_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r"email", re.I), "email"),
    (re.compile(r"first.?name", re.I), "first_name"),
    (re.compile(r"last.?name", re.I), "last_name"),
    (re.compile(r"\bssn\b|social.?sec", re.I), "ssn"),
    (re.compile(r"phone|cell", re.I), "phone"),
    (re.compile(r"\bdob\b|birth.?date|date.?of.?birth", re.I), "dob"),
    (re.compile(r"street|\baddress\b", re.I), "street_address"),
    (re.compile(r"\bzip\b|postal", re.I), "zip"),
    (re.compile(r"\bcity\b", re.I), "city"),
    (re.compile(r"employer", re.I), "employer_name"),
    (re.compile(r"licen.*state|driver.*state", re.I), "dl_state"),  # before the bare "licen" rule
    (re.compile(r"licen", re.I), "dl_number"),
    (re.compile(r"\bstate\b", re.I), "state"),  # home-state select, if the site ever asks separately
    (re.compile(r"rout|\baba\b", re.I), "routing_number"),
    (re.compile(r"bank.?name", re.I), "bank_name"),
    (re.compile(r"account.?type", re.I), "account_type"),
    (re.compile(r"account", re.I), "account_number"),
    (re.compile(r"next.?pay|pay.?date|payday", re.I), "next_payday"),
    (re.compile(r"loan.?amount|amount", re.I), "loan_amount"),
]

# Step headings that are a plain Yes/No chip choice -> fields-dict key.
_YESNO_HEADING_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r"military", re.I), "is_military"),
    (re.compile(r"home\s*owner", re.I), "is_homeowner"),
    (re.compile(r"direct\s*deposit", re.I), "is_direct_deposit"),
]

# Step headings that are a category chip choice -> raw sheet column(s) to
# match chip text against (first non-empty wins).
_CATEGORY_HEADING_MAP: list[tuple[re.Pattern, tuple[str, ...]]] = [
    (re.compile(r"income\s*source", re.I), ("Income Source",)),
    (re.compile(r"pay\s*frequency|how often", re.I), ("Pay Frequency",)),
    (re.compile(r"credit\s*score", re.I), ("Credit Score Rating",)),
    (re.compile(r"loan\s*purpose|purpose of|loan for|loan.*used for", re.I), ("Loan Purpose",)),
    (re.compile(r"account\s*type", re.I), ("Account Type",)),
]

# Whole-word synonyms applied to the raw sheet text before matching, so real
# lead data ("POOR" credit) lines up with a differently-worded chip ("Bad
# Credit (639 or less)").
_CATEGORY_SYNONYMS = {
    "poor": "bad", "terrible": "bad", "awful": "bad", "below": "bad",
    "great": "excellent", "employed": "employment", "employment": "employed",
}

# Sheet wording -> form chip text for "How Often You Get Paid?"
_PAY_FREQ_ALIASES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"bi[- ]?week|every\s*(2|two)\s*weeks?", re.I), "Biweekly"),
    (re.compile(r"semi[- ]?month|twice\s*(a|/)?\s*month|twice\s*monthly", re.I), "Semimonthly"),
    (re.compile(r"week", re.I), "Weekly"),
    (re.compile(r"month", re.I), "Monthly"),
]

# A button matching one of these is the platform's own designated catch-all
# for "none of the specific options fit" -- safe to use as a last resort
# when nothing else matches, since it's not guessing at meaning, it's using
# the escape hatch the site itself offers.
_CATCHALL_BUTTON_RE = re.compile(r"^other$|^not\s*sure$|^none(\s+of\s+the\s+above)?$|^n/a$|^prefer\s*not", re.I)

# Step headings that are a tenure chip choice ("Years at ...") -> raw sheet
# column holding a year count.
_TENURE_HEADING_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r"years?\s*at\s*(your\s*)?address|how long.*address", re.I), "Years at Address"),
    (re.compile(r"years?\s*at\s*(your\s*)?(job|employer)|how long.*employ", re.I), "Years at Employer"),
    (re.compile(r"years?\s*at\s*(your\s*)?bank|how long.*bank", re.I), "Years at Bank"),
]

# Step headings that are a dollar-range chip choice ("$1,501 - $3,000", "$8,500
# or more", "Less than - $1,500") -> raw sheet column holding a dollar figure.
_DOLLAR_BRACKET_HEADING_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r"gross income|monthly income|net income", re.I), "Monthly Net Income ($)"),
    (re.compile(r"credit card debt|how much.*debt|debt amount|unsecured debt|in debt", re.I), "Credit Card Debt"),
]

_FIELD_JS = """() => {
    const vis = e => e.offsetParent !== null && e.getClientRects().length > 0;
    return Array.from(document.querySelectorAll('input,select,textarea')).filter(vis).map(e => {
        const lbl = e.id ? document.querySelector('label[for="' + e.id + '"]') : null;
        return {
            name: e.name || e.id || '',
            tag: e.tagName,
            type: e.type || '',
            key: ((e.name||'') + ' ' + (e.id||'') + ' ' + (e.placeholder||'') + ' ' + (lbl ? lbl.innerText : '')).toLowerCase(),
        };
    }).filter(f => f.name);
}"""

_BUTTONS_JS = """() => {
    const vis = e => e.offsetParent !== null && e.getClientRects().length > 0;
    const t = e => (e.innerText || e.value || '').replace(/\\s+/g, ' ').trim();
    const no = /^back$|^next$|terms of use|privacy policy|credit authorization/i;
    return Array.from(document.querySelectorAll('button,[role=button]'))
        .filter(vis).filter(e => !e.disabled)
        .map(t).filter(s => s && s.length < 60 && !no.test(s));
}"""

_HEADING_JS = """() => {
    const vis = e => e.offsetParent !== null && e.getClientRects().length > 0;
    const main = document.querySelector('.ef-title-main');
    if (main && vis(main)) return main.innerText.trim();
    const el = Array.from(document.querySelectorAll('.ef-title,[class*=question]')).filter(vis)[0];
    return el ? el.innerText.trim() : '';
}"""


class FormFiller(BasePlatformFiller):
    """simplelendingdirect.com — 'ef-' component wizard, same platform family
    as ExaBucks and SimaCash. Congratulations / submitted = Success; no 247 wait."""

    default_url = "https://simplelendingdirect.com/"

    # The bank-details step's submit button reads "Request Cash" (per-site
    # copy), which the shared _JS_CLICK_CONTINUE in lead_platform.py doesn't
    # recognise as a CTA -- override with "request" added so
    # _handle_post_offer's CTA finder can locate and click it (then chase the
    # resulting popup/new-tab/in-place navigation).
    _JS_CLICK_CONTINUE = r"""(dryRun) => {
        const vis = e => e.offsetParent !== null && e.getClientRects().length > 0;
        const t = e => (e.innerText || e.value || '').replace(/\s+/g, ' ').trim();
        const go = /^(continue|next|accept|agree|submit|proceed|confirm|finish|get started|start here|start now|start|see my|see offers?|see if|view|view my|view offer|view details|get my|get offer|get started now|claim|redeem|show me|complete|i agree|apply now|apply|yes\b.*|accept.*offer|get.*offer|see.*offer|request.*cash|request.*fund|request.*loan|request.*now|request)/i;
        const no = /(back|cancel|decline|no thanks|edit|previous|return to|log ?in|sign ?in|sign ?up|español|terms|disclosure|privacy|conditions)/i;
        const b = Array.from(document.querySelectorAll(
                'button,input[type=submit],input[type=button],[role=button],a.btn,a.button,a'))
            .filter(vis).filter(e => !e.disabled)
            .find(e => { const s = t(e); return s && s.length < 40 && go.test(s) && !no.test(s); });
        if (b) { if (!dryRun) b.click(); return t(b).slice(0, 40); }
        return '';
    }"""

    def _parse_fields(self, row: dict) -> dict:
        # Stash the raw sheet row so choice-button steps (Income Source, Pay
        # Frequency, Credit Score, Loan Purpose, Account Type, tenure) can
        # match chip text against the sheet's own human-readable values —
        # the parsed dict only carries platform option *codes*, not text.
        self._raw_row = row
        return super()._parse_fields(row)

    # --------------------------------------------------------------- flow

    def _fill_form(self, page: Page, f: dict, row_number: int, stop_event) -> str:
        if not self._is_welcome_back_review(page):
            self._pick_loan_amount_chip(page, f, row_number)

        seen: dict[str, int] = {}
        for step_num in range(self._max_steps):
            self._check_stop(stop_event)
            self._bypass_returning_applicant(page, row_number)
            if self._is_congratulations(page):
                log.info("form.congratulations", row=row_number)
                break
            if self._complete_welcome_back_review(page, row_number):
                break

            names = self._visible_fields(page)
            heading = self._heading(page)
            sig = ",".join(n["name"] for n in names) or heading
            seen[sig] = seen.get(sig, 0) + 1
            if seen[sig] > 3:
                self._screenshot(page, row_number, f"stuck_{step_num}")
                raise FormFillerError(
                    f"Form stopped advancing at step {step_num} ('{heading}')",
                    error_type="stuck",
                )

            self._live(page)

            if names:
                log.info("form.step", step=step_num, fields=sig[:70], row=row_number)
                self._read_pause()
                res = self._handle_text_step(page, names, f)
                if not res["known"]:
                    self._screenshot(page, row_number, f"unhandled_{step_num}")
                    raise FormFillerError(
                        f"Unrecognised step {step_num} ('{heading}') — fields "
                        f"this filler does not map: {sig}",
                        error_type="unhandled_step",
                    )
                if not res["filled"]:
                    self._screenshot(page, row_number, f"unfilled_{step_num}")
                    raise FormFillerError(
                        f"Could not set any field on step {step_num} ('{heading}'): "
                        f"{res['failed']}",
                        error_type="field_rejected",
                    )
                self._action_pause()
                err = self._visible_field_error(page)
                if err:
                    self._screenshot(page, row_number, f"invalid_{step_num}")
                    raise FormFillerError(
                        f"Field rejected on step {step_num} ('{heading}'): {err}",
                        error_type="field_rejected",
                    )
                if not self._click_next(page):
                    # Final wizard step (SSN / "Request Cash") — click that CTA
                    # but only leave the wizard if the step actually advanced.
                    self._click_final_cta(page)
                    self._await_change(page, sig, timeout=10.0)
                    still = self._heading(page)
                    err = self._visible_field_error(page)
                    if err or (still and still == heading):
                        self._screenshot(page, row_number, f"final_stuck_{step_num}")
                        raise FormFillerError(
                            f"Final step did not advance ('{still or heading}'): "
                            f"{err or 'CTA click left the same question on screen'}",
                            error_type="field_rejected",
                        )
                    break
                self._await_change(page, sig)
                continue

            if re.search(r"next pay ?date|when is your next pay", heading, re.I):
                self._read_pause()
                self._set_payday_from_sheet(page, row_number)
                self._action_pause()
                self._click_next(page)
                self._await_change(page, sig)
                continue

            # No <input> on this step: either a chip-choice screen, or we've
            # already arrived at the post-application lender-match page.
            buttons = self._choice_buttons(page)
            if self._is_congratulations(page):
                log.info("form.congratulations", row=row_number)
                break
            if not buttons:
                post = page.evaluate(self._JS_POST_STATE)
                if post.get("processing") or post.get("buttons"):
                    break
                self._screenshot(page, row_number, f"empty_{step_num}")
                raise FormFillerError(
                    f"Step {step_num} ('{heading}') has no fields and no "
                    f"buttons to act on.", error_type="stuck",
                )

            log.info("form.choice_step", step=step_num, heading=heading[:60],
                     options=buttons[:6], row=row_number)
            self._read_pause()
            target = self._pick_choice(heading, buttons, f)
            if not target:
                self._screenshot(page, row_number, f"unhandled_choice_{step_num}")
                raise FormFillerError(
                    f"Unrecognised choice step {step_num} ('{heading}'), "
                    f"options: {buttons}", error_type="unhandled_step",
                )
            self._action_pause()
            self._click_button_text(page, target)
            self._await_change(page, sig)
        else:
            raise FormFillerError(
                f"Form did not complete within {self._max_steps} steps", error_type="timeout"
            )

        self._handle_post_offer(page, f, row_number, stop_event)
        return "submitted"

    # ------------------------------------------------------------- landing

    def _pick_loan_amount_chip(self, page: Page, f: dict, row_number: int) -> None:
        wanted = int(f.get("loan_amount") or 1000)
        nearest = min(_LOAN_CHIPS, key=lambda c: abs(c - wanted))
        labels = [f"${nearest:,}", f"${nearest}", f"${nearest:,.0f}"]
        fr = self._form_frame(page)
        last_err = ""
        for root in (fr, page):
            for label in labels:
                try:
                    loc = root.locator("button.ef-btn, button, [role=button]", has_text=label).first
                    loc.wait_for(state="visible", timeout=8000)
                    loc.click(force=True)
                    log.info("form.loan_amount", chip=label, row=row_number)
                    time.sleep(1.5)
                    return
                except Exception as e:
                    last_err = str(e)[:100]
                    continue
        names = self._visible_fields(page)
        heading = self._heading(page)
        if names or (heading and not re.search(r"how much|loan amount|need\?", heading, re.I)):
            log.info("form.loan_amount_skipped", heading=heading[:60],
                     fields=[n["name"] for n in names][:6], row=row_number)
            return
        self._screenshot(page, row_number, "no_amount_chip")
        raise FormFillerError(
            f"Could not find loan-amount chip '{labels[0]}': {last_err}", error_type="stuck"
        )

    def _pick_payday(self, page: Page, row_number: int) -> None:
        """The next-payday step is a calendar grid, not a plain chip choice.
        Day numbers repeat (leading days borrowed from the previous month,
        trailing days borrowed from the next one carry the same 1-31 labels
        as the real month), so a plain text match is ambiguous. There's no
        date attribute to disambiguate on, so this falls back to the same
        layout convention every such grid uses: low day numbers (<=15) that
        are padding sit at the very end of the grid, high day numbers (>15)
        that are padding sit at the very start — so the *first* occurrence of
        a low target day and the *last* occurrence of a high one is the
        current month's real cell."""
        raw = self._sheet_payday()
        target_day = None
        if raw:
            # "MM/DD/YYYY" -> day is the second number.
            parts = re.findall(r"\d+", raw)
            if len(parts) >= 2:
                try:
                    target_day = int(parts[1])
                except ValueError:
                    target_day = None
        days: list[dict] = []
        deadline = time.time() + 8
        fr = self._form_frame(page)
        while time.time() < deadline and not days:
            try:
                days = fr.evaluate(
                    """() => Array.from(document.querySelectorAll(
                        '.ef-calendar__day-btn, [class*="calendar"] button, [role="gridcell"]'
                    )).filter(b => {
                        const st = getComputedStyle(b);
                        if (st.display === 'none' || st.visibility === 'hidden') return false;
                        const r = b.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    }).map(b => ({
                        text: b.innerText.trim(),
                        disabled: !!(b.disabled
                            || b.getAttribute('aria-disabled') === 'true'
                            || /disabled|outside|other-month|muted/i.test(b.className || ''))
                    }))"""
                ) or []
            except Exception:
                days = []
            if not days:
                time.sleep(0.5)
        if not days:
            self._screenshot(page, row_number, "no_calendar")
            raise FormFillerError("Next-payday calendar did not render", error_type="stuck")

        if target_day is None:
            # No usable sheet date -- take the first enabled day (soonest).
            idx = next((i for i, d in enumerate(days)
                        if not d["disabled"] and re.fullmatch(r"\d+( Today)?", d["text"])), None)
        else:
            matches = [i for i, d in enumerate(days)
                       if re.match(rf"^{target_day}(\s|$)", d["text"])]
            if not matches:
                idx = None
            elif target_day <= 15:
                idx = matches[0]
            else:
                idx = matches[-1]
            if idx is not None and days[idx]["disabled"]:
                enabled = [i for i in matches if not days[i]["disabled"]]
                idx = enabled[0] if enabled else None
        if idx is not None and days[idx]["disabled"]:
            idx = next((i for i, d in enumerate(days)
                        if not d["disabled"] and re.fullmatch(r"\d+( Today)?", d["text"])), None)

        if idx is None:
            self._screenshot(page, row_number, "payday_unmatched")
            raise FormFillerError(
                f"Could not match a next-payday calendar cell for day={target_day} "
                f"among {[d['text'] for d in days]}", error_type="unhandled_step",
            )

        try:
            loc = fr.locator(".ef-calendar__day-btn, [class*='calendar'] button, [role='gridcell']")
            loc.nth(idx).click()
        except Exception as e:
            self._screenshot(page, row_number, "payday_click_failed")
            raise FormFillerError(f"Could not click payday cell: {e}", error_type="stuck")
        log.info("form.payday_picked", day=target_day, cell=days[idx]["text"], row=row_number)
        time.sleep(1)

    # ------------------------------------------------------- returning user

    def _form_frame(self, page: Page):
        """The ef- wizard is sometimes in the main document, sometimes an iframe."""
        for fr in page.frames:
            try:
                if fr.is_detached():
                    continue
                if fr.evaluate(
                    """() => {
                        if (document.querySelector('#ef-container')) return true;
                        const b = (document.body && document.body.innerText) || '';
                        return /welcome back|next pay date|request cash|what is your email/i.test(b);
                    }"""
                ):
                    return fr
            except Exception:
                continue
        return page

    def _is_welcome_back_review(self, page: Page) -> bool:
        """Short returning-applicant screen: Welcome Back + Next Pay Date + Request Cash."""
        try:
            return bool(self._form_frame(page).evaluate(
                r"""() => {
                    const body = (document.body && document.body.innerText) || '';
                    if (!/welcome back/i.test(body)) return false;
                    return /next pay date|request cash|please choose a date from the calendar/i.test(body);
                }"""
            ))
        except Exception:
            return False

    def _calendar_is_open(self, page: Page) -> bool:
        """True only when a day grid is showing — not the trailing calendar icon."""
        try:
            return bool(self._form_frame(page).evaluate(
                r"""() => {
                    const vis = e => {
                        if (!e) return false;
                        const st = getComputedStyle(e);
                        if (st.display === 'none' || st.visibility === 'hidden' || Number(st.opacity) === 0)
                            return false;
                        const r = e.getBoundingClientRect();
                        return r.width > 8 && r.height > 8;
                    };
                    const days = Array.from(document.querySelectorAll(
                        '.ef-calendar__day-btn, [class*="calendar"] button, [class*="datepicker"] button, [role="gridcell"]'
                    )).filter(vis).filter(e => /^\d{1,2}(\s*Today)?$/.test(
                        (e.innerText || '').replace(/\s+/g, ' ').trim()
                    ));
                    return days.length >= 7;
                }"""
            ))
        except Exception:
            return False

    def _payday_value(self, page: Page) -> str:
        """Date actually committed into Next Pay Date — not the placeholder."""
        try:
            return (self._form_frame(page).evaluate(
                r"""() => {
                    const vis = e => {
                        if (!e) return false;
                        const st = getComputedStyle(e);
                        if (st.display === 'none' || st.visibility === 'hidden') return false;
                        const r = e.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    };
                    const key = e => ((e.placeholder || '') + ' ' + (e.name || '') + ' ' + (e.id || '')
                        + ' ' + (e.getAttribute('aria-label') || '')).toLowerCase();
                    for (const e of Array.from(document.querySelectorAll('input')).filter(vis)) {
                        if (!/pay/.test(key(e))) continue;
                        const v = (e.value || '').trim();
                        if (v && /\d/.test(v) && !/^next pay date$/i.test(v)) return v;
                    }
                    for (const e of Array.from(document.querySelectorAll('div,label,span')).filter(vis)) {
                        const t = (e.innerText || '').replace(/\s+/g, ' ').trim();
                        if (t.length > 80 || !/next pay date/i.test(t)) continue;
                        const m = t.match(/\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4}/);
                        if (m) return m[0];
                    }
                    return '';
                }"""
            ) or "").strip()
        except Exception:
            return ""

    def _payday_input(self, page: Page):
        fr = self._form_frame(page)
        for sel in (
            'input[placeholder*="Next Pay" i]',
            'input[placeholder*="Pay Date" i]',
            'input[aria-label*="Next Pay" i]',
            'input[name*="payDate" i]',
            'input[name*="PayDate" i]',
            'input[id*="payDate" i]',
            'input[id*="PayDate" i]',
            '[class*="datepicker"] input',
        ):
            loc = fr.locator(sel).first
            try:
                if loc.count() > 0:
                    return loc
            except Exception:
                continue
        return None

    def _tap_payday_field(self, page: Page) -> bool:
        """Click the calendar icon on the right of Next Pay Date."""
        fr = self._form_frame(page)
        row = fr.locator(
            'xpath=//*[normalize-space()="Next Pay Date"]/ancestor::*[.//svg or .//button][1]'
        )
        try:
            target = row.first
            target.scroll_into_view_if_needed(timeout=2000)
            box = target.bounding_box()
            if box and box["width"] > 24:
                # Icon sits on the far right of the field.
                page.mouse.click(box["x"] + box["width"] - 16, box["y"] + box["height"] / 2)
                time.sleep(0.6)
                if self._calendar_is_open(page):
                    return True
            icon = target.locator("svg, button, [class*='calendar'], [class*='icon']").last
            icon.click(timeout=2000, force=True)
            time.sleep(0.6)
            if self._calendar_is_open(page):
                return True
        except Exception:
            pass
        try:
            fr.evaluate(
                r"""() => {
                    const vis = e => {
                        const r = e.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    };
                    const label = Array.from(document.querySelectorAll('div,span,label,p'))
                        .find(e => vis(e) && (e.innerText || '').replace(/\s+/g, ' ').trim() === 'Next Pay Date');
                    if (!label) return false;
                    let row = label;
                    for (let i = 0; i < 6 && row && row !== document.body; i++) {
                        const t = (row.innerText || '').replace(/\s+/g, ' ').trim();
                        if (t.length < 60) {
                            const icon = row.querySelector('svg, [class*="calendar"], button');
                            if (icon) {
                                (icon.closest('button') || icon).click();
                                return true;
                            }
                        }
                        row = row.parentElement;
                    }
                    label.click();
                    return true;
                }"""
            )
            time.sleep(0.6)
        except Exception:
            pass
        return self._calendar_is_open(page)

    def _open_payday_calendar(self, page: Page) -> bool:
        if self._calendar_is_open(page):
            return True
        return self._tap_payday_field(page)

    def _set_payday_from_sheet(self, page: Page, row_number: int) -> str:
        """Tap Next Pay Date, wait for the calendar, click the sheet day."""
        wanted = self._sheet_payday()
        log.info("form.payday_tap_calendar", value=wanted, row=row_number)
        for _ in range(5):
            if self._calendar_is_open(page):
                break
            self._tap_payday_field(page)
            time.sleep(0.4)
        if self._calendar_is_open(page):
            try:
                self._pick_payday(page, row_number)
            except FormFillerError as e:
                log.warning("form.payday_calendar_unmatched", error=str(e)[:80],
                            row=row_number)
            err = self._visible_field_error(page)
            if err and re.search(r"calendar|date|choose", err, re.I):
                picked = self._click_enabled_calendar_day(page)
                if picked:
                    log.info("form.payday_fallback_future", day=picked, row=row_number)
        else:
            log.warning("form.payday_calendar_not_open", row=row_number)
        time.sleep(0.35)
        return self._payday_value(page) or wanted

    def _click_enabled_calendar_day(self, page: Page) -> str:
        """Click the first enabled visible day. Never clicks disabled padding days."""
        deadline = time.time() + 3.5
        while time.time() < deadline and not self._calendar_is_open(page):
            time.sleep(0.2)
        if not self._calendar_is_open(page):
            return ""
        fr = self._form_frame(page)
        loc = fr.locator(
            '.ef-calendar__day-btn, [role="gridcell"], [class*="calendar"] button, [class*="datepicker"] button'
        )
        try:
            n = loc.count()
        except Exception:
            return ""
        for i in range(n):
            cell = loc.nth(i)
            try:
                if not cell.is_visible():
                    continue
                text = re.sub(r"\s+", " ", cell.inner_text() or "").strip()
                if not re.fullmatch(r"\d{1,2}(\s*Today)?", text):
                    continue
                if cell.is_disabled():
                    continue
                if cell.get_attribute("aria-disabled") == "true":
                    continue
                cls = cell.get_attribute("class") or ""
                if re.search(r"disabled|outside|muted|inactive|faded|other-month", cls, re.I):
                    continue
                if re.search(r"today", text, re.I):
                    continue
                day_n = int(re.sub(r"\D", "", text) or "0")
                if 1 <= day_n <= date.today().day:
                    # Same-month past/today cells are still clickable but the
                    # form rejects them. Prefer a later day; next-month padding
                    # is filtered by other-month class above.
                    continue
                cell.click(timeout=2000)
                return re.sub(r"\s*Today", "", text, flags=re.I).strip()
            except Exception:
                continue
        return ""

    def _sheet_payday(self) -> str:
        """Next Payday from the sheet as MM/DD/YYYY, always after today."""
        return self._ensure_future_payday(
            self._raw(("Next Payday", "Next Pay Date", "Payday"))
        )

    def _click_request_cash(self, page: Page) -> bool:
        fr = self._form_frame(page)
        try:
            btn = fr.get_by_role("button", name=re.compile(r"request\s*(cash|loan|now)", re.I)).first
            btn.click(timeout=3000)
            return True
        except Exception:
            pass
        try:
            return bool(fr.evaluate(
                r"""() => {
                    const vis = e => {
                        if (!e) return false;
                        const r = e.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    };
                    const t = e => (e.innerText || e.value || '').replace(/\s+/g, ' ').trim();
                    const b = Array.from(document.querySelectorAll(
                        'button,input[type=submit],[role=button]'
                    )).filter(vis).find(e => /request\s*(cash|loan|now)/i.test(t(e)));
                    if (!b) return false;
                    b.click();
                    return true;
                }"""
            ))
        except Exception:
            return False

    def _complete_welcome_back_review(self, page: Page, row_number: int) -> bool:
        """Click the calendar icon, pick the sheet day, then Request Cash."""
        if not self._is_welcome_back_review(page):
            return False
        log.info("form.welcome_back_review", row=row_number)
        self._read_pause()
        self._set_payday_from_sheet(page, row_number)
        if not self._payday_value(page):
            self._tap_payday_field(page)
            try:
                self._pick_payday(page, row_number)
            except FormFillerError:
                pass
        if not self._payday_value(page):
            self._screenshot(page, row_number, "welcome_back_payday")
            raise FormFillerError(
                "Next Pay Date calendar did not open or accept a day — Request Cash not clicked",
                error_type="field_rejected",
            )
        self._action_pause()
        if not self._click_request_cash(page):
            self._click_final_cta(page)
        deadline = time.time() + 15
        while time.time() < deadline:
            time.sleep(0.6)
            if not self._is_welcome_back_review(page):
                log.info("form.welcome_back_submitted", row=row_number)
                return True
            err = self._visible_field_error(page)
            if err and re.search(r"date|calendar", err, re.I):
                self._set_payday_from_sheet(page, row_number)
                if self._payday_value(page):
                    self._click_request_cash(page)
        if self._is_welcome_back_review(page):
            self._screenshot(page, row_number, "welcome_back_payday")
            raise FormFillerError(
                "Welcome Back still on screen after calendar date + Request Cash",
                error_type="field_rejected",
            )
        return True

    def _bypass_returning_applicant(self, page: Page, row_number: int) -> None:
        """A phone number the platform already has triggers a "Welcome Back"
        shortcut; always take the full-form path so every sheet field gets
        submitted (so every sheet field is posted)."""
        try:
            clicked = page.evaluate(
                r"""() => {
                    const vis = e => e && e.offsetParent !== null && e.getClientRects().length > 0;
                    const body = document.body ? document.body.innerText : '';
                    if (!/welcome back/i.test(body)) return false;
                    const t = e => (e.innerText||'').replace(/\s+/g,' ').trim();
                    const b = Array.from(document.querySelectorAll('a,button,[role=button]'))
                        .filter(vis).find(e => /continue filling full form/i.test(t(e)));
                    if (b) { b.click(); return true; }
                    return false;
                }"""
            )
        except Exception:
            clicked = False
        if clicked:
            log.info("form.returning_applicant_bypass", row=row_number)
            time.sleep(1.5)

    # --------------------------------------------------------------- steps

    def _visible_fields(self, page: Page) -> list[dict]:
        try:
            return self._form_frame(page).evaluate(_FIELD_JS) or []
        except Exception:
            return []

    def _choice_buttons(self, page: Page) -> list[str]:
        try:
            return self._form_frame(page).evaluate(_BUTTONS_JS) or []
        except Exception:
            return []

    def _heading(self, page: Page) -> str:
        try:
            return self._form_frame(page).evaluate(_HEADING_JS) or ""
        except Exception:
            return ""

    def _handle_text_step(self, page: Page, fields: list[dict], f: dict) -> dict:
        known, filled, failed = [], [], []
        for field in fields:
            name, key = field["name"], None
            for pattern, fkey in _TEXT_FIELD_MAP:
                if pattern.search(field["key"]):
                    key = fkey
                    break
            if key is None:
                continue
            if known:
                self._field_pause()
            known.append(name)
            try:
                value = str(f.get(key, "") or "")
                if key == "next_payday":
                    value = value or self._sheet_payday()
                if key == "ssn":
                    digits = re.sub(r"\D", "", value)
                    if len(digits) == 9:
                        value = f"{digits[:3]}-{digits[3:5]}-{digits[5:]}"
                ok = (self._select_by_field(page, name, field, value)
                      if field["tag"] == "SELECT" else self._type_field(page, name, value))
                (filled if ok else failed).append(name)
            except Exception as e:
                failed.append(name)
                log.warning("form.field_error", field=name, error=str(e)[:90])
        return {"known": known, "filled": filled, "failed": failed}

    def _type_field(self, page: Page, name: str, value: str) -> bool:
        if not value:
            return False
        fr = self._form_frame(page)
        typed = False
        try:
            loc = fr.locator(f'[name="{name}"]').first
            loc.wait_for(state="visible", timeout=8000)
            loc.click()
            loc.fill("")
            loc.press_sequentially(value, delay=self._key_delay())
            typed = True
        except Exception as e:
            log.warning("form.type_failed", field=name, error=str(e)[:80])
        if not typed:
            try:
                typed = bool(fr.evaluate(
                    """([n, v]) => {
                        const el = document.querySelector('[name=\"' + n + '\"]');
                        if (!el) return false;
                        el.focus();
                        const proto = el.tagName === 'TEXTAREA'
                            ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                        const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                        if (desc && desc.set) desc.set.call(el, v); else el.value = v;
                        ['input','change','blur'].forEach(ev =>
                            el.dispatchEvent(new Event(ev, {bubbles: true})));
                        return true;
                    }""",
                    [name, value],
                ))
            except Exception as e:
                log.warning("form.type_js_failed", field=name, error=str(e)[:80])
                return False
        try:
            got = fr.evaluate(
                "(n) => { const e = document.querySelector('[name=\"'+n+'\"]'); return e ? e.value : ''; }",
                name,
            ) or ""
        except Exception:
            got = ""
        if not got:
            return False
        if got.strip().lower() == value.strip().lower():
            return True
        if re.sub(r"\D", "", got) and re.sub(r"\D", "", got) == re.sub(r"\D", "", value):
            return True
        return False

    def _select_by_field(self, page: Page, name: str, field: dict, value: str) -> bool:
        """Selects: pick the raw sheet text for this concept when the coded
        `value` (a platform-agnostic bracket/flag) isn't directly meaningful."""
        desired = value
        for pattern, cols in _CATEGORY_HEADING_MAP:
            if pattern.search(field["key"]):
                desired = self._raw(cols) or value
                break
        loc = page.locator(f'select[name="{name}"]').first
        for kwargs in ({"value": desired}, {"label": desired}):
            try:
                loc.select_option(timeout=4000, **kwargs)
                return True
            except Exception:
                pass
        try:
            matched = page.evaluate(
                """([n, v]) => {
                    const sel = document.querySelector('select[name="' + n + '"]');
                    if (!sel) return false;
                    const want = String(v).trim().toLowerCase();
                    const opts = Array.from(sel.options);
                    const hit = opts.find(o => o.value.trim().toLowerCase() === want) ||
                                opts.find(o => o.text.trim().toLowerCase() === want) ||
                                opts.find(o => o.text.trim().toLowerCase().includes(want) && want.length > 1);
                    if (!hit) return false;
                    sel.value = hit.value;
                    sel.dispatchEvent(new Event('input', {bubbles: true}));
                    sel.dispatchEvent(new Event('change', {bubbles: true}));
                    return true;
                }""",
                [name, desired],
            )
            return bool(matched)
        except Exception as e:
            log.warning("form.select_failed", field=name, value=desired, error=str(e)[:80])
            return False

    def _visible_field_error(self, page: Page) -> str:
        """Inline validation painted in red under an input — not legal/footer copy."""
        try:
            return (self._form_frame(page).evaluate(
                r"""() => {
                    const vis = e => e && e.offsetParent !== null && e.getClientRects().length > 0;
                    const norm = e => (e.innerText || '').replace(/\s+/g, ' ').trim();
                    const legal = t => /disclosure|terms of use|privacy policy|e-signature|credit authorization|read carefully|mobile phone disclosure/i.test(t);

                    for (const n of document.querySelectorAll('span,p,div,small,label')) {
                        if (!vis(n)) continue;
                        const t = norm(n);
                        if (t && t.length < 120 && /please choose a date from the calendar/i.test(t))
                            return t;
                    }

                    const nodes = Array.from(document.querySelectorAll(
                        '[class*="error"],[class*="invalid"],.ef-field__error,.ef-hint--error,[role="alert"]'
                    )).filter(vis);
                    for (const n of nodes) {
                        const t = norm(n);
                        if (!t || t.length > 160 || legal(t)) continue;
                        if (/invalid|required|enter a|please enter|please choose|must be|not valid/i.test(t))
                            return t;
                    }
                    return '';
                }"""
            ) or "").strip()
        except Exception:
            return ""

    def _click_final_cta(self, page: Page) -> None:
        """Click Request Cash / Request Loan when there is no Next button."""
        try:
            page.evaluate(self._JS_CLICK_CONTINUE, False)
        except Exception:
            pass

    def _click_next(self, page: Page) -> bool:
        try:
            clicked = self._form_frame(page).evaluate(
                r"""() => {
                    const vis = e => e.offsetParent !== null && e.getClientRects().length > 0;
                    const t = e => (e.innerText||'').replace(/\s+/g,' ').trim().toLowerCase();
                    const b = Array.from(document.querySelectorAll('button'))
                        .filter(vis).filter(e => !e.disabled).find(e => t(e) === 'next');
                    if (b) { b.click(); return true; }
                    return false;
                }"""
            )
        except Exception:
            clicked = False
        return bool(clicked)

    def _click_button_text(self, page: Page, text: str) -> None:
        try:
            self._form_frame(page).evaluate(
                r"""(want) => {
                    const vis = e => e.offsetParent !== null && e.getClientRects().length > 0;
                    const t = e => (e.innerText||'').replace(/\s+/g,' ').trim();
                    const b = Array.from(document.querySelectorAll('button,[role=button]'))
                        .filter(vis).filter(e => !e.disabled).find(e => t(e) === want);
                    if (b) b.click();
                }""",
                text,
            )
        except Exception as e:
            log.warning("form.choice_click_failed", value=text, error=str(e)[:80])

    def _await_change(self, page: Page, prev_sig: str, timeout: float = 20.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.6)
            names = self._visible_fields(page)
            heading = self._heading(page)
            sig = ",".join(n["name"] for n in names) or heading
            if sig != prev_sig:
                return

    # --------------------------------------------------------- choice logic

    def _raw(self, cols: tuple[str, ...]) -> str:
        row = getattr(self, "_raw_row", {}) or {}
        norm = {re.sub(r"\s+", " ", str(k)).strip().lower(): v for k, v in row.items()}
        for col in cols:
            v = str(norm.get(col.strip().lower()) or "").strip()
            if v:
                return v
        return ""

    def _pick_choice(self, heading: str, buttons: list[str], f: dict) -> str:
        for pattern, key in _YESNO_HEADING_MAP:
            if pattern.search(heading):
                want = "yes" if str(f.get(key, "0")) == "1" else "no"
                for b in buttons:
                    if b.strip().lower() == want:
                        return b
                return ""

        if re.search(r"pay\s*frequency|how often", heading, re.I):
            raw = self._raw(("Pay Frequency",))
            want = ""
            for pat, chip in _PAY_FREQ_ALIASES:
                if pat.search(raw or ""):
                    want = chip
                    break
            if want:
                for b in buttons:
                    if re.sub(r"[^a-z]", "", b.lower()) == re.sub(r"[^a-z]", "", want.lower()):
                        return b
            # Fall through to generic category matching.

        for pattern, cols in _CATEGORY_HEADING_MAP:
            if pattern.search(heading):
                raw = self._raw(cols).lower()
                if not raw:
                    return buttons[0] if buttons else ""
                raw_words0 = re.findall(r"[a-z]+", raw)
                raw = " ".join(_CATEGORY_SYNONYMS.get(w, w) for w in raw_words0)
                # Compare with punctuation/spacing stripped so "Bi-Weekly"
                # matches a "Biweekly" chip and "Credit Card Debt" matches
                # "Credit Card Payoff" loosely via the word-overlap fallback.
                raw_bare = re.sub(r"[^a-z0-9]", "", raw)
                for b in buttons:
                    bl = re.sub(r"[^a-z0-9]", "", b.lower())
                    if raw_bare in bl or bl in raw_bare:
                        return b
                # loose word overlap fallback
                raw_words = set(re.findall(r"[a-z]+", raw))
                best, best_score = "", 0
                for b in buttons:
                    score = len(raw_words & set(re.findall(r"[a-z]+", b.lower())))
                    if score > best_score:
                        best, best_score = b, score
                if best:
                    return best
                # Nothing matched this real-world value ("EMERGENCY_SITUATION",
                # "MOVING", ...) -- fall back to the site's own catch-all
                # option rather than failing a lead over unmapped vocabulary.
                catchall = next((b for b in buttons if _CATCHALL_BUTTON_RE.match(b.strip())), None)
                if catchall:
                    log.info("form.category_fallback_catchall", heading=heading[:40],
                             raw=raw[:40], picked=catchall)
                    return catchall
                return ""

        for pattern, col in _TENURE_HEADING_MAP:
            if pattern.search(heading):
                raw = self._raw((col,))
                years_match = re.search(r"\d+", raw)
                years = int(years_match.group()) if years_match else 5
                # Sheet4 stores months in these columns (36, 60, …).
                if years > 12 and "year" not in (raw or "").lower():
                    years = max(1, round(years / 12))
                best, best_diff = "", None
                for b in buttons:
                    nums = [int(n) for n in re.findall(r"\d+", b)]
                    if not nums:
                        continue
                    # buttons may read in months or years -- compare against both
                    for n in nums:
                        for target in (years, years * 12):
                            diff = abs(n - target)
                            if best_diff is None or diff < best_diff:
                                best, best_diff = b, diff
                return best

        for pattern, col in _DOLLAR_BRACKET_HEADING_MAP:
            if pattern.search(heading):
                raw = re.sub(r"[,$\s]", "", self._raw((col,)))
                try:
                    target = int(float(raw)) if raw else None
                except ValueError:
                    target = None
                if target is None:
                    return buttons[0] if buttons else ""
                zero_btn = next((b for b in buttons
                                  if re.search(r"^no\b|none|\$0\b|zero", b, re.I)), None)
                numeric: list[tuple[str, float, float]] = []
                for b in buttons:
                    if b == zero_btn:
                        continue
                    low_b = b.lower()
                    nums = [int(n.replace(",", "")) for n in re.findall(r"[\d,]+", b)]
                    if not nums:
                        continue
                    if "or more" in low_b or "+" in b:
                        lo, hi = nums[0], float("inf")
                    elif "less than" in low_b:
                        lo, hi = 0, nums[-1]
                    elif len(nums) >= 2:
                        lo, hi = nums[0], nums[1]
                    else:
                        lo = hi = nums[0]
                    numeric.append((b, lo, hi))
                if zero_btn and (target <= 0 or (numeric and target < min(lo for _, lo, _ in numeric))):
                    return zero_btn
                best, best_in_range, best_diff = "", False, None
                for b, lo, hi in numeric:
                    in_range = lo <= target <= hi
                    diff = 0 if in_range else min(abs(target - lo), abs(target - hi))
                    if in_range and not best_in_range:
                        best, best_in_range, best_diff = b, True, diff
                    elif not best_in_range and (best_diff is None or diff < best_diff):
                        best, best_diff = b, diff
                return best

        # A plain Yes/No step with no sheet column behind it (e.g. an
        # optional collateral/upsell question like "Would you use your car
        # title for a larger loan?") -- decline rather than opt into
        # something the lead data says nothing about.
        button_set = {b.strip().lower() for b in buttons}
        if button_set == {"yes", "no"}:
            for b in buttons:
                if b.strip().lower() == "no":
                    return b

        return ""
