"""
core/poster_roundsky.py — Round Sky / LeadHorizon payday ping-post integration.

A server-to-server poster, not a browser automation.  It exposes the same
``FormFiller`` / ``FormFillerError`` interface the engine already drives, so it
drops into app.py alongside the Playwright fillers with no engine changes — but
there is no browser, no proxy rotation and no device fingerprint involved.  A
post takes well under a second instead of ~70s, and the buyer answers with a
real decision and reason rather than a screenshot to interpret.

Protocol (from Round Sky's REVSHARE integration doc):
  * POST form-encoded fields to the test or live endpoint.
  * Responses come back pipe / xml / json; this uses json.
  * DECISION is APPROVED or DECLINED, with LEADID, PRICE, MESSAGE and URL.
  * Price ladder: post one ``minimum_price`` at a time, descending, reposting
    the same lead lower until it sells (e.g. 50 -> 20 -> 3).  ``tier`` is the
    legacy alternative — never send both.
  * Test endpoint: first_name "approved" forces APPROVED, "declined" forces
    DECLINED.

Mandatory buyer-side filters, applied here *before* posting so a lead that
cannot qualify never leaves the machine:
    checking accounts only · no active military · no NY · age 20-80 ·
    monthly income 1200-10000 · work_phone must differ from home_phone

Two fields are deliberately not synthesised:

  ``customer_ip`` and ``browser_info`` describe the *applicant's* own session
  and are what a buyer uses to judge where a lead came from.  Inventing them
  would misrepresent the lead's origin, so they are read from the sheet
  (captured at lead-generation time) and a row without them is rejected rather
  than filled in with something plausible.

See also the redirect obligation in the docs: an approved lead is supposed to
have the applicant's browser sent to the returned URL, and Round Sky pays only
for approved leads that redirect once the rate drops below 90%.  This module
records that URL on the row; it does not fetch it, because a server-side fetch
is not a consumer redirect.  ROUNDSKY_FOLLOW_REDIRECT=true will fetch it, and
is honoured only against the test endpoint, where it exists to complete Round
Sky's step-3 parsing check.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
import structlog

log = structlog.get_logger(__name__)

__all__ = ["FormFiller", "FormFillerError"]

TEST_URL = "https://www.leadhorizon.com/leads/payday/test.php"
LIVE_URL = "https://www.leadhorizon.com/leads/payday/live.php"

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


def _digits(raw: Any) -> str:
    return re.sub(r"\D", "", str(raw or ""))


class FormFillerError(Exception):
    """Raised for a lead that cannot be posted, or that the buyer declined."""

    def __init__(self, message: str, error_type: str = "unknown"):
        super().__init__(message)
        self.error_type = error_type


class FormFiller:
    """Posts one sheet row to Round Sky and reports the buyer's decision."""

    # Filters Round Sky require us to apply on our side.
    _MIN_AGE, _MAX_AGE = 20, 80
    _MIN_INCOME, _MAX_INCOME = 1200, 10000
    _BLOCKED_STATES = {"NY"}

    def __init__(self, config: dict) -> None:
        self._config = config
        cfg = config.get("roundsky", {}) or {}
        self._sub_id = os.getenv("ROUNDSKY_SUB_ID", cfg.get("sub_id", "")).strip()
        self._domain = os.getenv("ROUNDSKY_DOMAIN", cfg.get("domain", "")).strip()
        self._partner = os.getenv("ROUNDSKY_PARTNER", "").strip()
        self._password = os.getenv("ROUNDSKY_PASSWORD", "").strip()
        self._time_allowed = max(20, int(cfg.get("time_allowed", 30)))
        # Descending ladder: try to sell high, then repost cheaper.
        self._prices = [str(p) for p in cfg.get("minimum_prices", [50, 20, 3])]
        self._tier = str(cfg.get("tier", "")).strip()
        endpoint = os.getenv("ROUNDSKY_ENDPOINT", cfg.get("endpoint", "test")).strip().lower()
        self._live = endpoint == "live"
        self._url = LIVE_URL if self._live else TEST_URL
        self._follow_redirect = (
            os.getenv("ROUNDSKY_FOLLOW_REDIRECT", "").strip().lower() in {"1", "true", "yes"}
        )
        self._ss_dir = Path(config.get("screenshots", {}).get("directory", "screenshots"))

    # ------------------------------------------------------------------ public

    def process_row(
        self,
        row: dict[str, Any],
        fingerprint: dict[str, Any] | None = None,   # unused: no browser here
        proxy_url: str | None = None,
        row_number: int = 0,
        stop_event=None,
    ) -> dict[str, Any]:
        if not self._partner or not self._password:
            raise FormFillerError(
                "ROUNDSKY_PARTNER / ROUNDSKY_PASSWORD are not set in .env",
                error_type="config",
            )
        if stop_event is not None and stop_event.is_set():
            raise FormFillerError("Stopped by user", error_type="stopped")

        fields = self._parse_fields(row)
        self._validate(fields)
        self._apply_buyer_filters(fields)

        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        payload = self._build_payload(fields)

        log.info("roundsky.posting", row=row_number, endpoint="live" if self._live else "test",
                 state=fields["state"], income=fields["monthly_income"],
                 prices=self._prices or [f"tier={self._tier}"])

        last: dict[str, str] = {}
        for attempt, price in enumerate(self._ladder(), start=1):
            if stop_event is not None and stop_event.is_set():
                raise FormFillerError("Stopped by user", error_type="stopped")
            body = dict(payload)
            body.update(price)
            try:
                resp = requests.post(self._url, data=body, proxies=proxies,
                                     timeout=self._time_allowed + 10)
            except requests.RequestException as e:
                raise FormFillerError(f"Post failed: {e}", error_type="network") from e

            parsed = self._parse_response(resp.text)
            last = parsed
            log.info("roundsky.response", row=row_number, step=attempt,
                     at=price.get("minimum_price") or price.get("tier"),
                     decision=parsed.get("DECISION"), price=parsed.get("PRICE"),
                     message=parsed.get("MESSAGE"), lead_id=parsed.get("LEADID"))

            if self._is_terminal_decline(parsed.get("MESSAGE", "")):
                # A malformed or missing field will be rejected at every price,
                # so walking the rest of the ladder just burns posts.
                raise FormFillerError(
                    f"DECLINED — {parsed.get('MESSAGE', '')}", error_type="declined")

            if (parsed.get("DECISION") or "").upper() == "APPROVED":
                url = parsed.get("URL", "")
                self._record_redirect(url, row_number)
                sold_at = price.get("minimum_price") or f"tier {price.get('tier')}"
                return {
                    "status": "Success",
                    "notes": (f"APPROVED ${parsed.get('PRICE', '?')} — "
                              f"{parsed.get('MESSAGE', '')} (min {sold_at}) | {url}"),
                    "submission_id": parsed.get("LEADID", ""),
                }

        raise FormFillerError(
            f"DECLINED — {last.get('MESSAGE', 'no buyer')} "
            f"(lead {last.get('LEADID', 'n/a')})",
            error_type="declined",
        )

    # ------------------------------------------------------------------ posting

    # Decline reasons that describe the lead itself rather than the market.
    # Reposting cheaper cannot fix them, so the ladder stops on sight.
    _TERMINAL_DECLINE = re.compile(
        r"invalid|missing|duplicate|malformed|not\s+allowed|blocked|banned", re.I)

    def _is_terminal_decline(self, message: str) -> bool:
        return bool(message) and bool(self._TERMINAL_DECLINE.search(message))

    def _ladder(self):
        """Yield the price/tier parameter for each successive post attempt.
        minimum_price and tier are mutually exclusive — never send both."""
        if self._tier:
            yield {"tier": self._tier}
            return
        for p in self._prices:
            yield {"minimum_price": p}

    def _build_payload(self, f: dict) -> dict[str, str]:
        payload = {
            "partner": self._partner,
            "partner_password": self._password,
            "customer_ip": f["customer_ip"],
            "sub_id": self._sub_id,
            "domain": self._domain,
            "time_allowed": str(self._time_allowed),
            "response_type": "json",
            "browser_info": f["browser_info"][:250],
            "state": f["state"],
            "first_name": f["first_name"],
            "last_name": f["last_name"],
            "email": f["email"],
            "home_phone": f["home_phone"],
            "zip": f["zip"],
            "address": f["address"],
            "city": f["city"],
            "housing": f["housing"],
            "monthly_income": f["monthly_income"],
            "account_type": f["account_type"],
            "direct_deposit": f["direct_deposit"],
            "pay_period": f["pay_period"],
            "next_pay_date": f["next_pay_date"],
            "requested_loan_amount": f["requested_loan_amount"],
            "months_at_residence": f["months_at_residence"],
            "income_type": f["income_type"],
            "active_military": f["active_military"],
            "employer": f["employer"],
            "work_phone": f["work_phone"],
            "months_employed": f["months_employed"],
            "bank_name": f["bank_name"],
            "account_number": f["account_number"],
            "routing_number": f["routing_number"],
            "months_with_bank": f["months_with_bank"],
            "driving_license_state": f["driving_license_state"],
            "driving_license_number": f["driving_license_number"],
            "birth_date": f["birth_date"],
            "social_security_number": f["social_security_number"],
        }
        # Optional — the docs note these lift buyer coverage, so send when present.
        for key in ("second_pay_date", "occupation", "high_debt",
                    "creditScore", "has_clean_title"):
            if f.get(key):
                payload[key] = f[key]
        return payload

    def _parse_response(self, text: str) -> dict[str, str]:
        text = (text or "").strip()
        if not text:
            return {"DECISION": "DECLINED", "MESSAGE": "empty response"}
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return {str(k).upper(): str(v) for k, v in data.items()}
        except ValueError:
            pass
        # Fall back to the pipe format: DECISION|LEADID|PRICE|MESSAGE|URL
        parts = text.split("|")
        if len(parts) >= 4:
            return {"DECISION": parts[0].strip(), "LEADID": parts[1].strip(),
                    "PRICE": parts[2].strip(), "MESSAGE": parts[3].strip(),
                    "URL": parts[4].strip() if len(parts) > 4 else ""}
        return {"DECISION": "DECLINED", "MESSAGE": text[:160]}

    def _record_redirect(self, url: str, row_number: int) -> None:
        """Keep the approved lead's redirect URL, and only fetch it in test mode.

        Round Sky pay for approved leads that redirect; the redirect is the
        applicant's own browser being sent to the buyer.  Fetching it from here
        is not that, so it is off by default and refused against live.
        """
        if not url:
            return
        if not self._follow_redirect:
            return
        if self._live:
            log.warning("roundsky.redirect_not_followed",
                        msg="ROUNDSKY_FOLLOW_REDIRECT ignored on the live endpoint — "
                            "a server-side fetch is not a consumer redirect",
                        row=row_number)
            return
        try:
            r = requests.get(url, timeout=20)
            log.info("roundsky.test_redirect_followed", row=row_number,
                     status=r.status_code, body=r.text.strip()[:200])
        except requests.RequestException as e:
            log.warning("roundsky.test_redirect_failed", row=row_number, error=str(e)[:100])

    # ------------------------------------------------------------------ mapping

    def _parse_fields(self, row: dict) -> dict[str, str]:
        def g(*keys: str) -> str:
            for k in keys:
                v = str(row.get(k) or "").strip()
                if v:
                    return v
            return ""

        home_phone = _digits(g("Phone Number", "Phone"))
        work_phone = _digits(g("Employer Work Phone", "Work Phone"))
        income = self._int(g("Monthly Net Income ($)", "Monthly_Income"))
        debt = self._int(g("Credit Card Debt", "Debt Amount"))

        return {
            "customer_ip":  g("Customer IP", "IP Address"),
            "browser_info": g("Browser Info", "User Agent"),
            "first_name":   g("First Name", "First_Name"),
            "last_name":    g("Last Name", "Last_Name"),
            "email":        g("Email Address", "Email"),
            "home_phone":   home_phone,
            "work_phone":   work_phone or home_phone,
            "zip":          _digits(g("ZIP Code", "Zip")).zfill(5)[:5],
            "address":      g("Street Address", "Address"),
            "city":         g("City"),
            "state":        self._state(g("State")),
            "housing":      "own" if self._truthy(g("Homeowner")) else "rent",
            "monthly_income": str(income),
            "account_type": "savings" if g("Account Type").lower().startswith("sav") else "checking",
            "direct_deposit": "true" if self._truthy(g("Direct Deposit"), default=True) else "false",
            "pay_period":   self._pay_period(g("Pay Frequency", "Pay_Frequency")),
            "next_pay_date":   self._date(g("Next Pay Date")),
            "second_pay_date": self._date(g("Second Pay Date")),
            "requested_loan_amount": self._loan_amount(g("Requested Loan Amount ($)")),
            "months_at_residence": self._months(g("Months at Address", "Years at Address")),
            "months_employed":     self._months(g("Months at Employer", "Years at Employer")),
            "months_with_bank":    self._months(g("Months at Bank", "Years at Bank")),
            "income_type":  self._income_type(g("Income Source")),
            "active_military": "true" if self._truthy(g("Military")) else "false",
            "occupation":   g("Occupation", "Job Title"),
            "employer":     g("Employer Name", "Employer_Name"),
            "bank_name":    g("Bank Name", "bankName"),
            "account_number":  _digits(g("Account Number")),
            "routing_number":  _digits(g("ABA Routing Number", "Routing Number")),
            "driving_license_state":  self._state(g("Driver License State") or g("State")),
            "driving_license_number": g("Driver License / ID Number"),
            "birth_date":   self._date(g("Date of Birth (DOB)", "DOB")),
            "social_security_number": _digits(g("SSN Full", "SSN")),
            "high_debt":    "true" if debt >= 10000 else "false",
            "creditScore":  self._credit_score(g("Credit Score Rating")),
            "has_clean_title": ("true" if self._truthy(g("Has Clean Title"))
                                else ("false" if g("Has Clean Title") else "")),
        }

    # ------------------------------------------------------------------ checks

    def _validate(self, f: dict) -> None:
        required = [
            "customer_ip", "browser_info", "first_name", "last_name", "email",
            "home_phone", "zip", "address", "housing", "monthly_income",
            "account_type", "direct_deposit", "pay_period", "next_pay_date",
            "requested_loan_amount", "months_at_residence", "income_type",
            "employer", "work_phone", "months_employed", "bank_name",
            "account_number", "routing_number", "months_with_bank",
            "driving_license_state", "driving_license_number", "birth_date",
            "social_security_number",
        ]
        missing = [k for k in required if not f.get(k)]
        if not self._sub_id:
            missing.append("sub_id(ROUNDSKY_SUB_ID)")
        if not self._domain:
            missing.append("domain(ROUNDSKY_DOMAIN)")
        if missing:
            raise FormFillerError(
                f"Missing required fields: {missing}", error_type="missing_data")

    def _apply_buyer_filters(self, f: dict) -> None:
        """Round Sky require these on our side; a lead failing one is not
        postable, so reject it here rather than spend a post on a sure decline."""
        reasons = []
        if f["account_type"] != "checking":
            reasons.append("account is not checking")
        if f["active_military"] == "true":
            reasons.append("active military")
        if f["state"] in self._BLOCKED_STATES:
            reasons.append(f"state {f['state']} is filtered")
        age = self._age(f["birth_date"])
        if age is None:
            reasons.append("birth_date unreadable")
        elif not (self._MIN_AGE <= age <= self._MAX_AGE):
            reasons.append(f"age {age} outside {self._MIN_AGE}-{self._MAX_AGE}")
        income = self._int(f["monthly_income"])
        if not (self._MIN_INCOME <= income <= self._MAX_INCOME):
            reasons.append(
                f"income {income} outside {self._MIN_INCOME}-{self._MAX_INCOME}")
        if f["work_phone"] and f["work_phone"] == f["home_phone"]:
            reasons.append("work_phone equals home_phone")
        if reasons:
            raise FormFillerError("Filtered: " + "; ".join(reasons),
                                  error_type="filtered")

    # ------------------------------------------------------------- conversions

    @staticmethod
    def _int(raw: str) -> int:
        try:
            return int(float(re.sub(r"[,$\s]", "", str(raw or "")) or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _truthy(raw: str, default: bool = False) -> bool:
        raw = (raw or "").strip().lower()
        if not raw:
            return default
        return raw in {"yes", "y", "true", "1", "own", "owner"}

    def _state(self, raw: str) -> str:
        raw = (raw or "").strip()
        if len(raw) == 2:
            return raw.upper()
        return _STATE_CODES.get(raw.lower(), raw.upper()[:2])

    def _date(self, raw: str) -> str:
        """Round Sky accept YYYY-MM-DD or MM/DD/YYYY; normalise to the former."""
        raw = (raw or "").strip()
        if not raw:
            return ""
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%d/%m/%Y", "%m/%d/%y", "%Y/%m/%d"):
            try:
                return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
            except ValueError:
                pass
        return raw

    def _age(self, birth_date: str) -> int | None:
        try:
            d = datetime.strptime(birth_date, "%Y-%m-%d")
        except (TypeError, ValueError):
            return None
        now = datetime.now()
        return now.year - d.year - ((now.month, now.day) < (d.month, d.day))

    def _months(self, raw: str) -> str:
        """Sheet may hold years or months; Round Sky want months, max 240."""
        raw = (raw or "").strip().lower()
        nums = re.findall(r"\d+", raw)
        if not nums:
            return ""
        n = int(nums[0])
        months = n if ("month" in raw or n > 20) else n * 12
        return str(min(months, 240))

    def _loan_amount(self, raw: str) -> str:
        """$50 increments, 50..50000."""
        amount = self._int(raw) or 500
        amount = max(50, min(50000, amount))
        return str(int(round(amount / 50.0)) * 50 or 50)

    def _pay_period(self, raw: str) -> str:
        raw = (raw or "").strip().lower()
        if "semi" in raw or "twice" in raw:
            return "twice monthly"
        if "bi" in raw and "week" in raw:
            return "biweekly"
        if "week" in raw:
            return "weekly"
        if "month" in raw:
            return "monthly"
        return "biweekly"

    def _income_type(self, raw: str) -> str:
        raw = (raw or "").strip().lower()
        if any(k in raw for k in ("benefit", "unemploy", "disab", "social", "pension", "retire")):
            return "benefits"
        if "self" in raw:
            return "self_employed"
        return "employment"

    def _credit_score(self, raw: str) -> str:
        raw = (raw or "").strip().lower()
        if not raw:
            return ""
        words = {"excellent": "720", "great": "720", "good": "660",
                 "fair": "600", "poor": "590"}
        for word, score in words.items():
            if raw.startswith(word):
                return score
        digits = re.sub(r"\D", "", raw)[:3]
        return digits if len(digits) == 3 else ""
