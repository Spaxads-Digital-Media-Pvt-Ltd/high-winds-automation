"""
core/form_filler_mlw.py — mylendingwallet.com multi-step form automation.

Same lead platform as americanemergencyfund.com — the site's own JS bundle
carries the identical 31-field vocabulary and posts to the identical endpoints
(``/?cmd=ExtApplyV2``, ``/?cmd=RenderResult``) — but a completely different
front-end: a React SPA (``template/10666``, Vite build) using react-hook-form,
rather than AEF's server-rendered Bootstrap wizard.  All value mapping is
therefore inherited unchanged from BasePlatformFiller; only the DOM layer here
differs.

DOM contract, captured by walking the live form (devtools/mlw_steps.json):

  * The <form> id is regenerated per render (e.g. ``tf---fcg23sw``), so it
    cannot be an anchor.  Scope instead to whichever element carries a
    platform-named field.
  * Every choice except step 0 is a real ``<input type=radio name=X value=Y>``
    group or a native ``<select>``, both carrying the platform's own value —
    identical values to AEF (tenure 60/48/36/24/12, yes/no 1/0, payfreq 1-4,
    …).  Selection is therefore **by value**, and the visible wording does not
    matter.  Only step 0's loan amount is button chips with no value attribute,
    which is the single case that needs label matching.
  * The radios carry no ``id``, so ``label[for=…]`` finds nothing; the click
    target is the input's wrapping <label>.
  * react-hook-form ignores a plain value assignment: setting a text field
    requires real typing, or a native-setter write followed by
    input/change/blur, which is what ``_text`` does.

Step order observed live: loanreqamt · fname/lname/dob · email/lastfourssn ·
phhm · haddress1/hpostal/hcity/hstate · i_ad_ccDebtAmt · hmonthsat · ishowner ·
netim · priincsrc · payfreq · isactmil · ename · emonthsat · phwrk ·
licn/licst · … · baba/bacc.  As on AEF the server decides which steps render,
so dispatch stays field-driven rather than index-driven.

An earlier version of this filler matched choices by label using AEF's
vocabulary.  That was wrong: this site words the same options differently
("5+ years" not "5 years or more", "Under 1 year" not "1 year or less",
"self-employed" hyphenated), which failed every tenure and income-source step.
Value matching removes the guesswork; the label table survives only as the
step-0 fallback and now holds this site's actual wording.
"""
from __future__ import annotations

import re
import time
from typing import Callable

import structlog
from playwright.sync_api import Page

from core.lead_platform import BasePlatformFiller, FormFillerError, _digits

log = structlog.get_logger(__name__)

__all__ = ["FormFiller", "FormFillerError"]

