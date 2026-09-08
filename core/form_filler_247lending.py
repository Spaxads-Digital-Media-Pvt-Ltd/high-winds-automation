"""
core/form_filler_247lending.py — 24/7 Lending Group post-offer application.

After Simple Lending Direct / ExaBucks / SimaCash complete their wizard, the
lender-match flow often lands on
https://www.247lendinggroup.com/unsecured-loan/apply.php
(new tab or in-place). That page is a second full application; leaving it
blank drops the lead. This module fills every visible field from the sheet
row and submits ``#submit-button``.
"""
from __future__ import annotations

import re
import time

import structlog
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

from core.lead_platform import FormFillerError

log = structlog.get_logger(__name__)

__all__ = ["fill_and_submit", "page_is_247"]


def page_is_247(page: Page) -> bool:
    try:
        url = (page.url or "").lower()
    except Exception:
        return False
    if "247lendinggroup.com" in url:
        return True
    try:
        return page.locator('form#form1 input[name="first_name"]').count() > 0
    except Exception:
        return False


def fill_and_submit(filler, page: Page, fields: dict, raw: dict, row_number: int, stop_event) -> None:
    """Fill the 247 apply form from the lead and click SUBMIT."""
    filler._check_stop(stop_event)
    try:
        page.bring_to_front()
    except Exception:
        pass
    for state in ("domcontentloaded", "load"):
        try:
            page.wait_for_load_state(state, timeout=20000)
        except Exception:
            pass
    try:
        page.wait_for_selector('input[name="first_name"]', timeout=25000)
    except PlaywrightTimeout as e:
        filler._screenshot(page, row_number, "247_form_missing")
        raise FormFillerError(
            "247 Lending Group page opened but the apply form did not render",
            error_type="stuck",
        ) from e

    vals = _map_247(fields, raw)
    log.info("form.247_fill_start", row=row_number, url=(page.url or "")[:90])
    filler._live(page)
    filler._screenshot(page, row_number, "247_arrived")

    alerts: list[str] = []

    def _on_dialog(dialog) -> None:
        alerts.append(dialog.message or "")
        try:
            dialog.accept()
        except Exception:
            try:
                dialog.dismiss()
            except Exception:
                pass

    page.on("dialog", _on_dialog)
    try:
        _fill_fields(page, vals, filler, row_number, stop_event)
        filler._screenshot(page, row_number, "247_ready")
        filler._live(page)
        _submit(page, filler, row_number, stop_event, alerts)
    finally:
        try:
            page.remove_listener("dialog", _on_dialog)
        except Exception:
            pass

    log.info("form.247_submitted", row=row_number, url=(page.url or "")[:90])
    filler._screenshot(page, row_number, "247_submitted")
    filler._live(page)


# ---------------------------------------------------------------- mapping


def _g(raw: dict, *keys: str) -> str:
    norm = {}
    for k, v in (raw or {}).items():
        nk = re.sub(r"\s+", " ", str(k)).strip().lower()
        if nk not in norm or str(v or "").strip():
            norm[nk] = v
    for k in keys:
        nk = re.sub(r"\s+", " ", str(k)).strip().lower()
        v = str(norm.get(nk) or "").strip()
        if v:
            return v
    return ""


def _digits(raw: str) -> str:
    return re.sub(r"\D", "", raw or "")


