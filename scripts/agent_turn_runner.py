#!/usr/bin/env python3
"""Run domain-pack agent turns with an LLM backend.

The runner only understands neutral concepts: agents, events, state rows,
turns, and configured domain-pack labels.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, Iterable, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sim_core.domain_runtime import DomainRuntime

DEFAULT_OUTPUT = ROOT / "outputs" / "runs" / "domain_agent_turns"
DEFAULT_STATE_TSV: Path | None = None
DEFAULT_AUTO_EVENTS_TSV: Path | None = None
ACTION_CATEGORIES = {
    "observe",
    "share",
    "request_help",
    "assist",
    "withdraw",
    "rest",
    "adjust",
}
DOMAIN = DomainRuntime.load(None)


def read_tsv(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def write_tsv(path: Path, rows: Iterable[Dict[str, Any]], fieldnames: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, delimiter="\t", fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def parse_agent_ids(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def first_value(row: Dict[str, str], *keys: str, fallback: str = "") -> str:
    for key in keys:
        value = row.get(key, "")
        if value != "":
            return value
    return fallback


def agent_id_value(row: Dict[str, str]) -> str:
    for key in DOMAIN.agent_id_columns():
        if row.get(key):
            return row[key]
    return first_value(row, "agent_id")


def population_weight_value(row: Dict[str, str]) -> str:
    for key in DOMAIN.population_weight_columns():
        if row.get(key):
            return row[key]
    return first_value(row, "population_weight")


def select_agents(
    agent_rows: List[Dict[str, str]],
    wanted_ids: List[str],
) -> List[Dict[str, str]]:
    # If IDs are omitted, include all available agents in source order.
    if not wanted_ids:
        return agent_rows
    wanted = set(wanted_ids)
    order = {agent_id: index for index, agent_id in enumerate(wanted_ids)}
    rows = [row for row in agent_rows if agent_id_value(row) in wanted]
    return sorted(rows, key=lambda row: order.get(agent_id_value(row), 999))


def to_float(value: Any, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def event_input_text(row: Dict[str, str]) -> str:
    for field in DOMAIN.event_input_fields():
        if row.get(field):
            return row[field]
    return ""


def event_value(row: Dict[str, str], *keys: str, fallback: str = "") -> str:
    for key in keys:
        value = row.get(key, "")
        if value != "":
            return value
    return fallback


def read_agent_panel_overrides(path: Path | None) -> Dict[str, Dict[str, str]]:
    rows = read_optional_tsv(path)
    enabled_fields = DOMAIN.column_aliases("panel_enabled", ["enabled"])
    return {
        agent_id_value(row): row
        for row in rows
        if agent_id_value(row)
        and first_value(row, *enabled_fields, fallback="1") != "0"
    }


def apply_agent_panel_overrides(
    agents: List[Dict[str, str]],
    overrides_by_id: Dict[str, Dict[str, str]],
) -> List[Dict[str, str]]:
    if not overrides_by_id:
        return agents
    patched = []
    for row in agents:
        override = overrides_by_id.get(agent_id_value(row))
        if not override:
            patched.append(row)
            continue
        merged = dict(row)
        population_weight = first_value(
            override,
            *DOMAIN.column_aliases("panel_population_weight", ["population_weight"]),
        )
        if population_weight:
            merged["population_weight"] = population_weight
        merged["panel_display_weight"] = first_value(
            override,
            *DOMAIN.column_aliases("panel_display_weight", ["display_weight"]),
        )
        merged["panel_selection_reason"] = first_value(
            override,
            *DOMAIN.column_aliases("panel_selection_reason", ["selection_reason"]),
        )
        patched.append(merged)
    return patched


def filter_events_for_scenario(events: List[Dict[str, str]], scenario_mode: str) -> List[Dict[str, str]]:
    if scenario_mode in {"baseline", "control"}:
        mode_config = DOMAIN.pipeline.get("scenario_modes", {})
        intervention_type = "intervention"
        prefixes: list[str] = []
        if isinstance(mode_config, dict):
            intervention_type = str(mode_config.get("intervention_event_type", intervention_type))
            prefixes = [str(item) for item in mode_config.get("intervention_event_id_prefixes", prefixes)]
        return [
            event for event in events
            if event_value(event, "event_type", "type") != intervention_type
            and not any(
                event_value(event, "event_id").startswith(prefix)
                for prefix in prefixes
            )
        ]
    return events


def active_events(events: List[Dict[str, str]], step: int) -> List[Dict[str, str]]:
    active: List[Dict[str, str]] = []
    for event in events:
        try:
            start = int(event_value(event, "start_step"))
            end = int(event_value(event, "end_step"))
        except (KeyError, ValueError):
            continue
        if start <= step <= end:
            active.append(event)
    return active


def build_time_schedule_by_step(rows: List[Dict[str, str]]) -> Dict[int, Dict[str, str]]:
    schedule: Dict[int, Dict[str, str]] = {}
    for row in rows:
        try:
            step = int(float(row.get("step", "")))
        except ValueError:
            continue
        schedule[step] = row
    return schedule


def relative_year_for_step(step: int, schedule_by_step: Dict[int, Dict[str, str]]) -> float:
    row = schedule_by_step.get(step, {})
    try:
        return float(row.get("relative_year", ""))
    except ValueError:
        pass
    return 0.0


def to_int(value: str, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def age_band_for_step(
    lower_age: str,
    upper_age: str,
    step: int,
    schedule_by_step: Dict[int, Dict[str, str]],
) -> str:
    lower = to_int(lower_age)
    upper = to_int(upper_age)
    relative_year = int(relative_year_for_step(step, schedule_by_step))
    return f"{lower + relative_year}-{upper + relative_year}"


def midpoint_age(lower_age: str, upper_age: str) -> str:
    lower = to_float(lower_age)
    upper = to_float(upper_age, lower)
    return str(int((lower + upper) / 2 + 0.5))


def current_age_for_step(base_age: str, step: int, schedule_by_step: Dict[int, Dict[str, str]]) -> str:
    try:
        age = float(base_age)
    except ValueError:
        return ""
    return str(int(age + relative_year_for_step(step, schedule_by_step)))


def compact_auxiliary_agent_context(
    auxiliary_rows: List[Dict[str, str]] | None,
    inflow_rows: List[Dict[str, str]] | None,
    start_step: int,
    steps: int,
    schedule_by_step: Dict[int, Dict[str, str]],
) -> Dict[str, Any]:
    if not auxiliary_rows:
        return {}

    step_range = range(start_step, start_step + steps)
    groups = []
    for row in auxiliary_rows:
        group_id = first_value(row, "group_id", "id")
        groups.append({
            "group_id": group_id,
            "label": first_value(row, "label", "name", "display_name"),
            "representative_age": first_value(row, "representative_age", "age"),
            "weight": first_value(row, "weight", "population_weight"),
            "context": first_value(row, "context", "description"),
            "age_by_step": {
                str(step): age_band_for_step(
                    first_value(row, "min_age", "age_min"),
                    first_value(row, "max_age", "age_max"),
                    step,
                    schedule_by_step,
                )
                for step in step_range
            },
        })

    inflows = []
    for row in inflow_rows or []:
        inflows.append({
            "template_id": first_value(row, "template_id", "id"),
            "source_group_id": first_value(row, "source_group_id", "group_id"),
            "trigger_step": first_value(row, "trigger_step", "step"),
            "target_layer": first_value(row, "target_layer", "layer"),
            "display_policy": first_value(row, "display_policy", "name_policy"),
        })

    return {
        "policy": "auxiliary context only; do not create named agents unless configured by the pack",
        "groups": groups,
        "inflow_templates": inflows,
    }


def auxiliary_rows_by_id(rows: List[Dict[str, str]]) -> Dict[str, Dict[str, str]]:
    return {
        first_value(row, "group_id", "id"): row
        for row in rows
        if first_value(row, "group_id", "id")
    }


def evaluation_from_scores(pathway: float, support: float, trust: float) -> tuple[str, str]:
    labels = DOMAIN.evaluation_labels()
    label_names = list(labels) or ["positive", "neutral", "warning", "critical"]
    positive = label_names[0]
    neutral = label_names[1] if len(label_names) > 1 else positive
    warning = label_names[2] if len(label_names) > 2 else neutral
    critical = label_names[3] if len(label_names) > 3 else warning
    if pathway >= 60 and support >= 56 and trust >= 50:
        return positive, DOMAIN.default_emotion(positive, "positive")
    if pathway <= 38 and support <= 38:
        return critical, DOMAIN.default_emotion(critical, "critical")
    if trust <= 38 or pathway <= 48 or support <= 45:
        return warning, DOMAIN.default_emotion(warning, "warning")
    default_label = DOMAIN.default_evaluation(neutral)
    if default_label not in labels:
        default_label = neutral
    return default_label, DOMAIN.default_emotion(default_label, "neutral")


def build_inflow_agents(
    auxiliary_rows: List[Dict[str, str]],
    inflow_rows: List[Dict[str, str]],
    start_step: int,
    steps: int,
) -> List[Dict[str, str]]:
    if not auxiliary_rows or not inflow_rows:
        return []
    groups = auxiliary_rows_by_id(auxiliary_rows)
    agents: List[Dict[str, str]] = []
    end_step = start_step + steps - 1
    for template in inflow_rows:
        trigger_step = to_int(first_value(template, "trigger_step", "step"), 9999)
        if end_step < trigger_step:
            continue
        group = groups.get(first_value(template, "source_group_id", "group_id"))
        if not group:
            continue

        pathway = round(clamp(to_float(first_value(group, "pathway", "self_efficacy"), 50.0)))
        support = round(clamp(to_float(first_value(group, "support"), 50.0)))
        trust = round(clamp(to_float(first_value(group, "trust", "institutional_trust"), 50.0)))
        hope = round(clamp(to_float(first_value(group, "hope"), 50.0)))
        evaluation, emotion = evaluation_from_scores(pathway, support, trust)

        agents.append({
            "agent_id": first_value(template, "agent_id", "generated_agent_id", "id"),
            "name": first_value(template, "name", "display_name") or first_value(group, "name", "label"),
            "layer": first_value(template, "layer", "target_layer") or first_value(group, "layer"),
            "persona": first_value(template, "persona") or first_value(group, "persona", "description"),
            "age": first_value(group, "representative_age", "age") or midpoint_age(
                first_value(group, "min_age", "age_min"),
                first_value(group, "max_age", "age_max"),
            ),
            "self_efficacy": str(pathway),
            "institutional_trust": str(trust),
            "hope": str(hope),
            "initial_evaluation": evaluation,
            "initial_emotion": emotion,
            "pathway": str(pathway),
            "support": str(support),
            "intensity": first_value(group, "intensity", fallback="50"),
            "population_weight": first_value(group, "population_weight", "weight", fallback="1.0"),
            "influence_weight": first_value(group, "influence_weight", fallback="0.8"),
            "inflow_flag": "1",
            "source_group_id": first_value(template, "source_group_id", "group_id"),
            "inflow_template_id": first_value(template, "template_id", "id"),
            "inflow_trigger_step": str(trigger_step),
        })
    return [agent for agent in agents if agent_id_value(agent)]


def agent_layer_from_row(row: Dict[str, str], age_value: str = "") -> str:
    for column in DOMAIN.layer_column_candidates():
        value = row.get(column, "")
        if value:
            return value

    age = to_float(age_value or row.get("age"), -1.0)
    for band in DOMAIN.agent_layer_age_bands():
        try:
            lower = float(band.get("min", -1))
            upper = float(band.get("max", -1))
        except (TypeError, ValueError):
            continue
        if lower <= age <= upper:
            return str(band.get("label", "agent"))

    agent_id = agent_id_value(row)
    prefixes = DOMAIN.agent_layer_prefixes()
    for prefix, layer in sorted(prefixes.items(), key=lambda item: len(item[0]), reverse=True):
        if agent_id.startswith(prefix):
            return layer
    return "agent"


def agent_layer_for_age(agent_id: str, age_value: str) -> str:
    return agent_layer_from_row({"agent_id": agent_id}, age_value)


def compact_time_context(step: int, schedule_by_step: Dict[int, Dict[str, str]]) -> Dict[str, Any]:
    row = schedule_by_step.get(step, {})
    return {
        "step": step,
        "label": row.get("label", ""),
        "relative_year": row.get("relative_year", relative_year_for_step(step, schedule_by_step)),
        "unit": row.get("unit", ""),
        "phase": row.get("phase", ""),
        "description": row.get("description", ""),
    }


def compact_societal_state(row: Dict[str, str]) -> Dict[str, Any]:
    if not row:
        return {}
    fields = DOMAIN.state_fields()
    compact: Dict[str, Any] = {
        "dominant_state": row.get(DOMAIN.state_dominant_field(), ""),
        "high_risk_factors": row.get(DOMAIN.state_high_risk_field(), ""),
        "context": row.get(DOMAIN.state_context_field(), ""),
    }
    for field in fields:
        if row.get(field, "") != "":
            compact[field] = row[field]
    return compact


def build_prompt(
    agents: List[Dict[str, str]],
    events: List[Dict[str, str]],
    start_step: int,
    steps: int,
    societal_state_rows: List[Dict[str, str]] | None = None,
    auto_event_rows: List[Dict[str, str]] | None = None,
    previous_states: Dict[str, Dict[str, Any]] | None = None,
    time_schedule_rows: List[Dict[str, str]] | None = None,
    auxiliary_rows: List[Dict[str, str]] | None = None,
    inflow_rows: List[Dict[str, str]] | None = None,
    prompt_style: str = "neutral_v2",
) -> str:
    schedule_by_step = build_time_schedule_by_step(time_schedule_rows or [])
    auxiliary_context = compact_auxiliary_agent_context(
        auxiliary_rows,
        inflow_rows,
        start_step,
        steps,
        schedule_by_step,
    )
    compact_agents = []
    for row in agents:
        base_age = first_value(row, "age")
        current_age = current_age_for_step(base_age, start_step, schedule_by_step)
        age_by_step = {
            str(step): current_age_for_step(base_age, step, schedule_by_step)
            for step in range(start_step, start_step + steps)
        }
        reserved_fields = {
            "agent_id",
            "name",
            "layer",
            "persona",
            "age",
            "population_weight",
            "initial_evaluation",
            "initial_emotion",
            "pathway",
            "support",
            "intensity",
            "panel_display_weight",
            "panel_selection_reason",
            "inflow_flag",
            "source_group_id",
            "inflow_template_id",
            *DOMAIN.agent_id_columns(),
            *DOMAIN.population_weight_columns(),
            *DOMAIN.layer_column_candidates(),
        }
        compact_agent = {
            "id": agent_id_value(row),
            "name": first_value(row, "name"),
            "layer": agent_layer_from_row(row, current_age or first_value(row, "age")),
            "persona": first_value(row, "persona", "name"),
            "age": current_age or first_value(row, "age"),
            "base_age": base_age,
            "current_age": current_age or first_value(row, "age"),
            "age_by_step": age_by_step,
            "represented_population_weight_percent": population_weight_value(row),
            "panel_display_weight": row.get("panel_display_weight", ""),
            "panel_selection_reason": row.get("panel_selection_reason", ""),
            "initial_evaluation": first_value(row, "initial_evaluation"),
            "initial_emotion": first_value(row, "initial_emotion"),
            "pathway": first_value(row, "pathway"),
            "support": first_value(row, "support"),
            "intensity": first_value(row, "intensity"),
            "inflow_flag": row.get("inflow_flag", "0"),
            "source_group_id": row.get("source_group_id", ""),
            "inflow_template": row.get("inflow_template_id", ""),
            "attributes": {
                key: value
                for key, value in row.items()
                if value != "" and key not in reserved_fields
            },
        }
        compact_agents.append({
            key: value
            for key, value in compact_agent.items()
            if value not in ("", None, {}, [])
        })

    compact_previous = []
    previous_states = previous_states or {}
    for row in agents:
        previous = previous_states.get(agent_id_value(row))
        if not previous:
            continue
        compact_previous.append({
            "id": agent_id_value(row),
            "previous_step": previous.get("step", ""),
            "evaluation": previous.get("evaluation", ""),
            "emotion": previous.get("emotion", ""),
            "pathway": previous.get("pathway", ""),
            "support": previous.get("support", ""),
            "intensity": previous.get("intensity", ""),
            "action": previous.get("action", ""),
            "action_category": previous.get("action_category", ""),
            "action_detail": previous.get("action_detail", ""),
            "memory_update": previous.get("memory_update", ""),
            "carryover_concern": previous.get("carryover_concern", ""),
        })

    societal_state_by_step = {
        int(row["step"]): row
        for row in societal_state_rows or []
        if row.get("step")
    }
    auto_events_by_step: Dict[int, List[Dict[str, str]]] = {}
    for row in auto_event_rows or []:
        if not row.get("step"):
            continue
        auto_events_by_step.setdefault(int(row["step"]), []).append(row)

    compact_events = []
    for step in range(start_step, start_step + steps):
        compact_events.append({
            "step": step,
            "time": compact_time_context(step, schedule_by_step),
            "scheduled_events": [
                {
                    "id": event_value(event, "event_id"),
                    "type": event_value(event, "event_type", "type"),
                    "name": event_value(event, "event_name"),
                    "intensity": event_value(event, "intensity"),
                    "target": event_value(event, "target"),
                    "direction": event_value(event, "direction"),
                    "description": event_value(event, "description"),
                }
                for event in active_events(events, step)
            ],
            "state": compact_societal_state(societal_state_by_step.get(step, {})),
            "auto_events": [
                {
                    "id": event_value(event, "event_id"),
                    "type": event_value(event, "event_type", "type"),
                    "name": event_value(event, "event_name"),
                    "intensity": event_value(event, "intensity"),
                    "target": event_value(event, "target"),
                    "direction": event_value(event, "direction"),
                    "source": event_value(event, "source"),
                    "scope": event_value(event, "scope"),
                    "description": event_input_text(event),
                }
                for event in auto_events_by_step.get(step, [])
            ],
        })

    if prompt_style == "legacy_v1":
        prompt_config = DOMAIN.prompt_block("agent", "legacy_v1")
        opening_block = prompt_config.get("role") or (
            "あなたはドメインパック実験の観測器です。\n"
            "packで指定された言語で考え、出力はJSONだけにしてください。コードブロックは禁止です。"
        )
        intensity_block = ""
        priority_block = ""
    else:
        prompt_config = DOMAIN.prompt_block("agent", "neutral_v2")
        opening_block = prompt_config.get("role") or (
            "あなたはドメインパック実験の観測器です。\n"
            "packで指定された言語で推論し、出力はJSONのみを返してください（コードブロック禁止）。"
        )
        intensity_block = (
            "- event.intensity は圧力の強さを示す観測パラメータです。高強度では、防衛反応・相談遅延・短期志向の増加を許容してください。"
        )
        priority_block = (
            "観測優先順位:\n"
            "1) 指示の最適化ではなく、自然な反応分布を優先する\n"
            "2) 全員を協力方向へ誘導しない\n"
            "3) 不安・撤退・沈黙などの反応も、条件に整合するなら正当な観測値として扱う"
        )
    state_label = prompt_config.get("state_label") or DOMAIN.event_state_field()
    labels = DOMAIN.evaluation_labels() or {
        "positive": ["positive"],
        "neutral": ["neutral"],
        "warning": ["warning"],
        "critical": ["critical"],
    }
    label_lines = "\n".join(
        f"- {label}: {'・'.join(emotions)}" for label, emotions in labels.items()
    )
    evaluation_options = "/".join(labels)
    action_categories = DOMAIN.action_categories() or sorted(ACTION_CATEGORIES)
    action_category_text = "/".join(action_categories)
    action_example = action_categories[1] if len(action_categories) > 1 else action_categories[0]
    label_names = list(labels)
    example_evaluation = label_names[2] if len(label_names) > 2 else next(iter(labels), "neutral")
    example_emotion = DOMAIN.default_emotion(example_evaluation, "warning")

    return f"""
{opening_block}

	この実行で行うこと:
	エージェント本人へ命令するのではなく、固定属性・現在状態・外部状態・{state_label}・情報環境を観測条件として渡す。
	その条件に置かれた人物モデルが、次ステップでどう知覚し、どう感じ、どう考え、どう動いたかを row データとして記録する。

	入力情報の扱い:
	- state、scheduled_events、auto_events は、本人が置かれている状況です。本人への命令ではありません。
	- event.direction は「こう変化させろ」という指示ではなく、状態変化の説明ラベルです。
{intensity_block}
- previous_agent_state は、前ステップから残っている本人の記憶・状態です。これも命令ではありません。
- age/current_age/base_age は、packが年齢を持つ場合だけ参照してください。複数ステップを観測する場合は age_by_step を優先してください。
	- 背景・身体条件・関係性に応じて、距離感、相談、休息、共同作業への反応を変えてください。
	- auxiliary_context が空でない場合だけ、補助条件として扱ってください。
