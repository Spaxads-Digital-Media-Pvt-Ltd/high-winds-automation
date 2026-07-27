"""
app.py — Multi-Site Lead Automation Web UI
──────────────────────────────────────────
Run multiple offers simultaneously. Each offer has its own engine thread,
log queue, stats, screenshot directory, and stop event.

Open: http://localhost:5000
"""
from __future__ import annotations

import importlib
import json
import os
import queue
import socket
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import requests as _requests
import yaml

from utils.lead_pacer import LeadPacer, ProcessingConfig
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, make_response, render_template_string, request

import urllib3.util.connection as _urllib3_cn
_urllib3_cn.allowed_gai_family = lambda: socket.AF_INET

load_dotenv()

app = Flask(__name__)

# ── Offers registry ───────────────────────────────────────────────────────────
# Every offer the project knows about.  Set "enabled": False to take one out of
# circulation without deleting anything — its filler, sheet tab, .env keys and
# mock all stay in place, and flipping the flag back brings it straight back.
ALL_OFFERS: dict[str, dict] = {
    "american_emergency_fund": {
        "name":          "American Emergency Fund",
        "url":           "https://www.americanemergencyfund.com/",
        "filler":        "core.form_filler_aef",
        "color":         "#38bdf8",
        "sheet_url_env": "SHEET_URL_AEF",
        "sheet_ws_env":  "SHEET_WS_AEF",
        "enabled":       True,
    },
    "my_lending_wallet": {
        "name":          "MyLendingWallet",
        "url":           "https://www.mylendingwallet.com/",
        "filler":        "core.form_filler_mlw",
        "color":         "#4ade80",
        "sheet_url_env": "SHEET_URL_MLW",
        "sheet_ws_env":  "SHEET_WS_MLW",
        # Parked: the filler drives the live form correctly up to the bank step,
        # but the final submit has never been exercised, so it is not fit to run
        # unattended yet.  Re-enable once that last step is confirmed.
        "enabled":       False,
    },
    "roundsky": {
        "name":          "Round Sky (ping-post)",
        # Server-to-server: no browser, so the URL is informational only —
        # the real endpoint comes from ROUNDSKY_ENDPOINT / config.yaml.
        "url":           "https://www.leadhorizon.com/leads/payday/test.php",
        "filler":        "core.poster_roundsky",
        "color":         "#fbbf24",
        "sheet_url_env": "SHEET_URL_ROUNDSKY",
        "sheet_ws_env":  "SHEET_WS_ROUNDSKY",
        "enabled":       True,
    },
}

# What the UI, the engines and the scheduler actually see.  Disabled offers are
# absent from here, so no card, route, engine or job can reference them.
OFFERS: dict[str, dict] = {
    oid: o for oid, o in ALL_OFFERS.items() if o.get("enabled", True)
}

# Pristine defaults — captured before any saved overrides are applied so the
# UI "reset to default" can restore them.
_DEFAULT_URLS: dict[str, str] = {oid: o["url"] for oid, o in OFFERS.items()}

# Runtime settings the UI can change (proxy + target URLs) are persisted here
# so they survive a Flask restart.  They are NOT written back to .env.
UI_CONFIG_PATH = Path("ui_config.json")


def _load_ui_config() -> dict:
    if UI_CONFIG_PATH.exists():
        try:
            return json.loads(UI_CONFIG_PATH.read_text())
        except Exception:
            pass
    return {}


def _save_ui_config(cfg: dict) -> None:
    UI_CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def _current_proxy_config() -> dict:
    """Read the live proxy settings straight from the environment."""
    return {
        "source":       os.getenv("PROXY_SOURCE", "rotating").strip().lower(),
        "rotating_url": os.getenv("ROTATING_PROXY_URL", "").strip(),
        "env_list":     os.getenv("PROXY_LIST", "").strip(),
    }


def _apply_proxy_env(proxy: dict) -> None:
    """Push proxy settings into the environment so the next ProxyManager()
    (built fresh at every engine Start) picks them up — no restart needed.

    The UI proxy always wins when filled: a non-empty value overrides whatever
    is in .env. An empty UI field is left alone so the .env fallback survives.
    For 'file' mode the line list is written to proxies.txt.
    """
    source = (proxy.get("source") or "rotating").strip().lower()
    os.environ["PROXY_SOURCE"] = source
    rotating = (proxy.get("rotating_url") or "").strip()
    if rotating:
        os.environ["ROTATING_PROXY_URL"] = rotating
    env_list = (proxy.get("env_list") or "").strip()
    if env_list:
        os.environ["PROXY_LIST"] = env_list
    if source == "file" and proxy.get("file_text") is not None:
        Path("proxies.txt").write_text(proxy["file_text"])


def _current_browser_config() -> dict:
    """Live browser settings (engine reads these from the environment)."""
    return {
        "channel":  os.getenv("BROWSER_CHANNEL", "chrome").strip().lower() or "chrome",
        "headless": os.getenv("HEADLESS", "true").strip().lower() != "false",
    }


def _apply_browser_env(browser: dict) -> None:
    """Push browser settings into the environment for the next engine start.

    Channel matters on this target: Playwright's bundled Chromium crashes its
    renderer on the offer's fraud-detection script, while a stock Google Chrome
    install loads the same page cleanly.  'chrome' is therefore the default.
    """
    channel = (browser.get("channel") or "chrome").strip().lower()
    os.environ["BROWSER_CHANNEL"] = channel
    headless = browser.get("headless")
    if headless is not None:
        os.environ["HEADLESS"] = "true" if headless else "false"


def _apply_config(cfg: dict) -> None:
    """Overlay a saved ui_config dict onto the live OFFERS + environment."""
    for oid, url in (cfg.get("urls") or {}).items():
        if oid in OFFERS and url:
            OFFERS[oid]["url"] = url
    if cfg.get("proxy"):
        _apply_proxy_env(cfg["proxy"])
    if cfg.get("browser"):
        _apply_browser_env(cfg["browser"])


def _test_proxy(proxy_url: str | None) -> dict:
    """Probe outbound connectivity through proxy_url (or direct if None)."""
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    try:
        ip = _requests.get("https://api.ipify.org?format=text", proxies=proxies, timeout=12).text.strip()
        return {"success": True, "ip": ip}
    except Exception as e:
        # HTTPS goes through CONNECT, so proxy rejections surface only as a terse
        # status (e.g. "422" / "407"). Re-probe over plain HTTP, where the proxy
        # returns a human-readable reason (e.g. "IP not in whitelist"), so the UI
        # shows the actual cause instead of a cryptic tunnel error.
        msg = str(e)
        if proxy_url:
            try:
                _requests.get("http://api.ipify.org?format=text", proxies=proxies, timeout=12)
            except Exception as e2:
                msg = str(e2)
        return {"success": False, "error": _clean_proxy_error(msg)}


def _clean_proxy_error(msg: str) -> str:
    """Strip urllib3's connection-pool wrapper to leave the proxy's own reason."""
    m = msg
    for marker in ("Tunnel connection failed: ", "OSError('", 'OSError("'):
        if marker in m:
            m = m.split(marker, 1)[1]
    return m.rstrip("')\"")[:200] or msg[:200]


# Apply any persisted overrides at import time so both the UI and the engines
# start from the saved state.
_apply_config(_load_ui_config())


# ── Lead pacing (paced scheduler mode) ─────────────────────────────────────────
# Only one paced run is active at a time. Engine threads read _active_pacer to
# decide when to release each lead.
_active_pacer: "LeadPacer | None" = None
_active_pacer_run_id: str | None = None
_pacer_lock = threading.Lock()


def _pacing_cfg() -> dict:
    try:
        with open("config.yaml") as fh:
            return (yaml.safe_load(fh) or {}).get("pacing", {}) or {}
    except Exception:
        return {}


def _build_pacer(offer_leads: dict, start_time: datetime, end_time: datetime) -> LeadPacer:
    cfg = _pacing_cfg()
    pcfg = cfg.get("processing", {})
    retry_cfg = {}
    try:
        with open("config.yaml") as fh:
            retry_cfg = (yaml.safe_load(fh) or {}).get("retry", {}) or {}
    except Exception:
        pass
    proc = ProcessingConfig(
        avg_success_time_min=float(pcfg.get("avg_success_time_min", 1.5)),
        avg_retry_time_min=float(pcfg.get("avg_retry_time_min", 3.0)),
        assumed_success_rate=float(pcfg.get("assumed_success_rate", 0.80)),
        max_retries=int(retry_cfg.get("max_retries", 1)),
    )
    offers = list(offer_leads.keys())
    return LeadPacer(
        offer_leads=offer_leads,
        start_time=start_time,
        end_time=end_time,
        peak_hours_config=cfg.get("peak_hours", []),
        processing_config=proc,
        stagger_minutes=int(cfg.get("offer_stagger_minutes", 12)),
        catch_up_threshold=float(cfg.get("catch_up_threshold", 0.30)),
        off_peak_multiplier=float(cfg.get("off_peak_multiplier", 1.0)),
        tz_name=cfg.get("timezone", "America/New_York"),
        offer_names={oid: OFFERS[oid]["name"] for oid in offers if oid in OFFERS},
    )


def _pacer_wait(offer_id: str, eng: dict, stop_event) -> "datetime | None":
    """Block until the pacer permits the next lead for this offer.
    Returns the release datetime, or None if the offer has no budget left
    (engine should stop) or a stop was requested. Surfaces a live countdown
    via eng['batch'] and honours the skip-wait button."""
    pacer = _active_pacer
    if pacer is None:
        return datetime.now()
    while True:
        if stop_event.is_set():
            return None
        wait = pacer.get_wait_seconds(offer_id)
        if wait is None:
            return None
        if wait <= 0.5:
            eng["batch"] = {"waiting": False, "next_in": 0, "batch_num": 0}
            return pacer.consume(offer_id)
        eng["batch"] = {"waiting": True, "next_in": int(wait), "batch_num": 0}
        for _ in range(int(min(wait, 30))):
            if stop_event.is_set():
                return None
            if eng["skip_wait"].is_set():
                eng["skip_wait"].clear()
                break
            time.sleep(1)
            cur = eng.get("batch", {})
            if cur.get("waiting"):
                cur["next_in"] = max(0, cur.get("next_in", 0) - 1)


# ── Per-engine state ──────────────────────────────────────────────────────────

def _mk_engine() -> dict:
    return {
        "running":    False,
        "stop_event": threading.Event(),
        "skip_wait":  threading.Event(),   # set -> skip the current inter-batch wait
        "thread":     None,
        "run_opts":   {},                  # {max_leads, batch_size, batch_interval}
        "batch":      {"waiting": False, "next_in": 0, "batch_num": 0},
        "log_queue":  queue.Queue(maxsize=1000),
        "stats":      {"success": 0, "failed": 0, "total": 0, "processed": 0},
    }

_engines: dict[str, dict] = {oid: _mk_engine() for oid in OFFERS}

# Thread-local: each engine thread stores its offer_id so the global
# structlog processor routes log lines to the right queue.
_tl = threading.local()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ss_path(offer_id: str) -> Path:
    return Path(f"screenshots/{offer_id}/live_view.png")


def _cleanup_row_screenshots(offer_id: str, row_num: int) -> None:
    """Delete a lead's per-row screenshots once it has been processed.

    Per-row captures (row_NNNN_success/error/stuck, offers_page,
    credit_tab, debug steps) are only needed transiently and otherwise pile up
    indefinitely. The rolling live-preview file (live_view.png) is kept so the
    browser preview keeps working."""
    ss_dir = Path(f"screenshots/{offer_id}")
    # Cover both naming styles: zero-padded (row_0002_*) and plain (row_2_*).
    patterns = [f"row_{row_num:04d}_*", f"row_{row_num}_*"]
    for base in (ss_dir, ss_dir / "debug_steps"):
        if not base.exists():
            continue
        for pat in patterns:
            for p in base.glob(pat):
                try:
                    p.unlink()
                except OSError:
                    pass


def _log(offer_id: str, msg: str) -> None:
    q = _engines[offer_id]["log_queue"]
    ts = time.strftime("%H:%M:%S")
    for part in str(msg).splitlines() or [""]:
        entry = f"[{ts}] {part}"
        if q.full():
            try:
                q.get_nowait()
            except queue.Empty:
                pass
        q.put_nowait(entry)


def _get_outbound_ip(proxy_url: str | None) -> str:
    try:
        kwargs: dict = {"timeout": 10}
        if proxy_url:
            kwargs["proxies"] = {"http": proxy_url, "https": proxy_url}
        return _requests.get("https://api.ipify.org?format=text", **kwargs).text.strip()
    except Exception:
        if proxy_url:
            return urlparse(proxy_url).hostname or "unknown"
        return "direct"


# ── Structlog global config ───────────────────────────────────────────────────

