"""FastAPI application: the clean HTTP surface over the legacy portal."""

import csv
import hmac
import io
import logging
import time
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path as PathParam, Query, Request, Response, Security
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.security import APIKeyHeader
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.cache import Fetched
from app.client import (
    PortalAuthError,
    PortalError,
    PortalNotFound,
    PortalProtocolError,
    PortalUnavailable,
    UrjaPortalClient,
)
from app.config import Settings, get_settings
from app.models import (
    AuthStatus,
    ConsumptionResponse,
    ErrorResponse,
    ExportResponse,
    HealthResponse,
    HierarchyResponse,
    Meter,
    MeterDetail,
    NearbyMeter,
    Page,
    ReadyResponse,
    StatsResponse,
    Transformer,
)
from app.service import MeterService

logger = logging.getLogger("flock.api")

STATIC_DIR = Path(__file__).parent / "static"
METER_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$"
SORT_PATTERN = (
    r"^-?(meter_id|serial_number|make|phase_type|install_status|install_type|build|dt_code|zone|feeder)$"
)

ERROR_RESPONSES = {
    404: {"model": ErrorResponse, "description": "Meter or resource not found"},
    422: {"model": ErrorResponse, "description": "Invalid query parameters"},
    502: {"model": ErrorResponse, "description": "The legacy portal returned an error"},
    503: {"model": ErrorResponse, "description": "The legacy portal is unavailable"},
}


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    )


def get_service(request: Request) -> MeterService:
    return request.app.state.service


ServiceDep = Annotated[MeterService, Depends(get_service)]


def _set_data_headers(response: Response, fetched: Fetched, max_age: int = 30) -> None:
    response.headers["X-Cache"] = fetched.cache
    response.headers["X-Data-Fetched-At"] = fetched.fetched_at.isoformat()
    response.headers["Cache-Control"] = f"private, max-age={max_age}"


def _csv_safe(value: object) -> str:
    text = "" if value is None else str(value)
    # Neutralise spreadsheet formula injection without altering normal values.
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def _build_csv(service: MeterService) -> tuple[str, Fetched]:
    fetched = service.export_meters()
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        [
            "meter_id",
            "serial_number",
            "make",
            "phase_type",
            "install_status",
            "install_type",
            "build",
            "dt_code",
            "zone_code",
            "zone_name",
            "circle_name",
            "division_name",
            "subdivision_name",
            "substation_name",
            "feeder_code",
            "feeder_name",
            "dt_name",
            "latitude",
            "longitude",
        ]
    )
    for meter in fetched.value.meters:
        hierarchy = meter.hierarchy
        writer.writerow(
            [
                _csv_safe(meter.meter_id),
                _csv_safe(meter.serial_number),
                _csv_safe(meter.make),
                _csv_safe(meter.phase_type),
                _csv_safe(meter.install_status),
                _csv_safe(meter.install_type),
                _csv_safe(meter.build),
                _csv_safe(meter.dt_code),
                _csv_safe(hierarchy.zone.code),
                _csv_safe(hierarchy.zone.name),
                _csv_safe(hierarchy.circle.name),
                _csv_safe(hierarchy.division.name),
                _csv_safe(hierarchy.subdivision.name),
                _csv_safe(hierarchy.substation.name),
                _csv_safe(hierarchy.feeder.code),
                _csv_safe(hierarchy.feeder.name),
                _csv_safe(hierarchy.dt.name),
                _csv_safe(meter.location.latitude if meter.location else ""),
                _csv_safe(meter.location.longitude if meter.location else ""),
            ]
        )
    return buffer.getvalue(), fetched


