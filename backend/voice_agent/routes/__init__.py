from backend.po_followup.api import admin, dashboard, replies
from backend.voice_agent.routes import calls, media, pages

# PO follow-up routers (backend/po_followup/api/):
#   replies: UltraMsg webhook storing vendor WhatsApp replies in the PO sheet
#   admin: admin page and API (system tests, users)
#   dashboard: SCM user page and API
routers = [pages.router, calls.router, media.router, replies.router, admin.page_router, admin.router,
           dashboard.page_router, dashboard.auth_router, dashboard.router]
