"""Same-origin static client installation."""

from pathlib import Path, PurePosixPath

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

_FRONTEND_ROOT = Path(__file__).resolve().parents[1] / "frontend" / "dist"
_RESERVED_ROOTS = frozenset(
    {
        "api",
        "assets",
        "docs",
        "favicon.ico",
        "favicon.svg",
        "health",
        "livez",
        "metrics",
        "openapi.json",
        "readyz",
        "redoc",
    }
)


def install_frontend(application: FastAPI) -> None:
    """Serve packaged assets without adding a second runtime or CORS boundary."""

    index_path = _FRONTEND_ROOT / "index.html"
    assets_path = _FRONTEND_ROOT / "assets"
    favicon_path = _FRONTEND_ROOT / "favicon.svg"
    if (
        not index_path.is_file()
        or not assets_path.is_dir()
        or not favicon_path.is_file()
    ):
        raise RuntimeError("packaged frontend assets are missing")

    application.mount(
        "/assets",
        StaticFiles(directory=assets_path),
        name="frontend-assets",
    )

    @application.get("/", include_in_schema=False)
    async def frontend_index() -> FileResponse:
        return _index_response(index_path)

    @application.get("/favicon.svg", include_in_schema=False)
    async def frontend_favicon() -> FileResponse:
        return FileResponse(
            favicon_path,
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @application.get("/favicon.ico", include_in_schema=False)
    async def empty_favicon() -> Response:
        """Avoid a noisy browser 404 until a branded image asset is supplied."""

        return Response(status_code=204)

    @application.api_route(
        "/{client_path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        include_in_schema=False,
    )
    async def frontend_spa(request: Request, client_path: str) -> FileResponse:
        """Return the SPA shell for safe client routes, never for server namespaces."""

        if request.method not in {"GET", "HEAD"} or not _is_safe_client_route(
            client_path
        ):
            raise HTTPException(status_code=404, detail="Not Found")
        return _index_response(index_path)


def _index_response(index_path: Path) -> FileResponse:
    return FileResponse(
        index_path,
        media_type="text/html",
        headers={"Cache-Control": "no-cache"},
    )


def _is_safe_client_route(client_path: str) -> bool:
    path = PurePosixPath(client_path)
    if not path.parts or path.parts[0] in _RESERVED_ROOTS:
        return False
    return not any(
        part in {".", ".."} or part.startswith(".") or "." in part
        for part in path.parts
    )
