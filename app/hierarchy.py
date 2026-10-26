"""Reconstructing a usable hierarchy from flat, sometimes inconsistent paths."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.models import (
    HierarchyConflict,
    HierarchyTreeNode,
    Meter,
    StatsResponse,
    Transformer,
)
from app.normalize import LEVELS, hierarchy_path, normalize_transformer


@dataclass
class _NodeAggregate:
    level: str
    code: str | None
    meter_count: int = 0
    names: Counter[str] = field(default_factory=Counter)
    quality: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class HierarchyBuild:
    zones: list[HierarchyTreeNode]
    conflicts: list[HierarchyConflict]
    node_counts: dict[str, int]


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "unknown"


def hierarchy_node_id(level: str, code: str | None, name: str) -> str:
    if code:
        return f"{level}:{code}"
    return f"{level}:name:{_slug(name)}"


def canonicalize_transformers(
    meters: list[Meter], raw_transformers: list[dict[str, Any]]
) -> tuple[list[Meter], list[Transformer]]:
    """Use the DT registry as the canonical transformer name and count meters.

    Some meter rows call DT-007 "Old Malviya Nagar Xfmr" while the registry and
    most rows call it "Sanganer DT 7". The registry wins; the alias is retained
    on the meter's hierarchy node for traceability.
    """
    registry: dict[str, dict[str, Any]] = {}
    for raw in raw_transformers:
        code = str(raw.get("code") or "").strip()
        if code:
            registry[code] = raw

    dt_meters: dict[str, list[Meter]] = defaultdict(list)
    for meter in meters:
        code = meter.dt_code or meter.hierarchy.dt.code
        if code:
            dt_meters[code].append(meter)

    canonicalized: list[Meter] = []
    for meter in meters:
        dt_code = meter.dt_code or meter.hierarchy.dt.code
        registry_row = registry.get(dt_code) if dt_code else None
        registry_name = str(registry_row.get("name") or "").strip() if registry_row else ""
        dt_node = meter.hierarchy.dt
        if registry_name and dt_node.name and registry_name != dt_node.name:
            quality = list(dt_node.data_quality)
            for value in ("name_alias", f"alias:{dt_node.name}"):
                if value not in quality:
                    quality.append(value)
            new_dt = dt_node.model_copy(update={"name": registry_name, "data_quality": quality})
            new_hierarchy = meter.hierarchy.model_copy(update={"dt": new_dt})
            meter = meter.model_copy(
                update={
                    "hierarchy": new_hierarchy,
                    "hierarchy_path": hierarchy_path(new_hierarchy),
                }
            )
        canonicalized.append(meter)

    transformers: list[Transformer] = []
    for code, rows in dt_meters.items():
        zone_counter = Counter(
            (meter.hierarchy.zone.code or "", meter.hierarchy.zone.name) for meter in rows
        )
        zone_code, zone_name = zone_counter.most_common(1)[0][0]
        statuses = Counter(meter.install_status.lower() for meter in rows)
        registry_row = registry.get(code)
        if registry_row is None:
            sample = rows[0].hierarchy
            registry_row = {
                "code": code,
                "name": sample.dt.name,
                "feederCode": sample.feeder.code or "UNKNOWN",
                "capacityKva": None,
            }
        transformers.append(
            normalize_transformer(
                registry_row,
                meter_count=len(rows),
                installed=statuses.get("installed", 0),
                faulty=statuses.get("faulty", 0),
                decommissioned=statuses.get("decommissioned", 0),
                zone_code=zone_code or None,
                zone_name=zone_name or None,
            )
        )

    transformers.sort(key=lambda item: item.code)
    return canonicalized, transformers


def build_hierarchy(
    meters: list[Meter], transformers: list[Transformer]
) -> HierarchyBuild:
    transformer_by_code = {transformer.code: transformer for transformer in transformers}
    aggregates: dict[str, _NodeAggregate] = {}
    parents: dict[str, Counter[str]] = defaultdict(Counter)

    for meter in meters:
        parent_id: str | None = None
        for level in LEVELS:
            node = getattr(meter.hierarchy, level)
            node_id = hierarchy_node_id(level, node.code, node.name)
            aggregate = aggregates.get(node_id)
            if aggregate is None:
                aggregate = _NodeAggregate(level=level, code=node.code)
                aggregates[node_id] = aggregate
            aggregate.meter_count += 1
            aggregate.names[node.name] += 1
            aggregate.quality.update(node.data_quality)
            if parent_id is not None:
                parents[node_id][parent_id] += 1
            parent_id = node_id

    tree_nodes: dict[str, HierarchyTreeNode] = {}
    conflicts: list[HierarchyConflict] = []
    for node_id, aggregate in aggregates.items():
        registry_name = None
        if aggregate.level == "dt" and aggregate.code in transformer_by_code:
            registry_name = transformer_by_code[aggregate.code].name

        if registry_name:
            canonical_name = registry_name
        else:
            # Most frequent spelling; deterministic alphabetical tie-break.
            canonical_name = sorted(
                aggregate.names.items(), key=lambda item: (-item[1], item[0])
            )[0][0]

        aliases = sorted(name for name in aggregate.names if name and name != canonical_name)
        quality = set(aggregate.quality)
        if aliases:
            quality.add("name_alias")
            quality.update(f"alias:{alias}" for alias in aliases[:5])

        capacity = None
        if aggregate.level == "dt" and aggregate.code in transformer_by_code:
            capacity = transformer_by_code[aggregate.code].capacity_kva

        tree_nodes[node_id] = HierarchyTreeNode(
            id=node_id,
            level=aggregate.level,
            name=canonical_name,
            code=aggregate.code,
            meter_count=aggregate.meter_count,
            capacity_kva=capacity,
            data_quality=sorted(quality),
            children=[],
        )

    roots: list[HierarchyTreeNode] = []
    for node_id, tree_node in tree_nodes.items():
        parent_counts = parents.get(node_id)
        if not parent_counts:
            if tree_node.level == "zone":
                roots.append(tree_node)
            else:
                tree_node.data_quality = sorted(set(tree_node.data_quality) | {"orphan"})
                roots.append(tree_node)
            continue

        if len(parent_counts) > 1:
            tree_node.data_quality = sorted(set(tree_node.data_quality) | {"parent_conflict"})
            conflicts.append(
                HierarchyConflict(
                    level=tree_node.level,
                    id=node_id,
                    name=tree_node.name,
                    meter_count=tree_node.meter_count,
                    reported_parents=sorted(parent_counts),
                    chosen_parent=sorted(
                        parent_counts.items(), key=lambda item: (-item[1], item[0])
                    )[0][0],
                )
            )

        chosen_parent = sorted(parent_counts.items(), key=lambda item: (-item[1], item[0]))[0][0]
        parent_node = tree_nodes.get(chosen_parent)
        if parent_node is not None:
            parent_node.children.append(tree_node)

    def sort_key(node: HierarchyTreeNode) -> tuple[str, str]:
        return (node.code or "", node.name.lower())

    def sort_tree(nodes: list[HierarchyTreeNode]) -> None:
        nodes.sort(key=sort_key)
        for node in nodes:
            sort_tree(node.children)

    sort_tree(roots)
    conflicts.sort(key=lambda item: (LEVELS.index(item.level), item.id))

    node_counts: Counter[str] = Counter()
    for aggregate in aggregates.values():
        node_counts[aggregate.level] += 1

    return HierarchyBuild(
        zones=roots,
        conflicts=conflicts,
        node_counts={level: node_counts.get(level, 0) for level in LEVELS},
    )


def _sorted_counts(values: list[str]) -> dict[str, int]:
    counter = Counter(value if value else "Unknown" for value in values)
    return dict(sorted(counter.items(), key=lambda item: (-item[1], item[0])))


def build_stats(
    meters: list[Meter],
    transformers: list[Transformer],
    hierarchy: HierarchyBuild,
    data_fetched_at: datetime,
) -> StatsResponse:
    return StatsResponse(
        total_meters=len(meters),
        by_status=_sorted_counts([meter.install_status for meter in meters]),
        by_make=_sorted_counts([meter.make for meter in meters]),
        by_phase=_sorted_counts([meter.phase_type for meter in meters]),
        by_build=_sorted_counts([meter.build for meter in meters]),
        by_install_type=_sorted_counts([meter.install_type for meter in meters]),
        transformers=len(transformers),
        zones=hierarchy.node_counts.get("zone", 0),
        feeders=hierarchy.node_counts.get("feeder", 0),
        substations=hierarchy.node_counts.get("substation", 0),
        meters_with_location=sum(1 for meter in meters if meter.location is not None),
        data_fetched_at=data_fetched_at,
    )
