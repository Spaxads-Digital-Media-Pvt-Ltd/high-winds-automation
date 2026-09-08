"""
core/form_filler_ef_hosted.py — shared filler for sites that host the
dynamicformrequest ``ef-`` wizard on a dedicated /form page.

Simple Lending Direct embeds the same widget on its homepage. ExaBucks and
SimaCash keep it on ``/form``. Field names, chips, calendar, returning-
applicant bypass and post-submit chase are identical (same campaignUid
``abb7704f-0295-4847-92eb-65c9df1be953``).
"""
from __future__ import annotations

import re
import time
from urllib.parse import urlparse

import structlog
from playwright.sync_api import Page

from core.form_filler_simplelending import (
    FormFiller as _SLDFiller,
    _BUTTONS_JS,
    _FIELD_JS,
    _HEADING_JS,
)
from core.lead_platform import FormFillerError

log = structlog.get_logger(__name__)

__all__ = ["EfHostedFormFiller", "FormFillerError"]


class EfHostedFormFiller(_SLDFiller):
    """SLD wizard dispatch + wait for a JS-injected #ef-container widget."""

    default_url = ""
    site_label = "offer"

    def _prepare(self, page: Page, row_number: int) -> None:
        """Tracker/homepage has GET STARTED and no #ef-container.

        Relative ``/form`` links on a cloaked trackog URL go to trackog.net/form
        (no widget). After the CTA click, always fall back to this offer's
        real ``default_url`` (exabucks.com/form, simacash.com/form).
        """
        self._wait_for_landing(page)
        if not self._ef_widget_ready(page):
            clicked = self._click_get_started(page)
            if clicked:
                log.info("form.landing_cta", site=self.site_label, cta=clicked,
                         url=(page.url or "")[:90], row=row_number)
                time.sleep(1.2)
        if not self._ef_widget_ready(page):
            self._open_direct_form(page, row_number)
        self._wait_ef_widget(page, row_number)

    def _wait_for_landing(self, page: Page) -> None:
        """Don't require the URL to leave trackog — cloaking keeps that host."""
        deadline = time.time() + 20
        while time.time() < deadline:
            if self._ef_widget_ready(page) or self._landing_cta_visible(page):
                return
            time.sleep(0.4)

    def _landing_cta_visible(self, page: Page) -> bool:
        try:
            return bool(page.evaluate(
                r"""() => {
                    const t = (document.body && document.body.innerText) || '';
                    if (/get started|apply now/i.test(t)) return true;
                    return !!document.querySelector('a[href*="/form"], a.btn--primary, .hero__button a');
                }"""
            ))
        except Exception:
            for fr in page.frames:
                try:
                    if fr.evaluate(
                        r"""() => /get started|apply now/i.test(
                            (document.body && document.body.innerText) || '')"""
                    ):
                        return True
                except Exception:
                    continue
            return False

    def _ef_widget_ready(self, page: Page) -> bool:
        for fr in page.frames:
            try:
                if fr.evaluate(
                    """() => {
                        const c = document.querySelector('#ef-container');
                        return !!(c && c.children.length > 0);
                    }"""
                ):
                    return True
            except Exception:
                continue
        return False

    def _click_get_started(self, page: Page) -> str:
        name_re = re.compile(
            r"get started|apply now|start here|start now|get started now", re.I
        )
        for fr in page.frames:
            locators = [
                fr.get_by_role("link", name=name_re),
                fr.get_by_role("button", name=name_re),
                fr.locator(".hero__button a, a.btn--primary, a[href*='/form']"),
                fr.get_by_text("GET STARTED", exact=False),
            ]
            for loc in locators:
                try:
                    target = loc.first
                    if target.count() == 0:
                        continue
                    target.scroll_into_view_if_needed(timeout=2000)
                    label = (target.inner_text() or "").replace("\n", " ").strip()[:40]
                    href = ""
                    try:
                        href = target.get_attribute("href") or ""
                    except Exception:
                        href = ""
                    host = (urlparse(page.url or "").hostname or "").lower()
                    cloaked = bool(re.search(r"trackog|digipalz|digipiz", host))
                    if cloaked and re.search(r"^/form", href or ""):
                        continue
                    target.click(timeout=4000, force=True)
                    return label or "GET STARTED"
                except Exception:
                    continue
            try:
                label = fr.evaluate(
                    r"""() => {
                        const vis = e => {
                            if (!e) return false;
                            const r = e.getBoundingClientRect();
                            return r.width > 0 && r.height > 0;
                        };
                        const t = e => (e.innerText || e.getAttribute('aria-label') || '')
                            .replace(/\s+/g, ' ').trim();
                        const hit = Array.from(document.querySelectorAll('a,button'))
                            .filter(vis)
                            .find(e => /get started|apply now|start here|start now/i.test(t(e)));
                        if (!hit) return '';
                        hit.click();
                        return t(hit).slice(0, 40) || 'GET STARTED';
                    }"""
                )
                if label:
                    return label
            except Exception:
                continue
        return ""

    def _open_direct_form(self, page: Page, row_number: int) -> None:
        dest = (self.default_url or "").strip()
        if not dest:
            parsed = urlparse(page.url or "")
            host = (parsed.hostname or "").lower()
            if host and not re.search(r"trackog|digipalz|digipiz", host):
                dest = f"{parsed.scheme}://{parsed.netloc}/form"
        if not dest:
            return
        if dest.rstrip("/") in (page.url or "").split("?")[0].rstrip("/"):
            return
        log.info("form.open_direct_form", url=dest[:90], row=row_number)
        try:
            page.goto(dest, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            log.warning("form.open_direct_form_failed", error=str(e)[:80], row=row_number)

    def _wait_ef_widget(self, page: Page, row_number: int) -> None:
        deadline = time.time() + 30
        while time.time() < deadline:
            if self._ef_widget_ready(page):
                time.sleep(0.6)
                log.info("form.widget_ready", site=self.site_label,
                         url=(page.url or "")[:90], row=row_number)
                return
            time.sleep(0.4)
        self._screenshot(page, row_number, "form_widget_missing")
        raise FormFillerError(
            f"{self.site_label} form widget did not load after GET STARTED / {self.default_url}",
            error_type="stuck",
        )

    def _form_frame(self, page: Page):
        for fr in page.frames:
            try:
                if fr.evaluate("() => !!document.querySelector('#ef-container')"):
                    return fr
            except Exception:
                continue
        return super()._form_frame(page)

    def _visible_fields(self, page: Page) -> list:
        try:
            return self._form_frame(page).evaluate(_FIELD_JS) or []
        except Exception:
            return []

    def _choice_buttons(self, page: Page) -> list:
        try:
            return self._form_frame(page).evaluate(_BUTTONS_JS) or []
        except Exception:
            return []

    def _heading(self, page: Page) -> str:
        try:
            return self._form_frame(page).evaluate(_HEADING_JS) or ""
        except Exception:
            return ""