- その人が情報を見ない、見ても反応しない、別の生活課題を優先する、矛盾した反応をすることも自然なら許容します。
	- 同じ情報でも、背景、身体条件、過去の記憶、現在いる場所によって反応は分岐します。
{priority_block}

分類語彙:
{label_lines}

行動カテゴリ語彙:
{action_category_text}
これは分類用語彙であり、選ばせたい行動ではありません。自然に近いものを1つ選びます。

観測項目:
- evaluation: {evaluation_options}
- emotion: 分類語彙に対応する主感情
- pathway: 本人から見た選択可能性 0-100
- support: 本人から見た支援/足場の強さ 0-100
- intensity: 反応強度 0-100
- action: 短い行動ラベル
- action_category: 行動カテゴリ語彙から1つ
- action_detail: 観測可能な具体行動を1文で書く
- thought: 内心。本音で、まだ言語化しきれていない不安や怒りも含める
- private_talk: 友達や近い人との会話。砕けた口調で、弱音・相談・共感が出る
- social_post: 共有ログ/公開メモ。短く、他者に見られる前提の言い方にする
- perceived_situation: 本人がこのステップで実際に知覚した状況。届かなかった情報は無理に含めない
- reasoning_basis: その反応になった根拠。属性、生活制約、記憶、知覚した情報を短く結ぶ
- memory_update: 次ステップに残る記憶
- carryover_concern: 次ステップへ持ち越す懸念

