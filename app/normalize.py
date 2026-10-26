"""Normalisation of portal payloads into the public contract.

Everything here is pure: input is a portal-shaped dict/HTML payload, output is a
Pydantic model. This makes the messy edge cases (blank hierarchy fields, two
detail schemas, cumulative registers, mixed granularity) easy to unit-test.
"""

from __future__ import annotations

import json
import re
import statistics
from datetime import date, datetime
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from app.models import (
    ConsumptionAnomalies,
    ConsumptionReading,
    ConsumptionResponse,
    ConsumptionSummary,
    GeoPoint,
    HierarchyNode,
    Meter,
    MeterHierarchy,
    Transformer,
)

LEVELS = ("zone", "circle", "division", "subdivision", "substation", "feeder", "dt")

LEVEL_LABELS = {
    "zone": "Zone",
    "circle": "Circle",
    "division": "Division",
    "subdivision": "Subdivision",
    "substation": "Substation",
    "feeder": "Feeder",
    "dt": "DT",
}

CODE_PREFIXES = {
    "zone": "Z",
    "circle": "C",
    "division": "D",
    "subdivision": "SD",
    "substation": "SS",
    "feeder": "F",
    "dt": "DT",
}

_CODE_WIDTHS = {
    "zone": 2,
    "circle": 2,
    "division": 2,
    "subdivision": 2,
    "substation": 2,
    "feeder": 3,
    "dt": 3,
}

_LEVEL_WORDS = {
    "zone": "zone",
    "circle": "circle",
    "division": "division",
    "subdivision": "subdivision",
    "substation": "substation",
    "sub-station": "substation",
    "feeder": "feeder",
    "dt": "dt",
}

_NAME_NUMBER_RE = re.compile(
    r"(?:^|\s)(zone|circle|division|subdivision|sub-station|substation|feeder|dt)"
    r"\s*[-#]?\s*(\d{1,4})\s*$",
    re.IGNORECASE,
)
_CODE_RE = re.compile(r"^([A-Za-z]{1,3})-(\d{1,4})$")
_PARENTHETICAL_CODE_RE = re.compile(r"^(?P<name>.*?)\s*\((?P<code>[^()]+)\)\s*$")