def create_app(settings: Settings | None = None, service: MeterService | None = None) -> FastAPI:
    settings = settings or (service.settings if service is not None else get_settings())
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.started_at = time.monotonic()
        if service is None:
            client = UrjaPortalClient(
                settings.portal_base_url,
                settings.portal_email,
                settings.portal_password,
                timeout_seconds=settings.portal_request_timeout_seconds,
                connect_timeout_seconds=settings.portal_connect_timeout_seconds,
                max_retries=settings.portal_max_retries,
                refresh_skew_seconds=settings.session_refresh_skew_seconds,
                user_agent=f"flock-energy-api/{settings.app_version} (read-only adapter)",
            )
            app.state.client = client
            app.state.service = MeterService(client, settings)
        else:
            app.state.client = None
            app.state.service = service
        logger.info(
            "Starting %s v%s (portal=%s, api_key=%s)",
            settings.app_name,
            settings.app_version,
            settings.portal_base_url,
            "enabled" if settings.api_key else "disabled",
        )
        try:
            yield
        finally:
            if app.state.client is not None:
                app.state.client.close()

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=(
            "A normalised, documented REST layer over the read-only **Urja Meter Ops** portal.\n\n"
            "The service authenticates with the legacy portal on your behalf, signs and caches "
            "the bulk export, repairs messy hierarchy data, converts cumulative meter registers "
            "into interval consumption, and exposes stable JSON endpoints.\n\n"
            "* Interactive docs: `/docs` (Swagger UI) or `/reference` (Scalar)\n"
            "* Raw contract: `/openapi.json`\n"
            "* Human dashboard: `/`\n"
        ),
        lifespan=lifespan,
        openapi_tags=[
            {"name": "Meters", "description": "Search, inspect, locate and export meters."},
            {"name": "Consumption", "description": "Cumulative register history, normalised into interval usage."},
            {"name": "Network", "description": "Reconstructed hierarchy and distribution transformers."},
            {"name": "Operations", "description": "Summary statistics, readiness and session health."},
            {"name": "Auth", "description": "Server-side portal session management."},
        ],
        contact={"name": "Flock Energy API take-home"},
    )

    if settings.cors_origin_list:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
        )

    api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
    router_dependencies = []
    if settings.api_key:

        def require_api_key(api_key: Annotated[str | None, Security(api_key_header)]) -> None:
            if not api_key or not hmac.compare_digest(api_key, settings.api_key or ""):
                raise HTTPException(
                    status_code=401,
                    detail="A valid X-API-Key header is required.",
                    headers={"WWW-Authenticate": "X-API-Key"},
                )

        router_dependencies.append(Depends(require_api_key))

    router = APIRouter(prefix="/api/v1", dependencies=router_dependencies, responses=ERROR_RESPONSES)

    @router.get("/", tags=["Operations"], summary="API index")
    def api_index() -> dict[str, str]:
        return {
            "name": settings.app_name,
            "version": settings.app_version,
            "docs": "/docs",
            "openapi": "/openapi.json",
        }

    # -- Meters -------------------------------------------------------
    @router.get(
        "/meters",
        response_model=Page[Meter],
        tags=["Meters"],
        summary="List and filter meters",
        description=(
            "Backed by the signed bulk export, so filtering happens locally across the whole "
            "dataset (unlike the portal's ID/serial-only search). Filters are case-insensitive; "
            "status, phase, make, build, dt, feeder and zone accept comma-separated values."
        ),
    )
    def list_meters(
        response: Response,
        service: ServiceDep,
        q: Annotated[str | None, Query(max_length=64, description="Substring across ID, serial, make and hierarchy")] = None,
        status: Annotated[str | None, Query(description="Installed, Faulty, Decommissioned")] = None,
        phase: Annotated[str | None, Query(description="single or three")] = None,
        make: Annotated[str | None, Query()] = None,
        build: Annotated[str | None, Query(description="legacy or v2")] = None,
        dt: Annotated[str | None, Query(description="DT code or name")] = None,
        feeder: Annotated[str | None, Query(description="Feeder code or name")] = None,
        zone: Annotated[str | None, Query(description="Zone code or name")] = None,
        sort: Annotated[str, Query(pattern=SORT_PATTERN, description="Field, prefix with - for descending")] = "meter_id",
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=200)] = settings.default_page_size,
    ) -> Page[Meter]:
        result = service.list_meters(
            q=q,
            status=status,
            phase=phase,
            make=make,
            build=build,
            dt=dt,
            feeder=feeder,
            zone=zone,
            sort=sort,
            page=page,
            page_size=page_size,
        )
        _set_data_headers(response, result)
        return result.value

    @router.get(
        "/meters/near",
        response_model=list[NearbyMeter],
        tags=["Meters"],
        summary="Find meters near a point",
        description="Haversine search over the dataset's coordinates. This is a local capability the portal lacks.",
    )
    def near_meters(
        response: Response,
        service: ServiceDep,
        latitude: Annotated[float, Query(ge=-90, le=90, description="Query point latitude")],
        longitude: Annotated[float, Query(ge=-180, le=180, description="Query point longitude")],
        radius_km: Annotated[float, Query(gt=0, le=100)] = 5.0,
        limit: Annotated[int, Query(ge=1, le=200)] = 25,
    ) -> list[NearbyMeter]:
        result = service.near_meters(
            latitude=latitude,
            longitude=longitude,
            radius_km=radius_km,
            limit=limit,
        )
        _set_data_headers(response, result)
        return result.value

    @router.get(
        "/meters/{meter_id}",
        response_model=MeterDetail,
        tags=["Meters"],
        summary="Get one meter",
        description=(
            "Nameplate fields come from the live server-rendered meter page (which uses two "
            "different detail schemas); hierarchy and coordinates come from the cached bulk "
            "snapshot. If the detail page fails, the cached snapshot is served with a warning."
        ),
    )
    def get_meter(
        meter_id: Annotated[str, PathParam(pattern=METER_ID_PATTERN, examples=["J100001"])],
        response: Response,
        service: ServiceDep,
    ) -> MeterDetail:
        result = service.get_meter_detail(meter_id)
        _set_data_headers(response, result, max_age=30)
        return result.value

    # -- Consumption --------------------------------------------------
    @router.get(
        "/meters/{meter_id}/consumption",
        response_model=ConsumptionResponse,
        tags=["Consumption"],
        summary="Consumption history",
        description=(
            "Returns the portal's readings with cumulative registers converted to typed values "
            "and per-interval deltas. Dates are inclusive and optional; omitting both uses the "
            "portal's 7-day default window. The portal mixes 30-minute and daily meters, so "
            "`granularity` is detected rather than assumed. Timestamps are interpreted as IST."
        ),
        responses={
            200: {
                "description": "Readings in the portal's available window",
            }
        },
    )
    def get_consumption(
        meter_id: Annotated[str, PathParam(pattern=METER_ID_PATTERN, examples=["J100001"])],
        response: Response,
        service: ServiceDep,
        from_date: Annotated[date | None, Query(alias="from", description="Inclusive start date (YYYY-MM-DD)")] = None,
        to_date: Annotated[date | None, Query(alias="to", description="Inclusive end date (YYYY-MM-DD)")] = None,
    ) -> ConsumptionResponse:
        try:
            result = service.get_consumption(meter_id, from_date=from_date, to_date=to_date)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _set_data_headers(response, result)
        return result.value

    # -- Network ------------------------------------------------------
    @router.get(
        "/hierarchy",
        response_model=HierarchyResponse,
        tags=["Network"],
        summary="Reconstructed network hierarchy",
        description=(
            "Meters are organised Zone → Circle → Division → Subdivision → Substation → Feeder → DT. "
            "The portal reports per-meter paths, not a normalised tree; this endpoint merges blank "
            "codes/names, treats the DT registry as authoritative for names, and resolves nodes "
            "reported under multiple parents using the most frequent parent. Conflicts and data "
            "quality flags are included rather than hidden."
        ),
    )
    def get_hierarchy(response: Response, service: ServiceDep) -> HierarchyResponse:
        result = service.get_hierarchy()
        _set_data_headers(response, result, max_age=60)
        return result.value

    @router.get(
        "/transformers",
        response_model=Page[Transformer],
        tags=["Network"],
        summary="Distribution transformers",
        description="The portal's DT registry joined to live meter counts and status breakdowns.",
    )
    def list_transformers(
        response: Response,
        service: ServiceDep,
        q: Annotated[str | None, Query(max_length=64, description="Code, name or feeder code")] = None,
        feeder: Annotated[str | None, Query(description="Feeder code")] = None,
        zone: Annotated[str | None, Query(description="Zone code or name")] = None,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=200)] = settings.default_page_size,
    ) -> Page[Transformer]:
        result = service.list_transformers(
            q=q, feeder=feeder, zone=zone, page=page, page_size=page_size
        )
        _set_data_headers(response, result)
        return result.value

    # -- Operations ---------------------------------------------------
    @router.get(
        "/stats/summary",
        response_model=StatsResponse,
        tags=["Operations"],
        summary="Dataset summary",
        description="Counts by status, make, phase, build and install type, plus dataset freshness.",
    )
    def get_stats(response: Response, service: ServiceDep) -> StatsResponse:
        result = service.get_stats()
        _set_data_headers(response, result, max_age=60)
        return result.value

    @router.get(
        "/export/meters",
        response_model=ExportResponse,
        tags=["Meters"],
        summary="Export the full clean dataset",
        description="One JSON document containing every normalised meter. Useful for warehouse loads.",
    )
    def export_meters(response: Response, service: ServiceDep) -> ExportResponse:
        result = service.export_meters()
        _set_data_headers(response, result, max_age=60)
        return ExportResponse(data=result.value.meters, total=len(result.value.meters), fetched_at=result.fetched_at)

    @router.get(
        "/export/meters.csv",
        response_class=Response,
        tags=["Meters"],
        summary="Export the full dataset as CSV",
        responses={
            200: {
                "content": {"text/csv": {"schema": {"type": "string"}}},
                "description": "Flat CSV, one row per meter",
            }
        },
    )
    def export_meters_csv(response: Response, service: ServiceDep) -> Response:
        content, fetched = _build_csv(service)
        response.headers["Content-Disposition"] = 'attachment; filename="urja-meters.csv"'
        _set_data_headers(response, fetched, max_age=60)
        return Response(
            content=content,
            media_type="text/csv; charset=utf-8",
            headers=dict(response.headers),
        )

    @router.get(
        "/auth/status",
        response_model=AuthStatus,
        tags=["Auth"],
        summary="Portal session status",
        description="Reports the adapter's local view of the portal session without contacting the portal.",
    )
    def auth_status(service: ServiceDep) -> AuthStatus:
        return service.auth_status()

    @router.post(
        "/auth/login",
        response_model=AuthStatus,
        tags=["Auth"],
        summary="Establish a portal session",
        description=(
            "Logs in with the server-side configured portal credentials and returns the new "
            "session expiry. No credentials are accepted from the caller; the service is a "
            "single-tenant adapter and never forwards client-supplied passwords."
        ),
    )
    def auth_login(service: ServiceDep) -> AuthStatus:
        status = service.auth_status(force_login=True)
        if not status.authenticated:
            raise PortalAuthError(status.detail or "Portal login failed")
        return status

    app.include_router(router)

    # -- Error handling ------------------------------------------------
    @app.exception_handler(PortalNotFound)
    async def portal_not_found(_: Request, exc: PortalNotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"error": "meter_not_found", "message": str(exc)})

    @app.exception_handler(PortalAuthError)
    async def portal_auth(_: Request, exc: PortalAuthError) -> JSONResponse:
        logger.error("Portal authentication failed: %s", exc)
        return JSONResponse(status_code=502, content={"error": "portal_auth_failed", "message": str(exc)})

    @app.exception_handler(PortalUnavailable)
    async def portal_unavailable(_: Request, exc: PortalUnavailable) -> JSONResponse:
        logger.error("Portal unavailable: %s", exc)
        return JSONResponse(status_code=503, content={"error": "portal_unavailable", "message": str(exc)})

    @app.exception_handler(PortalProtocolError)
    async def portal_protocol(_: Request, exc: PortalProtocolError) -> JSONResponse:
        logger.error("Portal protocol error: %s", exc)
        return JSONResponse(status_code=502, content={"error": "portal_protocol_error", "message": str(exc)})

    @app.exception_handler(PortalError)
    async def portal_error(_: Request, exc: PortalError) -> JSONResponse:
        logger.error("Portal error: %s", exc)
        return JSONResponse(status_code=502, content={"error": "portal_error", "message": str(exc)})

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {401: "unauthorized", 404: "not_found", 422: "validation_error"}.get(
            exc.status_code, "http_error"
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": code, "message": str(exc.detail)},
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": "validation_error",
                "message": "One or more query parameters are invalid.",
                "details": jsonable_encoder(exc.errors()),
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"error": "internal_error", "message": "An unexpected error occurred."},
        )

    # -- Health, docs and dashboard -----------------------------------
    @app.get("/health", response_model=HealthResponse, tags=["Operations"])
    def health(request: Request, service: ServiceDep) -> HealthResponse:
        status = service.auth_status()
        # Liveness: credentials are all this process needs to be able to work.
        # Whether the portal answers is reported by /health/ready; whether the
        # cached session is present is exposed in the body.
        return HealthResponse(
            status="ok" if settings.credentials_configured else "degraded",
            service=settings.app_name,
            version=settings.app_version,
            uptime_seconds=round(time.monotonic() - request.app.state.started_at, 3),
            credentials_configured=settings.credentials_configured,
            session=status,
            cache=service.cache.stats(),
        )

    @app.get("/health/ready", response_model=ReadyResponse, tags=["Operations"])
    def ready(response: Response, service: ServiceDep) -> ReadyResponse:
        ok, fetched, detail = service.readiness()
        if not ok:
            response.status_code = 503
        return ReadyResponse(
            status="ready" if ok else "unavailable",
            dataset_cached=fetched is not None,
            meter_count=len(fetched.value.meters) if fetched is not None else None,
            data_fetched_at=fetched.fetched_at if fetched is not None else None,
            detail=detail,
        )

    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        index = STATIC_DIR / "index.html"
        if not index.exists():
            return HTMLResponse(
                "<h1>Flock Energy — Urja Meter Ops API</h1>"
                "<p>Dashboard not installed. See <a href='/docs'>/docs</a>.</p>"
            )
        return FileResponse(index, media_type="text/html")

    @app.get("/reference", include_in_schema=False)
    def scalar_reference() -> HTMLResponse:
        return HTMLResponse(
            """<!doctype html>
<html><head><meta charset="utf-8"><title>API Reference</title>
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body>
<script id="api-reference" data-url="/openapi.json"></script>
<script src="https://cdn.jsdelivr.net/npm/@scalar/api-reference"></script>
</body></html>"""
        )

    return app


app = create_app()
