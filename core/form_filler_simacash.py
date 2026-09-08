"""
core/form_filler_simacash.py — simacash.com multi-step form automation.

Current live site is the same lead-platform family as Simple Lending Direct
and ExaBucks: ``#ef-container`` + ``https://dynamicformrequest.com/form-loader.js``,
``form: 'main'``, campaignUid ``abb7704f-0295-4847-92eb-65c9df1be953``.

GET STARTED on https://simacash.com/ lands on /form. Sheet columns map the
same way as the other two offers (email, name, phone, military, address/zip,
homeowner, DOB, income source, pay frequency, payday calendar, income
brackets, employer, DL, credit, loan purpose, debt, direct deposit, account
type, SSN, routing, account, bank name).
"""
from __future__ import annotations

from core.form_filler_ef_hosted import EfHostedFormFiller, FormFillerError

__all__ = ["FormFiller", "FormFillerError"]


class FormFiller(EfHostedFormFiller):
    """simacash.com — shared ef- wizard on /form."""

    default_url = "https://simacash.com/form"
    site_label = "SimaCash"