def _setup_structlog() -> None:
    import structlog

    def _routing_renderer(lgr, method, ev):
        offer_id = getattr(_tl, "offer_id", None)
        if offer_id:
            lvl = ev.get("level", method).upper()[:4]
            event = str(ev.get("event", ""))
            _skip = {"level", "event", "_record", "timestamp", "_logger"}

            def _fv(v):
                s = str(v).replace('"', "'")
                return f'"{s}"' if " " in s else s

            parts = [
                f"{k}={_fv(v)}"
                for k, v in ev.items()
                if k not in _skip and not k.startswith("_")
            ][:5]
            _log(offer_id, f"{lvl}  {event}" + ("  " + "  ".join(parts) if parts else ""))
        raise structlog.DropEvent()

    structlog.configure(
        processors=[_routing_renderer],
        wrapper_class=structlog.make_filtering_bound_logger(20),
        logger_factory=structlog.PrintLoggerFactory(file=open(os.devnull, "w")),
        cache_logger_on_first_use=False,
    )


# ── Engine runner ─────────────────────────────────────────────────────────────

def _run_engine(offer_id: str, target_url: str) -> None:
    eng = _engines[offer_id]
    stop_event = eng["stop_event"]
    skip_wait  = eng["skip_wait"]
    eng["running"] = True
    eng["stats"] = {"success": 0, "failed": 0, "total": 0, "processed": 0}
    eng["batch"] = {"waiting": False, "next_in": 0, "batch_num": 0}
    run_opts       = eng.get("run_opts") or {}
    max_leads      = run_opts.get("max_leads")
    batch_size     = run_opts.get("batch_size")
    batch_interval = int(run_opts.get("batch_interval") or 0)
    _tl.offer_id = offer_id

    # Browser mode comes from Settings (persisted in ui_config.json).  Headless
    # is the default — the live preview is streamed into the offer card via
    # live_view.png polling, so no window is needed.  Headed is available for
    # watching a run locally; it cannot be used on a headless server without a
    # virtual display.
    _apply_browser_env(_load_ui_config().get("browser") or _current_browser_config())
    _log(offer_id, f"INFO  Browser: {os.getenv('BROWSER_CHANNEL', 'chrome')} "
                   f"({'headless' if os.getenv('HEADLESS', 'true') != 'false' else 'headed'})")

    try:
        filler_module_path = OFFERS[offer_id]["filler"]
        try:
            mod = importlib.import_module(filler_module_path)
            # Reload so edits to the filler take effect on the next Start
            # without restarting the whole Flask process (import_module would
            # otherwise return the stale cached module).
            mod = importlib.reload(mod)
        except ModuleNotFoundError as e:
            # Distinguish "the filler file itself is missing" from "the filler
            # imported a dependency that is missing" — the latter previously
            # showed the misleading 'module not found' message below.
            missing = getattr(e, "name", "") or ""
            if missing == filler_module_path or filler_module_path.startswith(missing + "."):
                _log(offer_id, f"ERR   Form-filler module not found: {filler_module_path}")
                _log(offer_id, "ERR   Create the file and paste your form-filler code into it.")
            else:
                _log(offer_id, f"ERR   Form-filler '{filler_module_path}' failed to import — "
                               f"missing dependency '{missing}'.")
                _log(offer_id, f"ERR   {type(e).__name__}: {e}")
                _log(offer_id, "ERR   Install requirements:  venv/bin/python -m pip install -r requirements.txt")
            return
        except Exception as e:
            _log(offer_id, f"ERR   Form-filler '{filler_module_path}' failed to import: {type(e).__name__}: {e}")
            return

        FormFiller = mod.FormFiller
        FormFillerError = mod.FormFillerError

        with open("config.yaml") as fh:
            config = yaml.safe_load(fh)

        config["target"]["url"] = target_url
        ss_dir = f"screenshots/{offer_id}"
        config.setdefault("screenshots", {})["directory"] = ss_dir
        Path(ss_dir).mkdir(parents=True, exist_ok=True)

        # Per-offer sheet target — read into locals and pass explicitly to
        # SheetHandler.  Mutating the global GOOGLE_SHEET_* env vars here caused
        # concurrently-scheduled engines to race and connect to each other's
        # worksheet, so status write-backs landed on the wrong sheet.
        offer_cfg = OFFERS[offer_id]
        url_env = offer_cfg.get("sheet_url_env", "")
        ws_env  = offer_cfg.get("sheet_ws_env",  "")
        sheet_url      = os.getenv(url_env, "") or os.getenv("GOOGLE_SHEET_URL", "")
        worksheet_name = os.getenv(ws_env, "")  or os.getenv("GOOGLE_SHEET_WORKSHEET", "Sheet1")

        _log(offer_id, f"INFO  Target -> {target_url}")
        _log(offer_id, "INFO  Connecting to Google Sheets...")

        from utils.sheet_handler import SheetHandler
        from utils.proxy_manager import ProxyManager
        from utils.device_manager import DeviceManager

        sheet = SheetHandler(config, sheet_url=sheet_url, worksheet_name=worksheet_name)
        proxy_mgr = ProxyManager()
        device_mgr = DeviceManager(config)
        form_filler = FormFiller(config)

        if proxy_mgr.has_proxies:
            _log(offer_id, f"INFO  Proxy pool: {proxy_mgr.total} proxy/proxies loaded")
        else:
            _log(offer_id, "WARN  No proxies -- running direct")

        pending = sheet.get_pending_rows()
        if not pending:
            _log(offer_id, "WARN  No pending rows found. Nothing to do.")
            return

        eng["stats"]["total"] = len(pending)
        _log(offer_id, f"INFO  {len(pending)} pending row(s) to process")
        if max_leads:
            _log(offer_id, f"INFO  Lead limit: {max_leads}")
        if batch_size and batch_interval:
            _log(offer_id, f"INFO  Batch mode: {batch_size} lead(s) then wait {batch_interval}s")

        done = 0  # rows fully processed this run (drives batch pacing + lead limit)

        retry_cfg = config.get("retry", {})
        max_retries = retry_cfg.get("max_retries", 3)
        backoff_base = retry_cfg.get("backoff_base", 2)
        backoff_max  = retry_cfg.get("backoff_max", 30)

        for row in pending:
            if stop_event.is_set():
                _log(offer_id, "INFO  Stop signal received -- halting.")
                break

            # Paced mode: wait for the pacer to release this lead.
            rel_dt = None
            if eng.get("paced") and _active_pacer is not None:
                rel_dt = _pacer_wait(offer_id, eng, stop_event)
                if rel_dt is None:
                    if not stop_event.is_set():
                        _log(offer_id, "INFO  Pacer: lead budget complete for this offer -- stopping.")
                    break

            row_num = row["_row_number"]
            _log(offer_id, f"INFO  -- Row {row_num} --")
            sheet.mark_in_progress(row_num)

            rc_col = config.get("sheet_columns", {}).get("retry_count", "Retry_Count")
            retry_count = int(row.get(rc_col, 0) or 0)
            attempt = 0
            success = False
            paced_success = False
            t_start = time.time()

            while attempt <= max_retries and not success:
                if stop_event.is_set():
                    break

                proxy_url  = proxy_mgr.next_proxy()
                fingerprint = device_mgr.build_fingerprint(row)
                if proxy_mgr.current_carrier:
                    fingerprint["_carrier"] = proxy_mgr.current_carrier
                carrier      = fingerprint.get("_carrier", "unknown")
                proxy_display = proxy_url or "direct"
                proxy_type    = ProxyManager.proxy_type(proxy_url)
                proxy_ip      = _get_outbound_ip(proxy_url)

                _log(offer_id,
                     f"INFO  Row {row_num} attempt {attempt+1}/{max_retries+1} | "
                     f"proxy: {proxy_type} ({proxy_ip}) | carrier: {carrier}")

                try:
                    result = form_filler.process_row(
                        row=row, fingerprint=fingerprint,
                        proxy_url=proxy_url, row_number=row_num,
                        stop_event=stop_event,
                    )
                    sheet.update_row(
                        row_num, status="Success",
                        notes=result.get("notes", ""),
                        proxy_used=proxy_display, ip=proxy_ip,
                        submission_id=result.get("submission_id", ""),
                        retry_count=retry_count + attempt,
                    )
                    eng["stats"]["success"]   += 1
                    eng["stats"]["processed"] += 1
                    success = True
                    paced_success = True
                    _log(offer_id, f"OK    Row {row_num} -> Success")

                except FormFillerError as e:
                    if e.error_type == "stopped":
                        sheet.update_row(row_num, status="Stopped",
                                         notes="[stopped] Run stopped by user",
                                         proxy_used=proxy_display, ip=proxy_ip,
                                         retry_count=retry_count + attempt)
                        _log(offer_id, f"INFO  Row {row_num} -> Stopped")
                        success = True
                        break

                    if e.error_type == "missing_data":
                        sheet.update_row(row_num, status="Failed",
                                         notes=f"[missing_data] {e}",
                                         proxy_used=proxy_display, ip=proxy_ip,
                                         retry_count=retry_count + attempt)
                        eng["stats"]["failed"]    += 1
                        eng["stats"]["processed"] += 1
                        success = True
                        _log(offer_id, f"ERR   Row {row_num} -> Failed (missing data)")
                        break

                    # Terminal buyer outcomes — reposting the same lead will get
                    # the same answer, so these never retry.  "declined" already
                    # walked its own price ladder before giving up; "filtered"
                    # means the lead can never qualify for this buyer.
                    if e.error_type in ("declined", "filtered", "config"):
                        status = {"declined": "Declined", "filtered": "Filtered",
                                  "config": "Failed"}[e.error_type]
                        sheet.update_row(row_num, status=status,
                                         notes=f"[{e.error_type}] {e}",
                                         proxy_used=proxy_display, ip=proxy_ip,
                                         retry_count=retry_count + attempt)
                        eng["stats"]["failed"]    += 1
                        eng["stats"]["processed"] += 1
                        success = True
                        _log(offer_id, f"ERR   Row {row_num} -> {status}: {str(e)[:70]}")
                        break

                    attempt += 1
                    if attempt > max_retries:
                        sheet.update_row(row_num, status="Failed",
                                         notes=f"[{e.error_type}] {e} (after {attempt} attempts)",
                                         proxy_used=proxy_display, ip=proxy_ip,
                                         retry_count=retry_count + attempt)
                        eng["stats"]["failed"]    += 1
                        eng["stats"]["processed"] += 1
                        _log(offer_id,
                             f"ERR   Row {row_num} -> Failed after {attempt} attempts ({e.error_type})")
                    else:
                        delay = min(backoff_base ** attempt, backoff_max)
                        sheet.update_row(row_num, status="Retry",
                                         notes=f"[{e.error_type}] retrying in {delay}s",
                                         proxy_used=proxy_display, ip=proxy_ip,
                                         retry_count=retry_count + attempt)
                        _log(offer_id, f"RETRY Row {row_num} -- waiting {delay}s")
                        for _ in range(int(delay)):
                            if stop_event.is_set():
                                break
                            time.sleep(1)

                except Exception as e:
                    sheet.update_row(row_num, status="Failed",
                                     notes=f"[unexpected] {e}",
                                     proxy_used=proxy_display, ip=proxy_ip,
                                     retry_count=retry_count + attempt)
                    eng["stats"]["failed"]    += 1
                    eng["stats"]["processed"] += 1
                    _log(offer_id, f"ERR   Row {row_num} -> Unexpected: {e}")
                    break

            # Paced mode: record the completed lead against its release hour.
            if rel_dt is not None and _active_pacer is not None:
                try:
                    _active_pacer.record_submission(
                        offer_id, rel_dt, paced_success, time.time() - t_start)
                except Exception:
                    pass

            # Lead finished (any status) — drop its per-row screenshots.
            _cleanup_row_screenshots(offer_id, row_num)

            # ── Batch pacing / lead-limit ───────────────────────────────────
            done += 1
            if max_leads and done >= max_leads:
                _log(offer_id, f"INFO  Reached lead limit ({max_leads}) -- stopping.")
                break
            more_remaining = done < len(pending)
            if batch_size and batch_interval and more_remaining and done % batch_size == 0:
                batch_num = done // batch_size
                _log(offer_id,
                     f"INFO  Batch {batch_num} complete ({done} leads). "
                     f"Waiting {batch_interval}s before next batch.")
                skip_wait.clear()
                for remaining in range(batch_interval, 0, -1):
                    if stop_event.is_set() or skip_wait.is_set():
                        break
                    eng["batch"] = {"waiting": True, "next_in": remaining, "batch_num": batch_num}
                    time.sleep(1)
                eng["batch"] = {"waiting": False, "next_in": 0, "batch_num": batch_num}
                if skip_wait.is_set():
                    _log(offer_id, "INFO  Batch wait skipped -- starting next batch now.")

        s = eng["stats"]
        _log(offer_id,
             f"DONE  Run complete -- Success: {s['success']}  "
             f"Failed: {s['failed']}  "
             f"Processed: {s['processed']}/{s['total']}")

    except Exception as e:
        _log(offer_id, f"FATAL Engine crashed: {type(e).__name__}: {e!r}")
        _log(offer_id, traceback.format_exc())
    finally:
        eng["running"] = False
        eng["batch"]   = {"waiting": False, "next_in": 0, "batch_num": 0}
        _tl.offer_id   = None