_TIMESTAMP_FORMATS = ("%d/%m/%Y %H:%M", "%d/%m/%Y", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S")


def _text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def parse_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    raw = _text(value).replace(",", "")
    if raw in ("", "-", "—", "N/A", "NA", "null", "None"):
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def derive_code(level: str, name: str) -> str | None:
    """Derive a code only when the name itself is unambiguous (e.g. 'Feeder 12')."""
    match = _NAME_NUMBER_RE.search(name)
    if not match:
        return None
    matched_level = _LEVEL_WORDS.get(match.group(1).lower())
    if matched_level != level:
        return None
    number = int(match.group(2))
    return f"{CODE_PREFIXES[level]}-{number:0{_CODE_WIDTHS[level]}d}"


def display_name_from_code(level: str, code: str) -> str | None:
    match = _CODE_RE.match(code)
    if not match:
        return None
    return f"{LEVEL_LABELS[level]} {int(match.group(2))}"


def _code_suffix_number(code: str | None) -> int | None:
    if not code:
        return None
    match = _CODE_RE.match(code)
    return int(match.group(2)) if match else None


def _name_number(name: str) -> int | None:
    match = _NAME_NUMBER_RE.search(name)
    return int(match.group(2)) if match else None


def normalize_node(
    level: str,
    *,
    name: Any = None,
    code: Any = None,
    extra_quality: Iterable[str] = (),
) -> HierarchyNode:
    """Normalise one hierarchy node, repairing a missing half when possible."""
    raw_name = _text(name)
    raw_code = _text(code).upper() if _text(code) else ""
    quality = list(dict.fromkeys(extra_quality))

    if raw_name and "(" in raw_name:
        parsed = _PARENTHETICAL_CODE_RE.match(raw_name)
        if parsed:
            raw_name = _text(parsed.group("name"))
            if not raw_code:
                raw_code = _text(parsed.group("code")).upper()
                quality.append("code_parsed_from_label")

    derived_code = False
    derived_name = False
    if not raw_code and raw_name:
        candidate = derive_code(level, raw_name)
        if candidate:
            raw_code = candidate
            derived_code = True
            quality.append("code_derived")
    if not raw_name and raw_code:
        candidate = display_name_from_code(level, raw_code)
        if candidate:
            raw_name = candidate
            derived_name = True
            quality.append("name_derived")

    if not raw_name and not raw_code:
        raw_name = f"Unknown {LEVEL_LABELS[level]}"
        quality.append("missing_in_source")
    elif not raw_name or not raw_code:
        quality.append("incomplete_source")

    if not derived_code and not derived_name and raw_code and raw_name:
        code_number = _code_suffix_number(raw_code)
        name_number = _name_number(raw_name)
        if code_number is not None and name_number is not None and code_number != name_number:
            quality.append("name_code_mismatch")

    return HierarchyNode(
        level=level,
        name=raw_name,
        code=raw_code or None,
        data_quality=list(dict.fromkeys(quality)),
    )


def normalize_hierarchy(raw: dict[str, Any] | None) -> MeterHierarchy:
    raw = raw or {}
    nodes: dict[str, HierarchyNode] = {}
    for level in LEVELS:
        item = raw.get(level)
        if isinstance(item, dict):
            nodes[level] = normalize_node(level, name=item.get("name"), code=item.get("code"))
        else:
            nodes[level] = normalize_node(level)
    return MeterHierarchy(**nodes)


_SSR_LEVEL_KEYS = {
    "Zone": "zone",
    "Circle": "circle",
    "Division": "division",
    "Subdivision": "subdivision",
    "Sub Station": "substation",
    "Substation": "substation",
    "Feeder": "feeder",
    "DT": "dt",
}


def normalize_ssr_hierarchy(raw: dict[str, Any] | None) -> MeterHierarchy:
    """Normalise the display-label hierarchy embedded in a meter SSR page."""
    raw = raw or {}
    nodes: dict[str, HierarchyNode] = {}
    for label, level in _SSR_LEVEL_KEYS.items():
        if label in raw:
            nodes[level] = normalize_node(level, name=raw.get(label))
    for level in LEVELS:
        nodes.setdefault(level, normalize_node(level))
    return MeterHierarchy(**nodes)


def hierarchy_path(hierarchy: MeterHierarchy) -> str:
    names: list[str] = []
    for level in LEVELS:
        name = getattr(hierarchy, level).name
        if name and (not names or names[-1] != name):
            names.append(name)
    return " / ".join(names)


def _coerce_geo(raw: Any) -> GeoPoint | None:
    if not isinstance(raw, dict):
        return None
    latitude = parse_float(raw.get("lat", raw.get("latitude")))
    longitude = parse_float(raw.get("lng", raw.get("lon", raw.get("longitude"))))
    if latitude is None or longitude is None:
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    return GeoPoint(latitude=latitude, longitude=longitude)


def normalize_meter(raw: dict[str, Any]) -> Meter:
    hierarchy = normalize_hierarchy(raw.get("hierarchy"))
    meter_id = _text(raw.get("meterId")) or _text(raw.get("meter_id"))
    if not meter_id:
        raise ValueError("Meter payload is missing meterId")
    dt_code = _text(raw.get("dtCode")) or (hierarchy.dt.code or "")
    return Meter(
        meter_id=meter_id,
        serial_number=_text(raw.get("serialNo") or raw.get("serial_number")) or "UNKNOWN",
        make=_text(raw.get("make")) or "UNKNOWN",
        phase_type=_text(raw.get("phaseType") or raw.get("phase_type")).lower() or "unknown",
        install_status=_text(raw.get("installStatus") or raw.get("install_status")) or "Unknown",
        install_type=_text(raw.get("installType") or raw.get("install_type")) or "Unknown",
        build=_text(raw.get("build")) or "unknown",
        dt_code=dt_code,
        hierarchy=hierarchy,
        hierarchy_path=hierarchy_path(hierarchy),
        location=_coerce_geo(raw.get("geo")),
    )


_DETAIL_LABELS = {
    "Meter ID": "meter_id",
    "Serial No": "serial_number",
    "Make": "make",
    "Phase Type": "phase_type",
    "Installation Status": "install_status",
    "Installation Type": "install_type",
}

_CLASS_DATA_KEYS = {
    "MeterId": "meter_id",
    "SerialNo": "serial_number",
    "Make": "make",
    "PhaseType": "phase_type",
    "InstallationStatus": "install_status",
    "InstallationType": "install_type",
    "meter_id": "meter_id",
    "serial_number": "serial_number",
    "make": "make",
    "phase_type": "phase_type",
    "install_status": "install_status",
    "install_type": "install_type",
}


def normalize_detail(raw_detail: dict[str, Any]) -> dict[str, str]:
    """Flatten both detail schemas into canonical snake_case fields.

    Legacy: ``{"data": [{"parameterName": ..., "parameterValue": ...}]}``
    v2:     ``{"classData": "<json string with installed_meter>"}``
    """
    fields: dict[str, str] = {}

    class_data = raw_detail.get("classData")
    if isinstance(class_data, str) and class_data.strip():
        parsed: Any
        try:
            parsed = json.loads(class_data)
        except ValueError as exc:
            raise ValueError("classData was not valid JSON") from exc
        installed = parsed.get("installed_meter") if isinstance(parsed, dict) else None
        if isinstance(installed, dict):
            for source_key, target_key in _CLASS_DATA_KEYS.items():
                if source_key in installed and target_key not in fields:
                    fields[target_key] = _text(installed[source_key])

    rows = raw_detail.get("data")
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            label = _text(row.get("parameterName"))
            value = _text(row.get("parameterValue"))
            target = _DETAIL_LABELS.get(label)
            if target and target not in fields:
                fields[target] = value

    if not fields:
        raise ValueError("Detail payload did not contain any recognisable fields")
    return fields


# ----------------------------------------------------------------------
# Consumption
# ----------------------------------------------------------------------
def _parse_reading_timestamp(value: Any, timezone: ZoneInfo) -> datetime | None:
    raw = _text(value)
    if not raw:
        return None
    for fmt in _TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone)
        except ValueError:
            continue
    return None