# Label fallback, used only when a field is rendered as button chips with no
# value attribute to target — which on this site is step 0's loan amount alone.
# Everything else is a real radio group or <select> carrying the platform value,
# so it is set by value and never needs these.
#
# Labels below are the ones this site actually renders (captured from the live
# form), NOT AEF's.  They differ in places that matter: "5+ years" vs AEF's
# "5 years or more", "Under 1 year" vs "1 year or less", and "self-employed"
# with a hyphen.  Matching on AEF's wording is what broke the tenure steps.
_CHOICE_LABELS: dict[str, dict[str, list[str]]] = {
    "loanreqamt": {"1000": ["up to $1,000"], "3000": ["up to $3,000"],
                   "5000": ["above + $3,000", "above $3,000"]},
    "hmonthsat":  {"60": ["5+ years"], "48": ["4 years"], "36": ["3 years"],
                   "24": ["2 years"], "12": ["Under 1 year"]},
    "emonthsat":  {"60": ["5+ years"], "48": ["4 years"], "36": ["3 years"],
                   "24": ["2 years"], "12": ["Under 1 year"]},
    "bmonthsat":  {"60": ["5+ years"], "48": ["4 years"], "36": ["3 years"],
                   "24": ["2 years"], "12": ["Under 1 year"]},
    "ishowner":   {"1": ["Yes"], "0": ["No"]},
    "isactmil":   {"1": ["Yes"], "0": ["No"]},
    "isdd":       {"1": ["Yes"], "0": ["No"]},
    "priincsrc":  {"1": ["Employed or self-employed"],
                   "2": ["Benefits or not employed"]},
    "payfreq":    {"1": ["Weekly"], "2": ["Bi-Weekly"],
                   "3": ["Monthly"], "4": ["Semi-Monthly"]},
    "bacctype":   {"1": ["Checking"], "2": ["Savings", "Saving"]},
    "crscore":    {"2": ["Great 700+", "700+"], "3": ["600 - 700"],
                   "4": ["500 - 600"], "5": ["Below 500"], "1": ["Not Sure"]},
    "loanreason": {"14": ["Credit card debt relief"], "1": ["Debt consolidation"],
                   "13": ["Other reasons", "Other"]},
    "netim":      {"11000": ["$10,000 or More"], "10000": ["$9,000 - $10,000"],
                   "9000": ["$8,000 - $9,000"], "8000": ["$7,000 - $8,000"],
                   "7000": ["$6,000 - $7,000"], "6000": ["$5,000 - $6,000"],
                   "5000": ["$4,000 - $5,000"], "4000": ["$3,000 - $4,000"],
                   "3000": ["$2,500 - $3,000"], "2500": ["$2,000 - $2,500"],
                   "2000": ["$1,500 - $2,000"], "1500": ["Below $1500"]},
    "i_ad_ccDebtAmt": {"0": ["None"], "4999": ["$1,000 - $4,999"],
                       "9999": ["$5,000 - $9,999"], "14999": ["$10,000 - $14,999"],
                       "19999": ["$15,000 - $19,999"], "24999": ["$20,000 - $24,999"],
                       "29999": ["$25,000 - $29,999"], "34999": ["$30,000 - $34,999"],
                       "39999": ["$35,000 - $39,999"], "44999": ["$40,000 - $44,999"],
                       "49999": ["$45,000 - $49,999"], "50000": ["$50,000+"]},
}

# Buttons that advance rather than choose.  "Start Request Now" is this site's
# step-0 call to action — observed live, not guessed.
_ADVANCE_RE = (r"^(continue|next|submit|request loan|get started|start request now"
               r"|see my offer.*|finish)$")

# Heading keywords that identify a choice-only step.  Several fields share an
# identical option set — ishowner / isactmil / isdd are all just Yes/No — so the
# visible labels alone cannot tell them apart; the question text is the only
# discriminator.  Ordered most-specific first; first match wins.
_HEADING_HINTS: list[tuple[str, tuple[str, ...]]] = [
    ("isdd",           ("direct deposit", "deposited", "paycheck")),
    ("isactmil",       ("military", "armed forces", "active duty")),
    ("ishowner",       ("own your home", "homeowner", "own or rent", "rent")),
    ("bmonthsat",      ("bank account", "with your bank", "banking")),
    ("emonthsat",      ("employer", "this job", "current job", "work there")),
    ("hmonthsat",      ("address", "residence", "live there", "lived")),
    ("bacctype",       ("account type", "type of account", "checking or savings")),
    ("priincsrc",      ("income source", "source of income", "primary income")),
    ("payfreq",        ("how often", "pay frequency", "paid")),
    ("crscore",        ("credit score", "credit rating")),
    ("loanreason",     ("reason", "purpose", "what will you use")),
    ("i_ad_ccDebtAmt", ("credit card debt", "debt amount", "unsecured debt")),
    ("netim",          ("net income", "monthly income", "take home")),
    ("loanreqamt",     ("loan amount", "how much")),
]