# ── Scheduling & batch orchestration ──────────────────────────────────────────

_scheduled_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()

# Grace period (seconds) after a job is launched before it may be treated as
# "done".  The engine thread sets its `running` flag a moment AFTER the job is
# marked "running", so without this window a freshly-fired job briefly looks
# finished and gets pruned from the schedule before it ever starts running.
_JOB_DONE_GRACE = 5


def _parse_run_opts(d: dict) -> dict:
    """Extract optional {max_leads, batch_size, batch_interval} from a request."""
    opts: dict = {}
    ml = d.get("max_leads")
    if ml not in (None, "", 0, "0"):
        try:
            opts["max_leads"] = max(1, int(ml))
        except (TypeError, ValueError):
            pass
    bs, bi = d.get("batch_size"), d.get("batch_interval")
    if bs and bi:
        try:
            opts["batch_size"]     = max(1, int(bs))
            opts["batch_interval"] = max(1, int(bi))
        except (TypeError, ValueError):
            pass
    return opts


def _launch_engine(offer_id: str, run_opts: dict | None = None) -> bool:
    """Start an offer engine thread (reads target URL fresh from OFFERS).
    Returns False if it is already running."""
    eng = _engines[offer_id]
    if eng["running"]:
        return False
    eng["run_opts"] = run_opts or {}
    eng["paced"] = bool((run_opts or {}).get("paced"))
    eng["stop_event"].clear()
    eng["skip_wait"].clear()
    while not eng["log_queue"].empty():
        try:
            eng["log_queue"].get_nowait()
        except queue.Empty:
            break
    t = threading.Thread(target=_run_engine, args=(offer_id, OFFERS[offer_id]["url"]), daemon=True)
    eng["thread"] = t
    t.start()
    _log(offer_id, f"INFO  Engine started -- {OFFERS[offer_id]['name']}")
    return True


def _job_view(job: dict) -> dict:
    """Public representation of a scheduled job (resolves live status)."""
    status = job["status"]
    if (status == "running"
            and time.time() - job.get("launched_ts", 0) > _JOB_DONE_GRACE
            and not any(_engines[o]["running"] for o in job["offers"])):
        status = "done"
    return {
        "id":        job["id"],
        "offers":    [OFFERS[o]["name"] for o in job["offers"]],
        "offer_ids": job["offers"],
        "start_ts":  job["start_ts"],
        "run_opts":  job["run_opts"],
        "status":    status,
    }


def _persist_jobs() -> None:
    """Persist only still-pending jobs so a restart keeps future schedules."""
    cfg = _load_ui_config()
    cfg["scheduled_jobs"] = [
        {k: job[k] for k in ("id", "offers", "start_ts", "run_opts", "status", "created_ts")}
        for job in _scheduled_jobs.values() if job["status"] == "pending"
    ]
    _save_ui_config(cfg)


def _load_persisted_jobs() -> None:
    for j in (_load_ui_config().get("scheduled_jobs") or []):
        if j.get("status") == "pending" and all(o in OFFERS for o in j.get("offers", [])):
            _scheduled_jobs[j["id"]] = j


def _scheduler_loop() -> None:
    """Background thread: fire due jobs, retire finished ones."""
    while True:
        try:
            now = time.time()
            with _jobs_lock:
                due = [j for j in _scheduled_jobs.values()
                       if j["status"] == "pending" and j["start_ts"] <= now]
            for job in due:
                for oid in job["offers"]:
                    if not _engines[oid]["running"]:
                        _launch_engine(oid, job["run_opts"])
                with _jobs_lock:
                    job["status"] = "running"
                    job["launched_ts"] = time.time()
            with _jobs_lock:
                changed = False
                for job in _scheduled_jobs.values():
                    if (job["status"] == "running"
                            and now - job.get("launched_ts", now) > _JOB_DONE_GRACE
                            and not any(_engines[o]["running"] for o in job["offers"])):
                        job["status"] = "done"
                        changed = True
                if due or changed:
                    _persist_jobs()
        except Exception:
            pass
        time.sleep(2)


