from po_followup import admin, replies

from . import browser, calls, media, pages

# replies: UltraMsg webhook storing vendor WhatsApp replies in the PO sheet (see po_followup/replies.py)
# admin: PO follow-up admin page and API (see po_followup/admin.py)
routers = [pages.router, calls.router, media.router, browser.router, replies.router, admin.page_router, admin.router]
