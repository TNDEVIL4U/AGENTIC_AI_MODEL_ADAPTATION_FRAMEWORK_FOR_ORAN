"""Monitoring -> DriftEvent mappers: turn what a monitoring system already sends (an
Alertmanager webhook, an Evidently drift report, any JSON document) into DriftEvents. A mapper is
a mapping file, not code (``config/mappers/*.toml``), named in ``DRIFT_MAPPERS``; the API serves
each one at ``POST /api/v1/adaptation/events/from/{name}`` and the CLI at ``oran-adapt event map``.

A mapping says where the records are (``records``), which of them count (``where``), and for
each DriftEvent field the path its value comes from (``fields``). Paths are dotted
(``labels.model_id``); a segment ``metrics[metric=DatasetDriftMetric]`` picks the first list
element whose ``metric`` is that value; a path that starts with ``/`` is read from the payload's
root instead of the record. A value missing from a record leaves its field unset, and the
DriftEvent contract decides whether that is allowed (``model_id`` is required).
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from oran_adapt.core.errors import ConfigurationError, EventMappingError
from oran_adapt.core.policies import load_policy_file
from oran_adapt.core.schemas import DriftEvent

_SEGMENT = re.compile(r"^(?P<name>[^\[\]=]*)(?:\[(?P<key>[^\[\]=]+)=(?P<value>[^\[\]]*)\])?$")
_FIELD_REF = "field:"
_MISSING: Any = object()


def _check_path(path: str) -> None:
    body = path.removeprefix("/")
    for segment in body.split("."):
        match = _SEGMENT.match(segment)
        if match is None or not (match["name"] or match["key"]):
            raise ValueError(f"invalid path {path!r} (segment {segment!r})")


class FeatureTable(BaseModel):
    """A per-feature drift table (Evidently's ``drift_by_columns``): feature name -> an object
    whose ``detected`` key says whether that feature drifted and ``score`` key holds its score.
    Drifted features become ``affected_features``; their scores become ``drift_metrics``."""

    model_config = ConfigDict(extra="forbid")
    path: str
    detected: str = Field(min_length=1)
    score: str | None = None


class EventIdRule(BaseModel):
    """``event_id`` built from values of the record (paths) or of already-mapped fields
    (``field:<name>``), joined by ``separator``. If any part is missing the event gets no id and
    its idempotency key is computed from its content instead."""

    model_config = ConfigDict(extra="forbid")
    parts: list[str] = Field(min_length=1)
    separator: str


class DriftMapping(BaseModel):
    """One mapping file (see the module docstring)."""

    model_config = ConfigDict(extra="forbid")
    version: Literal["1"]
    description: str = ""
    records: str = ""
    where: dict[str, str] = Field(default_factory=dict)
    fields: dict[str, str] = Field(default_factory=dict)
    constants: dict[str, Any] = Field(default_factory=dict)
    split: dict[str, str] = Field(default_factory=dict)
    value_maps: dict[str, dict[str, str]] = Field(default_factory=dict)
    event_id: EventIdRule | None = None
    feature_table: FeatureTable | None = None

    @model_validator(mode="after")
    def _consistent(self) -> DriftMapping:
        known = set(DriftEvent.model_fields)
        for section in ("fields", "constants", "split", "value_maps"):
            unknown = sorted(set(getattr(self, section)) - known)
            if unknown:
                raise ValueError(f"{section} names unknown DriftEvent field(s): {unknown}")
        paths = [*self.where, *self.fields.values()]
        if self.records:
            paths.append(self.records)
        if self.feature_table is not None:
            paths.append(self.feature_table.path)
        if self.event_id is not None:
            for part in self.event_id.parts:
                if part.startswith(_FIELD_REF):
                    if part.removeprefix(_FIELD_REF) not in known:
                        raise ValueError(f"event_id part {part!r} is not a DriftEvent field")
                else:
                    paths.append(part)
        for path in paths:
            _check_path(path)
        for field in self.split:
            if field not in self.fields:
                raise ValueError(f"split.{field} has no fields.{field} path to split")
        return self


def load_mapping(path: str, key: str = "drift_mappers") -> DriftMapping:
    """Read and validate one mapping file; ConfigurationError naming ``key`` otherwise."""
    mapping: DriftMapping = load_policy_file(path, DriftMapping, key, "drift-event mapping")
    return mapping


def load_mappers(mappers: dict[str, str]) -> dict[str, DriftMapping]:
    """Every configured mapper (name -> mapping file), each validated."""
    return {name: load_mapping(path) for name, path in sorted(mappers.items())}


def resolve(document: Any, path: str) -> Any:
    """The value at ``path`` in ``document``; ``_MISSING`` when any step is absent."""
    node = document
    for segment in path.split("."):
        match = _SEGMENT.match(segment)
        if match is None:
            raise EventMappingError(f"invalid path {path!r}", path=path)
        if match["name"]:
            if not isinstance(node, dict) or match["name"] not in node:
                return _MISSING
            node = node[match["name"]]
        if match["key"] is not None:
            if not isinstance(node, list):
                return _MISSING
            node = next(
                (item for item in node
                 if isinstance(item, dict) and _text(item.get(match["key"])) == match["value"]),
                _MISSING,
            )
            if node is _MISSING:
                return node
    return node


def _text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _get(payload: dict[str, Any], record: Any, path: str) -> Any:
    if path.startswith("/"):
        return resolve(payload, path[1:])
    return resolve(record, path)


def map_payload(
    mapping: DriftMapping,
    payload: Any,
    *,
    max_events: int,
    overrides: dict[str, Any] | None = None,
) -> list[DriftEvent]:
    """The DriftEvents ``payload`` describes under ``mapping`` (possibly none, when no record
    passes ``where``). ``overrides`` set fields the payload does not carry (e.g. the model id an
    Evidently report was computed for). EventMappingError for a payload of the wrong shape, too
    many records, or values that do not fit the DriftEvent contract."""
    if not isinstance(payload, dict):
        raise EventMappingError("the payload is not a JSON object")
    if mapping.records:
        records = resolve(payload, mapping.records)
        if not isinstance(records, list):
            raise EventMappingError(f"the payload has no list at {mapping.records!r}",
                                    path=mapping.records)
    else:
        records = [payload]
    selected = [
        record for record in records
        if isinstance(record, dict)
        and all(_text(_get(payload, record, path)) == value
                for path, value in mapping.where.items())
    ]
    if len(selected) > max_events:
        raise EventMappingError(
            f"the payload holds {len(selected)} events, more than DRIFT_MAPPER_MAX_EVENTS",
            events=len(selected), limit=max_events,
        )
    return [_event(mapping, payload, record, overrides or {}, index)
            for index, record in enumerate(selected)]


def _event(mapping: DriftMapping, payload: dict[str, Any], record: dict[str, Any],
           overrides: dict[str, Any], index: int) -> DriftEvent:
    values: dict[str, Any] = dict(mapping.constants)
    for field, path in mapping.fields.items():
        value = _get(payload, record, path)
        if value is _MISSING or value is None:
            continue
        if field in mapping.split and isinstance(value, str):
            value = [item.strip() for item in value.split(mapping.split[field]) if item.strip()]
        if field in mapping.value_maps:
            table = {k.lower(): v for k, v in mapping.value_maps[field].items()}
            if _text(value).lower() not in table:
                raise EventMappingError(
                    f"{field}: {value!r} has no entry in the mapping's value_maps.{field}",
                    field=field, value=_text(value), known=sorted(table),
                )
            value = table[_text(value).lower()]
        values[field] = value
    if mapping.feature_table is not None:
        _add_feature_table(mapping.feature_table, payload, record, values)
    values.update({k: v for k, v in overrides.items() if v is not None})
    if mapping.event_id is not None and "event_id" not in values:
        parts = [
            values.get(part.removeprefix(_FIELD_REF), _MISSING) if part.startswith(_FIELD_REF)
            else _get(payload, record, part)
            for part in mapping.event_id.parts
        ]
        if all(part is not _MISSING and part is not None for part in parts):
            values["event_id"] = mapping.event_id.separator.join(_text(p) for p in parts)
    try:
        return DriftEvent.model_validate(values)
    except ValidationError as exc:
        problems = [
            {"field": ".".join(str(p) for p in err["loc"]) or "event", "error": err["msg"]}
            for err in exc.errors()
        ]
        raise EventMappingError(
            f"record {index}: the mapped values do not fit the DriftEvent contract "
            f"({problems[0]['field']}: {problems[0]['error']})",
            record=index, problems=problems,
        ) from None


def _add_feature_table(table_rule: FeatureTable, payload: dict[str, Any],
                       record: dict[str, Any], values: dict[str, Any]) -> None:
    table = _get(payload, record, table_rule.path)
    if not isinstance(table, dict):
        return
    drifted = {name: column for name, column in table.items()
               if isinstance(column, dict) and column.get(table_rule.detected) is True}
    values.setdefault("affected_features", sorted(drifted))
    if table_rule.score is not None:
        values.setdefault("drift_metrics", {
            name: float(column[table_rule.score]) for name, column in sorted(drifted.items())
            if isinstance(column.get(table_rule.score), int | float)
            and not isinstance(column.get(table_rule.score), bool)
        })


def check_mappers(mappers: dict[str, str]) -> None:
    """Fail fast (ConfigurationError) on a mapper whose file is missing or invalid."""
    for name, path in mappers.items():
        if not name or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
            raise ConfigurationError(
                f"DRIFT_MAPPERS: {name!r} is not a valid mapper name (lowercase letters, "
                "digits, '-' and '_')", key="DRIFT_MAPPERS",
            )
        load_mapping(path)
