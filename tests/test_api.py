"""End-to-end API tests through the real service, with a fake portal client."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.client import PortalNotFound
from app.main import _csv_safe, create_app
from app.service import MeterService


def _flatten(nodes):
    for node in nodes:
        yield node
        yield from _flatten(node["children"])


def test_index_reports_docs_and_openapi(client):
    response = client.get("/api/v1/")

    assert response.status_code == 200
    assert response.json()["docs"] == "/docs"


def test_list_meters_returns_the_whole_normalised_page(client):
    response = client.get("/api/v1/meters")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert body["page"] == 1
    assert body["page_size"] == 25
    assert [item["meter_id"] for item in body["items"]] == ["J100001", "J100002", "J100003"]
    assert response.headers["X-Cache"] == "miss"
    assert response.headers["Cache-Control"].startswith("private")

    again = client.get("/api/v1/meters")
    assert again.headers["X-Cache"] == "hit"


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("status=Faulty", ["J100002"]),
        ("phase=three", ["J100002"]),
        ("make=L%26T", ["J100001", "J100003"]),
        ("build=v2", ["J100002"]),
        ("dt=DT-002", ["J100001", "J100003"]),
        ("feeder=F-007", ["J100002"]),
        ("zone=Z-03", ["J100002"]),
        ("q=GENUS", ["J100002"]),
        ("q=mansarovar", ["J100001", "J100003"]),
        ("status=installed,faulty", ["J100001", "J100002", "J100003"]),
    ],
)
def test_list_meters_filters(client, query, expected):
    response = client.get(f"/api/v1/meters?{query}&page_size=50")

    assert response.status_code == 200
    assert [item["meter_id"] for item in response.json()["items"]] == expected


def test_list_meters_paginates_and_sorts(client):
    first = client.get("/api/v1/meters?page_size=1").json()
    second = client.get("/api/v1/meters?page_size=1&page=2").json()
    descending = client.get("/api/v1/meters?sort=-meter_id").json()

    assert first["pages"] == 3
    assert first["items"][0]["meter_id"] == "J100001"
    assert second["items"][0]["meter_id"] == "J100002"
    assert descending["items"][0]["meter_id"] == "J100003"


def test_invalid_query_parameters_use_the_error_envelope(client):
    response = client.get("/api/v1/meters?page=0")

    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "validation_error"
    assert body["details"]


def test_legacy_detail_page_is_normalised(client):
    response = client.get("/api/v1/meters/J100001")

    assert response.status_code == 200
    body = response.json()
    assert body["detail_source"] == "portal_ssr"
    assert body["serial_number"] == "GE84132"
    assert body["make"] == "L&T"
    assert body["install_status"] == "Installed"
    assert body["hierarchy"]["dt"]["code"] == "DT-002"
    assert body["hierarchy_path"].startswith("Jaipur Zone 2")
    assert body["warnings"] == []


def test_v2_classdata_detail_is_normalised_and_mismatches_are_flagged(client):
    body = client.get("/api/v1/meters/J100002").json()

    assert body["detail_source"] == "portal_ssr"
    assert body["make"] == "Genus"
    assert body["install_status"] == "Faulty"
    assert body["install_type"] == "Direct"
    assert any("install_type differs" in warning for warning in body["warnings"])


def test_missing_meter_returns_404_envelope(client):
    response = client.get("/api/v1/meters/J999999")

    assert response.status_code == 404
    assert response.json()["error"] == "meter_not_found"
    assert "J999999" in response.json()["message"]


def test_detail_falls_back_to_the_bulk_snapshot(settings, portal_client_factory):
    failing = portal_client_factory(detail_failure=PortalNotFound("detail page is gone"))
    app = create_app(settings=settings, service=MeterService(failing, settings))

    with TestClient(app) as page_client:
        response = page_client.get("/api/v1/meters/J100001")

    assert response.status_code == 200
    body = response.json()
    assert body["detail_source"] == "bulk_export_cached"
    assert any("404" in warning for warning in body["warnings"])
    assert body["serial_number"] == "GE84132"


def test_consumption_converts_registers_to_usage(client):
    response = client.get("/api/v1/meters/J100001/consumption")

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 3
    assert body["granularity"] == "30min"
    assert body["interval_minutes"] == 30
    assert [reading["kwh_delta"] for reading in body["readings"]] == [None, 4.0, 6.0]
    assert body["summary"]["kwh_consumed"] == pytest.approx(10.0)
    assert body["summary"]["kvah_consumed"] == pytest.approx(12.0)
    assert body["first_reading_at"].endswith("+05:30")
    assert body["anomalies"] == {
        "register_regressions": 0,
        "duplicate_timestamps": 0,
        "unparsable_readings": 0,
    }


def test_consumption_validates_ranges(client):
    reversed_range = client.get("/api/v1/meters/J100001/consumption?from=2026-06-02&to=2026-06-01")
    too_wide = client.get("/api/v1/meters/J100001/consumption?from=2026-01-01&to=2026-06-30")

    assert reversed_range.status_code == 422
    assert "on or before" in reversed_range.json()["message"]
    assert too_wide.status_code == 422
    assert "92 days" in too_wide.json()["message"]


def test_consumption_for_unknown_meter_is_404(client):
    response = client.get("/api/v1/meters/J999999/consumption")

    assert response.status_code == 404
    assert response.json()["error"] == "meter_not_found"


def test_hierarchy_uses_registry_names_and_keeps_aliases(client):
    response = client.get("/api/v1/hierarchy")

    assert response.status_code == 200
    body = response.json()
    assert body["total_meters"] == 3
    assert body["node_counts"]["zone"] == 2
    assert body["node_counts"]["dt"] == 2

    nodes = list(_flatten(body["zones"]))
    dt = next(node for node in nodes if node["code"] == "DT-007")
    assert dt["name"] == "Sanganer DT 7"
    assert "name_alias" in dt["data_quality"]
    assert dt["capacity_kva"] == 400
    assert dt["meter_count"] == 1


def test_transformers_join_registry_to_meter_counts(client):
    response = client.get("/api/v1/transformers")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2

    dt = next(item for item in body["items"] if item["code"] == "DT-007")
    assert dt["name"] == "Sanganer DT 7"
    assert dt["meter_count"] == 1
    assert dt["faulty_count"] == 1
    assert dt["zone_code"] == "Z-03"

    filtered = client.get("/api/v1/transformers?feeder=F-002").json()
    assert [item["code"] for item in filtered["items"]] == ["DT-002"]


def test_stats_summary(client):
    body = client.get("/api/v1/stats/summary").json()

    assert body["total_meters"] == 3
    assert body["by_status"] == {"Installed": 2, "Faulty": 1}
    assert body["by_phase"] == {"single": 2, "three": 1}
    assert body["by_build"] == {"legacy": 2, "v2": 1}
    assert body["transformers"] == 2
    assert body["meters_with_location"] == 3


def test_nearby_search_orders_by_distance(client):
    body = client.get("/api/v1/meters/near?latitude=26.85&longitude=75.8&radius_km=1").json()

    assert [item["meter_id"] for item in body] == ["J100001", "J100003"]
    assert body[0]["distance_km"] == pytest.approx(0.0)

    limited = client.get("/api/v1/meters/near?latitude=26.85&longitude=75.8&radius_km=1&limit=1").json()
    assert len(limited) == 1


def test_json_and_csv_exports_include_every_meter(client):
    payload = client.get("/api/v1/export/meters").json()
    csv_response = client.get("/api/v1/export/meters.csv")

    assert payload["total"] == 3
    assert len(payload["data"]) == 3

    assert csv_response.status_code == 200
    assert csv_response.headers["content-type"].startswith("text/csv")
    assert 'attachment; filename="urja-meters.csv"' == csv_response.headers["content-disposition"]
    lines = csv_response.text.strip().splitlines()
    assert len(lines) == 4
    assert lines[0].startswith("meter_id,serial_number")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("J100001", "J100001"),
        ("=cmd|' /C calc'!A0", "'=cmd|' /C calc'!A0"),
        ("+1-800", "'+1-800"),
        ("@SUM(A1)", "'@SUM(A1)"),
        ("-5", "'-5"),
    ],
)
def test_csv_formula_injection_is_neutralised(value, expected):
    assert _csv_safe(value) == expected


def test_health_and_readiness(client):
    health = client.get("/health")
    ready = client.get("/health/ready")

    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["credentials_configured"] is True
    assert health.json()["session"]["authenticated"] is True

    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
    assert ready.json()["meter_count"] == 3


def test_auth_status_and_login(client):
    status = client.get("/api/v1/auth/status")
    login = client.post("/api/v1/auth/login")

    assert status.status_code == 200
    assert status.json()["authenticated"] is True
    assert login.status_code == 200
    assert login.json()["authenticated"] is True


def test_optional_api_key_gates_only_the_api_router(settings, portal_client_factory):
    keyed = settings.model_copy(update={"api_key": "s3cret"})
    app = create_app(settings=keyed, service=MeterService(portal_client_factory(), keyed))

    with TestClient(app) as page_client:
        missing = page_client.get("/api/v1/meters")
        wrong = page_client.get("/api/v1/meters", headers={"X-API-Key": "nope"})
        accepted = page_client.get("/api/v1/meters", headers={"X-API-Key": "s3cret"})
        health = page_client.get("/health")

    assert missing.status_code == 401
    assert missing.json()["error"] == "unauthorized"
    assert wrong.status_code == 401
    assert accepted.status_code == 200
    assert health.status_code == 200


def test_docs_and_openapi_are_available(client):
    openapi = client.get("/openapi.json")
    docs = client.get("/docs")
    reference = client.get("/reference")

    assert openapi.status_code == 200
    schema = openapi.json()
    assert schema["openapi"].startswith("3.")
    assert "/api/v1/meters" in schema["paths"]
    assert "/api/v1/meters/{meter_id}/consumption" in schema["paths"]
    assert docs.status_code == 200
    assert reference.status_code == 200


def test_dataset_cache_avoids_repeated_portal_calls(client, service):
    client.get("/api/v1/meters")
    client.get("/api/v1/hierarchy")
    client.get("/api/v1/transformers")
    client.get("/api/v1/stats/summary")

    dataset = service.peek_dataset()
    assert dataset is not None
    assert dataset.value.fetched_at.tzinfo == timezone.utc
    assert isinstance(dataset.value.fetched_at, datetime)
    assert client.get("/api/v1/meters").headers["X-Cache"] == "hit"