def _map_247(fields: dict, raw: dict) -> dict:
    dob = fields.get("dob") or ""
    mm = dd = yyyy = ""
    m = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", dob)
    if m:
        mm, dd, yyyy = m.group(1), m.group(2), m.group(3)

    ssn = _digits(fields.get("ssn") or "")
    phone = _phone_dashed(fields.get("phone") or "")

    monthly = fields.get("monthly_income")
    if not isinstance(monthly, int):
        try:
            monthly = int(float(re.sub(r"[,$\s]", "", str(monthly or "0"))))
        except (ValueError, TypeError):
            monthly = 3000
    annual = monthly * 12

    try:
        loan_amount = int(fields.get("loan_amount") or 5000)
    except (ValueError, TypeError):
        loan_amount = 5000

    debt_raw = _g(raw, "Credit Card Debt", "Debt Amount")
    try:
        debt = int(float(re.sub(r"[,$\s]", "", debt_raw) or "0"))
    except (ValueError, TypeError):
        debt = 0

    housing = _digits(_g(raw, "Monthly Housing Payment", "Monthly Payment", "Rent"))
    if not housing:
        housing = "1200"

    credit_num = _digits(_g(raw, "Credit Score Number"))[:3]
    if len(credit_num) != 3:
        credit_num = _credit_number(
            _g(raw, "Credit Score Rating", "Credit_Score"),
            fields.get("credit_score") or "",
        )

    return {
        "first_name": (fields.get("first_name") or "")[:25],
        "last_name": (fields.get("last_name") or "")[:25],
        "dob_m": mm,
        "dob_d": dd,
        "dob_y": yyyy,
        "street": (fields.get("street_address") or "")[:50],
        "zip": fields.get("zip") or "",
        "_months_at_address": _months_at_address(_g(raw, "Years at Address", "Months at Address")),
        "rent_or_own": "own" if str(fields.get("is_homeowner")) == "1" else "rent",
        "Monthlypayment": housing,
        "homephone": phone,
        "credit_rating": credit_num,
        "email": fields.get("email") or "",
        "auto_title": _auto_title(_g(raw, "Own a Car")),
        "checking": _yes_no(_g(raw, "Has Checking Account"), default="yes"),
        "yearly_income": _yearly_income(annual),
        "incomeSource": _income_source(_g(raw, "Income Source", "Primary Income Source")),
        "verify_income": _verify_income(_g(raw, "Verify Income")),
        "employer_name": (fields.get("employer_name") or "Employer")[:23],
        "direct_deposit": "yes" if str(fields.get("is_direct_deposit", "1")) == "1" else "no",
        "military": "yes" if str(fields.get("is_military")) == "1" else "no",
        "loan_amount": _loan_amount(loan_amount),
        "Loan_Purpose1": _loan_purpose(_g(raw, "Loan Purpose", "Loan_Purpose")),
        "total_debt": _total_debt(debt),
        "ssn_part_1": ssn[:3],
        "ssn_part_2": ssn[3:5],
        "ssn_part_3": ssn[5:9],
        "business_checking": _yes_no(_g(raw, "Business Checking Account"), default="no"),
        "business_age": _business_age(_g(raw, "Business Age")),
        "business_revenue": _business_revenue(_g(raw, "Business Revenue")),
    }


def _phone_dashed(phone: str) -> str:
    d = _digits(phone)
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    if len(d) != 10:
        return ""
    return f"{d[:3]}-{d[3:6]}-{d[6:]}"


def _credit_number(rating_text: str, code: str) -> str:
    raw = (rating_text or "").strip().lower()
    words = {
        "excellent": "750", "great": "750", "good": "680",
        "fair": "620", "poor": "580", "bad": "580",
    }
    for word, num in words.items():
        if raw.startswith(word) or word in raw:
            return num
    digits = _digits(rating_text)[:3]
    if len(digits) == 3:
        n = int(digits)
        if 300 <= n <= 850:
            return digits
    return {"2": "750", "3": "680", "4": "620", "5": "580"}.get(str(code), "650")


def _months_at_address(raw: str) -> str:
    raw_l = (raw or "").strip().lower()
    nums = re.findall(r"\d+", raw_l)
    n = int(nums[0]) if nums else 24
    months = n if ("month" in raw_l or n > 12) else n * 12
    if months <= 1:
        return "1"
    if months <= 2:
        return "2"
    if months <= 3:
        return "3"
    if months <= 6:
        return "6"
    if months <= 12:
        return "12"
    if months <= 24:
        return "24"
    return "30"


def _auto_title(raw: str) -> str:
    s = (raw or "").strip().lower()
    if not s:
        return "NA"
    if any(k in s for k in ("paid", "full title", "owned outright", "own it")):
        return "Full Title"
    if any(k in s for k in ("payment", "financ", "loan")):
        return "Payments"
    if s in {"yes", "y"}:
        return "Full Title"
    return "NA"


def _yes_no(raw: str, default: str = "yes") -> str:
    s = (raw or "").strip().lower()
    if not s:
        return default
    if s in {"yes", "y", "true", "1"}:
        return "yes"
    if s in {"no", "n", "false", "0"}:
        return "no"
    return default


def _verify_income(raw: str) -> str:
    s = (raw or "").strip().lower()
    if s in {"no", "n", "false", "0"}:
        return "no"
    return "Yes"


def _yearly_income(annual: int) -> str:
    brackets = [
        (10000, "10000"), (15000, "12000"), (20000, "18000"), (30000, "25000"),
        (40000, "35000"), (50000, "48000"), (60000, "55000"), (70000, "65000"),
        (80000, "75000"), (90000, "85000"), (100000, "95000"),
    ]
    for upper, val in brackets:
        if annual < upper:
            return val
    return "100000"


