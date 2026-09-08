"""
core/form_filler_exabucks.py — exabucks.com multi-step form automation.

Same ``ef-`` wizard as Simple Lending Direct. Live page hosts it at /form
(GET STARTED from the homepage) with campaignUid
``abb7704f-0295-4847-92eb-65c9df1be953`` and brand colour ``#1dbf25``.
"""
from __future__ import annotations

from core.form_filler_ef_hosted import EfHostedFormFiller, FormFillerError

__all__ = ["FormFiller", "FormFillerError"]


class FormFiller(EfHostedFormFiller):
    """exabucks.com — shared ef- wizard on /form."""

    default_url = "https://exabucks.com/form"
    site_label = "ExaBucks"