class FormFiller(BasePlatformFiller):
    """mylendingwallet.com — React SPA over the shared lead platform."""

    default_url = "https://www.mylendingwallet.com/"

    # ------------------------------------------------------------------ setup

    def _prepare(self, page: Page, row_number: int) -> None:
        """Dismiss any consent / splash gate standing before the first step."""
        deadline = time.time() + 20
        while time.time() < deadline:
            if self._form_ready(page):
                return
            try:
                clicked = page.evaluate(
                    """() => {
                        const vis = e => e && e.offsetParent !== null;
                        const re = /^(i agree|agree|accept|continue|get started|start)$/i;
                        const b = Array.from(document.querySelectorAll('button,[role=button]'))
                            .filter(vis).filter(e => !e.disabled)
                            .find(e => re.test((e.innerText || '').trim()));
                        if (b) { b.click(); return (b.innerText || '').trim(); }
                        return null;
                    }"""
                )
            except Exception:
                clicked = None
            if clicked:
                log.info("form.gate_clicked", label=clicked, row=row_number)
                time.sleep(1.2)
            else:
                time.sleep(1)

    def _form_ready(self, page: Page) -> bool:
        try:
            return bool(page.evaluate(self._JS_READY))
        except Exception:
            return False

    # The form id is generated per render, so anchor on "the form that contains
    # platform-named fields" — or the document, if the fields sit outside a form.
    _JS_PLATFORM_FIELDS = """
        const NAMES = ['loanreqamt','fname','lname','dob','email','lastfourssn','phhm',
          'haddress1','hpostal','hcity','hstate','i_ad_ccDebtAmt','hmonthsat','ishowner',
          'netim','priincsrc','payfreq','isactmil','ename','emonthsat','phwrk','licn',
          'licst','ssn','bacctype','bmonthsat','isdd','crscore','loanreason','baba','bacc'];
        const lower = NAMES.map(n => n.toLowerCase());
        const vis = e => e.offsetParent !== null && e.getClientRects().length > 0;
    """

    _JS_READY = "() => {" + _JS_PLATFORM_FIELDS + """
        const els = Array.from(document.querySelectorAll('input[name],select[name],textarea[name]'));
        return els.some(e => vis(e) && lower.includes((e.name || '').toLowerCase()));
    }"""

    # ------------------------------------------------------------- form flow

    def _fill_form(self, page: Page, f: dict, row_number: int, stop_event) -> str:
        for _ in range(60):
            self._check_stop(stop_event)
            if self._form_ready(page) or self._completion_state(page):
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
            choices = self._visible_choices(page)
            if not names and not choices:
                time.sleep(1.5)
                continue

            # Choice-only steps expose no named input at all — the whole step is
            # a row of labelled buttons — so the field has to be inferred from
            # what is on screen before it can be dispatched.
            if not names and choices:
                inferred = self._infer_choice_field(page, choices)
                if inferred:
                    log.info("form.inferred_field", field=inferred, row=row_number)
                    names = [inferred]

            sig = ",".join(sorted(names)) or "choices:" + ",".join(sorted(choices))[:60]
            seen[sig] = seen.get(sig, 0) + 1
            if seen[sig] > 3:
                self._screenshot(page, row_number, f"stuck_{step_num}")
                raise FormFillerError(
                    f"Form stopped advancing at step {step_num} (fields: {sig})",
                    error_type="stuck",
                )

            log.info("form.step", step=step_num, fields=sig[:70],
                     choices=choices[:6], row=row_number)
            self._live(page)

            res = self._handle_step(page, names, f)
            if not res["known"]:
                self._screenshot(page, row_number, f"unhandled_{step_num}")
                raise FormFillerError(
                    f"Unrecognised step {step_num} — fields={sig} choices={choices[:8]}",
                    error_type="unhandled_step",
                )
            if not res["filled"]:
                self._screenshot(page, row_number, f"unfilled_{step_num}")
                raise FormFillerError(
                    f"Could not set any field on step {step_num}: {res['failed']} "
                    f"(value rejected, or option label not found among {choices[:8]})",
                    error_type="field_rejected",
                )
            if res["failed"]:
                log.warning("form.partial_step", step=step_num, filled=res["filled"],
                            failed=res["failed"], row=row_number)

            self._action_pause()
            self._click_next(page)
            self._await_change(page, sig)
            self._live(page)

        raise FormFillerError(
            f"Form did not complete within {self._max_steps} steps", error_type="timeout")

    def _handle_step(self, page: Page, names: list[str], f: dict) -> dict:
        """Fill every platform field the current step exposes."""
        def choice(field: str, value: str) -> bool:
            return self._set_option(page, field, value)

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
            "hstate":         lambda: self._set_option(page, "hstate", f["state"]),
            "ename":          lambda: self._text(page, "ename", f["employer_name"]),
            "licn":           lambda: self._text(page, "licn", f["dl_number"]),
            "licst":          lambda: self._set_option(page, "licst", f["dl_state"]),
            "ssn":            lambda: self._text(page, "ssn", f["ssn"]),
            "baba":           lambda: self._text(page, "baba", f["routing_number"]),
            "bacc":           lambda: self._text(page, "bacc", f["account_number"]),
            "bname":          lambda: self._text(page, "bname", f["bank_name"]),
            "i_ad_ccDebtAmt": lambda: self._set_option(page, "i_ad_ccDebtAmt", f["debt_bracket"]),
            "netim":          lambda: self._set_option(page, "netim", f["income_bracket"]),
            "hmonthsat":      lambda: choice("hmonthsat", f["address_months"]),
            "emonthsat":      lambda: choice("emonthsat", f["employer_months"]),
            "bmonthsat":      lambda: choice("bmonthsat", f["bank_months"]),
            "ishowner":       lambda: choice("ishowner", f["is_homeowner"]),
            "isactmil":       lambda: choice("isactmil", f["is_military"]),
            "isdd":           lambda: choice("isdd", f["is_direct_deposit"]),
            "priincsrc":      lambda: choice("priincsrc", f["income_source"]),
            "payfreq":        lambda: choice("payfreq", f["pay_freq"]),
            "bacctype":       lambda: choice("bacctype", f["account_type"]),
            "crscore":        lambda: choice("crscore", f["credit_score"]),
            "loanreason":     lambda: choice("loanreason", f["loan_reason"]),
        }

        known, filled, failed = [], [], []
        for name in names:
            fn = handlers.get(name)
            if fn is None:
                log.warning("form.unknown_field", field=name)
                continue
            known.append(name)
            try:
                (filled if fn() else failed).append(name)
            except Exception as e:
                failed.append(name)
                log.warning("form.field_error", field=name,
                            error=f"{type(e).__name__}: {e}"[:110])
        return {"known": known, "filled": filled, "failed": failed}

    # ----------------------------------------------------------- interactions

    def _visible_field_names(self, page: Page) -> list[str]:
        try:
            return page.evaluate("() => {" + self._JS_PLATFORM_FIELDS + """
                const out = [];
                document.querySelectorAll('input[name],select[name],textarea[name]').forEach(e => {
                    const n = (e.name || '');
                    const i = lower.indexOf(n.toLowerCase());
                    if (i < 0) return;
                    if (e.type === 'hidden' || !vis(e)) return;
                    if (!out.includes(NAMES[i])) out.push(NAMES[i]);
                });
                return out;
            }""") or []
        except Exception:
            return []

    def _infer_choice_field(self, page: Page, choices: list[str]) -> str | None:
        """Work out which platform field a button-only step is asking for.

        Two signals, in order:
          1. Option-label overlap — decisive for fields with a distinctive set
             (pay frequency, credit band, income bracket, …).
          2. The step's heading — the only way to separate the fields whose
             options are identical (ishowner / isactmil / isdd are all Yes/No,
             and the three tenure questions share one set of five labels).
        """
        seen = {c.strip().lower() for c in choices}

        scored: list[tuple[int, str]] = []
        for field, mapping in _CHOICE_LABELS.items():
            labels = {lbl.strip().lower() for lbls in mapping.values() for lbl in lbls}
            overlap = len(seen & labels)
            if overlap:
                scored.append((overlap, field))
        scored.sort(reverse=True)

        # A single clear winner on labels alone is enough.
        if len(scored) == 1 or (len(scored) > 1 and scored[0][0] > scored[1][0]):
            return scored[0][1]

        heading = self._heading(page).lower()
        if heading:
            tied = {f for _s, f in scored} or None
            for field, keywords in _HEADING_HINTS:
                if tied and field not in tied:
                    continue
                if any(k in heading for k in keywords):
                    return field

        if scored:
            log.warning("form.ambiguous_choice_step", heading=heading[:80],
                        candidates=[f for _s, f in scored][:5], choices=choices[:6])
            return None
        log.warning("form.unmapped_choice_step", heading=heading[:80], choices=choices[:8])
        return None

    def _heading(self, page: Page) -> str:
        """Visible question text for the current step."""
        try:
            return page.evaluate(r"""() => {
                const vis = e => e.offsetParent !== null && e.getClientRects().length > 0;
                const els = Array.from(document.querySelectorAll(
                    'h1,h2,h3,h4,legend,label,[class*=question],[class*=title],p'));
                for (const e of els) {
                    if (!vis(e)) continue;
                    const t = (e.innerText || '').replace(/\s+/g, ' ').trim();
                    if (t.length > 3 && t.length < 160) return t;
                }
                return '';
            }""") or ""
        except Exception:
            return ""

    def _visible_choices(self, page: Page) -> list[str]:
        """Labels of the clickable choice buttons on the current step."""
        try:
            return page.evaluate(r"""() => {
                const vis = e => e.offsetParent !== null && e.getClientRects().length > 0;
                const skip = /^(back|\?|continue|next|submit|request loan)$/i;
                return Array.from(document.querySelectorAll('button,[role=button]'))
                    .filter(vis).filter(e => !e.disabled)
                    .map(e => (e.innerText || '').replace(/\s+/g, ' ').trim())
                    .filter(t => t && t.length < 60 && !skip.test(t));
            }""") or []
        except Exception:
            return []

    def _text(self, page: Page, name: str, value: str) -> bool:
        """Type into a react-hook-form input, verifying by read-back."""
        value = str(value or "")
        if not value:
            return False
        sel = f'[name="{name}"]'
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=8000)
            loc.click()
            loc.fill("")
            loc.press_sequentially(value, delay=self._key_delay())
            loc.blur()
        except Exception as e:
            log.warning("form.type_failed", field=name, error=str(e)[:80])

        got = self._read_back(page, name)
        if _digits(got) == _digits(value) or got.strip() == value.strip():
            return True

        # react-hook-form only observes events, so a bare value write is ignored:
        # use the native setter then fire the events its listeners subscribe to.
        try:
            page.evaluate(
                """([n, v]) => {
                    const el = document.querySelector('[name="' + n + '"]');
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
            log.warning("form.value_mismatch", field=name, wanted=value[:20], got=got[:20])
        return ok

    def _read_back(self, page: Page, name: str) -> str:
        try:
            return page.evaluate(
                """(n) => { const el = document.querySelector('[name="' + n + '"]');
                            return el ? (el.value || '') : ''; }""", name) or ""
        except Exception:
            return ""

    def _set_option(self, page: Page, name: str, value: str) -> bool:
        """Set any option-style field, whichever way this step renders it.

        Order matters.  Live inspection showed almost every choice on this site
        is a real ``<input type=radio name=X value=Y>`` group or a native
        ``<select>``, both carrying the platform's own value — so matching on
        value is exact and label wording is irrelevant.  Only step 0's loan
        amount is button chips with no value to target, which is the sole case
        that falls through to label matching.
        """
        if not value:
            return False
        if self._radio(page, name, value):
            return True
        if self._select(page, name, value):
            return True
        return self._choose(page, name, value)

    def _radio(self, page: Page, name: str, value: str) -> bool:
        """Click a radio by its value.  These radios carry no id, so the click
        target is the input's wrapping <label> when there is one."""
        try:
            return bool(page.evaluate(
                """([n, v]) => {
                    const el = document.querySelector(
                        'input[type=radio][name="' + n + '"][value="' + v + '"]');
                    if (!el) return false;
                    const lbl = el.closest('label')
                        || (el.id ? document.querySelector('label[for="' + el.id + '"]') : null);
                    (lbl || el).click();
                    if (!el.checked) {
                        el.checked = true;
                        el.dispatchEvent(new Event('click',  { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                    return el.checked;
                }""",
                [name, value]))
        except Exception as e:
            log.warning("form.radio_failed", field=name, value=value, error=str(e)[:80])
            return False

    def _select(self, page: Page, name: str, value: str) -> bool:
        """Set a <select>-backed field, verifying the value actually stuck.

        Some of these are plain native selects.  Others are HeroUI <Select>
        components, where the element carrying the ``name`` is a *visually
        hidden a11y mirror* (``data-testid="hidden-select-container"``, clipped
        to 1px) and the real control is a custom listbox that owns the React
        state.  Writing the mirror leaves the component untouched: the field
        reads back empty and the step refuses to advance with "… is required",
        while select_option() reports success.  Hence the read-back check —
        never trust the write — and the listbox fallback below.
        """
        try:
            present = bool(page.evaluate(
                """(n) => !!document.querySelector('select[name="' + n + '"]')""", name))
        except Exception:
            present = False
        if not present:
            return False

        loc = page.locator(f'select[name="{name}"]').first
        for kwargs in ({"value": value}, {"label": value}):
            try:
                loc.select_option(timeout=6000, **kwargs)
                if self._read_back(page, name) == value:
                    return True
            except Exception:
                pass

        # Native setter + events: React ignores a plain `.value =` assignment.
        try:
            page.evaluate(
                """([n, v]) => {
                    const sel = document.querySelector('select[name="' + n + '"]');
                    if (!sel) return;
                    const setter = Object.getOwnPropertyDescriptor(
                        HTMLSelectElement.prototype, 'value').set;
                    setter.call(sel, v);
                    sel.dispatchEvent(new Event('input',  { bubbles: true }));
                    sel.dispatchEvent(new Event('change', { bubbles: true }));
                }""", [name, value])
            if self._read_back(page, name) == value:
                return True
        except Exception as e:
            log.warning("form.select_native_failed", field=name, error=str(e)[:80])

        return self._select_listbox(page, name, value)

    def _select_listbox(self, page: Page, name: str, value: str) -> bool:
        """Drive a HeroUI custom Select.

        Three things make this awkward, all confirmed on the live form:
          * The trigger only opens for a real user click — a JS ``.click()`` on
            it does nothing.
          * The listbox is virtualised: with 51 states only ~9 options exist in
            the DOM at a time, so querying for the target usually finds nothing
            and the scroll container is barely taller than its viewport.
          * Type-ahead does not engage; typing just leaves the first option
            focused.

        What works: open the listbox, press ArrowDown until the wanted row
        materialises, then click it.

        What does not, and cost the most time proving: every way of asking
        "which option is highlighted?" lies here.  DOM focus never moves,
        ``aria-activedescendant`` is null, and ``data-focus="true"`` sits on a
        recycled node — so a walk driven by any of them appears to stall on the
        9th option while the rendered window is demonstrably still advancing.
        Hence: don't track the highlight, just watch for the target to appear.
        """
        try:
            base = page.locator(f'select[name="{name}"]').locator(
                'xpath=ancestor::*[@data-slot="base"][1]')
            if not base.count():
                return False
            trigger = base.locator(
                'button[data-slot="trigger"], [aria-haspopup="listbox"], button').first
            if not trigger.count():
                log.warning("form.listbox_no_trigger", field=name)
                return False
            trigger.click()          # real click — JS .click() will not open it
            page.wait_for_selector('[role="option"]', timeout=5000)

            # Don't track which option is highlighted — the virtualiser recycles
            # the option nodes, so data-focus/tabindex/activeElement all report a
            # stale element and any walk based on them appears to stall after the
            # first rendered window.  ArrowDown *does* advance the window, so
            # simply step until the target row materialises, then click it.
            budget = page.evaluate(
                """(n) => {
                    const s = document.querySelector('select[name="' + n + '"]');
                    return s ? Array.from(s.options).filter(o => o.value !== '').length : 60;
                }""", name) or 60
            target = page.locator(f'[role="option"][data-key="{value}"]')
            for _ in range(budget + 10):
                if target.count():
                    target.first.scroll_into_view_if_needed(timeout=2000)
                    target.first.click()
                    time.sleep(0.4)
                    ok = self._read_back(page, name) == value
                    log.info("form.listbox_select", field=name, value=value, ok=ok)
                    return ok
                page.keyboard.press("ArrowDown")
                time.sleep(0.12)

            rendered = page.evaluate(
                """() => Array.from(document.querySelectorAll('[role="option"]'))
                          .map(o => o.getAttribute('data-key'))""")
            log.warning("form.listbox_option_missing", field=name, value=value,
                        rendered=rendered)
            page.keyboard.press("Escape")
            return False
        except Exception as e:
            log.warning("form.listbox_failed", field=name, error=str(e)[:90])
            return False

    def _choose(self, page: Page, field: str, value: str) -> bool:
        """Click the choice whose visible label maps to ``value``.

        This front-end renders options as <button>, so there is no value
        attribute to match — selection is by the platform's label vocabulary,
        with a normalised comparison and a contains-fallback.
        """
        if not value:
            return False
        labels = _CHOICE_LABELS.get(field, {}).get(str(value), [])
        if not labels:
            log.warning("form.no_label_mapping", field=field, value=value)
            return False
        try:
            hit = page.evaluate(
                """([labels, field]) => {
                    const vis = e => e.offsetParent !== null && e.getClientRects().length > 0;
                    const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    const wanted = labels.map(norm);
                    let els = Array.from(document.querySelectorAll(
                        'button,[role=button],[role=radio],[role=option],label'));
                    els = els.filter(vis).filter(e => !e.disabled);
                    let hit = els.find(e => wanted.includes(norm(e.innerText)));
                    if (!hit) hit = els.find(e => {
                        const t = norm(e.innerText);
                        return t && wanted.some(w => t === w || (w.length > 3 && t.includes(w)));
                    });
                    if (!hit) return null;
                    hit.click();
                    return (hit.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 40);
                }""",
                [labels, field],
            )
            if hit:
                log.info("form.choice", field=field, value=value, label=hit)
                return True
            log.warning("form.choice_not_found", field=field, value=value,
                        wanted=labels, seen=self._visible_choices(page)[:8])
        except Exception as e:
            log.warning("form.choice_failed", field=field, error=str(e)[:80])
        return False

    def _set_loan_amount(self, page: Page, f: dict) -> bool:
        """Step 0 offers a free-text amount box plus three bucket buttons."""
        amount = f["loan_amount"]
        try:
            has_input = bool(page.locator('[name="loanreqamt"]').count())
        except Exception:
            has_input = False
        if has_input and self._text(page, "loanreqamt", str(amount)):
            return True
        bucket = "1000" if amount <= 1000 else "3000" if amount <= 3000 else "5000"
        return self._choose(page, "loanreqamt", bucket)

    def _click_next(self, page: Page) -> None:
        """Advance. Many steps in this SPA auto-advance on choice, so a missing
        Continue button is normal rather than an error."""
        try:
            page.evaluate(
                """(re) => {
                    const rx = new RegExp(re, 'i');
                    const vis = e => e.offsetParent !== null && e.getClientRects().length > 0;
                    const b = Array.from(document.querySelectorAll(
                        'button,[role=button],input[type=submit]'))
                        .filter(vis).filter(e => !e.disabled)
                        .find(e => rx.test((e.innerText || e.value || '').replace(/\\s+/g,' ').trim()));
                    if (b) b.click();
                }""",
                _ADVANCE_RE,
            )
        except Exception as e:
            log.warning("form.next_failed", error=str(e)[:80])

    def _await_change(self, page: Page, prev_sig: str, timeout: float = 20.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.6)
            if self._completion_state(page):
                return
            names = self._visible_field_names(page)
            if names and ",".join(sorted(names)) != prev_sig:
                return
            if not names and prev_sig.startswith("choices:"):
                return
            err = self._validation_error(page)
            if err:
                log.warning("form.validation_error", error=err[:110])
                return

    def _validation_error(self, page: Page) -> str:
        try:
            return page.evaluate(
                """() => {
                    const vis = e => e.offsetParent !== null;
                    const el = Array.from(document.querySelectorAll(
                        '[role=alert],[aria-invalid=true]+*,.error,.text-red-500,.text-destructive'))
                        .filter(vis)[0];
                    return el ? (el.innerText || '').trim().slice(0, 160) : '';
                }""") or ""
        except Exception:
            return ""

    def _completion_state(self, page: Page) -> str:
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