観測原則:
- 望ましい発表ストーリーに合わせる必要はありません。
- 全員を同じ方向に動かさないでください。
- 大きな事件が起きても、本人に届かなければ反応は小さくてよいです。
- 逆に小さな出来事でも、その人の生活制約に刺されば大きく反応してよいです。
- 数値は前ステップから大きく変わってもよいですが、reasoning_basis と矛盾しない範囲にします。
- thought/private_talk/social_post は同じ内容の言い換えにしないでください。
- 発言はきれいに整理しすぎず、現実の人が言いそうな迷い・矛盾・言い切れなさを残します。
- thought は90字以内、private_talk は80字以内、social_post は60字以内にします。
- action_detail、perceived_situation、reasoning_basis、memory_update、carryover_concern は各80字以内にします。
- JSON以外の説明文は一切出力しないでください。

対象エージェント:
{json.dumps(compact_agents, ensure_ascii=False, indent=2)}

	補助条件:
	{json.dumps(auxiliary_context, ensure_ascii=False, indent=2)}

前ステップから残っている状態:
{json.dumps(compact_previous, ensure_ascii=False, indent=2)}

ステップ別の観測条件:
{json.dumps(compact_events, ensure_ascii=False, indent=2)}

JSON形式:
{{
  "turns": [
    {{
      "step": {start_step},
      "agents": [
        {{
	          "agent_id": "A001",
          "evaluation": "{example_evaluation}",
          "emotion": "{example_emotion}",
          "pathway": 40,
          "support": 36,
          "intensity": 70,
          "action": "警戒",
          "action_category": "{action_example}",
	          "action_detail": "混雑を避け、少し離れて様子を見る",
          "thought": "...",
          "private_talk": "...",
          "social_post": "...",
          "perceived_situation": "...",
          "reasoning_basis": "...",
          "memory_update": "...",
          "carryover_concern": "..."
        }}
      ]
    }}
  ]
}}
""".strip()


def extract_json_from_claude(stdout: str) -> Dict[str, Any]:
    outer = json.loads(stdout)
    result = outer.get("result", stdout)
    if isinstance(result, dict):
        return result
    text = str(result).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            raise
        return json.loads(match.group(0))


def run_claude(prompt: str, model: str, budget: float, timeout: int) -> Dict[str, Any]:
    cmd = [
        "claude",
        "-p",
        "--model",
        model,
        "--max-budget-usd",
        str(budget),
        "--tools",
        "",
        "--output-format",
        "json",
        prompt,
    ]
    completed = subprocess.run(
        cmd,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout)
    return extract_json_from_claude(completed.stdout)


def combine_payloads_by_step(payloads_by_agent: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    turns_by_step: Dict[int, List[Dict[str, Any]]] = {}
    for payload in payloads_by_agent.values():
        for turn in payload.get("turns", []):
            step = int(turn["step"])
            turns_by_step.setdefault(step, [])
            turns_by_step[step].extend(turn.get("agents", []))
    return {
        "turns": [
            {"step": step, "agents": sorted(turns_by_step[step], key=lambda item: item.get("agent_id", ""))}
            for step in sorted(turns_by_step)
        ],
        "raw_by_agent": payloads_by_agent,
    }


def flatten_turns(
    payload: Dict[str, Any],
    agents_by_id: Dict[str, Dict[str, str]],
    time_schedule_rows: List[Dict[str, str]] | None = None,
) -> List[Dict[str, Any]]:
    schedule_by_step = build_time_schedule_by_step(time_schedule_rows or [])
    rows: List[Dict[str, Any]] = []
    for turn in payload.get("turns", []):
        step = int(turn["step"])
        for item in turn.get("agents", []):
            agent = agents_by_id.get(item["agent_id"], {})
            current_age = current_age_for_step(agent.get("age", ""), step, schedule_by_step)
            rows.append({
                "step": step,
                "agent_id": item["agent_id"],
                "name": first_value(agent, "name"),
                "layer": agent_layer_from_row(agent, current_age or first_value(agent, "age")),
                "base_age": first_value(agent, "age"),
                "current_age": current_age or first_value(agent, "age"),
                "evaluation": normalize_evaluation(item.get("evaluation", "")),
                "emotion": normalize_emotion(item.get("evaluation", ""), item.get("emotion", "")),
                "pathway": round(clamp(float(item.get("pathway", 0))), 1),
                "support": round(clamp(float(item.get("support", 0))), 1),
                "intensity": round(clamp(float(item.get("intensity", 0))), 1),
                "action": item.get("action", ""),
                "action_category": normalize_action_category(
                    item.get("action_category", ""),
                    item.get("action", ""),
                ),
                "action_detail": item.get("action_detail", "") or action_detail_fallback(item.get("action", "")),
                "thought": item.get("thought", ""),
                "private_talk": item.get("private_talk", ""),
                "social_post": item.get("social_post", ""),
                "perceived_situation": item.get("perceived_situation", ""),
                "reasoning_basis": item.get("reasoning_basis", ""),
                "memory_update": item.get("memory_update", ""),
                "carryover_concern": item.get("carryover_concern", ""),
            })
    return rows


def normalize_evaluation(value: str) -> str:
    labels = DOMAIN.evaluation_labels()
    if labels and value in labels:
        return value
    return DOMAIN.default_evaluation("neutral")


def normalize_emotion(evaluation: str, emotion: str) -> str:
    normalized_evaluation = normalize_evaluation(evaluation)
    allowed = DOMAIN.evaluation_labels() or {
        "positive": {"positive"},
        "neutral": {"neutral"},
        "warning": {"warning"},
        "critical": {"critical"},
    }
    allowed_sets = {
        label: set(values)
        for label, values in allowed.items()
    }
    if emotion in allowed_sets.get(normalized_evaluation, set()):
        return emotion
    configured_default = DOMAIN.default_emotion(normalized_evaluation, "")
    if configured_default:
        return configured_default
    label_names = list(allowed)
    if normalized_evaluation in label_names:
        return next(iter(allowed_sets.get(normalized_evaluation, {"neutral"})), "neutral")
    return "neutral"


def normalize_action_category(value: str, action: str) -> str:
    categories = set(DOMAIN.action_categories() or ACTION_CATEGORIES)
    if value in categories:
        return value
    return infer_action_category(action)


def infer_action_category(action: str) -> str:
    categories = set(DOMAIN.action_categories() or ACTION_CATEGORIES)
    return next(iter(categories), "observe")


def action_detail_fallback(action: str) -> str:
    return action or "observe"


def latest_agent_states(rows: List[Dict[str, str]]) -> Dict[str, Dict[str, Any]]:
    latest: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        agent_id = row.get("agent_id", "")
        if not agent_id:
            continue
        step = int(float(row.get("step") or 0))
        current_step = int(float(latest.get(agent_id, {}).get("step") or -1))
        if step >= current_step:
            latest[agent_id] = dict(row)
    return latest


def run_stateful_turns(
    agents: List[Dict[str, str]],
    agents_by_id: Dict[str, Dict[str, str]],
    event_rows: List[Dict[str, str]],
    start_step: int,
    steps: int,
    societal_state_rows: List[Dict[str, str]],
    auto_event_rows: List[Dict[str, str]],
    time_schedule_rows: List[Dict[str, str]],
    auxiliary_rows: List[Dict[str, str]],
    inflow_rows: List[Dict[str, str]],
    initial_previous_states: Dict[str, Dict[str, Any]],
    model: str,
    budget: float,
    timeout: int,
    parallel_by_agent: bool,
    workers: int,
    prompt_style: str,
) -> Dict[str, Any]:
    previous_states: Dict[str, Dict[str, Any]] = dict(initial_previous_states)
    all_turns: List[Dict[str, Any]] = []
    raw_by_step: List[Dict[str, Any]] = []

    for step in range(start_step, start_step + steps):
        active_agents = [
            agent for agent in agents
            if to_int(agent.get("inflow_trigger_step", str(start_step)), start_step) <= step
        ]
        if parallel_by_agent:
            payloads_by_agent: Dict[str, Dict[str, Any]] = {}
            with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
                futures = {
                    executor.submit(
                        run_claude,
                        build_prompt(
                            [agent],
                            event_rows,
                            step,
                            1,
                            societal_state_rows,
                            auto_event_rows,
                            previous_states,
                            time_schedule_rows,
                            auxiliary_rows,
                            inflow_rows,
                            prompt_style,
                        ),
                        model,
                        budget,
                        timeout,
                    ): agent_id_value(agent)
                    for agent in active_agents
                }
                for future in as_completed(futures):
                    agent_id = futures[future]
                    payloads_by_agent[agent_id] = future.result()
                    print(f"Finished {agent_id} step {step}", flush=True)
            step_payload = combine_payloads_by_step(payloads_by_agent)
        else:
            prompt = build_prompt(
                active_agents,
                event_rows,
                step,
                1,
                societal_state_rows,
                auto_event_rows,
                previous_states,
                time_schedule_rows,
                auxiliary_rows,
                inflow_rows,
                prompt_style,
            )
            step_payload = run_claude(prompt, model, budget, timeout)
            print(f"Finished step {step}", flush=True)

        flat_rows = flatten_turns(step_payload, agents_by_id, time_schedule_rows)
        for row in flat_rows:
            previous_states[row["agent_id"]] = row

        all_turns.extend(step_payload.get("turns", []))
        raw_by_step.append({"step": step, "payload": step_payload})

    return {
        "turns": sorted(all_turns, key=lambda turn: int(turn["step"])),
        "raw_by_step": raw_by_step,
    }


def read_optional_tsv(path: Path | None) -> List[Dict[str, str]]:
    if not path or not path.exists():
        return []
    return read_tsv(path)


def max_schedule_step(rows: List[Dict[str, str]]) -> int | None:
    steps = []
    for row in rows:
        try:
            steps.append(int(float(row.get("step", ""))))
        except ValueError:
            continue
    return max(steps) if steps else None


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
    parser.add_argument("--start-step", type=int, default=4)
    parser.add_argument("--steps", type=int, default=3)
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--budget", type=float, default=0.50)
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--agents-tsv", type=Path)
    parser.add_argument("--agent-source-tsv", type=Path, action="append", default=[])
    parser.add_argument("--events-tsv", type=Path)
    parser.add_argument("--state-tsv", type=Path)
    parser.add_argument("--auto-events-tsv", type=Path)
    parser.add_argument("--agent-panel-tsv", type=Path)
    parser.add_argument("--time-schedule-tsv", type=Path)
    parser.add_argument("--auxiliary-agents-tsv", type=Path)
    parser.add_argument(
        "--inflow-templates-tsv",
        type=Path,
    )
    parser.add_argument("--agent-ids", default="")
    parser.add_argument("--parallel-by-agent", action="store_true")
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--stateful", action="store_true")
    parser.add_argument("--previous-agent-turns-tsv", type=Path)
    parser.add_argument(
        "--scenario-mode",
        choices=["baseline", "control", "all"],
        default=None,
        help="Filter scheduled events for comparison runs.",
    )
    parser.add_argument(
        "--prompt-style",
        choices=["legacy_v1", "neutral_v2"],
        default="neutral_v2",
        help="Prompt variant for ablation testing.",
    )
    args = parser.parse_args()

    DOMAIN = DomainRuntime.load(args.pack, scenario=args.scenario)
    data_config = DOMAIN.data
    runtime_config = DOMAIN.runtime

    explicit_agent_sources = [
        path for path in [args.agents_tsv, *args.agent_source_tsv]
        if path
    ]
    agent_source_paths = DOMAIN.agent_source_paths(explicit_agent_sources)
    events_tsv = args.events_tsv or DOMAIN.data_path("events")
    agent_panel_tsv = args.agent_panel_tsv or DOMAIN.panel_path()
    time_schedule_tsv = args.time_schedule_tsv or DOMAIN.data_path("time_schedule")
    auxiliary_agents_tsv = args.auxiliary_agents_tsv or DOMAIN.data_path("auxiliary_agents")
    inflow_templates_tsv = args.inflow_templates_tsv or DOMAIN.data_path("inflow_templates")
    scenario_mode = args.scenario_mode or DOMAIN.scenario_mode_default("all")

    if not agent_source_paths:
        raise SystemExit("Missing required input: agent sources (provide --pack pipeline.agents.sources or --agent-source-tsv)")
    agent_rows = [
        row
        for source_path in agent_source_paths
        for row in read_tsv(source_path)
    ]
    event_rows = filter_events_for_scenario(
        read_tsv(require_input_path(events_tsv, "--events-tsv")),
        scenario_mode,
    )
    state_tsv = args.state_tsv
    if state_tsv is None and not args.pack:
        state_tsv = DEFAULT_STATE_TSV
    societal_state_rows = read_optional_tsv(state_tsv)
    auto_events_tsv = args.auto_events_tsv
    if auto_events_tsv is None and not args.pack:
        auto_events_tsv = DEFAULT_AUTO_EVENTS_TSV
    auto_event_rows = read_optional_tsv(auto_events_tsv)
    time_schedule_rows = read_optional_tsv(time_schedule_tsv)
    max_step = max_schedule_step(time_schedule_rows)
    if max_step is not None and args.start_step + args.steps - 1 > max_step:
        raise SystemExit(
            f"Requested steps exceed time schedule: "
            f"start_step={args.start_step}, steps={args.steps}, max_step={max_step}"
        )
    auxiliary_rows = read_optional_tsv(auxiliary_agents_tsv)
    inflow_rows = read_optional_tsv(inflow_templates_tsv)
    agent_ids = parse_agent_ids(args.agent_ids)
    if not agent_ids and isinstance(runtime_config, dict):
        raw_default_agent_ids = runtime_config.get("default_agent_ids")
        if isinstance(raw_default_agent_ids, list):
            agent_ids = [str(item).strip() for item in raw_default_agent_ids if str(item).strip()]
        elif raw_default_agent_ids:
            agent_ids = [str(item).strip() for item in str(raw_default_agent_ids).split(",") if str(item).strip()]
    agents = select_agents(agent_rows, agent_ids)
    inflow_agents = build_inflow_agents(
        auxiliary_rows,
        inflow_rows,
        args.start_step,
        args.steps,
    )
    agents.extend(inflow_agents)
    if not args.stateful:
        agents = [
            agent for agent in agents
            if to_int(agent.get("inflow_trigger_step", str(args.start_step)), args.start_step) <= args.start_step
        ]
    panel_overrides = read_agent_panel_overrides(agent_panel_tsv)
    agents = apply_agent_panel_overrides(agents, panel_overrides)
    agents_by_id = {agent_id_value(row): row for row in agents}
    previous_agent_rows = read_optional_tsv(args.previous_agent_turns_tsv)
    previous_states = latest_agent_states(previous_agent_rows)

    if args.stateful:
        payload = run_stateful_turns(
            agents,
            agents_by_id,
            event_rows,
            args.start_step,
            args.steps,
            societal_state_rows,
            auto_event_rows,
            time_schedule_rows,
            auxiliary_rows,
            inflow_rows,
            previous_states,
            args.model,
            args.budget,
            args.timeout,
            args.parallel_by_agent,
            args.workers,
            args.prompt_style,
        )
    elif args.parallel_by_agent:
        payloads_by_agent: Dict[str, Dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
            futures = {
                executor.submit(
                    run_claude,
                    build_prompt(
                        [agent],
                        event_rows,
                        args.start_step,
                        args.steps,
                        societal_state_rows,
                        auto_event_rows,
                        None,
                        time_schedule_rows,
                        auxiliary_rows,
                        inflow_rows,
                        args.prompt_style,
                    ),
                    args.model,
                    args.budget,
                    args.timeout,
                ): agent_id_value(agent)
                for agent in agents
            }
            for future in as_completed(futures):
                agent_id = futures[future]
                payloads_by_agent[agent_id] = future.result()
                print(f"Finished {agent_id}", flush=True)
        payload = combine_payloads_by_step(payloads_by_agent)
    else:
        prompt = build_prompt(
            agents,
            event_rows,
            args.start_step,
            args.steps,
            societal_state_rows,
            auto_event_rows,
            None,
            time_schedule_rows,
            auxiliary_rows,
            inflow_rows,
            args.prompt_style,
        )
        payload = run_claude(prompt, args.model, args.budget, args.timeout)
    rows = flatten_turns(payload, agents_by_id, time_schedule_rows)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "raw_claude_response.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_jsonl(args.output_dir / "agent_states.jsonl", rows)
    write_tsv(
        args.output_dir / "turns.tsv",
        rows,
        [
            "step",
            "agent_id",
            "name",
            "layer",
            "base_age",
            "current_age",
            "evaluation",
            "emotion",
            "pathway",
            "support",
            "intensity",
            "action",
            "action_category",
            "action_detail",
            "thought",
            "private_talk",
            "social_post",
            "perceived_situation",
            "reasoning_basis",
            "memory_update",
            "carryover_concern",
        ],
    )
    manifest = {
        "kind": "domain_agent_turns",
        "model": args.model,
        "pack": str(args.pack) if args.pack else "",
        "scenario": args.scenario or "",
        "start_step": args.start_step,
        "steps": args.steps,
        "stateful": args.stateful,
        "scenario_mode": scenario_mode,
        "prompt_style": args.prompt_style,
        "parallel_by_agent": args.parallel_by_agent,
        "agent_source_tsv": [str(path) for path in agent_source_paths],
        "events_tsv": str(events_tsv),
        "state_tsv": str(state_tsv) if societal_state_rows else "",
        "auto_events_tsv": str(auto_events_tsv) if auto_event_rows and auto_events_tsv else "",
        "agent_panel_tsv": str(agent_panel_tsv) if panel_overrides and agent_panel_tsv else "",
        "time_schedule_tsv": str(time_schedule_tsv) if time_schedule_rows and time_schedule_tsv else "",
        "auxiliary_agents_tsv": str(auxiliary_agents_tsv) if auxiliary_rows and auxiliary_agents_tsv else "",
        "inflow_templates_tsv": str(inflow_templates_tsv) if inflow_rows and inflow_templates_tsv else "",
        "previous_agent_turns_tsv": str(args.previous_agent_turns_tsv or ""),
        "agents": list(agents_by_id),
        "inflow_agents": [agent_id_value(row) for row in inflow_agents],
        "outputs": ["turns.tsv", "agent_states.jsonl", "raw_claude_response.json"],
    }
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"Wrote {len(rows)} rows to {args.output_dir}")


if __name__ == "__main__":
    main()
