#!/usr/bin/env python3
"""Build neutral state rows and auto-events from a domain pack.

The script intentionally knows only about domain-pack concepts:
steps, state variables, events, and configured column aliases. Domain-specific
meaning lives in the pack data and pipeline config.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sim_core.domain_runtime import DomainRuntime


DOMAIN = DomainRuntime.load(None)


def read_tsv(path: Path | None) -> list[dict[str, str]]:
    if not path or not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def write_tsv(path: Path, rows: Iterable[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, delimiter="\t", fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def to_float(value: Any, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def to_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return fallback


def first_value(row: dict[str, str], *keys: str, fallback: str = "") -> str:
    for key in keys:
        value = row.get(key, "")
        if value != "":
            return value
    return fallback


def step_count(time_schedule: list[dict[str, str]]) -> int:
    configured = DOMAIN.config.get("time", {}) if isinstance(DOMAIN.config.get("time"), dict) else {}
    configured_steps = to_int(configured.get("steps"), 0)
    schedule_steps = [
        to_int(first_value(row, "step"), 0)
        for row in time_schedule
    ]
    return max([configured_steps, *schedule_steps, 1])


def state_initial_values() -> dict[str, float]:
    rows = read_tsv(DOMAIN.data_path("state_variables"))
    values: dict[str, float] = {}
    for row in rows:
        state_id = first_value(row, "state_id", "id")
        if state_id:
            values[state_id] = to_float(first_value(row, "initial_value"), 50.0)
    for field in DOMAIN.state_fields():
        values.setdefault(field, 50.0)
    return values


def event_start(row: dict[str, str]) -> int:
    return to_int(first_value(row, "start_step", "step"), 0)


def event_end(row: dict[str, str]) -> int:
    explicit = first_value(row, "end_step")
    if explicit:
        return to_int(explicit, event_start(row))
    return event_start(row)


def event_intensity(row: dict[str, str]) -> float:
    raw = first_value(row, "intensity", fallback="0")
    value = to_float(raw, 0.0)
    return value if value <= 1.0 else value / 100.0


def active_events(events: list[dict[str, str]], step: int) -> list[dict[str, str]]:
    return [
        row for row in events
        if event_start(row) <= step <= max(event_start(row), event_end(row))
    ]


def intervention_event_types() -> set[str]:
    scenario_modes = DOMAIN.pipeline.get("scenario_modes", {})
    if not isinstance(scenario_modes, dict):
        return {"object"}
    configured = scenario_modes.get("intervention_event_type", "object")
    if isinstance(configured, list):
        return {str(item) for item in configured if str(item)}
    return {str(configured)} if configured else {"object"}


def build_state_rows(events: list[dict[str, str]], time_schedule: list[dict[str, str]]) -> list[dict[str, Any]]:
    fields = DOMAIN.state_fields()
    initial_values = state_initial_values()
    labels = DOMAIN.state_field_labels()
    negative_fields = DOMAIN.state_negative_fields()
    buffer_event_types = intervention_event_types()
    rows: list[dict[str, Any]] = []
    for step in range(1, step_count(time_schedule) + 1):
        events_for_step = active_events(events, step)
        pressure = sum(
            event_intensity(row)
            for row in events_for_step
            if first_value(row, "event_type") not in buffer_event_types
        )
        buffer = sum(
            event_intensity(row)
            for row in events_for_step
            if first_value(row, "event_type") in buffer_event_types
        )
        row: dict[str, Any] = {"step": step}
        for field in fields:
            base = initial_values.get(field, 50.0)
            polarity = "buffer" if field == DOMAIN.state_buffer_field() else "pressure"
            delta = (buffer - pressure) * 4.0 if polarity == "buffer" else pressure * 4.0
            row[field] = round(max(0.0, min(100.0, base + delta)), 1)
        if fields:
            dominant = max(negative_fields or fields, key=lambda key: to_float(row.get(key), 0.0))
            row[DOMAIN.state_dominant_field()] = labels.get(dominant, dominant)
        row[DOMAIN.state_high_risk_field()] = str(DOMAIN.state_config().get("no_high_risk_text", "none"))
        event_names = [
            first_value(event, "event_name", fallback="event")
            for event in events_for_step
        ]
        row[DOMAIN.state_context_field()] = (
            f"step {step}: " + (" / ".join(event_names) if event_names else "baseline")
        )
        rows.append(row)
    return rows


def build_auto_event_rows(events: list[dict[str, str]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    input_fields = DOMAIN.event_input_fields()
    primary_input_field = input_fields[0] if input_fields else "description"
    for event in events:
        start = event_start(event)
        end = max(start, event_end(event))
        for step in range(start, end + 1):
            description = first_value(event, "description", primary_input_field)
            rows.append({
                "event_id": first_value(event, "event_id", fallback=f"event_{step}"),
                "step": step,
                "event_type": first_value(event, "event_type"),
                "event_name": first_value(event, "event_name"),
                "intensity": first_value(event, "intensity"),
                "target": first_value(event, "target"),
                "direction": first_value(event, "direction"),
                "source": "domain_event",
                DOMAIN.event_state_field(): description,
                primary_input_field: description,
            })
    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build neutral state artifacts from a domain pack.")
    parser.add_argument("--pack", type=Path, required=True)
    parser.add_argument("--scenario", default="")
    parser.add_argument("--events-tsv", type=Path)
    parser.add_argument("--time-schedule-tsv", type=Path)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "outputs" / "runs" / "domain_state")
    return parser.parse_args()


def main() -> None:
    global DOMAIN
    args = parse_args()
    DOMAIN = DomainRuntime.load(args.pack, scenario=args.scenario or None)
    events_tsv = args.events_tsv or DOMAIN.data_path("events")
    time_schedule_tsv = args.time_schedule_tsv or DOMAIN.data_path("time_schedule")
    events = read_tsv(events_tsv)
    time_schedule = read_tsv(time_schedule_tsv)

    state_rows = build_state_rows(events, time_schedule)
    auto_event_rows = build_auto_event_rows(events)
    auto_event_rows.sort(key=lambda row: (to_int(row.get("step"), 0), str(row.get("event_id", ""))))
    state_fields = DOMAIN.state_fields()
    state_output = DOMAIN.state_output_name()

    state_fieldnames = [
        "step",
        *state_fields,
        DOMAIN.state_dominant_field(),
        DOMAIN.state_high_risk_field(),
        DOMAIN.state_context_field(),
    ]
    event_fieldnames = [
        "event_id",
        "step",
        "event_type",
        "event_name",
        "intensity",
        "target",
        "direction",
        "source",
        DOMAIN.event_state_field(),
        *(DOMAIN.event_input_fields() or ["description"]),
    ]

    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_tsv(args.output_dir / state_output, state_rows, state_fieldnames)
    write_tsv(args.output_dir / "auto_events.tsv", auto_event_rows, event_fieldnames)
    manifest = {
        "kind": "domain_state",
        "pack": str(args.pack),
        "scenario": args.scenario,
        "events_tsv": str(events_tsv or ""),
        "time_schedule_tsv": str(time_schedule_tsv or ""),
        "outputs": [state_output, "auto_events.tsv"],
    }
    (args.output_dir / "state_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote {len(state_rows)} state rows and {len(auto_event_rows)} auto-events to {args.output_dir}")


if __name__ == "__main__":
    main()
