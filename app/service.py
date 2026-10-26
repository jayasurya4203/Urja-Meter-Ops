"""Application service: a clean, cached view over the portal adapter.

The unit of freshness is a *dataset snapshot*: one bulk export plus the DT
registry, normalised and indexed together. Consumption and single-meter detail
are cached separately because they are the only genuinely per-meter calls.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from app.cache import Fetched, TTLCache
from app.client import (
    PortalError,
    PortalNotFound,
    PortalProtocolError,
    SessionStatus,
    UrjaPortalClient,
)
from app.config import Settings
from app.hierarchy import HierarchyBuild, build_hierarchy, build_stats, canonicalize_transformers
from app.models import (
    AuthStatus,
    ConsumptionResponse,
    HierarchyResponse,
    Meter,
    MeterDetail,
    NearbyMeter,
    Page,
    StatsResponse,
    Transformer,
)
from app.normalize import (
    hierarchy_path,
    normalize_detail,
    normalize_energy,
    normalize_meter,
    normalize_ssr_hierarchy,
)

logger = logging.getLogger("flock.service")

DATASET_KEY = "dataset"


@dataclass(frozen=True)
class Dataset:
    meters: list[Meter]
    meters_by_id: dict[str, Meter]
    transformers: list[Transformer]
    hierarchy: HierarchyBuild
    stats: StatsResponse
    fetched_at: datetime
    warnings: list[str]


@dataclass(frozen=True)
class DetailPayload:
    fields: dict[str, str]
    ssr_hierarchy: Any


def _spec_values(spec: str | None) -> set[str]:
    if not spec:
        return set()
    return {item.strip().lower() for item in spec.split(",") if item.strip()}


def _matches(value: str | None, allowed: set[str]) -> bool:
    if not allowed:
        return True
    return (value or "").strip().lower() in allowed


def _matches_node(code: str | None, name: str | None, allowed: set[str]) -> bool:
    if not allowed:
        return True
    values = {(code or "").strip().lower(), (name or "").strip().lower()}
    return bool(values & allowed)


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


class MeterService:
    def __init__(self, client: UrjaPortalClient, settings: Settings, cache: TTLCache | None = None) -> None:
        self._client = client
        self._settings = settings
        self._cache = cache or TTLCache(max_entries=settings.cache_max_entries)

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def cache(self) -> TTLCache:
        return self._cache

    # ------------------------------------------------------------------
    # Dataset
    # ------------------------------------------------------------------
    def _load_dataset(self) -> Dataset:
        payload = self._client.get_export()
        warnings: list[str] = []
        meters_by_id: dict[str, Meter] = {}
        skipped = 0
        for raw in payload["data"]:
            if not isinstance(raw, dict):
                skipped += 1
                continue
            try:
                meter = normalize_meter(raw)
            except ValueError:
                skipped += 1
                continue
            if meter.meter_id in meters_by_id:
                warnings.append(f"duplicate meter in export: {meter.meter_id}")
            meters_by_id[meter.meter_id] = meter
        if skipped:
            warnings.append(f"skipped {skipped} unparsable meter row(s) in the export")
        if not meters_by_id:
            raise PortalProtocolError("The bulk export contained no usable meters")

        raw_transformers: list[dict[str, Any]] = []
        try:
            raw_transformers = self._client.get_all_transformers()
        except PortalError as exc:
            warnings.append(f"transformer registry unavailable: {exc}")
            logger.warning("Transformer registry unavailable: %s", exc)

        meters = sorted(meters_by_id.values(), key=lambda meter: meter.meter_id)
        meters, transformers = canonicalize_transformers(meters, raw_transformers)
        hierarchy = build_hierarchy(meters, transformers)
        fetched_at = datetime.now(timezone.utc)
        stats = build_stats(meters, transformers, hierarchy, fetched_at)
        logger.info(
            "Dataset refreshed: %s meters, %s transformers, %s skipped",
            len(meters),
            len(transformers),
            skipped,
        )
        return Dataset(
            meters=meters,
            meters_by_id={meter.meter_id: meter for meter in meters},
            transformers=transformers,
            hierarchy=hierarchy,
            stats=stats,
            fetched_at=fetched_at,
            warnings=warnings,
        )

    def get_dataset(self) -> Fetched[Dataset]:
        return self._cache.get_or_load(
            DATASET_KEY,
            self._settings.dataset_cache_ttl_seconds,
            self._load_dataset,
        )

    def peek_dataset(self) -> Fetched[Dataset] | None:
        return self._cache.peek(DATASET_KEY)

    # ------------------------------------------------------------------
    # Meter queries
    # ------------------------------------------------------------------
    def list_meters(
        self,
        *,
        q: str | None = None,
        status: str | None = None,
        phase: str | None = None,
        make: str | None = None,
        build: str | None = None,
        dt: str | None = None,
        feeder: str | None = None,
        zone: str | None = None,
        sort: str = "meter_id",
        page: int = 1,
        page_size: int | None = None,
    ) -> Fetched[Page[Meter]]:
        dataset_result = self.get_dataset()
        dataset = dataset_result.value
        page_size = page_size or self._settings.default_page_size

        statuses = _spec_values(status)
        phases = _spec_values(phase)
        makes = _spec_values(make)
        builds = _spec_values(build)
        dts = _spec_values(dt)
        feeders = _spec_values(feeder)
        zones = _spec_values(zone)
        needle = (q or "").strip().lower()

        def keep(meter: Meter) -> bool:
            if not _matches(meter.install_status, statuses):
                return False
            if not _matches(meter.phase_type, phases):
                return False
            if not _matches(meter.make, makes):
                return False
            if not _matches(meter.build, builds):
                return False
            if not _matches_node(meter.hierarchy.dt.code, meter.hierarchy.dt.name, dts):
                return False
            if not _matches_node(meter.hierarchy.feeder.code, meter.hierarchy.feeder.name, feeders):
                return False
            if not _matches_node(meter.hierarchy.zone.code, meter.hierarchy.zone.name, zones):
                return False
            if needle:
                haystack = " ".join(
                    [
                        meter.meter_id,
                        meter.serial_number,
                        meter.make,
                        meter.dt_code,
                        meter.hierarchy.zone.name,
                        meter.hierarchy.circle.name,
                        meter.hierarchy.feeder.name,
                        meter.hierarchy.dt.name,
                    ]
                ).lower()
                if needle not in haystack:
                    return False
            return True

        filtered = [meter for meter in dataset.meters if keep(meter)]
        descending = sort.startswith("-")
        getter = self._sort_getter(sort)
        filtered.sort(
            key=lambda meter: (str(getter(meter)).lower(), meter.meter_id),
            reverse=descending,
        )
        total = len(filtered)
        start = (page - 1) * page_size
        items = filtered[start : start + page_size]
        pages = (total + page_size - 1) // page_size
        return Fetched(
            value=Page(items=items, page=page, page_size=page_size, total=total, pages=pages),
            cache=dataset_result.cache,
            fetched_at=dataset_result.fetched_at,
        )

    @staticmethod
    def _sort_getter(sort: str):
        field = sort.lstrip("-")
        allowed = {
            "meter_id": lambda meter: meter.meter_id,
            "serial_number": lambda meter: meter.serial_number,
            "make": lambda meter: meter.make,
            "phase_type": lambda meter: meter.phase_type,
            "install_status": lambda meter: meter.install_status,
            "install_type": lambda meter: meter.install_type,
            "build": lambda meter: meter.build,
            "dt_code": lambda meter: meter.dt_code,
            "zone": lambda meter: meter.hierarchy.zone.name,
            "feeder": lambda meter: meter.hierarchy.feeder.name,
        }
        return allowed.get(field, allowed["meter_id"])

    def get_meter(self, meter_id: str) -> Fetched[Meter]:
        result = self.get_dataset()
        meter = result.value.meters_by_id.get(meter_id)
        if meter is None:
            raise PortalNotFound(f"Meter {meter_id} was not found")
        return Fetched(value=meter, cache=result.cache, fetched_at=result.fetched_at)

    def near_meters(
        self,
        *,
        latitude: float,
        longitude: float,
        radius_km: float,
        limit: int = 25,
    ) -> Fetched[list[NearbyMeter]]:
        dataset_result = self.get_dataset()
        matches: list[NearbyMeter] = []
        for meter in dataset_result.value.meters:
            if meter.location is None:
                continue
            distance = _haversine_km(
                latitude, longitude, meter.location.latitude, meter.location.longitude
            )
            if distance <= radius_km:
                matches.append(
                    NearbyMeter(
                        **meter.model_dump(),
                        distance_km=round(distance, 3),
                    )
                )
        matches.sort(key=lambda meter: meter.distance_km)
        return Fetched(
            value=matches[:limit],
            cache=dataset_result.cache,
            fetched_at=dataset_result.fetched_at,
        )

    # ------------------------------------------------------------------
    # Detail
    # ------------------------------------------------------------------
    def _load_detail(self, meter_id: str) -> DetailPayload:
        raw = self._client.get_meter_detail(meter_id)
        if str(raw["meter_id"]).upper() != meter_id.upper():
            logger.warning("SSR detail id mismatch: requested %s, received %s", meter_id, raw["meter_id"])
        try:
            fields = normalize_detail(raw["detail"])
        except ValueError as exc:
            raise PortalProtocolError(f"Meter detail payload could not be normalised: {exc}") from exc
        ssr_hierarchy = normalize_ssr_hierarchy(raw["hierarchy"])
        return DetailPayload(fields=fields, ssr_hierarchy=ssr_hierarchy)

    def get_meter_detail(self, meter_id: str) -> Fetched[MeterDetail]:
        dataset_result = self.get_dataset()
        base = dataset_result.value.meters_by_id.get(meter_id)
        warnings: list[str] = []

        detail_result: Fetched[DetailPayload] | None
        try:
            detail_result = self._cache.get_or_load(
                f"detail:{meter_id}",
                self._settings.detail_cache_ttl_seconds,
                lambda: self._load_detail(meter_id),
            )
        except PortalError as exc:
            if base is None:
                if isinstance(exc, PortalNotFound):
                    raise PortalNotFound(f"Meter {meter_id} was not found") from exc
                raise
            if isinstance(exc, PortalNotFound):
                warnings.append("portal detail page returned 404; served from the cached bulk export")
            else:
                warnings.append(
                    f"portal detail page unavailable ({exc}); served from the cached bulk export"
                )
            detail_result = None

        if detail_result is not None and detail_result.cache == "stale":
            warnings.append("portal detail refresh failed; served a stale cached detail page")

        if detail_result is None:
            if base is None:
                raise PortalNotFound(f"Meter {meter_id} was not found")
            return Fetched(
                value=MeterDetail(
                    **base.model_dump(),
                    detail_source="bulk_export_cached",
                    observed_at=dataset_result.fetched_at,
                    warnings=warnings,
                ),
                cache=dataset_result.cache,
                fetched_at=dataset_result.fetched_at,
            )

        fields = detail_result.value.fields
        hierarchy = base.hierarchy if base is not None else detail_result.value.ssr_hierarchy
        location = base.location if base is not None else None
        if base is not None:
            for field_name in ("serial_number", "make", "phase_type", "install_status", "install_type"):
                portal_value = fields.get(field_name)
                bulk_value = getattr(base, field_name)
                if portal_value and portal_value.lower() != str(bulk_value).lower():
                    warnings.append(
                        f"{field_name} differs between bulk export ({bulk_value}) and detail page ({portal_value})"
                    )
        observed_at = detail_result.fetched_at
        meter = MeterDetail(
            meter_id=meter_id,
            serial_number=fields.get("serial_number") or (base.serial_number if base else "UNKNOWN"),
            make=fields.get("make") or (base.make if base else "UNKNOWN"),
            phase_type=(fields.get("phase_type") or (base.phase_type if base else "unknown")).lower(),
            install_status=fields.get("install_status") or (base.install_status if base else "Unknown"),
            install_type=fields.get("install_type") or (base.install_type if base else "Unknown"),
            build=base.build if base else "unknown",
            dt_code=(base.dt_code if base else (hierarchy.dt.code or "")),
            hierarchy=hierarchy,
            hierarchy_path=hierarchy_path(hierarchy),
            location=location,
            detail_source="portal_ssr",
            observed_at=observed_at,
            warnings=warnings,
        )
        return Fetched(
            value=meter,
            cache=detail_result.cache,
            fetched_at=detail_result.fetched_at,
        )

    # ------------------------------------------------------------------
    # Consumption
    # ------------------------------------------------------------------
    def get_consumption(
        self,
        meter_id: str,
        *,
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> Fetched[ConsumptionResponse]:
        dataset_result = self.get_dataset()
        if meter_id not in dataset_result.value.meters_by_id:
            raise PortalNotFound(f"Meter {meter_id} was not found")

        if from_date and to_date and from_date > to_date:
            raise ValueError("from must be on or before to")
        if from_date and to_date and (to_date - from_date).days > self._settings.max_consumption_range_days:
            raise ValueError(
                f"consumption range exceeds {self._settings.max_consumption_range_days} days"
            )

        key = f"energy:{meter_id}:{from_date or '*'}:{to_date or '*'}"

        def load() -> ConsumptionResponse:
            rows = self._client.get_meter_energy(meter_id, from_date, to_date)
            return normalize_energy(
                rows,
                meter_id=meter_id,
                timezone=self._settings.timezone,
                requested_from=from_date,
                requested_to=to_date,
            )

        return self._cache.get_or_load(key, self._settings.energy_cache_ttl_seconds, load)

    # ------------------------------------------------------------------
    # Hierarchy / transformers / stats
    # ------------------------------------------------------------------
    def get_hierarchy(self) -> Fetched[HierarchyResponse]:
        result = self.get_dataset()
        dataset = result.value
        response = HierarchyResponse(
            generated_at=datetime.now(timezone.utc),
            data_fetched_at=dataset.fetched_at,
            total_meters=len(dataset.meters),
            node_counts=dataset.hierarchy.node_counts,
            conflicts=dataset.hierarchy.conflicts,
            zones=dataset.hierarchy.zones,
        )
        return Fetched(value=response, cache=result.cache, fetched_at=result.fetched_at)

    def list_transformers(
        self,
        *,
        q: str | None = None,
        feeder: str | None = None,
        zone: str | None = None,
        page: int = 1,
        page_size: int | None = None,
    ) -> Fetched[Page[Transformer]]:
        result = self.get_dataset()
        page_size = page_size or self._settings.default_page_size
        feeders = _spec_values(feeder)
        zones = _spec_values(zone)
        needle = (q or "").strip().lower()

        items = [
            transformer
            for transformer in result.value.transformers
            if (not feeders or transformer.feeder_code.lower() in feeders)
            and _matches_node(transformer.zone_code, transformer.zone_name, zones)
            and (
                not needle
                or needle in transformer.code.lower()
                or needle in transformer.name.lower()
                or needle in transformer.feeder_code.lower()
            )
        ]
        total = len(items)
        start = (page - 1) * page_size
        page_items = items[start : start + page_size]
        return Fetched(
            value=Page(
                items=page_items,
                page=page,
                page_size=page_size,
                total=total,
                pages=(total + page_size - 1) // page_size,
            ),
            cache=result.cache,
            fetched_at=result.fetched_at,
        )

    def get_stats(self) -> Fetched[StatsResponse]:
        result = self.get_dataset()
        return Fetched(value=result.value.stats, cache=result.cache, fetched_at=result.fetched_at)

    def export_meters(self) -> Fetched[Dataset]:
        return self.get_dataset()

    # ------------------------------------------------------------------
    # Session / health
    # ------------------------------------------------------------------
    def session_status(self) -> SessionStatus:
        return self._client.session_status()

    def auth_status(self, *, force_login: bool = False) -> AuthStatus:
        checked_at = datetime.now(timezone.utc)
        if force_login:
            try:
                status = self._client.login()
            except PortalError as exc:
                return AuthStatus(
                    authenticated=False,
                    expires_at=None,
                    portal_base_url=self._settings.portal_base_url,
                    checked_at=checked_at,
                    detail=str(exc),
                )
        else:
            status = self._client.session_status()
        return AuthStatus(
            authenticated=status.authenticated,
            expires_at=status.expires_at,
            portal_base_url=self._settings.portal_base_url,
            checked_at=checked_at,
        )

    def readiness(self) -> tuple[bool, Fetched[Dataset] | None, str | None]:
        cached = self.peek_dataset()
        if cached is not None and cached.cache == "hit":
            return True, cached, None
        try:
            loaded = self.get_dataset()
            return True, loaded, None
        except PortalError as exc:
            return False, cached, str(exc)
