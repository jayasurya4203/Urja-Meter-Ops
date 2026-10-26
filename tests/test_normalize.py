"""Pure normalisation tests: the portal's mess is pinned down here."""

from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo

import pytest

from app.normalize import (
    hierarchy_path,
    normalize_detail,
    normalize_energy,
    normalize_hierarchy,
    normalize_meter,
    normalize_node,
    normalize_ssr_hierarchy,
    normalize_transformer,
    parse_float,
)

IST = ZoneInfo("Asia/Kolkata")


def test_node_derives_code_from_an_unambiguous_name():
    node = normalize_node("circle", name="Circle 7")

    assert node.code == "C-07"
    assert node.name == "Circle 7"
    assert "code_derived" in node.data_quality


def test_node_derives_name_from_an_unambiguous_code():
    node = normalize_node("feeder", code="F-12")

    assert node.code == "F-12"
    assert node.name == "Feeder 12"
    assert "name_derived" in node.data_quality


def test_node_splits_a_parenthesised_code_and_flags_the_mismatch():
    node = normalize_node("zone", name="Jaipur Zone 2 (Z-09)")

    assert node.code == "Z-09"
    assert node.name == "Jaipur Zone 2"
    assert "code_parsed_from_label" in node.data_quality
    assert "name_code_mismatch" in node.data_quality


def test_node_keeps_an_ambiguous_name_and_flags_incomplete_source():
    node = normalize_node("dt", name="Old Xfmr")

    assert node.name == "Old Xfmr"
    assert node.code is None
    assert "incomplete_source" in node.data_quality


def test_node_never_returns_an_empty_label():
    node = normalize_node("dt")

    assert node.name == "Unknown DT"
    assert node.code is None
    assert "missing_in_source" in node.data_quality


def test_hierarchy_normalises_every_level_even_when_input_is_empty():
    hierarchy = normalize_hierarchy(None)

    assert hierarchy.zone.name == "Unknown Zone"
    assert hierarchy.dt.name == "Unknown DT"
    assert hierarchy_path(hierarchy) == "Unknown Zone / Unknown Circle / Unknown Division / Unknown Subdivision / Unknown Substation / Unknown Feeder / Unknown DT"


def test_ssr_hierarchy_uses_display_labels():
    hierarchy = normalize_ssr_hierarchy(
        {
            "Zone": "Jaipur Zone 2",
            "Circle": "Circle 2",
            "Division": "Division 2",
            "Subdivision": "Subdivision 2",
            "Sub Station": "Substation 2",
            "Feeder": "Feeder 2",
            "DT": "Mansarovar DT 2",
        }
    )

    assert hierarchy.substation.name == "Substation 2"
    assert hierarchy.dt.name == "Mansarovar DT 2"


def test_meter_parses_numbers_and_coordinates(raw_meters):
    meter = normalize_meter(raw_meters[0])

    assert meter.meter_id == "J100001"
    assert meter.dt_code == "DT-002"
    assert meter.location is not None
    assert meter.location.latitude == pytest.approx(26.85)
    assert meter.phase_type == "single"
    assert meter.hierarchy_path.startswith("Jaipur Zone 2 / Circle 2")


def test_meter_without_location_gets_none(raw_meters):
    raw = dict(raw_meters[0])
    raw["geo"] = {"lat": "not-a-number", "lng": "75.8"}

    assert normalize_meter(raw).location is None


def test_meter_without_id_is_rejected(raw_meters):
    raw = dict(raw_meters[0])
    raw.pop("meterId")

    with pytest.raises(ValueError, match="missing meterId"):
        normalize_meter(raw)


def test_legacy_detail_schema():
    detail = {
        "data": [
            {"parameterName": "Meter ID", "parameterValue": "J100001"},
            {"parameterName": "Serial No", "parameterValue": "GE84132"},
            {"parameterName": "Make", "parameterValue": "L&T"},
            {"parameterName": "Ignored", "parameterValue": "x"},
        ]
    }

    fields = normalize_detail(detail)

    assert fields == {"meter_id": "J100001", "serial_number": "GE84132", "make": "L&T"}


def test_v2_classdata_schema_is_unwrapped():
    detail = {
        "classData": '{"installed_meter": {"MeterId": "J100002", "SerialNo": "GEN2", "Make": "Genus",'
        ' "PhaseType": "three", "InstallationStatus": "Faulty", "InstallationType": "Direct"}}'
    }

    fields = normalize_detail(detail)

    assert fields["meter_id"] == "J100002"
    assert fields["install_status"] == "Faulty"
    assert fields["phase_type"] == "three"


def test_classdata_takes_precedence_over_legacy_rows():
    detail = {
        "classData": '{"installed_meter": {"MeterId": "J100002", "Make": "Genus"}}',
        "data": [
            {"parameterName": "Meter ID", "parameterValue": "CONFLICT"},
            {"parameterName": "Serial No", "parameterValue": "GEN2"},
        ],
    }

    fields = normalize_detail(detail)

    assert fields["meter_id"] == "J100002"
    assert fields["serial_number"] == "GEN2"


