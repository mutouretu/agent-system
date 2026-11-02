"""Independent Agent management page route; keeps api.py wiring minimal."""

from pathlib import Path

from fastapi import FastAPI
from starlette.responses import FileResponse, HTMLResponse

STATIC_DIR = Path(__file__).parent / "static"
AGENTS_PAGE = STATIC_DIR / "agents.html"

# Same policy the browser boundary middleware applies to "/" and "/assets/*".
CSP_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
)


def harden(response):
    """Apply the root page's CSP and nosniff headers to a standalone page response."""

    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = CSP_POLICY
    return response


def install_agent_page(app: FastAPI) -> None:
    """Serve the standalone /agents page from the service static directory."""

    @app.get("/agents", include_in_schema=False)
    async def agent_management_page():
        if AGENTS_PAGE.is_file():
            return harden(FileResponse(AGENTS_PAGE, media_type="text/html"))
        return harden(
            HTMLResponse(
                "<!DOCTYPE html><title>Agent 管理</title><p>Agent 管理页面资源缺失。</p>",
                status_code=503,
            )
        )
