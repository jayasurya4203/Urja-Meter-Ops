"""Pydantic models: the public, normalised contract of this API."""

from __future__ import annotations

from datetime import date, datetime
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, Field


class HierarchyNode(BaseModel):
    """One level of a meter's network path."""

    level: str = Field(description="zone | circle | division | subdivision | substation | feeder | dt")
    name: str = Field(description="Human-readable name. Normalised when the portal leaves it blank.")
    code: str | None = Field(default=None, description="Stable-looking code such as C-02, when known.")
    data_quality: list[str] = Field(
        default_factory=list,
        description=(
            "Set when this node required repair or has conflicting source values, e.g. "
            "'code_derived', 'name_derived', 'name_alias', 'missing_in_source'."
        ),
    )


class MeterHierarchy(BaseModel):
    zone: HierarchyNode
    circle: HierarchyNode
    division: HierarchyNode
    subdivision: HierarchyNode
    substation: HierarchyNode
    feeder: HierarchyNode
    dt: HierarchyNode


class GeoPoint(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class Meter(BaseModel):
    meter_id: str = Field(examples=["J100001"])
    serial_number: str = Field(examples=["GE84132"])
    make: str = Field(examples=["L&T"])
    phase_type: str = Field(description="single or three", examples=["single"])
    install_status: str = Field(description="Installed, Faulty, Decommissioned (open set)", examples=["Installed"])
    install_type: str = Field(examples=["CT Operated"])
    build: str = Field(description="Portal data generation, legacy or v2", examples=["legacy"])
    dt_code: str = Field(examples=["DT-002"])
    hierarchy: MeterHierarchy
    hierarchy_path: str = Field(
        description="Readable breadcrumb with duplicate names collapsed.",
        examples=["Jaipur Zone 2 / Circle 2 / Division 2 / Subdivision 2 / Substation 2 / Feeder 2 / Mansarovar DT 2"],
    )
    location: GeoPoint | None = Field(default=None)


class MeterDetail(Meter):
    detail_source: Literal["portal_ssr", "bulk_export_cached"] = Field(
        description="Where the nameplate fields came from. The single-meter endpoint prefers the live SSR page."
    )
    observed_at: datetime = Field(description="When the source payload was fetched.")
    warnings: list[str] = Field(default_factory=list)


class NearbyMeter(Meter):
    distance_km: float = Field(description="Great-circle distance from the query point.")


class Transformer(BaseModel):
    code: str = Field(examples=["DT-001"])
    name: str = Field(examples=["Malviya Nagar DT 1"])
    feeder_code: str = Field(examples=["F-001"])
    capacity_kva: int | None = Field(description="Nameplate capacity in kVA")
    meter_count: int = Field(description="Meters attached to this DT in the current dataset")
    installed_count: int
    faulty_count: int
    decommissioned_count: int
    zone_code: str | None = None
    zone_name: str | None = None


class ConsumptionReading(BaseModel):
    timestamp: datetime = Field(description="ISO-8601 timestamp with the portal's assumed timezone offset.")
    kwh: float = Field(description="Cumulative active-energy register (not interval usage).")
    kvah: float = Field(description="Cumulative apparent-energy register.")
    volt_r: float | None = Field(description="R-phase voltage, when reported.")
    kwh_delta: float | None = Field(
        description="Usage since the previous reading; null for the first reading or after a register reset."
    )
    kvah_delta: float | None = None


class ConsumptionSummary(BaseModel):
    kwh_consumed: float = Field(description="Sum of positive kWh deltas in the window.")
    kvah_consumed: float
    first_register_kwh: float | None
    last_register_kwh: float | None
    average_voltage: float | None
    min_voltage: float | None
    max_voltage: float | None


class ConsumptionAnomalies(BaseModel):
    register_regressions: int = Field(description="Negative deltas (meter reset, replacement, or bad data)")
    duplicate_timestamps: int
    unparsable_readings: int


class ConsumptionResponse(BaseModel):
    meter_id: str
    requested_from: date | None = Field(description="Inclusive date filter sent to the portal, if any.")
    requested_to: date | None
    first_reading_at: datetime | None
    last_reading_at: datetime | None
    granularity: Literal["30min", "daily", "irregular", "empty"] = Field(
        description="Detected interval spacing. The portal mixes 30-minute and daily meters."
    )
    interval_minutes: int | None = Field(description="Median spacing between readings.")
    count: int
    readings: list[ConsumptionReading]
    summary: ConsumptionSummary
    anomalies: ConsumptionAnomalies


class HierarchyTreeNode(BaseModel):
    id: str = Field(description="Stable node id, such as zone:Z-02 or feeder:F-002.")
    level: str
    name: str
    code: str | None
    meter_count: int = Field(description="Meters whose path passes through this node.")
    capacity_kva: int | None = Field(default=None, description="Only present on DT nodes.")
    data_quality: list[str] = Field(default_factory=list)
    children: list["HierarchyTreeNode"] = Field(default_factory=list)


class HierarchyConflict(BaseModel):
    level: str
    id: str
    name: str
    meter_count: int
    reported_parents: list[str]
    chosen_parent: str = Field(description="Most frequently reported parent; deterministic tie-breaks by id.")


class HierarchyResponse(BaseModel):
    generated_at: datetime
    data_fetched_at: datetime
    total_meters: int
    node_counts: dict[str, int]
    conflicts: list[HierarchyConflict]
    zones: list[HierarchyTreeNode]


class StatsResponse(BaseModel):
    total_meters: int
    by_status: dict[str, int]
    by_make: dict[str, int]
    by_phase: dict[str, int]
    by_build: dict[str, int]
    by_install_type: dict[str, int]
    transformers: int
    zones: int
    feeders: int
    substations: int
    meters_with_location: int
    data_fetched_at: datetime


T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    page: int
    page_size: int
    total: int
    pages: int


class ExportResponse(BaseModel):
    data: list[Meter]
    total: int
    fetched_at: datetime


class AuthStatus(BaseModel):
    authenticated: bool
    expires_at: datetime | None
    portal_base_url: str
    checked_at: datetime
    detail: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    service: str
    version: str
    uptime_seconds: float
    credentials_configured: bool
    session: AuthStatus
    cache: dict[str, int]


class ReadyResponse(BaseModel):
    status: Literal["ready", "unavailable"]
    dataset_cached: bool
    meter_count: int | None
    data_fetched_at: datetime | None
    detail: str | None = None


class ErrorResponse(BaseModel):
    error: str
    message: str
