from backend.po_followup.api import admin, dashboard, replies
from backend.voice_agent.routes import browser, calls, media, pages

# PO follow-up routers (backend/po_followup/api/):
#   replies: UltraMsg webhook storing vendor WhatsApp replies in the PO sheet
#   admin: PO follow-up admin page and API
#   dashboard: read-only business view of overdue PO lines and their scenario status
routers = [pages.router, calls.router, media.router, browser.router, replies.router, admin.page_router, admin.router,
           dashboard.page_router, dashboard.router]
