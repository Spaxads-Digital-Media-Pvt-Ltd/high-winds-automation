"""Diagnose the i_ad_ccDebtAmt step, and every other <select>-backed step.

No auto-pick fallback: if the filler cannot set a field, the walk stops there
and reports it, instead of quietly clicking something to keep going (which is
what hid this failure last time).  Hard-stops before the bank step.
"""
import json
import os
import sys
import time
import traceback

os.environ["BROWSER_CHANNEL"] = "chrome"
sys.path.insert(0, "/Users/mac/Desktop/Lead-Automation-New")

import structlog
import yaml
from playwright.sync_api import sync_playwright

structlog.configure(processors=[structlog.processors.KeyValueRenderer(key_order=["event"])],
                    logger_factory=structlog.PrintLoggerFactory())
from core.form_filler_mlw import FormFiller  # noqa: E402

INSPECT = """(n) => {
  const vis = e => e.offsetParent !== null && e.getClientRects().length > 0;
  const sel = document.querySelector('select[name="' + n + '"]');
  if (!sel) return {present: false};
  const base = sel.closest('[data-slot="base"]');
  const hiddenWrap = sel.closest('[data-testid="hidden-select-container"]');
  const trig = base ? base.querySelector('button[data-slot="trigger"],[aria-haspopup="listbox"],button') : null;
  return {
    present: true,
    value: sel.value,
    optCount: sel.options.length,
    firstOpts: Array.from(sel.options).slice(0, 4).map(o => o.value + '|' + o.text),
    selVisible: vis(sel),
    isHeroUI: !!hiddenWrap,
    hasBase: !!base,
    triggerText: trig ? (trig.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 50) : null,
    triggerTag: trig ? trig.tagName + '/' + (trig.getAttribute('data-slot') || '') : null,
  };
}"""

OPTIONS_NOW = """() => Array.from(document.querySelectorAll('[role=option]'))
    .map(o => ({k: o.getAttribute('data-key'), t: (o.innerText || '').trim().slice(0, 28)}))"""

ROW = {"First Name": "Elena", "Last Name": "Vasquez", "Email Address": "elena.vasquez@example.com",
       "Phone Number": "(305) 555-0151", "Date of Birth (DOB)": "06/18/1988",
       "SSN Full": "900-45-6701", "Street Address": "720 Brickell Ave", "City": "Miami",
       "State": "FL", "ZIP Code": "33131", "ABA Routing Number": "267084131",
       "Account Number": "400500600700", "Requested Loan Amount ($)": "5000",
       "Monthly Net Income ($)": "3500", "Credit Card Debt": "8000",
       "Years at Address": "4", "Years at Employer": "3", "Years at Bank": "6",
       "Homeowner": "No", "Military": "No", "Direct Deposit": "Yes",
       "Income Source": "Employed", "Pay Frequency": "Bi-Weekly",
       "Employer Name": "Bayfront Media", "Employer Work Phone": "(305) 555-0152",
       "Driver License / ID Number": "FL30221", "Driver License State": "FL",
       "Account Type": "Checking", "Credit Score Rating": "680",
       "Loan Purpose": "Debt consolidation"}

cfg = yaml.safe_load(open("/Users/mac/Desktop/Lead-Automation-New/config.yaml"))
cfg["screenshots"]["directory"] = "/tmp/mlw_debt"
cfg["delays"] = {"min_typing_delay": 0.01, "max_typing_delay": 0.02,
                 "min_action_delay": 0.15, "max_action_delay": 0.3}
ff = FormFiller(cfg)
f = ff._parse_fields(ROW)
print("parsed debt_bracket =", repr(f["debt_bracket"]),
      " income_bracket =", repr(f["income_bracket"]), flush=True)

try:
    with sync_playwright() as pw:
        br = pw.chromium.launch(headless=True, channel="chrome",
                                args=["--no-sandbox", "--disable-dev-shm-usage"])
        p = br.new_context(
            user_agent="Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/125.0.6422.165 Mobile Safari/537.36",
            viewport={"width": 412, "height": 915}, is_mobile=True, has_touch=True).new_page()
        p.goto("https://www.mylendingwallet.com/", wait_until="commit", timeout=90000)
        ff._prepare(p, 1)

        for i in range(30):
            time.sleep(1.1)
            names = ff._visible_field_names(p)
            if not names:
                continue
            if any(n in ("baba", "bacc") for n in names):
                print("\nreached bank step — stopping, nothing submitted", flush=True)
                break

            print(f"\n=== step {i}: {names}", flush=True)
            for n in names:
                info = p.evaluate(INSPECT, n)
                if info.get("present"):
                    print(f"   [{n}] select: heroui={info['isHeroUI']} base={info['hasBase']} "
                          f"opts={info['optCount']} trigger={info['triggerText']!r} "
                          f"({info['triggerTag']})", flush=True)
                    print(f"        first opts: {info['firstOpts']}", flush=True)

            res = ff._handle_step(p, names, f)
            print(f"   handle_step -> {res}", flush=True)
            if res["failed"]:
                bad = res["failed"][0]
                print(f"\n   !!! FAILED on {bad} — diagnosing", flush=True)
                info = p.evaluate(INSPECT, bad)
                print("   inspect:", json.dumps(info), flush=True)
                # open its listbox by hand and show what is really inside
                try:
                    base = p.locator(f'select[name="{bad}"]').locator(
                        'xpath=ancestor::*[@data-slot="base"][1]')
                    print("   base count:", base.count(), flush=True)
                    trig = base.locator(
                        'button[data-slot="trigger"], [aria-haspopup="listbox"], button').first
                    print("   trigger count:", trig.count(), flush=True)
                    trig.click()
                    time.sleep(1.0)
                    print("   listboxes:", p.locator('[role=listbox]').count(),
                          " options:", p.locator('[role=option]').count(), flush=True)
                    print("   options now:", json.dumps(p.evaluate(OPTIONS_NOW))[:500], flush=True)
                    want = f["debt_bracket"] if bad == "i_ad_ccDebtAmt" else None
                    if want:
                        print(f"   target data-key {want!r} present:",
                              p.locator(f'[role=option][data-key="{want}"]').count(), flush=True)
                except Exception as e:
                    print("   probe error:", str(e)[:120], flush=True)
                p.screenshot(path="/tmp/mlw_debt_fail.png")
                break
            ff._click_next(p)
        br.close()
except Exception:
    traceback.print_exc()