@pytest.mark.parametrize(
    "detail",
    [
        {"classData": "not json"},
        {"classData": '{"installed_meter": "nope"}'},
        {"data": []},
        {},
    ],
)
def test_unusable_detail_payloads_raise_value_error(detail):
    with pytest.raises(ValueError):
        normalize_detail(detail)


def test_energy_turns_cumulative_registers_into_interval_deltas():
    rows = [
        {"timestamp": "01/06/2026 00:00", "kwh": "100.0", "kvah": "110.0", "voltR": "230"},
        {"timestamp": "01/06/2026 00:30", "kwh": "104.0", "kvah": "115.0", "voltR": "231"},
        {"timestamp": "01/06/2026 01:00", "kwh": "110.0", "kvah": "122.0", "voltR": "229"},
    ]

    result = normalize_energy(rows, meter_id="J100001", timezone=IST)

    assert result.count == 3
    assert result.granularity == "30min"
    assert result.interval_minutes == 30
    assert [reading.kwh_delta for reading in result.readings] == [None, 4.0, 6.0]
    assert result.summary.kwh_consumed == pytest.approx(10.0)
    assert result.summary.kvah_consumed == pytest.approx(12.0)
    assert result.summary.first_register_kwh == pytest.approx(100.0)
    assert result.summary.last_register_kwh == pytest.approx(110.0)
    assert result.summary.average_voltage == pytest.approx(230.0)
    assert result.readings[0].timestamp.utcoffset().total_seconds() == 5.5 * 3600


def test_energy_detects_daily_meters():
    rows = [
        {"timestamp": "01/06/2026 00:00", "kwh": "10", "kvah": "11"},
        {"timestamp": "02/06/2026 00:00", "kwh": "20", "kvah": "22"},
        {"timestamp": "03/06/2026 00:00", "kwh": "31", "kvah": "34"},
    ]

    result = normalize_energy(rows, meter_id="J100165", timezone=IST)

    assert result.granularity == "daily"
    assert result.interval_minutes == 1440
    assert result.summary.kwh_consumed == pytest.approx(21.0)


def test_energy_keeps_the_later_duplicate_and_reports_it():
    rows = [
        {"timestamp": "01/06/2026 00:00", "kwh": "100", "kvah": "110"},
        {"timestamp": "01/06/2026 00:00", "kwh": "102", "kvah": "112"},
        {"timestamp": "01/06/2026 00:30", "kwh": "106", "kvah": "118"},
    ]

    result = normalize_energy(rows, meter_id="J100001", timezone=IST)

    assert result.count == 2
    assert result.anomalies.duplicate_timestamps == 1
    assert result.readings[0].kwh == 102.0
    assert result.readings[1].kwh_delta == 4.0


def test_energy_flags_register_regressions_and_never_invents_usage():
    rows = [
        {"timestamp": "01/06/2026 00:00", "kwh": "100", "kvah": "110"},
        {"timestamp": "01/06/2026 00:30", "kwh": "90", "kvah": "100"},
        {"timestamp": "01/06/2026 01:00", "kwh": "95", "kvah": "106"},
    ]

    result = normalize_energy(rows, meter_id="J100001", timezone=IST)

    assert result.anomalies.register_regressions == 1
    assert result.readings[1].kwh_delta is None
    assert result.readings[2].kwh_delta == 5.0
    assert result.summary.kwh_consumed == pytest.approx(5.0)


def test_energy_counts_unparsable_rows_and_sorts_by_time():
    rows = [
        {"timestamp": "01/06/2026 01:00", "kwh": "110", "kvah": "122"},
        {"timestamp": "not a time", "kwh": "1", "kvah": "2"},
        {"timestamp": "01/06/2026 00:00", "kwh": "100", "kvah": "110"},
        {"timestamp": "01/06/2026 00:30", "kwh": "n/a", "kvah": "115"},
    ]

    result = normalize_energy(rows, meter_id="J100001", timezone=IST, requested_from=date(2026, 6, 1))

    assert result.count == 2
    assert result.anomalies.unparsable_readings == 2
    assert result.requested_from == date(2026, 6, 1)
    assert result.readings[0].timestamp.hour == 0


def test_energy_empty_window():
    result = normalize_energy([], meter_id="J100001", timezone=IST)

    assert result.granularity == "empty"
    assert result.interval_minutes is None
    assert result.first_reading_at is None
    assert result.summary.kwh_consumed == 0.0


def test_transformer_capacity_is_numeric():
    transformer = normalize_transformer(
        {"code": "DT-002", "name": "Mansarovar DT 2", "feederCode": "F-002", "capacityKva": "630"},
        meter_count=2,
        installed=2,
    )

    assert transformer.capacity_kva == 630
    assert transformer.meter_count == 2
    assert transformer.installed_count == 2


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1,234.5", 1234.5),
        (12, 12.0),
        ("-", None),
        ("", None),
        (None, None),
        (True, None),
        ("not a number", None),
    ],
)
def test_parse_float(raw, expected):
    assert parse_float(raw) == expected