# ── HTML ──────────────────────────────────────────────────────────────────────

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Lead Automation Engine</title>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: 'Segoe UI', system-ui, sans-serif;
  background: #0b0e18; color: #e2e8f0;
  min-height: 100vh; padding: 22px 14px 50px;
}
.wrap { max-width: 1080px; margin: 0 auto; }
.header {
  display: flex; align-items: flex-start; justify-content: space-between;
  margin-bottom: 22px; flex-wrap: wrap; gap: 12px;
}
h1 { font-size: 1.45rem; font-weight: 700; color: #7dd3fc; letter-spacing: -.5px; }
.subtitle { font-size: .78rem; color: #4b5563; margin-top: 3px; }
.stop-all {
  padding: 9px 20px; background: #7f1d1d; color: #fca5a5;
  border: 1px solid #b91c1c; border-radius: 8px;
  font-size: .82rem; font-weight: 600; cursor: pointer; white-space: nowrap;
}
.stop-all:hover { background: #991b1b; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
@media (max-width: 700px) { .grid { grid-template-columns: 1fr; } }
.card {
  background: #131926; border: 2px solid #1e2d40;
  border-radius: 12px; overflow: hidden;
  display: flex; flex-direction: column; transition: border-color .2s;
}
.card.is-running  { border-color: #166534; }
.card.is-stopping { border-color: #7c2d12; }
.card-head { padding: 14px 16px 12px; }
.name-row {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 4px;
}
.offer-name { font-size: .95rem; font-weight: 700; }
.offer-url {
  font-size: .61rem; color: #374151; word-break: break-all;
  margin-bottom: 11px; white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis;
}
.badge {
  padding: 3px 10px; border-radius: 20px;
  font-size: .64rem; font-weight: 700; letter-spacing: .5px; white-space: nowrap;
}
.badge.idle     { background: #1a2d47; color: #60a5fa; }
.badge.running  { background: #14532d; color: #4ade80; animation: pulse 1.4s infinite; }
.badge.stopping { background: #431407; color: #fb923c; animation: pulse 1s infinite; }
@keyframes pulse { 0%,100%{opacity:1}50%{opacity:.5} }
.ctrls { display: flex; gap: 8px; margin-bottom: 11px; }
.btn {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 7px 15px; font-size: .78rem; font-weight: 600;
  border: none; border-radius: 7px; cursor: pointer; transition: opacity .15s;
}
.btn:disabled { opacity: .3; cursor: not-allowed; }
.btn-start { background: #16a34a; color: #f0fdf4; }
.btn-start:hover:not(:disabled) { background: #22c55e; }
.btn-stop  { background: #dc2626; color: #fff; }
.btn-stop:hover:not(:disabled)  { background: #ef4444; }
.stats { display: flex; gap: 5px; }
.stat {
  flex: 1; background: #0d1117; border: 1px solid #1e2d40;
  border-radius: 6px; padding: 6px 4px; text-align: center;
}
.stat-val { font-size: 1.05rem; font-weight: 700; line-height: 1.1; }
.stat-lbl {
  font-size: .52rem; color: #4b5563; margin-top: 2px;
  text-transform: uppercase; letter-spacing: .5px;
}
.sv-ok   .stat-val { color: #4ade80; }
.sv-fail .stat-val { color: #f87171; }
.sv-tot  .stat-val { color: #7dd3fc; }
.ss-wrap {
  position: relative; background: #07090f;
  border-top: 1px solid #1a2535;
  min-height: 80px; max-height: 200px; overflow: hidden;
  display: flex; align-items: center; justify-content: center;
}
.ss-img { width: 100%; height: auto; display: none; max-height: 200px; object-fit: contain; }
.ss-ph  { font-size: .68rem; color: #1f2937; }
.step-lbl {
  position: absolute; bottom: 0; left: 0; right: 0;
  background: rgba(7,9,15,.8); text-align: center;
  font-size: .6rem; color: #60a5fa; padding: 3px 6px;
}
.mini-log {
  flex: 1; min-height: 120px; max-height: 160px; overflow-y: auto;
  padding: 8px 10px;
  font-family: 'JetBrains Mono','Cascadia Code','Fira Code',monospace;
  font-size: .66rem; line-height: 1.65;
  border-top: 1px solid #1a2535; background: #07090f;
}
.ll   { white-space: pre-wrap; word-break: break-all; }
.ok   { color: #4ade80; }
.warn { color: #facc15; }
.err  { color: #f87171; }
.rty  { color: #fb923c; }
.done { color: #c4b5fd; font-weight: 600; }
.ftl  { color: #f43f5e; font-weight: 700; }
.inf  { color: #64748b; }
.muted{ color: #1f2937; font-style: italic; }
.settings-btn {
  padding: 9px 20px; background: #1a2d47; color: #7dd3fc;
  border: 1px solid #1e40af; border-radius: 8px;
  font-size: .82rem; font-weight: 600; cursor: pointer; white-space: nowrap;
}
.settings-btn:hover { background: #1e3a5f; }
.modal-overlay {
  display: none; position: fixed; inset: 0; background: rgba(3,6,12,.78);
  z-index: 50; align-items: flex-start; justify-content: center;
  padding: 40px 14px; overflow-y: auto;
}
.modal {
  background: #0f1623; border: 2px solid #1e2d40; border-radius: 14px;
  width: 100%; max-width: 620px; box-shadow: 0 20px 60px rgba(0,0,0,.5);
}
.modal-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 16px 20px; border-bottom: 1px solid #1a2535;
}
.modal-title { font-size: 1.05rem; font-weight: 700; color: #7dd3fc; }
.modal-close {
  background: none; border: none; color: #64748b;
  font-size: 1.5rem; cursor: pointer; line-height: 1;
}
.modal-close:hover { color: #e2e8f0; }
.tabs { display: flex; gap: 4px; padding: 12px 20px 0; }
.tab {
  padding: 8px 16px; background: none; border: none;
  border-bottom: 2px solid transparent; color: #64748b;
  font-size: .82rem; font-weight: 600; cursor: pointer;
}
.tab.active { color: #7dd3fc; border-bottom-color: #38bdf8; }
.tab-body { padding: 18px 20px 22px; }
.fld-lbl {
  display: block; font-size: .72rem; font-weight: 600;
  color: #94a3b8; margin: 12px 0 5px;
}
.src-block:first-child .fld-lbl { margin-top: 0; }
.inp {
  width: 100%; background: #07090f; border: 1px solid #1e2d40;
  border-radius: 7px; color: #e2e8f0; padding: 9px 11px;
  font-size: .8rem; font-family: inherit;
}
.inp:focus { outline: none; border-color: #38bdf8; }
.mono { font-family: 'JetBrains Mono','Cascadia Code',monospace; font-size: .72rem; }
.hint { font-size: .66rem; color: #475569; margin-top: 4px; }
.btn-row { display: flex; gap: 10px; margin-top: 16px; }
.m-btn {
  padding: 9px 18px; border-radius: 7px; font-size: .8rem;
  font-weight: 600; cursor: pointer; border: none;
}
.m-btn.primary { background: #16a34a; color: #f0fdf4; }
.m-btn.primary:hover { background: #22c55e; }
.m-btn.ghost { background: #1a2d47; color: #7dd3fc; border: 1px solid #1e40af; }
.m-btn.ghost:hover { background: #1e3a5f; }
.result { margin-top: 12px; font-size: .74rem; min-height: 1em; word-break: break-all; }
.result.ok-res  { color: #4ade80; }
.result.err-res { color: #f87171; }
.url-card {
  background: #0b121e; border: 1px solid #1a2535;
  border-radius: 9px; padding: 12px 13px; margin-bottom: 10px;
}
.url-card-head {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 6px;
}
.url-name { font-size: .82rem; font-weight: 700; color: #cbd5e1; }
.reset-link {
  background: none; border: none; color: #475569;
  font-size: .68rem; cursor: pointer; text-decoration: underline;
}
.reset-link:hover { color: #94a3b8; }
.url-dot { font-size: .7rem; margin-left: 4px; }
.chk-row { display: flex; flex-wrap: wrap; gap: 8px 16px; margin-top: 4px; }
.chk { display: flex; align-items: center; gap: 6px; font-size: .78rem; color: #cbd5e1; cursor: pointer; }
.chk input { accent-color: #38bdf8; }
.radio-row { display: flex; gap: 16px; margin-top: 4px; }
.two-col { display: flex; gap: 12px; }
.two-col > div { flex: 1; }
.preview {
  margin-top: 14px; padding: 10px 12px; background: #0b121e;
  border: 1px solid #1a2535; border-radius: 8px;
  font-size: .72rem; color: #94a3b8; line-height: 1.7; white-space: pre-wrap;
}
.batch-bar {
  display: none; align-items: center; justify-content: space-between; gap: 8px;
  padding: 6px 12px; background: #161007; border-top: 1px solid #3b2606;
  font-size: .68rem; color: #fbbf24;
}
.batch-skip {
  background: #422006; color: #fbbf24; border: 1px solid #b45309;
  border-radius: 5px; padding: 3px 9px; font-size: .64rem;
  font-weight: 600; cursor: pointer;
}
.batch-skip:hover { background: #5a2c08; }
.jobs-panel {
  margin-top: 22px; background: #131926; border: 2px solid #1e2d40;
  border-radius: 12px; padding: 14px 16px;
}
.jobs-title { font-size: .9rem; font-weight: 700; color: #7dd3fc; margin-bottom: 10px; }
.job-row {
  display: flex; align-items: center; justify-content: space-between; gap: 10px;
  padding: 9px 11px; background: #0b121e; border: 1px solid #1a2535;
  border-radius: 8px; margin-bottom: 8px;
}
.job-info { font-size: .74rem; color: #cbd5e1; }
.job-meta { font-size: .64rem; color: #64748b; margin-top: 3px; }
.job-status {
  font-size: .58rem; font-weight: 700; padding: 2px 8px; border-radius: 10px;
  text-transform: uppercase; letter-spacing: .5px; margin-left: 6px;
}
.js-pending { background: #1a2d47; color: #60a5fa; }
.js-running { background: #14532d; color: #4ade80; }
.job-cancel {
  background: #7f1d1d; color: #fca5a5; border: 1px solid #b91c1c;
  border-radius: 6px; padding: 5px 12px; font-size: .68rem;
  font-weight: 600; cursor: pointer; white-space: nowrap;
}
.job-cancel:hover { background: #991b1b; }
.mode-toggle { display: flex; gap: 6px; margin-bottom: 14px; }
.mode-btn {
  flex: 1; padding: 9px; border-radius: 8px; border: 1px solid #1e2d40;
  background: #0b121e; color: #64748b; font-size: .82rem; font-weight: 700; cursor: pointer;
}
.mode-btn.active { background: #1a2d47; color: #7dd3fc; border-color: #1e40af; }
.pace-banner {
  margin: 12px 0; padding: 10px 12px; border-radius: 8px;
  background: #422006; border: 1px solid #b45309; color: #fbbf24;
  font-size: .74rem; line-height: 1.5;
}
.pace-banner.ok { background: #052e16; border-color: #166534; color: #4ade80; }
.pace-plan-meta { font-size: .68rem; color: #94a3b8; margin: 10px 0 6px; }
.pace-table { width: 100%; border-collapse: collapse; font-size: .66rem; }
.pace-table th, .pace-table td {
  border: 1px solid #1a2535; padding: 4px 6px; text-align: center; white-space: nowrap;
}
.pace-table th { background: #0d1424; color: #7dd3fc; position: sticky; top: 0; }
.pace-table td.lbl { text-align: left; color: #94a3b8; }
.pace-table tr.peak td { background: #1a1408; }
.pace-table input.cell {
  width: 38px; background: #07090f; border: 1px solid #1e2d40; color: #e2e8f0;
  border-radius: 4px; text-align: center; font-size: .66rem; padding: 2px;
}
.pace-table input.cell.over { border-color: #ef4444; color: #f87171; background: #2a0a0a; }
.pace-cap-hi { color: #f87171; font-weight: 700; }
.pace-ind {
  display: none; padding: 5px 10px; font-size: .64rem; font-weight: 600;
  border-top: 1px solid #1a2535; background: #07090f;
}
.pace-ind.on_track  { color: #4ade80; }
.pace-ind.behind    { color: #facc15; }
.pace-ind.way_behind{ color: #f87171; }
.pacing-panel {
  margin-top: 22px; background: #131926; border: 2px solid #1e2d40;
  border-radius: 12px; padding: 14px 16px;
}
.pacing-head {
  display: flex; align-items: center; justify-content: space-between;
  cursor: pointer; font-size: .9rem; font-weight: 700; color: #7dd3fc;
}
.pacing-sub { font-size: .66rem; color: #64748b; margin-top: 6px; }
.st-on_track  { color: #4ade80; }
.st-behind    { color: #facc15; }
.st-way_behind{ color: #f87171; }
.st-future    { color: #374151; }
.us-clock {
  display: flex; flex-direction: column; align-items: flex-end;
  background: #131926; border: 1px solid #1e2d40; border-radius: 10px;
  padding: 6px 12px; line-height: 1.15;
}
.us-clock-lbl  { font-size: .58rem; letter-spacing: .08em; text-transform: uppercase; color: #64748b; }
.us-clock-time { font-size: 1.05rem; font-weight: 700; color: #7dd3fc; font-variant-numeric: tabular-nums; }
.us-clock-date { font-size: .62rem; color: #94a3b8; }
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <div>
      <h1>Lead Automation Engine</h1>
      <p class="subtitle">Run multiple offers simultaneously — each card is fully independent.</p>
    </div>
    <div style="display:flex; gap:10px; flex-wrap:wrap; align-items:center;">
      <div class="us-clock" id="us-clock" title="Current US Eastern Time (America/New_York)">
        <span class="us-clock-lbl">US Eastern</span>
        <span class="us-clock-time" id="us-clock-time">--:--:--</span>
        <span class="us-clock-date" id="us-clock-date"></span>
      </div>
      <button class="settings-btn" onclick="openSettings()">&#9881; Settings</button>
      <button class="stop-all" onclick="stopAll()">&#9632; Stop All Running</button>
    </div>
  </div>

  <div class="grid">
    {% for key, offer in offers.items() %}
    <div class="card" id="card-{{ key }}">
      <div class="card-head">
        <div class="name-row">
          <span class="offer-name" style="color:{{ offer.color }}">{{ offer.name }}</span>
          <span class="badge idle" id="badge-{{ key }}">IDLE</span>
        </div>
        <div class="offer-url" id="offer-url-{{ key }}" title="{{ offer.url }}">{{ offer.url }}</div>
        <div class="ctrls">
          <button class="btn btn-start" id="btn-start-{{ key }}"
                  onclick="startEngine('{{ key }}')">&#9654; Start</button>
          <button class="btn btn-stop"  id="btn-stop-{{ key }}"
                  onclick="stopEngine('{{ key }}')" disabled>&#9632; Stop</button>
        </div>
        <div class="stats">
          <div class="stat sv-ok">
            <div class="stat-val" id="s-ok-{{ key }}">0</div>
            <div class="stat-lbl">OK</div>
          </div>
          <div class="stat sv-fail">
            <div class="stat-val" id="s-fail-{{ key }}">0</div>
            <div class="stat-lbl">Fail</div>
          </div>
          <div class="stat sv-tot">
            <div class="stat-val" id="s-proc-{{ key }}">0</div>
            <div class="stat-lbl">Done</div>
          </div>
          <div class="stat sv-tot">
            <div class="stat-val" id="s-tot-{{ key }}">0</div>
            <div class="stat-lbl">Total</div>
          </div>
        </div>
      </div>
      <div class="batch-bar" id="batch-{{ key }}">
        <span class="batch-txt"></span>
        <button class="batch-skip" onclick="skipBatch('{{ key }}')">Start next batch now</button>
      </div>
      <div class="pace-ind" id="pace-ind-{{ key }}"></div>
      <div class="ss-wrap">
        <img class="ss-img" id="ss-{{ key }}" alt="preview"/>
        <div class="ss-ph" id="ss-ph-{{ key }}">no preview</div>
        <div class="step-lbl" id="step-{{ key }}"></div>
      </div>
      <div class="mini-log" id="log-{{ key }}">
        <div class="ll muted">Waiting to start...</div>
      </div>
    </div>
    {% endfor %}
  </div>

  <div class="jobs-panel" id="jobs-panel" style="display:none">
    <div class="jobs-title">&#9201; Scheduled Jobs</div>
    <div id="jobs-list"></div>
  </div>

  <div class="pacing-panel" id="pacing-panel" style="display:none">
    <div class="pacing-head" onclick="togglePacing()">
      <span>&#128202; Pacing Overview <span id="pacing-collapse">▼</span></span>
      <span id="pacing-summary" class="pacing-sub" style="margin:0"></span>
    </div>
    <div id="pacing-body">
      <div id="pacing-drift" class="pace-banner" style="display:none"></div>
      <div style="overflow:auto; max-height:360px; margin-top:10px;">
        <table class="pace-table" id="pacing-table"></table>
      </div>
    </div>
  </div>
</div>

<div class="modal-overlay" id="settings-modal">
  <div class="modal">
    <div class="modal-head">
      <span class="modal-title">&#9881; Settings</span>
      <button class="modal-close" onclick="closeSettings()">&times;</button>
    </div>
    <div class="tabs">
      <button class="tab active" id="tab-proxy" onclick="switchTab('proxy')">Proxy</button>
      <button class="tab" id="tab-browser" onclick="switchTab('browser')">Browser</button>
      <button class="tab" id="tab-urls" onclick="switchTab('urls')">Target URLs</button>
      <button class="tab" id="tab-schedule" onclick="switchTab('schedule')">Schedule</button>
    </div>

    <div class="tab-body" id="body-browser" style="display:none">
      <div class="src-block">
        <label class="fld-lbl">Browser Engine</label>
        <select id="br-channel" class="inp">
          <option value="chrome">Google Chrome &mdash; recommended</option>
          <option value="chromium">Chromium (Playwright bundled)</option>
          <option value="msedge">Microsoft Edge</option>
        </select>
        <div class="hint">
          Playwright's bundled Chromium crashes on this offer's fraud-detection
          script, headless and headed alike. Stock Google Chrome loads the same
          page cleanly &mdash; keep this on <b>chrome</b> unless you are testing.
        </div>
      </div>
      <div class="src-block">
        <label class="fld-lbl">Window Mode</label>
        <select id="br-headless" class="inp">
          <option value="true">Headless &mdash; no window (required on a server)</option>
          <option value="false">Headed &mdash; show the browser window</option>
        </select>
        <div class="hint">
          Headless is the default; the live preview in each card streams the same
          view. Headed only works on a desktop with a display attached.
        </div>
      </div>
      <div class="btn-row">
        <button class="m-btn ghost" onclick="testBrowser()">Test Against Offer</button>
        <button class="m-btn primary" onclick="saveBrowser()">Save &amp; Apply</button>
      </div>
      <div class="result" id="br-result"></div>
    </div>

    <div class="tab-body" id="body-proxy">
      <div class="src-block">
        <label class="fld-lbl">Proxy Source</label>
        <select id="px-source" class="inp" onchange="onSourceChange()">
          <option value="none">none &mdash; direct connection</option>
          <option value="rotating">rotating &mdash; single endpoint</option>
          <option value="file">file &mdash; proxies.txt list</option>
          <option value="env">env &mdash; comma-separated list</option>
        </select>
      </div>
      <div id="px-rotating" class="src-block">
        <label class="fld-lbl">Rotating Proxy URL</label>
        <input id="px-url" class="inp mono" placeholder="http://user:pass@host:port"/>
        <div class="hint">Format: http://username:password;mobile;us;;;@proxy.froxy.com:9000</div>
      </div>
      <div id="px-env" class="src-block" style="display:none">
        <label class="fld-lbl">Proxy List (comma-separated)</label>
        <textarea id="px-list" class="inp mono" rows="3"
          placeholder="http://user:pass@p1:8080,http://user:pass@p2:8080"></textarea>
      </div>
      <div id="px-file" class="src-block" style="display:none">
        <label class="fld-lbl">proxies.txt &mdash; one proxy per line</label>
        <textarea id="px-file-text" class="inp mono" rows="5"
          placeholder="http://user:pass@host:port"></textarea>
      </div>
      <div class="btn-row">
        <button class="m-btn ghost" onclick="testProxy()">Test Connection</button>
        <button class="m-btn primary" onclick="saveProxy()">Save &amp; Apply</button>
      </div>
      <div class="result" id="px-result"></div>
    </div>

    <div class="tab-body" id="body-urls" style="display:none">
      <div id="url-cards"></div>
      <div class="btn-row">
        <button class="m-btn primary" onclick="saveUrls()">Save All Target URLs</button>
      </div>
      <div class="result" id="url-result"></div>
    </div>

    <div class="tab-body" id="body-schedule" style="display:none">
      <div class="mode-toggle">
        <button class="mode-btn active" id="mode-normal" onclick="setSchedMode('normal')">&#9889; Normal</button>
        <button class="mode-btn" id="mode-paced" onclick="setSchedMode('paced')">&#128202; Paced</button>
      </div>

      <div id="sched-normal">
      <label class="fld-lbl">Offers to run</label>
      <div class="chk-row" id="sch-offers"></div>

      <label class="fld-lbl">When</label>
      <div class="radio-row">
        <label class="chk"><input type="radio" name="sch-when" value="now" checked onchange="onWhenChange()"/> Run now</label>
        <label class="chk"><input type="radio" name="sch-when" value="later" onchange="onWhenChange()"/> Schedule for later</label>
      </div>
      <div id="sch-time-wrap" style="display:none; margin-top:8px;">
        <input type="datetime-local" id="sch-time" class="inp" onchange="updatePreview()"/>
      </div>

      <label class="fld-lbl">Number of leads (blank = all pending)</label>
      <input type="number" id="sch-leads" class="inp" min="1" placeholder="all" oninput="updatePreview()"/>

      <label class="chk" style="margin-top:16px;">
        <input type="checkbox" id="sch-batch-on" onchange="onBatchToggle(); updatePreview();"/>
        Process in batches with a pause between
      </label>
      <div id="sch-batch-wrap" style="display:none; margin-top:10px;">
        <div class="two-col">
          <div>
            <label class="fld-lbl">Leads per batch</label>
            <input type="number" id="sch-batch-size" class="inp" min="1" value="4" oninput="updatePreview()"/>
          </div>
          <div>
            <label class="fld-lbl">Wait between batches</label>
            <select id="sch-interval" class="inp" onchange="onIntervalChange(); updatePreview();">
              <option value="30">30 seconds</option>
              <option value="60" selected>1 minute</option>
              <option value="120">2 minutes</option>
              <option value="300">5 minutes</option>
              <option value="600">10 minutes</option>
              <option value="900">15 minutes</option>
              <option value="1800">30 minutes</option>
              <option value="3600">1 hour</option>
              <option value="custom">Custom…</option>
            </select>
          </div>
        </div>
        <div id="sch-custom-wrap" class="two-col" style="display:none; margin-top:10px;">
          <div>
            <label class="fld-lbl">Custom amount</label>
            <input type="number" id="sch-custom-val" class="inp" min="1" value="5" oninput="updatePreview()"/>
          </div>
          <div>
            <label class="fld-lbl">Unit</label>
            <select id="sch-custom-unit" class="inp" onchange="updatePreview()">
              <option value="1">seconds</option>
              <option value="60" selected>minutes</option>
              <option value="3600">hours</option>
            </select>
          </div>
        </div>
      </div>

      <div class="preview" id="sch-preview"></div>
      <div class="btn-row">
        <button class="m-btn primary" onclick="createSchedule()">Schedule &amp; Start</button>
      </div>
      <div class="result" id="sch-result"></div>
      </div><!-- /sched-normal -->

      <div id="sched-paced" style="display:none">
        <label class="fld-lbl">Leads per offer <span style="color:#64748b; font-weight:400;">(blank/0 = skip that offer)</span></label>
        <div id="pace-offer-inputs"></div>

        <div class="two-col" style="margin-top:10px;">
          <div>
            <label class="fld-lbl">Start (US Eastern)</label>
            <input type="datetime-local" id="pace-start" class="inp"/>
          </div>
          <div>
            <label class="fld-lbl">End (US Eastern)</label>
            <input type="datetime-local" id="pace-end" class="inp"/>
          </div>
        </div>

        <div class="btn-row">
          <button class="m-btn ghost" onclick="generatePlan()">Generate Plan</button>
        </div>

        <div id="pace-banner" class="pace-banner" style="display:none"></div>
        <div id="pace-plan-wrap" style="display:none">
          <div class="pace-plan-meta" id="pace-plan-meta"></div>
          <div style="overflow:auto; max-height:320px;">
            <table class="pace-table" id="pace-plan-table"></table>
          </div>
          <label class="chk" id="pace-override-row" style="display:none; margin-top:10px;">
            <input type="checkbox" id="pace-override-ack" onchange="refreshConfirmState()"/>
            I understand capacity is exceeded, proceed anyway
          </label>
          <div class="btn-row">
            <button class="m-btn primary" id="pace-confirm-btn" onclick="confirmPaced()">Confirm &amp; Start</button>
          </div>
        </div>
        <div class="result" id="pace-result"></div>
      </div>
    </div>
  </div>
</div>

<script>
const OFFER_KEYS = {{ offer_keys | tojson }};
const evtSrcs = {}, pollTmrs = {}, ssTmrs = {}, stopping = {};

// --- Live US Eastern clock (America/New_York) ---
function updateUsClock() {
  const now = new Date();
  const t = new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York', hour: '2-digit', minute: '2-digit',
    second: '2-digit', hour12: true
  }).format(now);
  const d = new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York', weekday: 'short', month: 'short',
    day: 'numeric', timeZoneName: 'short'
  }).format(now);
  const te = document.getElementById('us-clock-time');
  const de = document.getElementById('us-clock-date');
  if (te) te.textContent = t;
  if (de) de.textContent = d;
}
updateUsClock();
setInterval(updateUsClock, 1000);

function logCls(l) {
  const u = l.toUpperCase();
  if (u.includes('] OK') || u.includes('SUCCESS')) return 'ok';
  if (u.includes('] WARN'))                        return 'warn';
  if (u.includes('] ERR') || u.includes('] FAIL')) return 'err';
  if (u.includes('] RETRY'))                       return 'rty';
  if (u.includes('] DONE'))                        return 'done';
  if (u.includes('] FATAL'))                       return 'ftl';
  return 'inf';
}

function appendLog(key, line) {
  const box = document.getElementById('log-' + key);
  box.querySelectorAll('.muted').forEach(e => e.remove());
  const d = document.createElement('div');
  d.className = 'll ' + logCls(line);
  d.textContent = line;
  box.appendChild(d);
  box.scrollTop = box.scrollHeight;
  const all = box.querySelectorAll('.ll');
  if (all.length > 300) all[0].remove();
}

function setRunning(key, running) {
  const card  = document.getElementById('card-' + key);
  const badge = document.getElementById('badge-' + key);
  document.getElementById('btn-start-' + key).disabled = running;
  document.getElementById('btn-stop-'  + key).disabled = !running || !!stopping[key];
  card.classList.toggle('is-running',  running && !stopping[key]);
  card.classList.toggle('is-stopping', !!stopping[key]);
  if (running) {
    stopping[key] = false;
    badge.className = 'badge running'; badge.textContent = 'RUNNING';
  } else {
    badge.className = 'badge idle'; badge.textContent = 'IDLE';
    stopping[key] = false; card.classList.remove('is-stopping');
  }
}

function updStats(key, s) {
  document.getElementById('s-ok-'   + key).textContent = s.success;
  document.getElementById('s-fail-' + key).textContent = s.failed;
  document.getElementById('s-proc-' + key).textContent = s.processed;
  document.getElementById('s-tot-'  + key).textContent = s.total;
}

function startSsPoll(key) {
  if (ssTmrs[key]) clearInterval(ssTmrs[key]);
  ssTmrs[key] = setInterval(() => {
    const probe = new Image();
    probe.onload = () => {
      const img = document.getElementById('ss-' + key);
      const ph  = document.getElementById('ss-ph-' + key);
      img.src = probe.src; img.style.display = 'block'; ph.style.display = 'none';
    };
    probe.src = '/screenshot/' + key + '?t=' + Date.now();
  }, 1500);
}
function stopSsPoll(key) { if (ssTmrs[key]) { clearInterval(ssTmrs[key]); delete ssTmrs[key]; } }

function startSSE(key) {
  if (evtSrcs[key]) evtSrcs[key].close();
  evtSrcs[key] = new EventSource('/logs/' + key);
  evtSrcs[key].onmessage = e => {
    if (!e.data || !e.data.trim()) return;
    appendLog(key, e.data);
    const m = e.data.match(/form\.step.*?step=(\d+).*?title="([^"]+)"/);
    if (m) document.getElementById('step-' + key).textContent =
             'Step ' + m[1] + ': ' + m[2].substring(0, 38);
  };
  evtSrcs[key].onerror = () => setTimeout(() => startSSE(key), 2000);
}

function startPoll(key) {
  if (pollTmrs[key]) clearInterval(pollTmrs[key]);
  pollTmrs[key] = setInterval(async () => {
    try {
      const d = await fetch('/status').then(r => r.json());
      const eng = d[key]; if (!eng) return;
      setRunning(key, eng.running); updStats(key, eng.stats);
      updateBatchBar(key, eng.batch);
      if (!eng.running) {
        clearInterval(pollTmrs[key]); delete pollTmrs[key];
        stopSsPoll(key);
        document.getElementById('step-' + key).textContent = '';
        updateBatchBar(key, null);
      }
    } catch(_) {}
  }, 1500);
}

async function startEngine(key) {
  document.getElementById('btn-start-' + key).disabled = true;
  const d = await fetch('/start/' + key, { method: 'POST' })
    .then(r => r.json()).catch(() => ({ ok: false, msg: 'Network error' }));
  if (d.ok) { setRunning(key, true); startSSE(key); startPoll(key); startSsPoll(key); }
  else { alert(d.msg || 'Could not start.'); document.getElementById('btn-start-' + key).disabled = false; }
}

async function stopEngine(key) {
  stopping[key] = true;
  document.getElementById('btn-stop-' + key).disabled = true;
  const badge = document.getElementById('badge-' + key);
  badge.className = 'badge stopping'; badge.textContent = 'STOPPING';
  document.getElementById('card-' + key).classList.replace('is-running', 'is-stopping');
  await fetch('/stop/' + key, { method: 'POST' }).catch(() => {});
}

function stopAll() {
  OFFER_KEYS.forEach(key => {
    const b = document.getElementById('badge-' + key);
    if (b && b.textContent === 'RUNNING') stopEngine(key);
  });
}

// ── Settings modal ────────────────────────────────────────────────────────
let CONFIG = null;

function openSettings()  { document.getElementById('settings-modal').style.display = 'flex'; loadConfig(); }
function closeSettings() { document.getElementById('settings-modal').style.display = 'none'; }

function switchTab(t) {
  ['proxy', 'browser', 'urls', 'schedule'].forEach(x => {
    document.getElementById('tab-'  + x).classList.toggle('active', x === t);
    document.getElementById('body-' + x).style.display = (x === t) ? 'block' : 'none';
  });
}

function onSourceChange() {
  const s = document.getElementById('px-source').value;
  document.getElementById('px-rotating').style.display = (s === 'rotating') ? 'block' : 'none';
  document.getElementById('px-env').style.display      = (s === 'env')      ? 'block' : 'none';
  document.getElementById('px-file').style.display     = (s === 'file')     ? 'block' : 'none';
}

async function loadConfig() {
  CONFIG = await fetch('/api/config').then(r => r.json()).catch(() => null);
  if (!CONFIG) return;
  const p = CONFIG.proxy || {};
  document.getElementById('px-source').value    = p.source || 'rotating';
  document.getElementById('px-url').value       = p.rotating_url || '';
  document.getElementById('px-list').value      = p.env_list || '';
  document.getElementById('px-file-text').value = CONFIG.proxies_txt || '';
  onSourceChange();

  const b = CONFIG.browser || {};
  document.getElementById('br-channel').value  = b.channel || 'chrome';
  document.getElementById('br-headless').value = (b.headless === false) ? 'false' : 'true';
  document.getElementById('br-result').textContent = '';

  const wrap = document.getElementById('url-cards');
  wrap.innerHTML = '';
  Object.keys(CONFIG.offers).forEach(oid => {
    const name = CONFIG.offers[oid], url = CONFIG.urls[oid] || '';
    const div = document.createElement('div');
    div.className = 'url-card';
    div.innerHTML =
      '<div class="url-card-head"><span class="url-name">' + name + '</span>' +
      '<button class="reset-link" onclick="resetUrl(\'' + oid + '\')">reset to default</button></div>' +
      '<input class="inp mono url-in" data-oid="' + oid + '" ' +
      'value="' + url.replace(/"/g, '&quot;') + '" oninput="markUrl(this)"/>' +
      '<span class="url-dot" id="dot-' + oid + '"></span>';
    wrap.appendChild(div);
    markUrl(div.querySelector('input'));
  });
  document.getElementById('px-result').textContent = '';
  document.getElementById('url-result').textContent = '';

  buildScheduleOffers();
  updatePreview();
}

function buildScheduleOffers() {
  if (!CONFIG) return;
  const wrap = document.getElementById('sch-offers');
  wrap.innerHTML = '';
  Object.keys(CONFIG.offers).forEach(oid => {
    const l = document.createElement('label');
    l.className = 'chk';
    l.innerHTML = '<input type="checkbox" value="' + oid + '" checked onchange="updatePreview()"/> ' + CONFIG.offers[oid];
    wrap.appendChild(l);
  });
}

function markUrl(inp) {
  const ok  = /^https?:\/\//i.test(inp.value.trim());
  const dot = document.getElementById('dot-' + inp.dataset.oid);
  if (dot) dot.textContent = inp.value.trim() ? (ok ? '\u{1F7E2}' : '\u{1F534}') : '';
}

function resetUrl(oid) {
  const inp = document.querySelector('.url-in[data-oid="' + oid + '"]');
  if (inp) { inp.value = (CONFIG.default_urls[oid] || ''); markUrl(inp); }
}

async function testProxy() {
  const r = document.getElementById('px-result');
  r.className = 'result'; r.textContent = 'Testing…';
  const s = document.getElementById('px-source').value;
  let url = null;
  if (s === 'rotating') url = document.getElementById('px-url').value.trim();
  else if (s === 'env')  url = (document.getElementById('px-list').value.split(',')[0] || '').trim();
  else if (s === 'file') url = (document.getElementById('px-file-text').value.split('\n')
                                  .find(l => l.trim() && !l.startsWith('#')) || '').trim();
  const d = await fetch('/api/proxy/test', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ proxy_url: url })
  }).then(r => r.json()).catch(() => ({ success: false, error: 'network error' }));
  if (d.success) { r.className = 'result ok-res';  r.textContent = '✅ Working — IP: ' + d.ip; }
  else           { r.className = 'result err-res'; r.textContent = '❌ Failed — ' + (d.error || 'unreachable'); }
}

async function saveProxy() {
  const s = document.getElementById('px-source').value;
  const body = {
    source:       s,
    rotating_url: document.getElementById('px-url').value.trim(),
    env_list:     document.getElementById('px-list').value.trim(),
    file_text:    document.getElementById('px-file-text').value,
  };
  const d = await fetch('/api/config/proxy', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  }).then(r => r.json()).catch(() => ({ ok: false }));
  const r = document.getElementById('px-result');
  if (d.ok) { r.className = 'result ok-res';  r.textContent = '✅ Proxy saved — applies on next Start'; }
  else      { r.className = 'result err-res'; r.textContent = '❌ ' + (d.msg || 'save failed'); }
}

async function testBrowser() {
  const r = document.getElementById('br-result');
  r.className = 'result'; r.textContent = 'Launching browser and loading the offer… (up to 30s)';
  const body = {
    channel:  document.getElementById('br-channel').value,
    headless: document.getElementById('br-headless').value === 'true',
  };
  const d = await fetch('/api/browser/test', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  }).then(r => r.json()).catch(() => ({ success: false, error: 'network error' }));
  if (d.success) { r.className = 'result ok-res';  r.textContent = '✅ ' + d.msg; }
  else           { r.className = 'result err-res'; r.textContent = '❌ ' + (d.error || 'failed'); }
}

async function saveBrowser() {
  const body = {
    channel:  document.getElementById('br-channel').value,
    headless: document.getElementById('br-headless').value === 'true',
  };
  const d = await fetch('/api/config/browser', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  }).then(r => r.json()).catch(() => ({ ok: false }));
  const r = document.getElementById('br-result');
  if (d.ok) { r.className = 'result ok-res';  r.textContent = '✅ Browser settings saved — applies on next Start'; }
  else      { r.className = 'result err-res'; r.textContent = '❌ ' + (d.msg || 'save failed'); }
}

async function saveUrls() {
  const urls = {};
  document.querySelectorAll('.url-in').forEach(i => urls[i.dataset.oid] = i.value.trim());
  const d = await fetch('/api/config/urls', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ urls })
  }).then(r => r.json()).catch(() => ({ ok: false }));
  const r = document.getElementById('url-result');
  if (d.ok) {
    r.className = 'result ok-res'; r.textContent = '✅ Target URLs saved';
    Object.keys(d.urls).forEach(oid => {
      const el = document.getElementById('offer-url-' + oid);
      if (el) { el.textContent = d.urls[oid]; el.title = d.urls[oid]; }
    });
  } else { r.className = 'result err-res'; r.textContent = '❌ ' + (d.msg || 'save failed'); }
}

// ── Schedule / batch ──────────────────────────────────────────────────────
function onWhenChange() {
  const later = document.querySelector('input[name="sch-when"]:checked').value === 'later';
  document.getElementById('sch-time-wrap').style.display = later ? 'block' : 'none';
  const t = document.getElementById('sch-time');
  if (later && !t.value) {
    const d = new Date(Date.now() + 3600000); d.setSeconds(0, 0);
    const p = n => String(n).padStart(2, '0');
    t.value = d.getFullYear() + '-' + p(d.getMonth()+1) + '-' + p(d.getDate()) +
              'T' + p(d.getHours()) + ':' + p(d.getMinutes());
  }
  updatePreview();
}
function onBatchToggle() {
  document.getElementById('sch-batch-wrap').style.display =
    document.getElementById('sch-batch-on').checked ? 'block' : 'none';
}
function onIntervalChange() {
  document.getElementById('sch-custom-wrap').style.display =
    document.getElementById('sch-interval').value === 'custom' ? 'flex' : 'none';
}
function getInterval() {
  const sel = document.getElementById('sch-interval').value;
  if (sel === 'custom')
    return (parseInt(document.getElementById('sch-custom-val').value) || 0) *
           (parseInt(document.getElementById('sch-custom-unit').value) || 1);
  return parseInt(sel) || 0;
}
function fmtDur(s) {
  if (!s) return '0s';
  if (s < 60) return s + 's';
  if (s < 3600) return (s % 60 ? (s/60).toFixed(1) : s/60) + ' min';
  return (s % 3600 ? (s/3600).toFixed(1) : s/3600) + ' hr';
}
function updatePreview() {
  if (!CONFIG) return;
  const offers = [...document.querySelectorAll('#sch-offers input:checked')].map(i => i.value);
  const when   = document.querySelector('input[name="sch-when"]:checked').value;
  const leads  = document.getElementById('sch-leads').value.trim();
  const lines  = [];
  lines.push('Offers: ' + (offers.length ? offers.map(o => CONFIG.offers[o]).join(', ') : '(none selected)'));
  if (when === 'later') {
    const t = document.getElementById('sch-time').value;
    lines.push('Starts: ' + (t ? new Date(t).toLocaleString() : '(pick a time)'));
  } else { lines.push('Starts: now'); }
  lines.push('Leads: ' + (leads || 'all pending'));
  if (document.getElementById('sch-batch-on').checked) {
    const bs = parseInt(document.getElementById('sch-batch-size').value) || 0;
    lines.push('Batches: ' + bs + ' lead(s), then wait ' + fmtDur(getInterval()));
  }
  document.getElementById('sch-preview').textContent = lines.join('\n');
}
async function createSchedule() {
  const r = document.getElementById('sch-result');
  const offers = [...document.querySelectorAll('#sch-offers input:checked')].map(i => i.value);
  if (!offers.length) { r.className = 'result err-res'; r.textContent = '❌ Select at least one offer.'; return; }
  const when = document.querySelector('input[name="sch-when"]:checked').value;
  let start_ts = 0;
  if (when === 'later') {
    const t = document.getElementById('sch-time').value;
    if (!t) { r.className = 'result err-res'; r.textContent = '❌ Pick a start time.'; return; }
    start_ts = new Date(t).getTime() / 1000;
    if (start_ts * 1000 < Date.now() - 60000) { r.className = 'result err-res'; r.textContent = '❌ Start time is in the past.'; return; }
  }
  const body = { offers, start_ts, max_leads: document.getElementById('sch-leads').value.trim() };
  if (document.getElementById('sch-batch-on').checked) {
    body.batch_size     = parseInt(document.getElementById('sch-batch-size').value) || 0;
    body.batch_interval = getInterval();
  }
  const d = await fetch('/api/schedule', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  }).then(r => r.json()).catch(() => ({ ok: false }));
  if (d.ok) {
    r.className = 'result ok-res';
    r.textContent = (when === 'later') ? '✅ Scheduled.' : '✅ Starting now…';
    loadJobs();
    if (when === 'now') setTimeout(closeSettings, 700);
  } else { r.className = 'result err-res'; r.textContent = '❌ ' + (d.msg || 'failed'); }
}

function updateBatchBar(key, batch) {
  const bar = document.getElementById('batch-' + key);
  if (!bar) return;
  if (batch && batch.waiting) {
    const s = batch.next_in, mm = Math.floor(s / 60), ss = String(s % 60).padStart(2, '0');
    bar.querySelector('.batch-txt').textContent =
      '⏸ Batch ' + batch.batch_num + ' done — next in ' + (mm > 0 ? mm + ':' + ss : s + 's');
    bar.style.display = 'flex';
  } else { bar.style.display = 'none'; }
}
async function skipBatch(key) { await fetch('/api/batch/skip/' + key, { method: 'POST' }).catch(() => {}); }

async function loadJobs() {
  const d = await fetch('/api/scheduled-jobs').then(r => r.json()).catch(() => ({ jobs: [] }));
  const panel = document.getElementById('jobs-panel'), list = document.getElementById('jobs-list');
  if (!d.jobs || !d.jobs.length) { panel.style.display = 'none'; list.innerHTML = ''; return; }
  panel.style.display = 'block'; list.innerHTML = '';
  d.jobs.forEach(j => {
    const future = j.start_ts && (j.start_ts > d.now);
    const when = j.status === 'running' ? 'started'
               : (future ? new Date(j.start_ts * 1000).toLocaleString() : 'now');
    const opts = [];
    if (j.run_opts.max_leads)  opts.push(j.run_opts.max_leads + ' leads');
    if (j.run_opts.batch_size) opts.push('batch ' + j.run_opts.batch_size + ' / ' + fmtDur(j.run_opts.batch_interval));
    const row = document.createElement('div');
    row.className = 'job-row';
    row.innerHTML =
      '<div><div class="job-info">' + j.offers.join(', ') +
      '<span class="job-status js-' + j.status + '">' + j.status + '</span></div>' +
      '<div class="job-meta">' + when + (opts.length ? ' · ' + opts.join(' · ') : '') + '</div></div>' +
      '<button class="job-cancel" onclick="cancelJob(\'' + j.id + '\')">' +
      (j.status === 'pending' ? 'Cancel' : 'Stop') + '</button>';
    list.appendChild(row);
  });
}
async function cancelJob(id) {
  await fetch('/api/scheduled-jobs/' + id, { method: 'DELETE' }).catch(() => {});
  loadJobs();
}

// ── Paced scheduler ─────────────────────────────────────────────────────────
let PACE_PLAN = null, PACE_CFG = null, PACE_OFFER_IDS = [];

function setSchedMode(mode) {
  document.getElementById('mode-normal').classList.toggle('active', mode === 'normal');
  document.getElementById('mode-paced').classList.toggle('active', mode === 'paced');
  document.getElementById('sched-normal').style.display = mode === 'normal' ? 'block' : 'none';
  document.getElementById('sched-paced').style.display  = mode === 'paced'  ? 'block' : 'none';
  if (mode === 'paced') buildPaceOffers();
}

function buildPaceOffers() {
  if (!CONFIG) return;
  const wrap = document.getElementById('pace-offer-inputs');
  if (!wrap.children.length) {
    Object.keys(CONFIG.offers).forEach(oid => {
      const row = document.createElement('div');
      row.style.cssText = 'display:flex; align-items:center; gap:10px; margin-bottom:7px;';
      row.innerHTML =
        '<span style="flex:1; font-size:.8rem; color:#cbd5e1;">' + CONFIG.offers[oid] + '</span>' +
        '<input type="number" min="0" id="pace-leads-' + oid + '" class="inp" ' +
        'style="width:110px;" placeholder="0" value="100"/>';
      wrap.appendChild(row);
    });
  }
  const pad = n => String(n).padStart(2, '0');
  const d = new Date();
  const ymd = d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  if (!document.getElementById('pace-start').value) document.getElementById('pace-start').value = ymd + 'T06:00';
  if (!document.getElementById('pace-end').value)   document.getElementById('pace-end').value   = ymd + 'T21:00';
}

function collectOfferLeads() {
  const offer_leads = {};
  Object.keys(CONFIG.offers).forEach(oid => {
    const el = document.getElementById('pace-leads-' + oid);
    const n = el ? parseInt(el.value) || 0 : 0;
    if (n > 0) offer_leads[oid] = n;
  });
  return offer_leads;
}

async function generatePlan() {
  const r = document.getElementById('pace-result'); r.textContent = '';
  const offer_leads = collectOfferLeads();
  const start = document.getElementById('pace-start').value;
  const end = document.getElementById('pace-end').value;
  if (!Object.keys(offer_leads).length) { r.className = 'result err-res'; r.textContent = '❌ Enter leads for at least one offer.'; return; }
  if (!start || !end) { r.className = 'result err-res'; r.textContent = '❌ Pick start and end times.'; return; }
  const d = await fetch('/api/schedule/paced/plan', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ offer_leads, start_time: start, end_time: end })
  }).then(x => x.json()).catch(() => ({ ok: false, msg: 'network error' }));
  if (!d.ok) { r.className = 'result err-res'; r.textContent = '❌ ' + (d.msg || 'failed'); return; }
  PACE_PLAN = d.plan; PACE_CFG = d.processing_config; PACE_OFFER_IDS = Object.keys(d.offers);
  const banner = document.getElementById('pace-banner');
  banner.style.display = 'block';
  banner.className = 'pace-banner' + (d.feasible ? ' ok' : '');
  banner.textContent = d.warning;
  document.getElementById('pace-override-row').style.display = d.feasible ? 'none' : 'flex';
  document.getElementById('pace-override-ack').checked = false;
  renderPlanTable(d.offers);
  document.getElementById('pace-plan-wrap').style.display = 'block';
  refreshConfirmState();
}

function renderPlanTable(offerNames) {
  const tbl = document.getElementById('pace-plan-table');
  const max = PACE_CFG.realistic_max_per_offer;
  let head = '<tr><th>Hr</th><th>Time</th>';
  PACE_OFFER_IDS.forEach(o => head += '<th>' + offerNames[o].split(' ')[0] + '</th>');
  head += '<th>Total</th><th>Cap%</th></tr>';
  let body = '', grand = 0;
  PACE_PLAN.forEach((s, si) => {
    grand += s.total_planned;
    body += '<tr class="' + (s.is_peak ? 'peak' : '') + '">';
    body += '<td class="lbl">' + s.index + (s.is_peak ? '*' : '') + '</td><td class="lbl">' + s.label + '</td>';
    PACE_OFFER_IDS.forEach(o => {
      const v = s.offer_budgets[o] || 0;
      body += '<td><input class="cell' + (v > max ? ' over' : '') + '" data-si="' + si + '" data-o="' + o +
              '" value="' + v + '" oninput="onCellEdit(this)"/></td>';
    });
    body += '<td id="rowtot-' + si + '">' + s.total_planned + '</td>';
    body += '<td class="' + (s.capacity_pct > 100 ? 'pace-cap-hi' : '') + '" id="rowcap-' + si + '">' + s.capacity_pct + '%</td></tr>';
  });
  tbl.innerHTML = head + body;
  document.getElementById('pace-plan-meta').innerHTML =
    'Total: <b>' + grand + '</b> leads | ' + PACE_PLAN.length + ' hrs | max ~' + max +
    '/hr/offer · ' + PACE_CFG.realistic_time_per_lead + ' min/lead. ' +
    '<span style="color:#facc15">* = peak (reduced)</span>. Cells turn red above per-offer max.';
}

function onCellEdit(inp) {
  const si = +inp.dataset.si, max = PACE_CFG.realistic_max_per_offer;
  const v = parseInt(inp.value) || 0;
  PACE_PLAN[si].offer_budgets[inp.dataset.o] = v;
  inp.classList.toggle('over', v > max);
  const tot = PACE_OFFER_IDS.reduce((a, o) => a + (PACE_PLAN[si].offer_budgets[o] || 0), 0);
  PACE_PLAN[si].total_planned = tot;
  const capTotal = max * PACE_OFFER_IDS.length;
  const cap = capTotal ? Math.round(tot / capTotal * 1000) / 10 : 0;
  PACE_PLAN[si].capacity_pct = cap;
  document.getElementById('rowtot-' + si).textContent = tot;
  const capEl = document.getElementById('rowcap-' + si);
  capEl.textContent = cap + '%'; capEl.className = cap > 100 ? 'pace-cap-hi' : '';
  refreshConfirmState();
}

function refreshConfirmState() {
  const anyOver = !!document.querySelector('#pace-plan-table input.cell.over');
  const needAck = document.getElementById('pace-override-row').style.display !== 'none';
  const ack = document.getElementById('pace-override-ack').checked;
  const btn = document.getElementById('pace-confirm-btn');
  btn.disabled = anyOver || (needAck && !ack);
  btn.style.opacity = btn.disabled ? '0.4' : '1';
}

async function confirmPaced() {
  const r = document.getElementById('pace-result');
  // Per-offer totals derived from the (possibly user-edited) plan.
  const offer_leads = {};
  PACE_OFFER_IDS.forEach(o => {
    offer_leads[o] = PACE_PLAN.reduce((a, s) => a + (s.offer_budgets[o] || 0), 0);
  });
  const body = {
    offer_leads,
    start_time: document.getElementById('pace-start').value,
    end_time: document.getElementById('pace-end').value,
    plan_override: PACE_PLAN.map(s => ({ offer_budgets: s.offer_budgets })),
  };
  const d = await fetch('/api/schedule/paced/confirm', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
  }).then(x => x.json()).catch(() => ({ ok: false }));
  if (d.ok) {
    r.className = 'result ok-res';
    r.textContent = '✅ Paced run started (' + d.started.length + ' offer' + (d.started.length === 1 ? '' : 's') + ').';
    loadPacing();
    setTimeout(closeSettings, 900);
  } else { r.className = 'result err-res'; r.textContent = '❌ ' + (d.msg || 'failed'); }
}

// ── Pacing overview (live) ──────────────────────────────────────────────────
let PACING_COLLAPSED = false;
function togglePacing() {
  PACING_COLLAPSED = !PACING_COLLAPSED;
  document.getElementById('pacing-body').style.display = PACING_COLLAPSED ? 'none' : 'block';
  document.getElementById('pacing-collapse').textContent = PACING_COLLAPSED ? '▶' : '▼';
}

async function loadPacing() {
  const d = await fetch('/api/pacing/stats').then(x => x.json()).catch(() => ({ active: false }));
  const panel = document.getElementById('pacing-panel');
  if (!d.active) {
    panel.style.display = 'none';
    OFFER_KEYS.forEach(k => { const e = document.getElementById('pace-ind-' + k); if (e) e.style.display = 'none'; });
    return;
  }
  panel.style.display = 'block';
  const oids = Object.keys(d.offers);
  let summary = 'Completed: ' + d.completed + '/' + d.planned_total + ' | Remaining: ' + d.remaining;
  if (d.avg_actual_rate) summary += ' | Avg: ' + d.avg_actual_rate + '/hr';
  if (d.est_finish) summary += ' | Est. finish: ' + d.est_finish;
  document.getElementById('pacing-summary').textContent = summary;
  const drift = document.getElementById('pacing-drift');
  if (d.drift_warning) { drift.style.display = 'block'; drift.textContent = d.drift_warning; }
  else drift.style.display = 'none';
  const stTxt = { on_track: '✅ On track', behind: '⚠️ Behind', way_behind: '❌ Behind' };
  let head = '<tr><th>Hr</th><th>Time</th>';
  oids.forEach(o => head += '<th>' + d.offers[o].split(' ')[0] + '</th>');
  head += '<th>Plan</th><th>Actual</th><th>Status</th></tr>';
  let body = '';
  d.rows.forEach(s => {
    body += '<tr class="' + (s.is_peak ? 'peak' : '') + '">';
    body += '<td class="lbl">' + s.index + (s.is_peak ? '*' : '') + (s.state === 'current' ? ' ◀' : '') + '</td><td class="lbl">' + s.label + '</td>';
    oids.forEach(o => {
      const pb = s.offer_budgets[o] || 0, ac = s.offer_actuals[o] || 0;
      body += '<td>' + (s.state === 'future' ? '<span class="st-future">' + pb + '</span>' : (pb || ac ? ac + '/' + pb : '—')) + '</td>';
    });
    body += '<td>' + s.total_planned + '</td><td>' + s.total_actual + '</td>';
    body += '<td class="st-' + s.status + '">' + (stTxt[s.status] || '') + '</td></tr>';
  });
  document.getElementById('pacing-table').innerHTML = head + body;
  const cur = d.rows.find(x => x.state === 'current');
  oids.forEach(o => {
    const ind = document.getElementById('pace-ind-' + o); if (!ind) return;
    if (!d.running || !cur) { ind.style.display = 'none'; return; }
    const pb = cur.offer_budgets[o] || 0, ac = cur.offer_actuals[o] || 0;
    const st = cur.status === '—' ? 'on_track' : cur.status;
    ind.style.display = 'block';
    ind.className = 'pace-ind ' + st;
    ind.textContent = '📊 Hour ' + d.current_hour + '/' + d.total_hours + ' | ' + ac + '/' + pb + ' leads | ' + (stTxt[cur.status] || 'On Track ✅');
  });
}

// Watcher: auto-attach engines started by the scheduler, refresh jobs + pacing.
setInterval(async () => {
  try {
    const d = await fetch('/status').then(r => r.json());
    OFFER_KEYS.forEach(key => {
      const eng = d[key]; if (!eng) return;
      if (eng.running && !pollTmrs[key]) {
        setRunning(key, true); startSSE(key); startPoll(key); startSsPoll(key);
      }
      updateBatchBar(key, eng.batch);
    });
  } catch(_) {}
  loadJobs();
  loadPacing();
}, 3000);
loadJobs();
loadPacing();

document.getElementById('settings-modal').addEventListener('click', e => {
  if (e.target.id === 'settings-modal') closeSettings();
});

(async () => {
  try {
    const d = await fetch('/status').then(r => r.json());
    OFFER_KEYS.forEach(key => {
      const eng = d[key]; if (!eng) return;
      setRunning(key, eng.running); updStats(key, eng.stats);
      if (eng.running) { startSSE(key); startPoll(key); startSsPoll(key); }
    });
  } catch(_) {}
})();
</script>
</body>
</html>
"""


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(_HTML, offers=OFFERS, offer_keys=list(OFFERS.keys()))


@app.route("/start/<offer_id>", methods=["POST"])
def start(offer_id: str):
    if offer_id not in OFFERS:
        return jsonify({"ok": False, "msg": f"Unknown offer: {offer_id}"})
    if _engines[offer_id]["running"]:
        return jsonify({"ok": False, "msg": f"{OFFERS[offer_id]['name']} is already running."})
    run_opts = _parse_run_opts(request.get_json(silent=True) or {})
    _launch_engine(offer_id, run_opts)
    return jsonify({"ok": True})


@app.route("/stop/<offer_id>", methods=["POST"])
def stop(offer_id: str):
    if offer_id not in _engines:
        return jsonify({"ok": False, "msg": "Unknown offer"})
    _engines[offer_id]["stop_event"].set()
    _log(offer_id, "INFO  Stop requested -- will halt after the current form step...")
    return jsonify({"ok": True})


@app.route("/status")
def status():
    return jsonify({
        oid: {"running": eng["running"], "stats": eng["stats"], "batch": eng["batch"]}
        for oid, eng in _engines.items()
    })


@app.route("/logs/<offer_id>")
def logs(offer_id: str):
    if offer_id not in _engines:
        return "", 404

    def stream():
        q = _engines[offer_id]["log_queue"]
        while True:
            try:
                msg = q.get(timeout=1.0)
                yield f"data: {msg}\n\n"
            except queue.Empty:
                yield "data: \n\n"

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/screenshot/<offer_id>")
def screenshot(offer_id: str):
    if offer_id not in OFFERS:
        return "", 404
    p = _ss_path(offer_id)
    if not p.exists():
        return "", 204
    try:
        data = p.read_bytes()
    except OSError:
        return "", 204
    resp = make_response(data)
    resp.headers["Content-Type"]  = "image/png"
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ── Settings API ──────────────────────────────────────────────────────────────

@app.route("/api/config")
def api_config():
    txt = Path("proxies.txt").read_text() if Path("proxies.txt").exists() else ""
    return jsonify({
        "proxy":        _current_proxy_config(),
        "browser":      _current_browser_config(),
        "urls":         {oid: o["url"] for oid, o in OFFERS.items()},
        "default_urls": _DEFAULT_URLS,
        "offers":       {oid: o["name"] for oid, o in OFFERS.items()},
        "proxies_txt":  txt,
    })


@app.route("/api/config/browser", methods=["POST"])
def api_config_browser():
    data = request.get_json(silent=True) or {}
    channel = (data.get("channel") or "chrome").strip().lower()
    if channel not in ("chrome", "chromium", "msedge", "chrome-beta"):
        return jsonify({"ok": False, "msg": f"Unsupported browser channel: {channel}"})
    browser = {"channel": channel, "headless": bool(data.get("headless", True))}
    _apply_browser_env(browser)
    cfg = _load_ui_config()
    cfg["browser"] = browser
    _save_ui_config(cfg)
    return jsonify({"ok": True, "browser": browser})


# Probe body for /api/browser/test.  Run out of process because a crashed
# renderer makes browser.close() block indefinitely — in-process that would
# wedge the Flask worker thread with no way to recover.
_BROWSER_PROBE = r"""
import json, sys, time
from playwright.sync_api import sync_playwright
channel, headless, url = sys.argv[1], sys.argv[2] == "1", sys.argv[3]
crashed = {"v": False}
rendered = False
try:
    with sync_playwright() as pw:
        kw = {"headless": headless, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
        if channel not in ("chromium", "bundled", "default"):
            kw["channel"] = channel
        b = pw.chromium.launch(**kw)
        p = b.new_page(
            user_agent="Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/125.0.6422.165 Mobile Safari/537.36",
            viewport={"width": 412, "height": 915}, is_mobile=True, has_touch=True)
        p.on("crash", lambda _p: crashed.__setitem__("v", True))
        try:
            p.goto(url, wait_until="domcontentloaded", timeout=40000)
        except Exception:
            pass
        for _ in range(8):
            time.sleep(2)
            if crashed["v"]:
                break
            try:
                rendered = bool(p.evaluate(
                    "() => { const f = document.getElementById('applicantForm');"
                    "        return !!f && f.querySelectorAll('input,select').length > 0; }"))
            except Exception:
                if crashed["v"]:
                    break
            if rendered:
                break
        print("RESULT" + json.dumps({"crashed": crashed["v"], "rendered": rendered}), flush=True)
        try:
            b.close()
        except Exception:
            pass
except Exception as e:
    print("RESULT" + json.dumps({"error": str(e)[:200]}), flush=True)
"""


@app.route("/api/browser/test", methods=["POST"])
def api_browser_test():
    """Launch the configured browser against the offer URL and report whether the
    page renders — this target crashes some Chromium builds outright."""
    import subprocess
    import sys

    data = request.get_json(silent=True) or {}
    channel = (data.get("channel") or "chrome").strip().lower()
    headless = bool(data.get("headless", True))
    url = next(iter(OFFERS.values()))["url"]

    proc = subprocess.Popen(
        [sys.executable, "-c", _BROWSER_PROBE, channel, "1" if headless else "0", url],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        out, _ = proc.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
        return jsonify({"success": False,
                        "error": f"{channel}: timed out — the browser hung "
                                 f"(a crashed renderer does not shut down cleanly)."})

    payload: dict = {}
    for line in (out or "").splitlines():
        if line.startswith("RESULT"):
            try:
                payload = json.loads(line[len("RESULT"):])
            except ValueError:
                pass
    if not payload:
        tail = (out or "").strip().splitlines()[-1:] or ["no output"]
        return jsonify({"success": False, "error": f"{channel}: probe failed — {tail[0][:180]}"})
    if payload.get("error"):
        return jsonify({"success": False, "error": f"{channel}: {payload['error']}"})
    if payload.get("crashed"):
        return jsonify({"success": False,
                        "error": f"{channel}: renderer crashed before the form rendered. "
                                 f"Use the 'chrome' channel for this target."})
    if not payload.get("rendered"):
        return jsonify({"success": False,
                        "error": f"{channel}: page loaded but the form did not render in time."})
    return jsonify({"success": True, "msg": f"{channel} loaded the form successfully."})


@app.route("/api/config/proxy", methods=["POST"])
def api_config_proxy():
    data = request.get_json(silent=True) or {}
    proxy = {
        "source":       (data.get("source") or "rotating").strip().lower(),
        "rotating_url": (data.get("rotating_url") or "").strip(),
        "env_list":     (data.get("env_list") or "").strip(),
    }
    if data.get("file_text") is not None:
        proxy["file_text"] = data["file_text"]
    _apply_proxy_env(proxy)
    cfg = _load_ui_config()
    # proxies.txt holds the 'file' lines; only persist the env-backed fields.
    cfg["proxy"] = {k: proxy[k] for k in ("source", "rotating_url", "env_list")}
    _save_ui_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/config/urls", methods=["POST"])
def api_config_urls():
    data = request.get_json(silent=True) or {}
    urls = data.get("urls") or {}
    bad = [
        oid for oid, u in urls.items()
        if oid in OFFERS and u and not str(u).startswith(("http://", "https://"))
    ]
    if bad:
        names = ", ".join(OFFERS[o]["name"] for o in bad)
        return jsonify({"ok": False, "msg": f"Invalid URL for: {names}"})
    for oid, u in urls.items():
        if oid in OFFERS and u:
            OFFERS[oid]["url"] = u.strip()
    cfg = _load_ui_config()
    # Merge rather than replace: a disabled offer is absent from OFFERS, and
    # rewriting the map wholesale would silently drop its saved URL.
    saved = cfg.get("urls") or {}
    saved.update({oid: OFFERS[oid]["url"] for oid in OFFERS})
    cfg["urls"] = saved
    _save_ui_config(cfg)
    return jsonify({"ok": True, "urls": {oid: OFFERS[oid]["url"] for oid in OFFERS}})


@app.route("/api/proxy/test", methods=["POST"])
def api_proxy_test():
    data = request.get_json(silent=True) or {}
    proxy_url = (data.get("proxy_url") or "").strip() or None
    return jsonify(_test_proxy(proxy_url))


# ── Scheduling API ────────────────────────────────────────────────────────────

@app.route("/api/schedule", methods=["POST"])
def api_schedule():
    data = request.get_json(silent=True) or {}
    offers = [o for o in (data.get("offers") or []) if o in OFFERS]
    if not offers:
        return jsonify({"ok": False, "msg": "Select at least one offer."})
    try:
        start_ts = float(data.get("start_ts") or 0)
    except (TypeError, ValueError):
        start_ts = 0.0
    # A start time in the past (or absent) means "run now" — the scheduler
    # picks it up on its next tick (~2s).
    job = {
        "id":         uuid4().hex[:8],
        "offers":     offers,
        "start_ts":   start_ts,
        "run_opts":   _parse_run_opts(data),
        "status":     "pending",
        "created_ts": time.time(),
    }
    with _jobs_lock:
        _scheduled_jobs[job["id"]] = job
        _persist_jobs()
    return jsonify({"ok": True, "job": _job_view(job)})


@app.route("/api/scheduled-jobs")
def api_scheduled_jobs():
    with _jobs_lock:
        jobs = [_job_view(j) for j in sorted(
            _scheduled_jobs.values(),
            key=lambda x: x["start_ts"] or x["created_ts"],
        )]
        # Drop jobs the scheduler loop has marked finished so the list
        # self-prunes.  Prune on the STORED status (which the loop only sets
        # after the grace window) rather than the live-resolved view status, so
        # a job is never removed during its engine's brief start-up window.
        for j in list(_scheduled_jobs.values()):
            if j["status"] in ("done", "cancelled"):
                _scheduled_jobs.pop(j["id"], None)
    return jsonify({"jobs": jobs, "now": time.time()})


@app.route("/api/scheduled-jobs/<job_id>", methods=["DELETE"])
def api_cancel_job(job_id: str):
    with _jobs_lock:
        job = _scheduled_jobs.get(job_id)
        if not job:
            return jsonify({"ok": False, "msg": "Job not found."})
        if job["status"] == "pending":
            _scheduled_jobs.pop(job_id, None)
        else:
            for oid in job["offers"]:
                _engines[oid]["stop_event"].set()
            job["status"] = "cancelled"
        _persist_jobs()
    return jsonify({"ok": True})


@app.route("/api/batch/skip/<offer_id>", methods=["POST"])
def api_batch_skip(offer_id: str):
    if offer_id not in _engines:
        return jsonify({"ok": False})
    _engines[offer_id]["skip_wait"].set()
    # In paced mode, skipping a wait redistributes the remaining leads across the
    # rest of the window and wakes every paced engine to re-evaluate.
    if _active_pacer is not None and _engines[offer_id].get("paced"):
        try:
            _active_pacer.recalculate_remaining()
            for oid in _active_pacer.offers:
                _engines[oid]["skip_wait"].set()
        except Exception:
            pass
    return jsonify({"ok": True})


# ── Paced scheduler API ────────────────────────────────────────────────────────

def _apply_plan_override(pacer: LeadPacer, override: list) -> None:
    """Overwrite the pacer's per-slot budgets with user-edited values."""
    try:
        with pacer._lock:
            cap_total = pacer.realistic_max * pacer.num_offers
            for i, row in enumerate(override):
                if i >= len(pacer.slots):
                    break
                slot = pacer.slots[i]
                budgets = row.get("offer_budgets") or {}
                for o in pacer.offers:
                    if o in budgets:
                        slot.offer_budgets[o] = max(0, int(budgets[o]))
                slot.total_planned = sum(slot.offer_budgets.values())
                slot.capacity_pct = round(slot.total_planned / cap_total * 100, 1) if cap_total else 0.0
            # Re-derive per-offer totals from the edited budgets so the pacer's
            # completion target matches the plan the user confirmed.
            pacer.offer_leads = {
                o: sum(s.offer_budgets.get(o, 0) for s in pacer.slots) for o in pacer.offers
            }
            pacer.total_leads = sum(pacer.offer_leads.values())
    except Exception:
        pass


def _parse_offer_leads(data: dict) -> tuple[dict, str]:
    """Read per-offer lead counts from the request. Returns (offer_leads, error)."""
    raw = data.get("offer_leads") or {}
    offer_leads = {}
    for oid, n in raw.items():
        if oid not in OFFERS:
            continue
        try:
            n = int(n)
        except (TypeError, ValueError):
            return {}, f"Invalid lead count for {oid}."
        if n > 0:
            offer_leads[oid] = n
    if not offer_leads:
        return {}, "Enter a lead count for at least one offer."
    return offer_leads, ""


@app.route("/api/schedule/paced/plan", methods=["POST"])
def api_paced_plan():
    data = request.get_json(silent=True) or {}
    offer_leads, err = _parse_offer_leads(data)
    if err:
        return jsonify({"ok": False, "msg": err})
    try:
        start = datetime.fromisoformat(data["start_time"])
        end = datetime.fromisoformat(data["end_time"])
    except Exception as e:
        return jsonify({"ok": False, "msg": f"Invalid inputs: {e}"})
    if end <= start:
        return jsonify({"ok": False, "msg": "End time must be after start time."})
    pacer = _build_pacer(offer_leads, start, end)
    feasible, warning = pacer.validate_feasibility()
    return jsonify({
        "ok": True,
        "feasible": feasible,
        "warning": warning,
        "plan": pacer.plan_dict(),
        "offers": {o: OFFERS[o]["name"] for o in offer_leads},
        "processing_config": {
            "realistic_max_per_offer": pacer.realistic_max,
            "realistic_time_per_lead": round(pacer.proc.realistic_time_per_lead, 2),
        },
    })


@app.route("/api/schedule/paced/confirm", methods=["POST"])
def api_paced_confirm():
    global _active_pacer, _active_pacer_run_id
    data = request.get_json(silent=True) or {}
    offer_leads, err = _parse_offer_leads(data)
    if err:
        return jsonify({"ok": False, "msg": err})
    offers = list(offer_leads.keys())
    with _pacer_lock:
        if _active_pacer is not None and any(_engines[o]["running"] for o in _active_pacer.offers):
            return jsonify({"ok": False, "msg": "A paced run is already active. Stop it first."})
        try:
            start = datetime.fromisoformat(data["start_time"])
            end = datetime.fromisoformat(data["end_time"])
        except Exception as e:
            return jsonify({"ok": False, "msg": f"Invalid inputs: {e}"})
        if end <= start:
            return jsonify({"ok": False, "msg": "End time must be after start time."})
        pacer = _build_pacer(offer_leads, start, end)
        if data.get("plan_override"):
            _apply_plan_override(pacer, data["plan_override"])
        _active_pacer = pacer
        _active_pacer_run_id = uuid4().hex[:8]
    started = [oid for oid in offers if _launch_engine(oid, {"paced": True})]
    return jsonify({"ok": True, "run_id": _active_pacer_run_id, "started": started})


@app.route("/api/pacing/stats")
def api_pacing_stats():
    if _active_pacer is None:
        return jsonify({"active": False})
    stats = _active_pacer.get_pacing_stats()
    stats["active"] = True
    stats["run_id"] = _active_pacer_run_id
    stats["offers"] = {o: OFFERS[o]["name"] for o in _active_pacer.offers}
    stats["running"] = any(_engines[o]["running"] for o in _active_pacer.offers)
    return jsonify(stats)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    _setup_structlog()
    _load_persisted_jobs()
    threading.Thread(target=_scheduler_loop, daemon=True).start()
    print("\n  Lead Automation UI  (multi-engine)")
    print("  ──────────────────────────────────")
    print("  Open in browser:  http://localhost:5000\n")
    app.run(host="0.0.0.0", port=5000, debug=False,
            use_reloader=False, threaded=True)
