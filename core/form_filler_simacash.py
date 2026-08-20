"""
core/form_filler_simacash.py — simacash.com (via the digipalz.trackog.net
tracking link).

The original SimaCash filler (a ~1300-line bespoke driver for a hash-routed
React wizard, deleted in 84619ce and restored from git history) no longer
matches the live site: a real run got stuck on the very first step, and the
diagnostic dump of visible clickables showed the loan-amount chips carry
`ef-btn ef-btn-group__btn` classes -- the exact same markup as
simplelendingdirect.com and exabucks.com. SimaCash has clearly been rebuilt
on the same "ef-" component wizard (dynamicformrequest.com/form-loader.js)
since that filler was last confirmed working; the `#loanAmount` URL hash
that survives on the tracking-link redirect is a leftover artifact, not
real routing anymore.

Same reasoning as form_filler_exabucks.py: reuse the Simple Lending Direct
filler wholesale rather than hand-maintain a near-duplicate, and override
just the target URL. Going straight to simacash.com hits a Cloudflare
challenge page (confirmed directly); the tracking link is the entry point
that actually reaches the real wizard, through whatever referrer/cookie
chain Cloudflare accepts for affiliate traffic.
"""
from __future__ import annotations

from core.form_filler_simplelending import FormFiller as _SimpleLendingFormFiller
from core.lead_platform import FormFillerError

__all__ = ["FormFiller", "FormFillerError"]


class FormFiller(_SimpleLendingFormFiller):
    default_url = "https://digipalz.trackog.net/c?oid=34&affid=442"