def _income_source(raw: str) -> str:
    s = (raw or "").strip().lower()
    if "self" in s:
        return "Self Employed"
    if "unemploy" in s:
        return "Unemployed"
    if "military" in s and "pension" in s:
        return "Other1"
    if "pension" in s or "retire" in s:
        return "Other1"
    if "annuit" in s:
        return "NonGovernmentAnnuity"
    if s in {"other", "benefits", "benefit", "disability", "social security"}:
        return "Other" if s == "other" else "Other1"
    return "Employed"


def _loan_amount(amount: int) -> str:
    brackets = [
        (1000, "1000"), (1500, "1500"), (2000, "2000"), (2500, "2500"),
        (5000, "5000"), (7500, "7500"), (10000, "10000"), (15000, "15000"),
        (20000, "20000"), (25000, "25000"), (30000, "30000"),
    ]
    for upper, val in brackets:
        if amount < upper:
            return val
    return "35000"


def _loan_purpose(raw: str) -> str:
    s = (raw or "").strip().lower()
    pairs = [
        ("start", "StartUp"),
        ("small business", "Business"),
        ("business", "Business"),
        ("debt", "DebtConsolidation"),
        ("consol", "DebtConsolidation"),
        ("credit card", "DebtConsolidation"),
        ("car purchase", "CarPurchase"),
        ("vehicle purchase", "CarPurchase"),
        ("home improve", "HomeImprovement"),
        ("auto repair", "Auto"),
        ("car repair", "Auto"),
        ("clothing", "Clothing"),
        ("tax", "Taxes"),
        ("medical", "Medical"),
        ("travel", "Travel"),
        ("vacation", "Travel"),
        ("furniture", "Furniture"),
        ("appliance", "Furniture"),
        ("holiday", "Holiday"),
        ("funeral", "Funeral"),
        ("dental", "Dental"),
        ("personal", "Personal"),
        ("emergency", "Personal"),
        ("other", "Other"),
    ]
    for needle, val in pairs:
        if needle in s:
            return val
    return "Personal"


def _total_debt(amount: int) -> str:
    if amount <= 0:
        return "0"
    brackets = [
        (5000, "2500"), (6000, "5000"), (7500, "6000"), (10000, "10000"),
        (15000, "15000"), (20000, "20000"), (25000, "25000"), (30000, "30000"),
        (35000, "35000"), (40000, "40000"), (45000, "45000"), (50000, "50000"),
    ]
    for upper, val in brackets:
        if amount < upper:
            return val
    return "50001"


def _business_age(raw: str) -> str:
    s = re.sub(r"\s+", "", (raw or "").strip().lower())
    if not s:
        return "Not yet started"
    mapping = [
        ("notyet", "Not yet started"),
        ("1-6", "1-6Months"),
        ("16month", "1-6Months"),
        ("7-12", "7-12Months"),
        ("712month", "7-12Months"),
        ("1-2", "1-2Years"),
        ("12year", "1-2Years"),
        ("2-5", "2-5Years"),
        ("25year", "2-5Years"),
        ("over5", "Over5Years"),
        ("5+", "Over5Years"),
    ]
    for needle, val in mapping:
        if needle in s:
            return val
    return "Not yet started"


def _business_revenue(raw: str) -> str:
    digits = _digits(raw)
    try:
        n = int(digits) if digits else 50000
    except ValueError:
        n = 50000
    if n >= 500000:
        return "500000"
    if n >= 300000:
        return "300000"
    if n >= 150000:
        return "150000"
    if n >= 100000:
        return "100000"
    return "50000"


# ---------------------------------------------------------------- DOM


