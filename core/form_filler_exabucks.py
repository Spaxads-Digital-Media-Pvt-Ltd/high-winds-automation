"""
core/form_filler_exabucks.py — exabucks.com/form.

Same "ef-" component wizard (dynamicformrequest.com/form-loader.js) as
simplelendingdirect.com — confirmed live: identical progress-bar/button
markup, identical landing-page loan chips ($500/$1,000/$2,000/$3,000), and
the first wizard step is byte-identical (".ef-title-main" reads "What Is
Your Email Address?", input[name=email]). Only the skin (campaignUid,
primaryColor) differs, so this offer reuses the Simple Lending Direct
filler wholesale and overrides just the target URL.

The post-offer "processing" screen's copy ("Do not close this window while
we process your request... This will take 2-3 minutes") turned out to be
shared verbatim across the whole platform family, not ExaBucks-specific --
that recognition now lives in the shared _JS_POST_STATE in
core/lead_platform.py rather than as an override here.
"""
from __future__ import annotations

from core.form_filler_simplelending import FormFiller as _SimpleLendingFormFiller
from core.lead_platform import FormFillerError

__all__ = ["FormFiller", "FormFillerError"]


class FormFiller(_SimpleLendingFormFiller):
    default_url = "https://exabucks.com/form"
