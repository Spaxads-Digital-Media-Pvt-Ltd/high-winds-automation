"""
core/form_filler_exabucks.py — exabucks.com/form.

Same "ef-" component wizard (dynamicformrequest.com/form-loader.js) as
simplelendingdirect.com — confirmed live: identical progress-bar/button
markup, identical landing-page loan chips ($500/$1,000/$2,000/$3,000), and
the first wizard step is byte-identical (".ef-title-main" reads "What Is
Your Email Address?", input[name=email]). Only the skin (campaignUid,
primaryColor) differs, so this offer reuses the Simple Lending Direct
filler wholesale and overrides just the target URL.
"""
from __future__ import annotations

from core.form_filler_simplelending import FormFiller as _SimpleLendingFormFiller
from core.lead_platform import FormFillerError

__all__ = ["FormFiller", "FormFillerError"]


class FormFiller(_SimpleLendingFormFiller):
    default_url = "https://exabucks.com/form"

    # The post-offer "processing" screen reads "Do not close this window while
    # we process your request... This will take 2-3 minutes... you will be
    # redirected..." -- none of the shared _JS_POST_STATE phrasing (tuned for
    # AEF/SLD's "processing your" / "connecting with" copy) matches this, so
    # the base class's `st.get("processing")` was always False here and
    # _handle_post_offer gave up after its 45s no-progress cutoff instead of
    # waiting out the full 2-3 minutes for the real CTA/redirect. Same JS as
    # the base class, with ExaBucks' own wording added to the processing regex.
    _JS_POST_STATE = r"""() => {
        const vis = e => e.offsetParent !== null && e.getClientRects().length > 0;
        const t = e => (e.innerText || e.value || '').replace(/\s+/g, ' ').trim();
        const body = (document.body ? document.body.innerText : '').toLowerCase();
        const processing = /(connecting with|trusted lenders|should only take|do not refresh|do not leave|please wait|one moment|processing your|matching you|finding you|searching for|finalis|finaliz|do not close this window|process your request|this will take|will take \d|connected with one of our|authorized lenders|lender.network)/.test(body);
        const fields = Array.from(document.querySelectorAll('input,select,textarea'))
            .filter(e => vis(e) && e.type !== 'hidden' && !e.disabled && !e.readOnly)
            .map(e => (e.name || e.id || ''));
        const buttons = Array.from(document.querySelectorAll(
                'button,input[type=submit],input[type=button],[role=button],a.btn,a.button'))
            .filter(e => vis(e) && !e.disabled).map(t).filter(x => x && x.length < 40);
        return { processing, fields, buttons, sig: fields.join(',') + '|' + buttons.join(',') };
    }"""
