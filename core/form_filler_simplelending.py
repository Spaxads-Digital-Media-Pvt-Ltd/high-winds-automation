"""
core/form_filler_simplelending.py — simplelendingdirect.com multi-step form
automation.

Same lead-platform family as americanemergencyfund.com (identical wizard
copy — "What Is Your Email Address? We use your email to send request
updates and connect you with lenders" is verbatim shared text), but skinned
with different markup: no #applicantForm/#nextBtn wrapper, plain page-level
inputs (name="email", name="firstName", …) and an "ef-" component library
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
recognition, same idea as AEF); "Continue filling full form" bypasses it.

Anything this filler doesn't recognise raises a clear ``unhandled_step`` /
``field_rejected`` error (with a screenshot), so a real run's log pinpoints
exactly what to extend rather than silently guessing. See
`_handle_text_step` / `_pick_choice` below.

Completion routes into the shared lender-match flow (`_handle_post_offer`
in core/lead_platform.py) — the same "Continue" chase across popups / new
tabs / in-place navigation used by AEF and MyLendingWallet.
"""
from __future__ import annotations

import re
import time

import structlog
from playwright.sync_api import Page

from core.lead_platform import BasePlatformFiller, FormFillerError, _pick_calendar_index, _target_day_from_raw

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
    "great": "excellent", "employed": "employment",
}

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
    as AEF (shared copy, shared lender-match post-offer flow)."""

    default_url = "https://simplelendingdirect.com/"

    # The bank-details step's submit button reads "Request Cash" (per-site
    # copy), which the shared _JS_CLICK_CONTINUE in lead_platform.py doesn't
    # recognise as a CTA -- override with "request" added so
    # _handle_post_offer's CTA finder can locate and click it (then chase the
    # resulting popup/new-tab/in-place navigation exactly like AEF).
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
        self._pick_loan_amount_chip(page, f, row_number)

        seen: dict[str, int] = {}
        for step_num in range(self._max_steps):
            self._check_stop(stop_event)
            # A mid-form navigation can fail (bad proxy exit IP, dropped
            # connection) and land the browser on Chrome's own error page
            # rather than the site. That page has genuinely-real DOM buttons
            # ("Reload", "Details"), so the "no fields, no choice buttons ->
            # must have reached post-offer" fallback a few lines down used to
            # treat it as a completed submission instead of the failure it
            # actually is.
            if (page.url or "").startswith("chrome-error://"):
                self._screenshot(page, row_number, f"nav_error_{step_num}")
                raise FormFillerError(
                    f"Navigation failed mid-form (step {step_num}) — landed on a "
                    f"browser error page, not the site.", error_type="proxy_error",
                )
            self._bypass_returning_applicant(page, row_number)

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
                if not self._click_next(page):
                    # No plain "Next" -- this was the final step (bank details);
                    # whatever CTA is present routes into the lender-match flow.
                    break
                self._await_change(page, sig)
                continue

            if re.search(r"next pay ?date|when is your next pay", heading, re.I):
                self._read_pause()
                self._pick_payday(page, row_number)
                self._action_pause()
                self._click_next(page)
                self._await_change(page, sig)
                continue

            # No <input> on this step: either a chip-choice screen, or we've
            # already arrived at the post-application lender-match page.
            buttons = self._choice_buttons(page)
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
        label = f"${nearest:,}"
        try:
            loc = page.locator("button.ef-btn", has_text=label).first
            loc.wait_for(state="visible", timeout=20000)
            loc.click()
        except Exception as e:
            self._screenshot(page, row_number, "no_amount_chip")
            raise FormFillerError(
                f"Could not find loan-amount chip '{label}': {e}", error_type="stuck"
            )
        log.info("form.loan_amount", chip=label, row=row_number)
        time.sleep(1.5)

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
        target_day = _target_day_from_raw(self._raw(("Next Payday",)))
        days: list[dict] = []
        deadline = time.time() + 8
        while time.time() < deadline and not days:
            try:
                days = page.evaluate(
                    """() => Array.from(document.querySelectorAll('.ef-calendar__day-btn')).map(b => ({
                        text: b.innerText.trim(), disabled: b.disabled,
                        attrs: Array.from(b.attributes).map(a => a.name + '=' + a.value).join(' '),
                        parentAttrs: b.parentElement ? Array.from(b.parentElement.attributes).map(a => a.name + '=' + a.value).join(' ') : ''
                    }))"""
                ) or []
            except Exception:
                days = []
            if not days:
                time.sleep(0.5)
        if not days:
            self._screenshot(page, row_number, "no_calendar")
            raise FormFillerError("Next-payday calendar did not render", error_type="stuck")

        log.info("form.payday_calendar_dump", row=row_number,
                 cells=[{"text": d["text"], "disabled": d["disabled"],
                         "attrs": d.get("attrs", ""), "parentAttrs": d.get("parentAttrs", "")}
                        for d in days])

        idx = _pick_calendar_index(days, target_day)

        if idx is None:
            self._screenshot(page, row_number, "payday_unmatched")
            raise FormFillerError(
                f"Could not match a next-payday calendar cell for day={target_day} "
                f"among {[d['text'] for d in days]}", error_type="unhandled_step",
            )

        try:
            page.locator(".ef-calendar__day-btn").nth(idx).click()
        except Exception as e:
            self._screenshot(page, row_number, "payday_click_failed")
            raise FormFillerError(f"Could not click payday cell: {e}", error_type="stuck")
        log.info("form.payday_picked", day=target_day, cell=days[idx]["text"], row=row_number)
        time.sleep(1)

    # ------------------------------------------------------- returning user

    def _bypass_returning_applicant(self, page: Page, row_number: int) -> None:
        """A phone number the platform already has triggers a "Welcome Back"
        shortcut; always take the full-form path so every sheet field gets
        submitted (mirrors AEF's returning-applicant handling)."""
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
            return page.evaluate(_FIELD_JS) or []
        except Exception:
            return []

    def _choice_buttons(self, page: Page) -> list[str]:
        try:
            return page.evaluate(_BUTTONS_JS) or []
        except Exception:
            return []

    def _heading(self, page: Page) -> str:
        try:
            return page.evaluate(_HEADING_JS) or ""
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
        try:
            loc = page.locator(f'[name="{name}"]').first
            loc.wait_for(state="visible", timeout=8000)
            loc.click()
            loc.fill("")
            loc.press_sequentially(value, delay=self._key_delay())
        except Exception as e:
            log.warning("form.type_failed", field=name, error=str(e)[:80])
            return False
        got = page.evaluate("(n) => { const e = document.querySelector('[name=\"'+n+'\"]'); return e ? e.value : ''; }", name)
        return bool(got and (got.strip() == value.strip() or re.sub(r"\D", "", got) == re.sub(r"\D", "", value)))

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

    def _click_next(self, page: Page) -> bool:
        try:
            clicked = page.evaluate(
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
            page.evaluate(
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