def _fill_fields(page: Page, vals: dict, filler, row_number: int, stop_event) -> None:
    texts = (
        ("input[name=first_name]", vals["first_name"]),
        ("input[name=last_name]", vals["last_name"]),
        ("input[name=street]", vals["street"]),
        ("#zip", vals["zip"]),
        ("input[name=Monthlypayment]", vals["Monthlypayment"]),
        ("#homephone", vals["homephone"]),
        ("#credit_rating", vals["credit_rating"]),
        ("#email", vals["email"]),
        ("input[name=employer_name]", vals["employer_name"]),
    )
    for sel, value in texts:
        filler._check_stop(stop_event)
        _set_text(page, sel, value)
        time.sleep(0.15)

    # Zip lookup fills hidden city/state. Must run before submit.
    try:
        page.locator("#zip").evaluate("el => el.blur()")
        page.evaluate("() => { if (typeof cszpop === 'function') cszpop(); }")
        time.sleep(0.6)
    except Exception as e:
        log.warning("form.247_zip_lookup", error=str(e)[:80], row=row_number)

    selects = (
        ("#dob_m", vals["dob_m"]),
        ("#dob_d", vals["dob_d"]),
        ("#dob_y", vals["dob_y"]),
        ('select[name="_months_at_address"]', vals["_months_at_address"]),
        ('select[name="rent_or_own"]', vals["rent_or_own"]),
        ("#auto_title", vals["auto_title"]),
        ('select[name="checking"]', vals["checking"]),
        ("#incomeSource", vals["incomeSource"]),
        ('select[name="verify_income"]', vals["verify_income"]),
        ("#direct_deposit", vals["direct_deposit"]),
        ("#military-2", vals["military"]),
        ('select[name="loan_amount"]', vals["loan_amount"]),
        ("#Loan_Purpose1", vals["Loan_Purpose1"]),
        ("#total_debt", vals["total_debt"]),
    )
    for sel, value in selects:
        filler._check_stop(stop_event)
        if value:
            _set_select(page, sel, value)

    # Yearly income onChange reveals the SSN fields.
    _set_select(page, "#yearly_income", vals["yearly_income"])
    try:
        page.evaluate("() => { jQuery('#ssn-div').show(); }")
    except Exception:
        pass
    try:
        page.locator("#ssn1").wait_for(state="visible", timeout=5000)
    except PlaywrightTimeout:
        try:
            page.locator("#ssn-div").evaluate("el => { el.style.display = 'block'; }")
        except Exception:
            pass

    _set_text(page, "#ssn1", vals["ssn_part_1"])
    _set_text(page, "#ssn2", vals["ssn_part_2"])
    _set_text(page, "#ssn3", vals["ssn_part_3"])

    try:
        if page.locator("#business-checking").is_visible():
            _set_select(page, "#business_checking", vals["business_checking"])
        if page.locator("#business-information").is_visible():
            _set_select(page, 'select[name="business_age"]', vals["business_age"])
            _set_select(page, 'select[name="business_revenue"]', vals["business_revenue"])
    except Exception:
        pass

    time.sleep(0.4)


def _set_text(page: Page, selector: str, value: str) -> None:
    loc = page.locator(selector).first
    try:
        loc.wait_for(state="visible", timeout=4000)
    except PlaywrightTimeout:
        log.warning("form.247_field_hidden", selector=selector)
        return
    loc.click()
    loc.fill("")
    if value:
        loc.fill(str(value))
        loc.dispatch_event("input")
        loc.dispatch_event("change")
        loc.dispatch_event("blur")


def _set_select(page: Page, selector: str, value: str) -> None:
    loc = page.locator(selector).first
    try:
        loc.wait_for(state="attached", timeout=4000)
        loc.select_option(value=str(value))
        loc.dispatch_event("change")
    except Exception as e:
        log.warning("form.247_select_failed", selector=selector, value=value, error=str(e)[:80])


def _submit(page: Page, filler, row_number: int, stop_event, alerts: list[str]) -> None:
    filler._check_stop(stop_event)
    btn = page.locator("#submit-button")
    try:
        btn.wait_for(state="visible", timeout=5000)
    except PlaywrightTimeout as e:
        raise FormFillerError(
            "247 Lending Group SUBMIT button was not on the page",
            error_type="stuck",
        ) from e

    url_before = page.url or ""
    time.sleep(0.5)
    try:
        with page.expect_navigation(timeout=35000, wait_until="domcontentloaded"):
            btn.click()
    except PlaywrightTimeout:
        msg = alerts[-1] if alerts else ""
        still = (page.url or "") == url_before or "apply.php" in (page.url or "")
        if still:
            filler._screenshot(page, row_number, "247_submit_blocked")
            raise FormFillerError(
                f"247 Lending Group form did not submit"
                + (f": {msg}" if msg else " (still on apply.php)"),
                error_type="field_rejected",
            )
    if alerts:
        # Validator alerts abort submit; navigation that still happened is fine.
        log.info("form.247_alert", message=alerts[-1][:120], row=row_number)
        if "apply.php" in (page.url or ""):
            filler._screenshot(page, row_number, "247_validator")
            raise FormFillerError(
                f"247 Lending Group rejected the form: {alerts[-1]}",
                error_type="field_rejected",
            )

    try:
        page.wait_for_load_state("load", timeout=15000)
    except Exception:
        pass
    time.sleep(1.5)
    filler._live(page)
