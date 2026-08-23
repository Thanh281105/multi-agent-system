"""Same-origin static client installation."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

_FRONTEND_ROOT = Path(__file__).resolve().parents[1] / "frontend"


def install_frontend(application: FastAPI) -> None:
    """Serve packaged assets without adding a second runtime or CORS boundary."""

    index_path = _FRONTEND_ROOT / "index.html"
    assets_path = _FRONTEND_ROOT / "assets"
    if not index_path.is_file() or not assets_path.is_dir():
        raise RuntimeError("packaged frontend assets are missing")

    application.mount(
        "/assets",
        StaticFiles(directory=assets_path),
        name="frontend-assets",
    )

    @application.get("/", include_in_schema=False)
    async def frontend_index() -> FileResponse:
        return FileResponse(index_path, media_type="text/html")

    @application.get("/favicon.ico", include_in_schema=False)
    async def empty_favicon() -> Response:
        """Avoid a noisy browser 404 until a branded image asset is supplied."""

        return Response(status_code=204)
