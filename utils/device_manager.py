"""
utils/device_manager.py — Device fingerprint manager.
Builds a Playwright-compatible fingerprint for each row.
"""
from __future__ import annotations
import random
import re
import threading
from collections import deque
from typing import Any
import structlog
from devices_pool import DEVICE_POOL
from .stealth import _CARRIERS


def _ua_os(ua: str) -> str:
    """Platform implied by the UA — so an iPhone is never labelled Android."""
    if "iPhone" in ua:
        return "ios-iphone"
    if "iPad" in ua:
        return "ios-ipad"
    return "android"


def _ua_browser(ua: str) -> str:
    """Engine + major version from a UA, for logging (e.g. 'CriOS/150')."""
    for name in ("CriOS", "FxiOS", "SamsungBrowser", "Silk", "EdgiOS", "Chrome"):
        m = re.search(rf"{name}/(\d+)", ua)
        if m:
            return f"{name}/{m.group(1)}"
    m = re.search(r"Version/(\d+)[\d.]*\s+.*Safari", ua)
    if m:
        return f"Safari/{m.group(1)}"
    return "Safari"

# Tracks the last carrier used so we never repeat it on the very next call.
_last_carrier: list[str] = [""]

# ── Non-repeating device rotation ────────────────────────────────────────────
# Every row fill (across ALL offers, which run in separate threads) draws the
# next device from a shared shuffled queue.  No user agent repeats until the
# whole pool has been used once; then it reshuffles.  The lock makes it safe for
# the concurrent engine threads, and the reshuffle avoids handing the same UA
# out back-to-back across a cycle boundary.
_device_lock = threading.Lock()
_device_queue: deque[int] = deque()
_last_device_idx: list[int] = [-1]


def _next_device() -> dict[str, Any]:
    with _device_lock:
        if not _device_queue:
            order = list(range(len(DEVICE_POOL)))
            random.shuffle(order)
            # Don't repeat the previous UA as the first of the new cycle.
            if len(order) > 1 and order[0] == _last_device_idx[0]:
                order[0], order[-1] = order[-1], order[0]
            _device_queue.extend(order)
        idx = _device_queue.popleft()
        _last_device_idx[0] = idx
    return DEVICE_POOL[idx]


log = structlog.get_logger(__name__)

_LOCALES = [
    "en-US", "en-GB", "en-CA", "en-AU", "en-IN",
    "es-ES", "es-MX", "fr-FR", "de-DE", "pt-BR",
]
_TIMEZONES = [
    "America/New_York", "America/Chicago", "America/Denver",
    "America/Los_Angeles", "Europe/London", "Europe/Paris",
    "Asia/Tokyo", "Asia/Kolkata", "Australia/Sydney",
]
_COLOR_SCHEMES = ["light", "dark", "no-preference"]


