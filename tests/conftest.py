"""Shared fixtures: realistic raw portal payloads and an in-memory portal client."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.client import PortalNotFound, SessionStatus
from app.config import Settings
from app.main import create_app
from app.service import MeterService


def _hierarchy(
    zone: tuple[str, str],
    circle: tuple[str, str],
    division: tuple[str, str],
    subdivision: tuple[str, str],
    substation: tuple[str, str],
    feeder: tuple[str, str],
    dt: tuple[str, str],
) -> dict[str, dict[str, str]]:
    pairs = {
        "zone": zone,
        "circle": circle,
        "division": division,
        "subdivision": subdivision,
        "substation": substation,
        "feeder": feeder,
        "dt": dt,
    }
    return {level: {"name": name, "code": code} for level, (code, name) in pairs.items()}


def _meter(
    meter_id: str,
    serial: str,
    make: str,
    status: str,
    phase: str,
    build: str,
    hierarchy: dict[str, dict[str, str]],
    *,
    location: tuple[float, float] | None,
    install_type: str = "CT Operated",
) -> dict[str, Any]:
    return {
        "meterId": meter_id,
        "serialNo": serial,
        "make": make,
        "phaseType": phase,
        "installStatus": status,
        "installType": install_type,
        "build": build,
        "dtCode": hierarchy["dt"]["code"],
        "geo": {"lat": location[0], "lng": location[1]} if location else None,
        "hierarchy": hierarchy,
    }


JAIPUR_2 = _hierarchy(
    ("Z-02", "Jaipur Zone 2"),
    ("C-02", "Circle 2"),
    ("D-02", "Division 2"),
    ("SD-02", "Subdivision 2"),
    ("SS-02", "Substation 2"),
    ("F-002", "Feeder 2"),
    ("DT-002", "Mansarovar DT 2"),
)

JAIPUR_3 = _hierarchy(
    ("Z-03", "Jaipur Zone 3"),
    ("C-03", "Circle 3"),
    ("D-03", "Division 3"),
    ("SD-03", "Subdivision 3"),
    ("SS-03", "Substation 3"),
    ("F-007", "Feeder 7"),
    ("DT-007", "Old Malviya Nagar Xfmr"),
)


@pytest.fixture
def raw_meters() -> list[dict[str, Any]]:
    return [
        _meter("J100001", "GE84132", "L&T", "Installed", "single", "legacy", JAIPUR_2, location=(26.8500, 75.8000)),
        _meter("J100002", "GEN0002", "Genus", "Faulty", "three", "v2", JAIPUR_3, location=(26.9100, 75.7900)),
        _meter("J100003", "GE84133", "L&T", "Installed", "single", "legacy", JAIPUR_2, location=(26.8510, 75.8010)),
    ]


@pytest.fixture
def raw_transformers() -> list[dict[str, Any]]:
    return [
        {"code": "DT-002", "name": "Mansarovar DT 2", "feederCode": "F-002", "capacityKva": 630},
        {"code": "DT-007", "name": "Sanganer DT 7", "feederCode": "F-007", "capacityKva": 400},
    ]


def legacy_detail(meter_id: str = "J100001") -> dict[str, Any]:
    rows = {
        "Meter ID": meter_id,
        "Serial No": "GE84132",
        "Make": "L&T",
        "Phase Type": "single",
        "Installation Status": "Installed",
        "Installation Type": "CT Operated",
    }
    return {"data": [{"parameterName": key, "parameterValue": value} for key, value in rows.items()]}


def v2_detail(meter_id: str = "J100002") -> dict[str, Any]:
    installed = {
        "MeterId": meter_id,
        "SerialNo": "GEN0002",
        "Make": "Genus",
        "PhaseType": "three",
        "InstallationStatus": "Faulty",
        "InstallationType": "Direct",
    }
    return {"classData": json.dumps({"installed_meter": installed})}


SSR_HIERARCHY = {
    "Zone": "Jaipur Zone 2",
    "Circle": "Circle 2",
    "Division": "Division 2",
    "Subdivision": "Subdivision 2",
    "Sub Station": "Substation 2",
    "Feeder": "Feeder 2",
    "DT": "Mansarovar DT 2",
}

ENERGY_ROWS = [
    {"timestamp": "01/06/2026 00:00", "kwh": "100.0", "kvah": "110.0", "voltR": "230"},
    {"timestamp": "01/06/2026 00:30", "kwh": "104.0", "kvah": "115.0", "voltR": "231"},
    {"timestamp": "01/06/2026 01:00", "kwh": "110.0", "kvah": "122.0", "voltR": "229"},
]


class FakePortalClient:
    """Stands in for UrjaPortalClient; returns the same raw shapes."""

    def __init__(self, meters, transformers, *, detail_failure: Exception | None = None):
        self.meters = meters
        self.transformers = transformers
        self.detail_failure = detail_failure
        self.detail_calls = 0
        self.energy_calls = 0

    def get_export(self) -> dict[str, Any]:
        return {"data": self.meters, "total": len(self.meters)}

    def get_all_transformers(self) -> list[dict[str, Any]]:
        return self.transformers

    def get_meter_detail(self, meter_id: str) -> dict[str, Any]:
        self.detail_calls += 1
        if self.detail_failure is not None:
            raise self.detail_failure
        for meter in self.meters:
            if meter["meterId"] != meter_id:
                continue
            if meter["build"] == "v2":
                return {"meter_id": meter_id, "detail": v2_detail(meter_id), "hierarchy": SSR_HIERARCHY}
            return {"meter_id": meter_id, "detail": legacy_detail(meter_id), "hierarchy": SSR_HIERARCHY}
        raise PortalNotFound(f"Meter {meter_id} was not found")

    def get_meter_energy(self, meter_id: str, from_date=None, to_date=None) -> list[dict[str, Any]]:
        self.energy_calls += 1
        return list(ENERGY_ROWS)

    def session_status(self) -> SessionStatus:
        return SessionStatus(authenticated=True, expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc))

    def login(self) -> SessionStatus:
        return self.session_status()

    def close(self) -> None:
        return None


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        portal_base_url="https://portal.test",
        portal_email="operator@example.test",
        portal_password="secret",
        dataset_cache_ttl_seconds=300,
        detail_cache_ttl_seconds=120,
        energy_cache_ttl_seconds=120,
        log_level="WARNING",
    )


@pytest.fixture
def fake_client(raw_meters, raw_transformers) -> FakePortalClient:
    return FakePortalClient(raw_meters, raw_transformers)


@pytest.fixture
def portal_client_factory(raw_meters, raw_transformers):
    def make(**kwargs) -> FakePortalClient:
        return FakePortalClient(raw_meters, raw_transformers, **kwargs)

    return make


@pytest.fixture
def service(fake_client, settings) -> MeterService:
    return MeterService(fake_client, settings)


@pytest.fixture
def client(service, settings) -> TestClient:
    app = create_app(settings=settings, service=service)
    with TestClient(app) as test_client:
        yield test_client