def _median_interval_minutes(readings: list[ConsumptionReading]) -> float | None:
    if len(readings) < 2:
        return None
    gaps = [
        (readings[i].timestamp - readings[i - 1].timestamp).total_seconds() / 60
        for i in range(1, len(readings))
        if readings[i].timestamp > readings[i - 1].timestamp
    ]
    if not gaps:
        return None
    return float(statistics.median(gaps))


def _classify_granularity(median_minutes: float | None, count: int) -> str:
    if count == 0 or median_minutes is None:
        return "empty" if count == 0 else "irregular"
    if median_minutes <= 45:
        return "30min"
    if 1380 <= median_minutes <= 1500:
        return "daily"
    return "irregular"


def normalize_energy(
    rows: list[dict[str, Any]],
    *,
    meter_id: str,
    timezone: ZoneInfo,
    requested_from: date | None = None,
    requested_to: date | None = None,
) -> ConsumptionResponse:
    """Turn cumulative register rows into typed readings plus interval usage."""
    parsed: list[tuple[datetime, float, float, float | None]] = []
    unparsable = 0
    for row in rows:
        timestamp = _parse_reading_timestamp(row.get("timestamp"), timezone)
        kwh = parse_float(row.get("kwh"))
        kvah = parse_float(row.get("kvah"))
        if timestamp is None or kwh is None or kvah is None:
            unparsable += 1
            continue
        parsed.append((timestamp, kwh, kvah, parse_float(row.get("voltR"))))

    parsed.sort(key=lambda item: item[0])

    duplicate_timestamps = 0
    deduped: list[tuple[datetime, float, float, float | None]] = []
    for item in parsed:
        if deduped and deduped[-1][0] == item[0]:
            duplicate_timestamps += 1
            deduped[-1] = item  # the later record wins
        else:
            deduped.append(item)

    readings: list[ConsumptionReading] = []
    regressions = 0
    for index, (timestamp, kwh, kvah, volt) in enumerate(deduped):
        kwh_delta: float | None = None
        kvah_delta: float | None = None
        if index > 0:
            previous = deduped[index - 1]
            raw_kwh_delta = kwh - previous[1]
            raw_kvah_delta = kvah - previous[2]
            if raw_kwh_delta < -0.001 or raw_kvah_delta < -0.001:
                regressions += 1
            else:
                kwh_delta = round(max(0.0, raw_kwh_delta), 3)
                kvah_delta = round(max(0.0, raw_kvah_delta), 3)
        readings.append(
            ConsumptionReading(
                timestamp=timestamp,
                kwh=round(kwh, 3),
                kvah=round(kvah, 3),
                volt_r=round(volt, 1) if volt is not None else None,
                kwh_delta=kwh_delta,
                kvah_delta=kvah_delta,
            )
        )

    voltages = [reading.volt_r for reading in readings if reading.volt_r is not None]
    kwh_consumed = round(sum(reading.kwh_delta or 0.0 for reading in readings), 3)
    kvah_consumed = round(sum(reading.kvah_delta or 0.0 for reading in readings), 3)
    median_minutes = _median_interval_minutes(readings)

    return ConsumptionResponse(
        meter_id=meter_id,
        requested_from=requested_from,
        requested_to=requested_to,
        first_reading_at=readings[0].timestamp if readings else None,
        last_reading_at=readings[-1].timestamp if readings else None,
        granularity=_classify_granularity(median_minutes, len(readings)),
        interval_minutes=int(round(median_minutes)) if median_minutes is not None else None,
        count=len(readings),
        readings=readings,
        summary=ConsumptionSummary(
            kwh_consumed=kwh_consumed,
            kvah_consumed=kvah_consumed,
            first_register_kwh=readings[0].kwh if readings else None,
            last_register_kwh=readings[-1].kwh if readings else None,
            average_voltage=round(statistics.fmean(voltages), 1) if voltages else None,
            min_voltage=min(voltages) if voltages else None,
            max_voltage=max(voltages) if voltages else None,
        ),
        anomalies=ConsumptionAnomalies(
            register_regressions=regressions,
            duplicate_timestamps=duplicate_timestamps,
            unparsable_readings=unparsable,
        ),
    )


def normalize_transformer(
    raw: dict[str, Any],
    *,
    meter_count: int = 0,
    installed: int = 0,
    faulty: int = 0,
    decommissioned: int = 0,
    zone_code: str | None = None,
    zone_name: str | None = None,
) -> Transformer:
    capacity = parse_float(raw.get("capacityKva"))
    return Transformer(
        code=_text(raw.get("code")) or "UNKNOWN",
        name=_text(raw.get("name")) or "UNKNOWN",
        feeder_code=_text(raw.get("feederCode")) or "UNKNOWN",
        capacity_kva=int(capacity) if capacity is not None else None,
        meter_count=meter_count,
        installed_count=installed,
        faulty_count=faulty,
        decommissioned_count=decommissioned,
        zone_code=zone_code,
        zone_name=zone_name,
    )


__all__ = [
    "LEVELS",
    "LEVEL_LABELS",
    "CODE_PREFIXES",
    "derive_code",
    "display_name_from_code",
    "hierarchy_path",
    "normalize_detail",
    "normalize_energy",
    "normalize_hierarchy",
    "normalize_meter",
    "normalize_node",
    "normalize_ssr_hierarchy",
    "normalize_transformer",
    "parse_float",
]