class DeviceManager:
    """Generates a fresh device fingerprint for every row."""

    def __init__(self, config: dict) -> None:
        self._defaults = config.get("device_defaults", {})
        self._col_map = config.get("sheet_columns", {})

    def build_fingerprint(self, row: dict[str, Any]) -> dict[str, Any]:
        """Build a Playwright-compatible context fingerprint from row data."""
        use_custom_col = self._col_map.get("use_custom_device", "Use_Custom_Device")
        use_custom = str(row.get(use_custom_col, "")).strip().lower() == "yes"

        fp = self._build_custom(row) if use_custom else self._build_random()

        # Apply orientation
        ori_col = self._col_map.get("orientation", "Orientation")
        orientation = str(row.get(ori_col, "")).strip().lower()
        if orientation == "random":
            orientation = random.choice(["portrait", "landscape"])
        if orientation == "landscape":
            vp = fp.get("viewport", {})
            fp["viewport"] = {"width": vp.get("height", 915), "height": vp.get("width", 412)}

        log.info("device.fingerprint", model=fp.get("_model", "random"),
                 os=fp.get("_os"), browser=fp.get("_browser"),
                 viewport=fp.get("viewport"), locale=fp.get("locale"))
        return fp

    def _build_custom(self, row: dict[str, Any]) -> dict[str, Any]:
        """Build fingerprint from sheet-specified device columns."""
        model_col = self._col_map.get("device_model", "Device_Model")
        ver_col = self._col_map.get("android_version", "Android_Version")
        model = str(row.get(model_col, "")).strip()
        android_ver = str(row.get(ver_col, "14")).strip()

        match = next((d for d in DEVICE_POOL if d["model"].lower() == model.lower()), None)
        if match:
            fp = self._device_to_fp(match)
            if android_ver and android_ver != match["android_version"]:
                fp["user_agent"] = fp["user_agent"].replace(
                    f"Android {match['android_version']}", f"Android {android_ver}")
        else:
            log.warning("device.unknown_model", model=model)
            fp = self._build_generic(model, android_ver)
        fp["_model"] = model
        return fp

    def _build_random(self) -> dict[str, Any]:
        """Pick the next (non-repeating) device and jitter its properties."""
        device = _next_device()
        fp = self._device_to_fp(device)
        # Jitter around the device's OWN viewport so an iPhone stays iPhone-sized
        # and an iPad stays iPad-sized — never squashed into an Android phone box
        # — while still varying a little per row.
        if self._defaults.get("random_viewport", True):
            base = device["viewport"]
            fp["viewport"] = {
                "width":  max(320, base["width"] + random.randint(-6, 6)),
                "height": max(480, base["height"] + random.randint(-14, 14)),
            }
        fp["_model"] = device["model"]
        return fp

    def _device_to_fp(self, device: dict) -> dict[str, Any]:
        """Convert a DEVICE_POOL entry into a Playwright context dict."""
        ua = device["user_agent"]
        fp: dict[str, Any] = {
            "user_agent": ua,
            "viewport": dict(device["viewport"]),
            "device_scale_factor": device.get("device_scale_factor", 2.625),
            "is_mobile": device.get("is_mobile", True),
            "has_touch": device.get("has_touch", True),
            # Internal labels (stripped before new_context) — keep an iPhone an
            # iPhone in the logs, never mislabelled Android.
            "_os": device.get("os") or _ua_os(ua),
            "_browser": _ua_browser(ua),
        }
        self._add_extras(fp)
        return fp

    def _build_generic(self, model: str, android_ver: str) -> dict[str, Any]:
        """Fallback fingerprint when model isn't in the pool."""
        w_r = self._defaults.get("viewport_width_range", [360, 430])
        h_r = self._defaults.get("viewport_height_range", [640, 932])
        ua = (f"Mozilla/5.0 (Linux; Android {android_ver}; {model}) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/125.0.6422.165 Mobile Safari/537.36")
        fp: dict[str, Any] = {
            "user_agent": ua,
            "viewport": {"width": random.randint(w_r[0], w_r[1]),
                         "height": random.randint(h_r[0], h_r[1])},
            "device_scale_factor": round(random.uniform(2.0, 3.5), 1),
            "is_mobile": True, "has_touch": True,
            "_os": _ua_os(ua), "_browser": _ua_browser(ua),
        }
        self._add_extras(fp)
        return fp

    def _add_extras(self, fp: dict[str, Any]) -> None:
        """Add locale, timezone, colour-scheme, and carrier randomisation."""
        if self._defaults.get("random_locale", True):
            fp["locale"] = random.choice(_LOCALES)
        if self._defaults.get("random_timezone", True):
            fp["timezone_id"] = random.choice(_TIMEZONES)
        if self._defaults.get("random_color_scheme", True):
            fp["color_scheme"] = random.choice(_COLOR_SCHEMES)
        # Always pick a carrier different from the previous attempt.
        pool = [c for c in _CARRIERS if c != _last_carrier[0]] or list(_CARRIERS)
        carrier = random.choice(pool)
        _last_carrier[0] = carrier
        fp["_carrier"] = carrier
