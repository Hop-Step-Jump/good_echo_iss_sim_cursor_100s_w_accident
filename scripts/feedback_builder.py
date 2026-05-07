#!/usr/bin/env python3
"""Convert agent actions into domain-configured societal-state feedback."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sim_core.domain_runtime import DomainRuntime

DEFAULT_AGENT_TURNS = ROOT / "outputs" / "runs" / "domain_agent_turns" / "turns.tsv"
DEFAULT_STATE_TSV: Path | None = None
DEFAULT_AUTO_EVENTS_TSV: Path | None = None
DEFAULT_OUTPUT = ROOT / "outputs" / "runs" / "domain_feedback"

STATE_FIELDS = [
    "pressure",
    "support",
    "trust",
    "buffer",
]

STATE_LABELS = {
    "pressure": "pressure",
    "support": "support",
    "trust": "trust",
    "buffer": "buffer",
}
DOMAIN = DomainRuntime.load(None)


def read_tsv(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def read_optional_tsv(path: Path | None) -> List[Dict[str, str]]:
    if not path or not path.exists():
        return []
    return read_tsv(path)


def write_tsv(path: Path, rows: Iterable[Dict[str, Any]], fieldnames: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, delimiter="\t", fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def to_float(value: Any, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def first_value(row: Dict[str, str], *keys: str, fallback: str = "") -> str:
    for key in keys:
        value = row.get(key, "")
        if value != "":
            return value
    return fallback


def rows_by_step(rows: List[Dict[str, str]]) -> Dict[int, List[Dict[str, str]]]:
    grouped: Dict[int, List[Dict[str, str]]] = {}
    for row in rows:
        step = int(to_float(row.get("step"), 0))
        grouped.setdefault(step, []).append(row)
    return grouped


def feedback_config() -> Dict[str, Any]:
    value = DOMAIN.pipeline.get("feedback", {})
    return value if isinstance(value, dict) else {}


def feedback_signal_keys() -> List[str]:
    keys = feedback_config().get("signal_keys", [])
    if isinstance(keys, list):
        return [str(key) for key in keys]
    return []


def feedback_influence_weighted_signals() -> set[str]:
    keys = feedback_config().get("influence_weighted_signals", [])
    if isinstance(keys, list):
        return {str(key) for key in keys}
    return set()


def read_agent_metadata(
    agent_panel_tsv: Path | None = None,
    agent_source_tsvs: List[Path] | None = None,
    auxiliary_agents_tsv: Path | None = None,
    inflow_templates_tsv: Path | None = None,
) -> Dict[str, Dict[str, str]]:
    rows = [
        row
        for source_path in agent_source_tsvs or []
        for row in read_optional_tsv(source_path)
    ]
    id_columns = DOMAIN.agent_id_columns()
    metadata = {}
    for row in rows:
        agent_id = next((row.get(column, "") for column in id_columns if row.get(column)), "")
        if agent_id:
            metadata[agent_id] = row
    metadata.update(
        read_inflow_metadata(
            auxiliary_agents_tsv=auxiliary_agents_tsv,
            inflow_templates_tsv=inflow_templates_tsv,
        )
    )
    for panel_row in read_optional_tsv(agent_panel_tsv):
        agent_id = next((panel_row.get(column, "") for column in id_columns if panel_row.get(column)), "")
        if not agent_id or agent_id not in metadata:
            continue
        patched = dict(metadata[agent_id])
        population_weight = first_value(
            panel_row,
            *DOMAIN.column_aliases("panel_population_weight", ["population_weight"]),
        )
        if population_weight:
            patched["population_weight"] = population_weight
        metadata[agent_id] = patched
    return metadata


def read_inflow_metadata(
    auxiliary_agents_tsv: Path | None = None,
    inflow_templates_tsv: Path | None = None,
) -> Dict[str, Dict[str, str]]:
    groups = {
        first_value(row, "group_id", "id"): row
        for row in read_optional_tsv(auxiliary_agents_tsv)
        if first_value(row, "group_id", "id")
    }
    metadata: Dict[str, Dict[str, str]] = {}
    for template in read_optional_tsv(inflow_templates_tsv):
        agent_id = first_value(template, "agent_id", "generated_agent_id", "id")
        group = groups.get(first_value(template, "source_group_id", "group_id"))
        if not agent_id or not group:
            continue
        metadata[agent_id] = {
            "agent_id": agent_id,
            "name": first_value(template, "name", "display_name") or first_value(group, "name", "label"),
            "population_weight": first_value(group, "population_weight", "weight", fallback="1.0"),
            "influence_weight": first_value(group, "influence_weight", fallback="0.8"),
        }
    return metadata


def action_signal(row: Dict[str, str], metadata: Dict[str, str], key: str) -> float:
    rules_by_key = feedback_config().get("signal_rules", {})
    rule = rules_by_key.get(key, {}) if isinstance(rules_by_key, dict) else {}
    if not isinstance(rule, dict):
        return 0.0
    category = row.get("action_category", "")
    emotion = row.get("emotion", "")
    evaluation = row.get("evaluation", "")
    intensity = clamp(to_float(row.get("intensity"), 0.0)) / 100.0

    signal = 0.0

    signal += to_float((rule.get("category_weights") or {}).get(category), 0.0)
    signal += to_float((rule.get("emotion_weights") or {}).get(emotion), 0.0)
    signal += to_float((rule.get("evaluation_weights") or {}).get(evaluation), 0.0)

    text_contains = rule.get("text_contains", {})
    if isinstance(text_contains, dict):
        for field, word_weights in text_contains.items():
            text = row.get(str(field), "")
            if not isinstance(word_weights, dict):
                continue
            for word, weight in word_weights.items():
                if str(word) in text:
                    signal += to_float(weight, 0.0)

    metadata_rules = rule.get("metadata_evaluation_weights", [])
    if isinstance(metadata_rules, list):
        for metadata_rule in metadata_rules:
            if not isinstance(metadata_rule, dict):
                continue
            evaluations = {str(item) for item in metadata_rule.get("evaluations", [])}
            if evaluations and evaluation not in evaluations:
                continue
            metadata_field = str(metadata_rule.get("metadata_field", ""))
            metadata_value = metadata.get(metadata_field, "")
            value_weights = metadata_rule.get("value_weights", {})
            if isinstance(value_weights, dict):
                signal += to_float(value_weights.get(metadata_value), 0.0)

    return clamp(signal * intensity * 100.0)


def build_feedback_rows(agent_turns: List[Dict[str, str]], metadata_by_id: Dict[str, Dict[str, str]]) -> List[Dict[str, Any]]:
    grouped = rows_by_step(agent_turns)
    rows = []
    evaluation_labels = list((DOMAIN.evaluation_labels() or {"positive": [], "neutral": [], "warning": [], "critical": []}).keys())
    default_evaluation = DOMAIN.default_evaluation("neutral")
    signal_keys = feedback_signal_keys()
    influence_weighted_signals = feedback_influence_weighted_signals()

    for step in sorted(grouped):
        turns = grouped[step]
        weighted = {key: 0.0 for key in signal_keys}
        weight_sum = 0.0
        influence_weighted = {key: 0.0 for key in signal_keys}
        influence_sum = 0.0
        evaluation_counts = {label: 0.0 for label in evaluation_labels}
        pathway_sum = 0.0
        support_sum = 0.0
        intensity_sum = 0.0

        for turn in turns:
            metadata = metadata_by_id.get(turn.get("agent_id", ""), {})
            population_weight = max(
                0.1,
                to_float(
                    first_value(
                        metadata,
                        *DOMAIN.population_weight_columns(),
                    ),
                    1.0,
                ),
            )
            influence_weight = population_weight * max(
                0.1,
                to_float(first_value(metadata, "influence_weight"), 1.0),
            )
            weight_sum += population_weight
            influence_sum += influence_weight
            evaluation = turn.get("evaluation", default_evaluation)
            evaluation_counts[evaluation] = evaluation_counts.get(evaluation, 0.0) + population_weight
            pathway_sum += to_float(turn.get("pathway"), 0.0) * population_weight
            support_sum += to_float(turn.get("support"), 0.0) * population_weight
            intensity_sum += to_float(turn.get("intensity"), 0.0) * population_weight

            for key in signal_keys:
                signal = action_signal(turn, metadata, key)
                weighted[key] += signal * population_weight
                influence_weighted[key] += signal * influence_weight

        if not weight_sum:
            continue

        row: Dict[str, Any] = {
            "step": step,
            "agent_count": len(turns),
            "population_weight_total": round(weight_sum, 2),
            "average_pathway": round(pathway_sum / weight_sum, 1),
            "average_support": round(support_sum / weight_sum, 1),
            "average_intensity": round(intensity_sum / weight_sum, 1),
        }
        for label in evaluation_labels:
            row[f"{label}_share"] = round(evaluation_counts.get(label, 0.0) / weight_sum * 100.0, 1)
        for key in signal_keys:
            denominator = influence_sum if key in influence_weighted_signals else weight_sum
            source = influence_weighted if key in influence_weighted_signals else weighted
            row[key] = round(source[key] / max(denominator, 0.1), 1)

        row["feedback_summary"] = summarize_feedback(row)
        rows.append(row)
    return rows


def summarize_feedback(row: Dict[str, Any]) -> str:
    labels = feedback_config().get("signal_labels", {})
    signals = [
        (str(labels.get(key, key)) if isinstance(labels, dict) else key, row[key])
        for key in feedback_signal_keys()
    ]
    top = sorted(signals, key=lambda item: item[1], reverse=True)[:3]
    return "、".join(f"{label}{value:.1f}" for label, value in top)


def dominant_pressure(row: Dict[str, Any]) -> str:
    negative = DOMAIN.state_negative_fields() or [field for field in STATE_FIELDS if field != DOMAIN.state_buffer_field()]
    labels = {**STATE_LABELS, **DOMAIN.state_field_labels()}
    field = max(negative, key=lambda key: to_float(row.get(key), 0.0))
    return labels.get(field, field)


def feedback_deltas(feedback: Dict[str, Any] | None) -> Dict[str, float]:
    state_fields = DOMAIN.state_fields() or STATE_FIELDS
    if not feedback:
        return {field: 0.0 for field in state_fields}

    coefficients = feedback_config().get("delta_coefficients", {})
    deltas: Dict[str, float] = {}
    for field in state_fields:
        field_coefficients = coefficients.get(field, {}) if isinstance(coefficients, dict) else {}
        if not isinstance(field_coefficients, dict):
            deltas[field] = 0.0
            continue
        deltas[field] = sum(
            to_float(feedback.get(signal_key), 0.0) * to_float(coefficient, 0.0)
            for signal_key, coefficient in field_coefficients.items()
        )
    return deltas


def build_feedback_state_rows(societal_state_rows: List[Dict[str, str]], feedback_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    feedback_by_step = {int(row["step"]): row for row in feedback_rows}
    state_fields = DOMAIN.state_fields() or STATE_FIELDS
    rows = []
    for state in societal_state_rows:
        step = int(to_float(state.get("step"), 0))
        source_feedback = feedback_by_step.get(step - 1)
        deltas = feedback_deltas(source_feedback)
        adjusted: Dict[str, Any] = {
            "step": step,
            "applied_feedback_from_step": source_feedback.get("step", "") if source_feedback else "",
        }
        for field in state_fields:
            original = to_float(state.get(field), 0.0)
            delta = round(deltas[field], 2)
            adjusted[field] = round(clamp(original + delta), 1)
            adjusted[f"{field}_delta_from_agent_feedback"] = delta

        adjusted[DOMAIN.state_dominant_field()] = dominant_pressure(adjusted)
        adjusted[DOMAIN.state_high_risk_field()] = state.get(DOMAIN.state_high_risk_field(), "")
        feedback_text = source_feedback.get("feedback_summary", "none") if source_feedback else "none"
        adjusted[DOMAIN.state_context_field()] = (
            f"{state.get(DOMAIN.state_context_field(), '')}"
            f" feedback: {feedback_text}."
        ).strip()
        rows.append(adjusted)
    return rows


def feedback_event(
    event_id: str,
    step: int,
    category: str,
    name: str,
    intensity: float,
    direction: str,
    source: str,
    summary: str,
    agent_input_text: str,
) -> Dict[str, Any]:
    row = {
        "event_id": event_id,
        "step": step,
        "event_type": category,
        "event_name": name,
        "intensity": f"{clamp(intensity, 0.0, 1.0):.2f}",
        "target": "all",
        "direction": direction,
        "source": source,
    }
    row["scope"] = str(DOMAIN.pipeline.get("events", {}).get("local_context_label", "local"))
    row[DOMAIN.event_state_field()] = summary
    for legacy_field in DOMAIN.legacy_event_state_fields():
        row[legacy_field] = summary
    for text_field in DOMAIN.event_input_fields():
        row[text_field] = agent_input_text
    return row


def build_feedback_events(feedback_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = []
    event_number = 1
    event_rules = feedback_config().get("event_rules", [])
    if not isinstance(event_rules, list):
        event_rules = []
    for feedback in feedback_rows:
        target_step = int(feedback["step"]) + 1
        summary = feedback["feedback_summary"]
        for rule in event_rules:
            if not isinstance(rule, dict):
                continue
            signal_key = str(rule.get("signal", ""))
            value = to_float(feedback.get(signal_key), 0.0)
            threshold = to_float(rule.get("threshold"), 0.0)
            if to_float(value) < threshold:
                continue
            rows.append(feedback_event(
                f"FB{event_number:03d}",
                target_step,
                str(rule.get("category", "feedback")),
                str(rule.get("name", signal_key)),
                round((to_float(value) - threshold) / 55.0 + 0.25, 2),
                str(rule.get("direction", "")),
                "agent_feedback",
                summary,
                str(rule.get("input_text", "")),
            ))
            event_number += 1
    return rows


def write_combined_events(path: Path, auto_events: List[Dict[str, str]], feedback_events: List[Dict[str, Any]]) -> None:
    fieldnames = [
        "event_id",
        "step",
        "event_type",
        "event_name",
        "intensity",
        "target",
        "direction",
        "source",
        "scope",
        DOMAIN.event_state_field(),
        *DOMAIN.legacy_event_state_fields(),
        *DOMAIN.event_input_fields(),
    ]
    rows = [*auto_events, *feedback_events]
    rows.sort(key=lambda row: (int(to_float(row.get("step"), 0)), str(row.get("event_id", ""))))
    write_tsv(path, rows, fieldnames)


def pick_pack_data_path(
    data_config: Dict[str, Any],
    key: str,
    fallback: Path | None = None,
) -> Path | None:
    value = data_config.get(key)
    if value is None:
        return fallback
    return Path(str(value))


def require_input_path(value: Path | None, arg_name: str) -> Path:
    if value is None:
        raise SystemExit(f"Missing required input: {arg_name} (or provide --pack with matching data key)")
    return value


def main() -> None:
    global DOMAIN
    parser = argparse.ArgumentParser()
    parser.add_argument("--pack", type=Path, help="Optional domain pack directory")
    parser.add_argument("--scenario", help="Optional domain pack scenario id")
    parser.add_argument("--agent-turns", type=Path, default=DEFAULT_AGENT_TURNS)
    parser.add_argument("--state-tsv", type=Path, default=DEFAULT_STATE_TSV)
    parser.add_argument("--auto-events-tsv", type=Path, default=DEFAULT_AUTO_EVENTS_TSV)
    parser.add_argument("--agent-panel-tsv", type=Path)
    parser.add_argument("--agents-tsv", type=Path)
    parser.add_argument("--agent-source-tsv", type=Path, action="append", default=[])
    parser.add_argument("--auxiliary-agents-tsv", type=Path)
    parser.add_argument(
        "--inflow-templates-tsv",
        type=Path,
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    DOMAIN = DomainRuntime.load(args.pack, scenario=args.scenario)
    explicit_agent_sources = [
        path for path in [args.agents_tsv, *args.agent_source_tsv]
        if path
    ]
    agent_source_paths = DOMAIN.agent_source_paths(explicit_agent_sources)
    if not agent_source_paths:
        raise SystemExit("Missing required input: agent sources (provide --pack pipeline.agents.sources or --agent-source-tsv)")
    agent_panel_tsv = args.agent_panel_tsv or DOMAIN.panel_path()
    auxiliary_agents_tsv = args.auxiliary_agents_tsv or DOMAIN.data_path("auxiliary_agents")
    inflow_templates_tsv = args.inflow_templates_tsv or DOMAIN.data_path("inflow_templates")
    state_tsv = args.state_tsv

    agent_turns = read_tsv(args.agent_turns)
    societal_state_rows = read_optional_tsv(state_tsv)
    auto_events = read_optional_tsv(args.auto_events_tsv)
    metadata_by_id = read_agent_metadata(
        agent_panel_tsv=agent_panel_tsv,
        agent_source_tsvs=agent_source_paths,
        auxiliary_agents_tsv=auxiliary_agents_tsv,
        inflow_templates_tsv=inflow_templates_tsv,
    )

    feedback_rows = build_feedback_rows(agent_turns, metadata_by_id)
    feedback_state_rows = build_feedback_state_rows(societal_state_rows, feedback_rows)
    feedback_events = build_feedback_events(feedback_rows)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    evaluation_labels = list((DOMAIN.evaluation_labels() or {"positive": [], "neutral": [], "warning": [], "critical": []}).keys())
    feedback_fieldnames = [
        "step",
        "agent_count",
        "population_weight_total",
        "average_pathway",
        "average_support",
        "average_intensity",
        *[f"{label}_share" for label in evaluation_labels],
        *feedback_signal_keys(),
        "feedback_summary",
    ]
    write_tsv(args.output_dir / "agent_feedback.tsv", feedback_rows, feedback_fieldnames)

    state_fields = DOMAIN.state_fields() or STATE_FIELDS
    state_fieldnames = [
        "step",
        "applied_feedback_from_step",
        *state_fields,
        *[f"{field}_delta_from_agent_feedback" for field in state_fields],
        DOMAIN.state_dominant_field(),
        DOMAIN.state_high_risk_field(),
        DOMAIN.state_context_field(),
    ]
    feedback_state_output = DOMAIN.feedback_state_output_name()
    write_tsv(args.output_dir / feedback_state_output, feedback_state_rows, state_fieldnames)
    for legacy_name in DOMAIN.state_config().get("legacy_feedback_outputs", []):
        if legacy_name != feedback_state_output:
            write_tsv(args.output_dir / str(legacy_name), feedback_state_rows, state_fieldnames)

    event_fieldnames = [
        "event_id",
        "step",
        "event_type",
        "event_name",
        "intensity",
        "target",
        "direction",
        "source",
        "scope",
        DOMAIN.event_state_field(),
        *DOMAIN.legacy_event_state_fields(),
        *DOMAIN.event_input_fields(),
    ]
    write_tsv(args.output_dir / "feedback_events.tsv", feedback_events, event_fieldnames)
    write_combined_events(args.output_dir / "auto_events_with_feedback.tsv", auto_events, feedback_events)

    (args.output_dir / "feedback_manifest.json").write_text(
        json.dumps(
            {
                "kind": "agent_feedback_loop",
                "pack": str(args.pack) if args.pack else "",
                "scenario": args.scenario or "",
                "agent_turns": str(args.agent_turns),
                "societal_state_tsv": str(state_tsv),
                "auto_events_tsv": str(args.auto_events_tsv),
                "agent_panel_tsv": str(agent_panel_tsv) if agent_panel_tsv else "",
                "agent_source_tsv": [str(path) for path in agent_source_paths],
                "auxiliary_agents_tsv": str(auxiliary_agents_tsv) if auxiliary_agents_tsv else "",
                "inflow_templates_tsv": str(inflow_templates_tsv) if inflow_templates_tsv else "",
                "outputs": [
                    "agent_feedback.tsv",
                    feedback_state_output,
                    *DOMAIN.state_config().get("legacy_feedback_outputs", []),
                    "feedback_events.tsv",
                    "auto_events_with_feedback.tsv",
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Wrote {len(feedback_rows)} feedback rows and {len(feedback_events)} feedback events to {args.output_dir}")


if __name__ == "__main__":
    main()
