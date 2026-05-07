#!/usr/bin/env python3
"""Export ISS habitat UI frames from a domain pack.

This exporter is intentionally presentation-facing. It normalizes existing
domain-pack data into stable UI artifacts without changing the simulation
inputs: frames, positions, occupancy, sleep assignments, conversations, and
messages.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sim_core.domain_runtime import DomainRuntime


DOMAIN = DomainRuntime.load(None)
DEFAULT_OUTPUT = ROOT / "outputs" / "runs" / "domain_habitat_ui"


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


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


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


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def stable_noise(*parts: Any) -> float:
    text = "|".join(str(part) for part in parts)
    total = 0
    for index, char in enumerate(text):
        total += (index + 17) * ord(char)
    return math.sin(total * 12.9898) - math.floor(math.sin(total * 12.9898))


def agent_id(row: dict[str, str]) -> str:
    for column in DOMAIN.agent_id_columns():
        if row.get(column):
            return row[column]
    return first_value(row, "agent_id")


def event_start(row: dict[str, str]) -> int:
    return to_int(first_value(row, "start_step", "step"), 0)


def event_end(row: dict[str, str]) -> int:
    return to_int(first_value(row, "end_step"), event_start(row))


def event_intensity(row: dict[str, str]) -> float:
    value = to_float(row.get("intensity"), 0.0)
    return value if value <= 1.0 else value / 100.0


def active_events(events: list[dict[str, str]], step: int) -> list[dict[str, str]]:
    return [
        row for row in events
        if event_start(row) <= step <= max(event_start(row), event_end(row))
    ]


def parse_ids(value: str) -> list[str]:
    normalized = value.replace(",", ";").replace("[", "").replace("]", "").replace('"', "").replace("'", "")
    return [item.strip() for item in normalized.split(";") if item.strip()]


def parse_agent_ids(value: str) -> list[str]:
    return [item for item in parse_ids(value) if item.startswith("ISS")]


def read_state_by_step(path: Path | None) -> dict[int, dict[str, str]]:
    return {
        to_int(row.get("step"), 0): row
        for row in read_tsv(path)
        if row.get("step")
    }


def step_count(time_schedule: list[dict[str, str]]) -> int:
    configured = DOMAIN.config.get("time", {}) if isinstance(DOMAIN.config.get("time"), dict) else {}
    schedule_steps = [to_int(row.get("step"), 0) for row in time_schedule]
    return max([to_int(configured.get("steps"), 0), *schedule_steps, 1])


def index_by(rows: list[dict[str, str]], key: str) -> dict[str, dict[str, str]]:
    return {row[key]: row for row in rows if row.get(key)}


def place_point(place: dict[str, str], slot: int, step: int) -> dict[str, float]:
    center_x = to_float(place.get("center_x"), 0.0)
    center_y = to_float(place.get("center_y"), 0.0)
    half = max(1.0, to_float(place.get("half_size"), 4.0))
    angle = ((slot * 137 + step * 19) % 360) * math.pi / 180
    radius = half * (0.22 + (slot % 4) * 0.13)
    # Convert small domain coordinates to UI-friendly percentage coordinates.
    x = 50 + (center_x + math.cos(angle) * radius) * 1.55
    y = 50 - (center_y + math.sin(angle) * radius) * 1.55
    return {"x": round(clamp(x, 4, 96), 2), "y": round(clamp(y, 4, 96), 2)}


def event_module(event: dict[str, str], objects_by_id: dict[str, dict[str, str]]) -> str:
    event_id = event.get("event_id", "")
    event_name = event.get("event_name", "")
    target = event.get("target", "")
    if event_id in objects_by_id:
        return first_value(objects_by_id[event_id], "place_key").split(";")[0]
    if target and not target.startswith("ISS"):
        return target.split(";")[0]
    if "キューポラ" in event_name:
        return "cupola"
    if "個室" in event_name:
        return "crew_quarters"
    if "運動" in event_name:
        return "exercise_area"
    if "資源" in event_name or "声量" in event_name:
        return "common_area"
    if "帰還" in event_name:
        return "cupola"
    return "common_area"


def active_incidents(
    events_for_step: list[dict[str, str]],
    objects_by_id: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    incidents: list[dict[str, Any]] = []
    for event in events_for_step:
        event_type = event.get("event_type", "")
        if event_type not in {"conflict", "repair"}:
            continue
        participants = parse_agent_ids(event.get("target", ""))
        intensity = event_intensity(event)
        status = "repair" if event_type == "repair" else "active"
        severity = "repair" if event_type == "repair" else ("danger" if intensity >= 0.65 else "caution")
        incidents.append({
            "event_id": event.get("event_id", ""),
            "participant_ids": participants,
            "module_id": event_module(event, objects_by_id),
            "severity": severity,
            "status": status,
            "label": event.get("event_name", ""),
        })
    return incidents


def preferred_module(
    agent: dict[str, str],
    agent_index: int,
    step: int,
    events_for_step: list[dict[str, str]],
    objects_by_id: dict[str, dict[str, str]],
) -> str:
    current_id = agent_id(agent)
    for incident in active_incidents(events_for_step, objects_by_id):
        if current_id in incident["participant_ids"]:
            return str(incident["module_id"])
    priority_ids = parse_ids(first_value(schedule_by_step_cache.get(step, {}), "private_room_priority"))
    if current_id in priority_ids and stable_noise(current_id, step, "private") < 0.45:
        return "crew_quarters"
    roll = stable_noise(current_id, step)
    if roll < 0.07:
        return "crew_quarters"
    if roll < 0.20:
        return "cupola"
    if roll < 0.34:
        return "exercise_area"
    if roll < 0.74:
        return "common_area"
    return "lab_module" if (agent_index + step) % 2 else "hab_module"


schedule_by_step_cache: dict[int, dict[str, str]] = {}


def stress_score(
    agent: dict[str, str],
    step: int,
    events_for_step: list[dict[str, str]],
    state_row: dict[str, str],
) -> float:
    base = to_float(agent.get("baseline_stress"), 5.0) * 10
    pressure_fields = [
        "confinement_stress",
        "resource_pressure",
        "privacy_pressure",
        "interpersonal_tension",
        "routine_fatigue",
        "communication_delay",
    ]
    pressure = sum(to_float(state_row.get(field), 50.0) for field in pressure_fields) / max(len(pressure_fields), 1)
    conflict_boost = sum(
        event_intensity(event) * 22
        for event in events_for_step
        if event.get("event_type") == "conflict" and agent_id(agent) in parse_ids(event.get("target", ""))
    )
    repair_relief = sum(
        event_intensity(event) * 10
        for event in events_for_step
        if event.get("event_type") == "repair" and agent_id(agent) in parse_ids(event.get("target", ""))
    )
    object_relief = sum(
        event_intensity(event) * 2.2
        for event in events_for_step
        if event.get("event_type") == "object"
    )
    noise = (stable_noise(agent_id(agent), step, "stress") - 0.5) * 7
    return round(clamp(base * 0.58 + pressure * 0.42 + conflict_boost - repair_relief - object_relief + noise), 1)


def evaluation_from_stress(stress: float) -> str:
    if stress >= 78:
        return "危険"
    if stress >= 58:
        return "注意"
    if stress <= 36:
        return "良好"
    return "中立"


def module_occupancy(states: list[dict[str, Any]], places: list[dict[str, str]]) -> list[dict[str, Any]]:
    rows = []
    for place in places:
        module_id = place.get("place_name", "")
        capacity = max(1, to_int(place.get("capacity"), 1))
        occupancy = sum(1 for state in states if state["module_id"] == module_id)
        ratio = occupancy / capacity
        rows.append({
            "module_id": module_id,
            "occupancy": occupancy,
            "capacity": capacity,
            "crowding_level": "over" if ratio > 1 else "high" if ratio >= 0.75 else "normal",
            "is_over_capacity": ratio > 1,
        })
    return rows


def module_oxygen_measurements(events_for_step: list[dict[str, str]], places: list[dict[str, str]], step: int) -> dict[str, float]:
    """Return module_id -> oxygen_percent measurements for this step.

    The engine does not currently simulate oxygen explicitly, so this exporter
    emits a stable "measurement" field derived from scheduled events.

    Viewer logic can then conditionally show alarms only when oxygen_percent < 50.
    """
    modules = {place.get("place_name", "") for place in places if place.get("place_name")}
    values = {module_id: 100.0 for module_id in modules}
    event_ids = {row.get("event_id", "") for row in events_for_step}

    if "DEBR04" in event_ids:
        # HAB oxygen leak window (60-64). Make it visibly under 50% and worsening.
        leak_progress = clamp((step - 60) / 4.0, 0.0, 1.0)
        values["hab_module"] = round(48.0 - leak_progress * 8.0, 1)  # 48 -> 40

    if "DEBR05" in event_ids:
        # LAB oxygen decline (65-100). Starts under 50 and drifts downward.
        decline_progress = clamp((step - 65) / 35.0, 0.0, 1.0)
        values["lab_module"] = round(clamp(49.0 - decline_progress * 22.0, 12.0, 100.0), 1)  # 49 -> 27

    # Defensive: ensure we always have keys for main modules even if places changed.
    values.setdefault("hab_module", 100.0)
    values.setdefault("lab_module", 100.0)
    return values


def active_objects(
    events_for_step: list[dict[str, str]],
    objects_by_id: dict[str, dict[str, str]],
    places_by_id: dict[str, dict[str, str]],
    step: int,
) -> list[dict[str, Any]]:
    rows = []
    for event in events_for_step:
        if event.get("event_type") != "object":
            continue
        obj = objects_by_id.get(event.get("event_id", ""), {})
        module_id = first_value(obj, "place_key", fallback=event.get("target", "common_area")).split(";")[0]
        point = place_point(places_by_id.get(module_id, {}), len(rows), step)
        rows.append({
            "object_id": event.get("event_id", ""),
            "object_name": first_value(obj, "object_name", fallback=event.get("event_name", "")),
            "module_id": module_id,
            "x": point["x"],
            "y": point["y"],
            "effect": event.get("direction", ""),
            "is_active": True,
        })
    return rows


def active_event_payload(events_for_step: list[dict[str, str]]) -> list[dict[str, Any]]:
    return [
        {
            "event_id": event.get("event_id", ""),
            "event_type": event.get("event_type", ""),
            "event_name": event.get("event_name", ""),
            "target": event.get("target", ""),
            "intensity": event.get("intensity", ""),
            "status": "active",
        }
        for event in events_for_step
    ]


def timeline_type(event_type: str) -> str:
    if event_type == "object":
        return "nudge"
    if event_type == "conflict":
        return "conflict"
    if event_type == "repair":
        return "repair"
    return "baseline"


def infer_related_object_id(event: dict[str, str], object_ids: set[str]) -> str:
    event_id = event.get("event_id", "")
    if event.get("event_type") == "object" and event_id in object_ids:
        return event_id

    text = " ".join(
        [
            event.get("event_name", ""),
            event.get("direction", ""),
            event.get("description", ""),
            event.get("target", ""),
        ]
    )
    patterns = [
        ("OBJ07", ["持ち寄り棚", "記憶共有", "故郷", "経験共有", "棚"]),
        ("OBJ09", ["個室聖域", "一人時間", "プライバシー", "個室"]),
        ("OBJ06", ["話しかけてOK", "声かけ許可", "声かけ", "キューポラ"]),
        ("OBJ03", ["リソース", "資源", "スコア", "共同達成"]),
        ("OBJ10", ["移動投票", "移動", "運動枠", "順番合意"]),
    ]
    for object_id, keywords in patterns:
        if object_id in object_ids and any(keyword in text for keyword in keywords):
            return object_id
    return ""


def build_event_timeline(
    events: list[dict[str, str]],
    objects_by_id: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    object_ids = {event.get("event_id", "") for event in events if event.get("event_type") == "object"}
    ordered_events = sorted(events, key=lambda row: (event_start(row), event_end(row), row.get("event_id", "")))

    for event in ordered_events:
        event_type = event.get("event_type", "")
        participants = parse_agent_ids(event.get("target", ""))
        related_conflict_id = ""
        if event_type == "repair":
            event_name = event.get("event_name", "").replace("の修復", "")
            participant_set = set(participants)
            for conflict in reversed(conflicts):
                conflict_participants = set(parse_ids(str(conflict.get("participant_ids", ""))))
                same_pair = bool(participant_set and conflict_participants and participant_set == conflict_participants)
                same_topic = event_name and event_name in str(conflict.get("label", ""))
                if same_pair or same_topic:
                    related_conflict_id = str(conflict.get("event_id", ""))
                    break

        related_object_id = "" if event_type == "baseline" else infer_related_object_id(event, object_ids)
        module_id = "" if event_type == "baseline" else event_module(event, objects_by_id)
        row = {
            "event_id": event.get("event_id", ""),
            "start_step": event_start(event),
            "end_step": event_end(event),
            "event_type": event_type,
            "timeline_type": timeline_type(event_type),
            "label": event.get("event_name", ""),
            "module_id": module_id,
            "participant_ids": ";".join(participants),
            "related_object_id": related_object_id,
            "related_conflict_id": related_conflict_id,
            "intensity": event.get("intensity", ""),
            "direction": event.get("direction", ""),
            "description": event.get("description", ""),
        }
        rows.append(row)
        if event_type == "conflict":
            conflicts.append(row)
    return rows


def nudge_effect_summary(object_row: dict[str, Any], related_rows: list[dict[str, Any]], object_record: dict[str, str]) -> str:
    repaired = [row for row in related_rows if row.get("event_type") == "repair"]
    conflicts = [row for row in related_rows if row.get("event_type") == "conflict"]
    if repaired:
        labels = "、".join(str(row.get("label", "")) for row in repaired[:2])
        return f"{labels}を修復イベントとして接続。会話量や距離の取り方が調整された。"
    if conflicts:
        labels = "、".join(str(row.get("label", "")) for row in conflicts[:2])
        return f"{labels}に関連。摩擦の発火後に、修復の入口になるかを観測中。"
    return first_value(object_record, "description_text", fallback=str(object_row.get("direction", "")))


def build_nudge_effects(
    timeline_rows: list[dict[str, Any]],
    objects_by_id: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    object_rows = [row for row in timeline_rows if row.get("event_type") == "object"]
    effects: list[dict[str, Any]] = []
    for object_row in object_rows:
        object_id = str(object_row.get("event_id", ""))
        related_rows = [
            row for row in timeline_rows
            if row.get("related_object_id") == object_id and row.get("event_id") != object_id
        ]
        affected_agent_ids = sorted({
            participant_id
            for row in related_rows
            for participant_id in parse_ids(str(row.get("participant_ids", "")))
            if participant_id
        })
        repaired_rows = [row for row in related_rows if row.get("event_type") == "repair"]
        conflict_rows = [row for row in related_rows if row.get("event_type") == "conflict"]
        repair_steps = [
            to_int(row.get("start_step"), 0)
            for row in repaired_rows
            if to_int(row.get("start_step"), 0) > 0
        ]
        first_repair_step = min(repair_steps) if repair_steps else ""
        path_parts = [f"S{int(object_row.get('start_step', 0)):02d} {object_row.get('label', object_id)}"]
        for row in related_rows[:3]:
            path_parts.append(f"S{int(row.get('start_step', 0)):02d} {row.get('label', row.get('event_id', ''))}")
        object_record = objects_by_id.get(object_id, {})
        effects.append({
            "effect_id": f"effect_{object_id}",
            "object_id": object_id,
            "object_name": first_value(object_record, "object_name", fallback=str(object_row.get("label", object_id))),
            "start_step": object_row.get("start_step", ""),
            "module_id": object_row.get("module_id", ""),
            "target_event_ids": ";".join(str(row.get("event_id", "")) for row in related_rows),
            "repaired_event_ids": ";".join(str(row.get("event_id", "")) for row in repaired_rows),
            "conflict_event_ids": ";".join(str(row.get("event_id", "")) for row in conflict_rows),
            "affected_agent_ids": ";".join(affected_agent_ids),
            "first_repair_step": first_repair_step,
            "status": "effective" if repaired_rows else "observing",
            "effect_summary": nudge_effect_summary(object_row, related_rows, object_record),
            "effect_path": " → ".join(path_parts),
        })
    return effects


def sleep_assignments(agents: list[dict[str, str]], step: int) -> list[dict[str, Any]]:
    ids = [agent_id(agent) for agent in agents]
    permanent = ids[:7]
    temporary = ids[7:]
    if temporary:
        offset = step % len(temporary)
        temporary = temporary[offset:] + temporary[:offset]
    rows = [
        {"slot_id": f"CQ{index + 1}", "agent_id": current_id, "is_temporary": False}
        for index, current_id in enumerate(permanent)
    ]
    temp_slots = ["Kibo", "Dragon", "Airlock"]
    rows.extend(
        {"slot_id": temp_slots[index], "agent_id": current_id, "is_temporary": True}
        for index, current_id in enumerate(temporary[: len(temp_slots)])
    )
    return rows


def agent_name(agent: dict[str, str]) -> str:
    return first_value(agent, "name", fallback=agent_id(agent))


def agent_style(agent: dict[str, str]) -> str:
    return first_value(agent, "communication_style", fallback="通常")


def agent_vulnerability(agent: dict[str, str]) -> str:
    return first_value(agent, "vulnerability_note", fallback="明示的な脆弱性メモなし")


def choose_variant(options: list[Any], *parts: Any) -> Any:
    if not options:
        return ""
    index = int(stable_noise(*parts) * len(options)) % len(options)
    return options[index]


def style_ack(agent: dict[str, str]) -> str:
    style = agent_style(agent)
    if "短く" in style or "控えめ" in style:
        return "了解。要点だけで。"
    if "内省" in style:
        return "少し考えたいけど、確認なら聞ける。"
    if "慎重" in style:
        return "責める話でないなら、少しだけ。"
    if "防衛" in style:
        return "評価の話じゃないなら聞く。"
    if "温か" in style:
        return "うん、無理ない範囲で合わせよう。"
    if "率直" in style:
        return "手順が見えれば合わせる。"
    if "表現" in style:
        return "いいよ。空気が重くならない形にしよう。"
    if "穏や" in style:
        return "急がず、必要な一点からにしよう。"
    return "分かった。短く確認しよう。"


def module_routine_templates(module_id: str, run_id: str, step: int) -> list[tuple[str, str, str]]:
    run_b = "_b" in run_id or run_id.endswith("b_smoke") or "nudge" in run_id
    templates = {
        "common_area": [
            ("食事と片付け", "夕食後の片付け、今日はどこまで一緒にやる？", "水の記録を見てからなら手伝える。"),
            ("持ち寄り棚", "棚に置いてあった写真、少し聞いてもいい？", "長くは話せないけど、一つだけなら。"),
            ("共用部の声量", "ここ、声が響くね。少し端に寄って話す？", "その方が助かる。明るい話題自体は嫌じゃない。"),
            ("補給品の置き場所", "補給品の袋、誰がどこに戻すか決めておこう。", "ラベルを見える向きにしておけば迷わない。"),
        ],
        "exercise_area": [
            ("運動枠", "運動枠、先に使う？それとも5分ずらす？", "5分ならずらせる。終わったらログを残す。"),
            ("身体ルーティン", "今日の負荷、少し強すぎない？", "強いけど必要。終わったら静かな場所に戻る。"),
            ("順番調整", "次の人が待っているから、終了時刻だけ合わせたい。", "分かった。タイマーを見える場所に置く。"),
        ],
        "cupola": [
            ("地球を見る時間", "ここにいる時は、話すより見る時間を優先したい？", "うん。でも短い問いなら答えられる。"),
            ("声かけサイン", "この席、話しかけていい合図として使えている？", "合図があると、断っても責められにくい。"),
            ("故郷の話", "地球を見ると、どの場所を思い出す？", "今日はまだ言葉にしにくい。隣にいるだけならいい。"),
        ],
        "crew_quarters": [
            ("一人時間", "入口の印が出ている間は、声を落としてもらえる？", "分かった。出る時刻だけメモしておく。"),
            ("静音時間", "夜の前に、固定具の音だけ直していい？", "助かる。静かな時間の前に終わらせよう。"),
            ("個室待ち", "個室を使う順番、感情じゃなく時間で決めよう。", "それなら待てる。責められている感じが少ない。"),
        ],
        "lab_module": [
            ("実験ログ", "実験ログの引き継ぎ、1点だけ確認したい。", "記録の順番だね。ここに書いておく。"),
            ("作業分担", "この作業、二人で分けた方が早い？", "私は確認側に回る。手順だけ共有して。"),
            ("資源スコア", "スコアは責任追及じゃなく、残量の見える化として見たい。", "その言い方なら受け取りやすい。"),
        ],
        "hab_module": [
            ("生活区画", "寝袋まわりの荷物、通路側だけ寄せておく？", "そうしよう。夜に浮くと危ない。"),
            ("移動のきっかけ", "投票パネルの質問、別モジュールで答える形式でいい？", "こもりっぱなしより、その方が動きやすい。"),
            ("空調と音", "空調音が気になる人、今日は多そうだね。", "静かな作業を後ろに回した方がよさそう。"),
        ],
    }
    selected = list(templates.get(module_id, templates["common_area"]))
    if run_b and step >= 6 and module_id == "common_area":
        selected.append(("ナッジ接点", "棚の前なら、用件だけじゃない話も始めやすいね。", "物があると、いきなり自分の話をしなくてすむ。"))
    if run_b and step >= 11 and module_id == "cupola":
        selected.append(("OKサイン", "サインが出ている時だけ声をかける、で合ってる？", "うん。答えない自由も残るなら安心する。"))
    if run_b and step >= 18 and module_id == "crew_quarters":
        selected.append(("聖域マーク", "マークが出ている時は、急ぎ以外は後に回すね。", "それだけで休むことへの罪悪感が少し減る。"))
    if run_b and step >= 24 and module_id in {"common_area", "lab_module"}:
        selected.append(("共同スコア", "残量表示、個人の失敗じゃなく全員の調整として読もう。", "その見方なら、数字を見ても身構えにくい。"))
    if run_b and step >= 28 and module_id in {"lab_module", "hab_module"}:
        selected.append(("移動投票", "投票の質問、次は別モジュールで答えてみる？", "移動する理由があるなら行きやすい。"))
    return selected


def routine_lines(
    a: str,
    b: str,
    module_id: str,
    step: int,
    run_id: str,
    state_by_agent: dict[str, dict[str, Any]],
    agents_by_id: dict[str, dict[str, str]],
) -> tuple[str, list[dict[str, str]]]:
    a_agent = agents_by_id.get(a, {"agent_id": a})
    b_agent = agents_by_id.get(b, {"agent_id": b})
    a_name = agent_name(a_agent)
    b_name = agent_name(b_agent)
    a_stress = to_float(state_by_agent.get(a, {}).get("stress"), 50.0)
    b_stress = to_float(state_by_agent.get(b, {}).get("stress"), 50.0)
    topic, opener, reply = choose_variant(module_routine_templates(module_id, run_id, step), a, b, module_id, step)

    if a_stress >= 70:
        opener = f"{b_name}、今は長く話す余裕がない。{topic}だけ確認したい。"
    elif b_stress >= 70:
        opener = f"{b_name}、負荷が高そうに見える。{topic}は後に回す？"
    elif "防衛" in agent_style(a_agent):
        opener = f"{b_name}、確認だけなら話せる。{topic}のこと。"
    elif "慎重" in agent_style(a_agent):
        opener = f"{b_name}、責める話ではないんだけど、{topic}を少し確認したい。"

    if b_stress >= 70:
        reply = f"{style_ack(b_agent)}長い説明は後にして、{topic}だけ決めたい。"
    elif "温か" in agent_style(b_agent) and a_stress >= 58:
        reply = f"{style_ack(b_agent)}無理に答えなくていい形で、{topic}だけ進めよう。"
    elif "短く" in agent_style(b_agent) or "控えめ" in agent_style(b_agent):
        reply = style_ack(b_agent)

    if not opener.startswith(f"{b_name}、"):
        opener = f"{b_name}、{opener}"
    if not reply.startswith(f"{a_name}、"):
        reply = f"{a_name}、{reply}"

    return topic, [
        {"speaker_id": a, "listener_ids": b, "utterance": opener},
        {"speaker_id": b, "listener_ids": a, "utterance": reply},
    ]


def event_ids_for_agent(events_for_step: list[dict[str, str]], current_agent_id: str) -> list[str]:
    return [
        event.get("event_id", "")
        for event in events_for_step
        if current_agent_id in parse_ids(event.get("target", ""))
    ]


def emotion_observation(
    agent: dict[str, str],
    stress: float,
    module_id: str,
    events_for_step: list[dict[str, str]],
) -> dict[str, Any]:
    current_agent_id = agent_id(agent)
    related_events = [
        event for event in events_for_step
        if current_agent_id in parse_ids(event.get("target", ""))
    ]
    conflict = next((event for event in related_events if event.get("event_type") == "conflict"), None)
    repair = next((event for event in related_events if event.get("event_type") == "repair"), None)
    base_emotion = first_value(agent, "initial_emotion", fallback="平静")
    name = agent_name(agent)
    vulnerability = agent_vulnerability(agent)

    if conflict:
        label = "緊張" if stress < 78 else base_emotion
        return {
            "label": label,
            "summary": f"{name}は「{conflict.get('event_name', '摩擦')}」で警戒が上がっている。",
            "detail": (
                f"{module_id}で起きた出来事が、{name}の弱点である「{vulnerability}」に触れている。"
                "これは観測発話とeventからの推定で、本人の内面rawではない。"
            ),
            "source": "derived_emotion",
            "evidence_event_ids": event_ids_for_agent(events_for_step, current_agent_id),
        }
    if repair:
        return {
            "label": "緊張緩和",
            "summary": f"{name}は修復の入口を見つけ、反応の強さが少し下がっている。",
            "detail": (
                f"「{repair.get('event_name', '修復')}」により、相手との距離や言い方を再調整している。"
                f"ただし基礎感情は「{base_emotion}」として残る。"
            ),
            "source": "derived_emotion",
            "evidence_event_ids": event_ids_for_agent(events_for_step, current_agent_id),
        }
    if stress >= 78:
        return {
            "label": base_emotion,
            "summary": f"{name}は高ストレスで、人との距離と静けさを強く必要としている。",
            "detail": f"{module_id}滞在、baseline_stress、privacy_needからの推定。脆弱性メモ: {vulnerability}",
            "source": "derived_emotion",
            "evidence_event_ids": event_ids_for_agent(events_for_step, current_agent_id),
        }
    if stress >= 58:
        return {
            "label": base_emotion,
            "summary": f"{name}は注意域にあり、短い確認なら応答できるが負荷は残っている。",
            "detail": f"初期感情「{base_emotion}」と現在ストレスからの推定。会話量の調整が必要。",
            "source": "derived_emotion",
            "evidence_event_ids": event_ids_for_agent(events_for_step, current_agent_id),
        }
    return {
        "label": base_emotion,
        "summary": f"{name}は比較的落ち着いており、場の流れを観察できている。",
        "detail": f"初期感情「{base_emotion}」をベースに、現在ストレスが低い状態として推定。",
        "source": "derived_emotion",
        "evidence_event_ids": event_ids_for_agent(events_for_step, current_agent_id),
    }


def action_observation(
    agent: dict[str, str],
    stress: float,
    module_id: str,
    events_for_step: list[dict[str, str]],
) -> dict[str, Any]:
    current_agent_id = agent_id(agent)
    related_events = [
        event for event in events_for_step
        if current_agent_id in parse_ids(event.get("target", ""))
    ]
    conflict = next((event for event in related_events if event.get("event_type") == "conflict"), None)
    repair = next((event for event in related_events if event.get("event_type") == "repair"), None)
    name = agent_name(agent)
    style = agent_style(agent)

    if conflict:
        return {
            "category": "距離を取る" if stress >= 78 else "境界を示す",
            "summary": f"{name}は会話を短く切り、相手との境界を出している。",
            "detail": f"発話スタイルは「{style}」。{conflict.get('event_name', '摩擦')}への反応として推定。",
            "source": "derived_action",
            "evidence_event_ids": event_ids_for_agent(events_for_step, current_agent_id),
        }
    if repair:
        return {
            "category": "修復を試す",
            "summary": f"{name}は謝罪や確認ではなく、次の行動を小さく変えようとしている。",
            "detail": f"{repair.get('event_name', '修復')}により、長い説明よりも場所・順番・声量の調整へ移っている。",
            "source": "derived_action",
            "evidence_event_ids": event_ids_for_agent(events_for_step, current_agent_id),
        }
    if module_id == "crew_quarters":
        return {
            "category": "一人で整える",
            "summary": f"{name}は個室ブースで刺激を減らし、次の共同作業に戻る準備をしている。",
            "detail": f"privacy_needと現在位置からの推定。会話拒否ではなく回復行動として扱う。",
            "source": "derived_action",
            "evidence_event_ids": event_ids_for_agent(events_for_step, current_agent_id),
        }
    if stress >= 58:
        return {
            "category": "短く相談",
            "summary": f"{name}は長話を避け、必要な確認だけを行っている。",
            "detail": f"注意域ストレスと発話スタイル「{style}」からの推定。支援は短く具体的な方が自然。",
            "source": "derived_action",
            "evidence_event_ids": event_ids_for_agent(events_for_step, current_agent_id),
        }
    return {
        "category": "観察",
        "summary": f"{name}は場の混雑や相手の様子を見ながら通常行動を続けている。",
        "detail": f"{module_id}での滞在と低〜中ストレスからの推定。",
        "source": "derived_action",
        "evidence_event_ids": event_ids_for_agent(events_for_step, current_agent_id),
    }


def conversation_summary_detail(
    event: dict[str, str],
    participants: list[str],
    agents_by_id: dict[str, dict[str, str]],
    module_id: str,
) -> tuple[str, str]:
    names = [agent_name(agents_by_id.get(current_id, {"agent_id": current_id})) for current_id in participants[:2]]
    profiles = [
        f"{name}: {agent_style(agents_by_id.get(current_id, {}))} / {agent_vulnerability(agents_by_id.get(current_id, {}))}"
        for name, current_id in zip(names, participants[:2])
    ]
    event_name = event.get("event_name", "会話")
    direction = event.get("direction") or event.get("description") or ""
    if event.get("event_type") == "repair":
        summary = f"{'と'.join(names)}が「{event_name}」の後、距離と言い方を戻し始める。"
        detail = (
            f"{module_id}での修復会話。"
            f"当事者特性: {'; '.join(profiles)}。"
            f"修復要因: {direction or '短い確認と次回行動の調整'}。"
        )
    else:
        summary = f"{'と'.join(names)}が「{event_name}」で摩擦を起こす。"
        detail = (
            f"{module_id}での摩擦会話。"
            f"当事者特性: {'; '.join(profiles)}。"
            f"摩擦要因: {direction or '場所の混雑、疲労、声かけのタイミング'}。"
        )
    return summary, detail


def routine_summary_detail(
    a: str,
    b: str,
    module_id: str,
    agents_by_id: dict[str, dict[str, str]],
    topic: str = "短い確認",
) -> tuple[str, str]:
    a_agent = agents_by_id.get(a, {"agent_id": a})
    b_agent = agents_by_id.get(b, {"agent_id": b})
    a_name = agent_name(a_agent)
    b_name = agent_name(b_agent)
    summary = f"{a_name}と{b_name}が「{topic}」を短く調整する。"
    detail = (
        f"{module_id}での通常会話。"
        f"{a_name}は「{agent_style(a_agent)}」、{b_name}は「{agent_style(b_agent)}」。"
        f"話題は「{topic}」。長い相談ではなく、共同生活を維持するための低負荷な接点として扱う。"
    )
    return summary, detail


def conversation_lines(event: dict[str, str], participants: list[str], agents_by_id: dict[str, dict[str, str]]) -> list[dict[str, str]]:
    if len(participants) < 2:
        return []
    a, b = participants[:2]
    a_name = agent_name(agents_by_id.get(a, {"agent_id": a}))
    b_name = agent_name(agents_by_id.get(b, {"agent_id": b}))
    event_type = event.get("event_type", "")
    event_name = event.get("event_name", "")
    if event_type == "repair":
        if "声量" in event_name:
            lines = [
                ("棚の前だと声が響きすぎたね。次は端で小さく話す。", "明るくしてくれたのは分かる。休む合図だけ見てほしい。"),
                ("さっきは場を明るくしようとして、声が大きくなった。", "嫌だったのは声量で、あなたの気持ちではない。"),
            ]
        elif "キューポラ" in event_name:
            lines = [
                ("OKサインが出ている時だけ、短く声をかけることにする。", "それなら助かる。答えない時もそのままにしてほしい。"),
                ("心配で近づきすぎた。次は合図を見てからにする。", "隣にいるだけなら大丈夫な時もある。"),
            ]
        elif "個室" in event_name:
            lines = [
                ("入口のマークを見て、順番を時間で決めよう。", "責められていないなら待てる。出る時刻だけ書いておく。"),
                ("一人時間が必要なのはわかった。使う終わりだけ共有して。", "ありがとう。閉じこもりたいというより、整える時間がいる。"),
            ]
        elif "資源" in event_name:
            lines = [
                ("点数じゃなくて、全員の残量として見よう。", "分かった。責める言い方になっていた。"),
                ("スコアを見る前に、誰かのせいにしないと決めたい。", "それなら数字を見ても息が詰まりにくい。"),
            ]
        elif "運動" in event_name:
            lines = [
                ("順番を取られた感じがして、言い方が強くなった。", "私も焦っていた。次はタイマーを先に見せる。"),
                ("身体のリズムが崩れるのが怖かった。", "そこは大事にしよう。枠だけ一緒に組み直す。"),
            ]
        elif "帰還" in event_name:
            lines = [
                ("助言のつもりが、評価みたいに聞こえたかもしれない。", "今は正解より、聞いてもらう時間がほしかった。"),
                ("帰還後の話は急ぎすぎた。今日は一つだけにする。", "その方が聞ける。全部決めるのはまだ重い。"),
            ]
        else:
            lines = [
                ("さっきの言い方、少し強かったかもしれない。", "大丈夫。次は先に確認しよう。"),
                ("一度止めて、何を決める話だったか戻したい。", "うん。気持ちと手順を分けて話そう。"),
            ]
        first, second = choose_variant(lines, event.get("event_id", ""), a, b)
        return [
            {"speaker_id": a, "listener_ids": b, "utterance": first},
            {"speaker_id": b, "listener_ids": a, "utterance": second},
        ]
    if "声量" in event_name:
        return [
            {"speaker_id": a, "listener_ids": b, "utterance": f"{b_name}、今の声量だと休めない。少し下げてほしい。"},
            {"speaker_id": b, "listener_ids": a, "utterance": f"{a_name}、場を明るくしたかっただけ。責められるとつらい。"},
        ]
    if "キューポラ" in event_name:
        return [
            {"speaker_id": b, "listener_ids": a, "utterance": "少し話せるかい。答えなくてもいい。"},
            {"speaker_id": a, "listener_ids": b, "utterance": "今は一人にしてほしい。勝手に分かったように言わないで。"},
        ]
    if "個室" in event_name:
        return [
            {"speaker_id": a, "listener_ids": b, "utterance": "また個室ですか。私も一人になりたいんです。"},
            {"speaker_id": b, "listener_ids": a, "utterance": "すまない。でも今は少し静かにしていたい。"},
        ]
    if "資源" in event_name:
        return [
            {"speaker_id": a, "listener_ids": b, "utterance": f"{b_name}、そのスコアの見方だと誰かを責めているように聞こえる。"},
            {"speaker_id": b, "listener_ids": a, "utterance": f"{a_name}、責めたいんじゃない。残量が怖いんだ。"},
        ]
    if "運動" in event_name:
        return [
            {"speaker_id": a, "listener_ids": b, "utterance": f"{b_name}、その枠は私が待っていた。先に言ってほしかった。"},
            {"speaker_id": b, "listener_ids": a, "utterance": f"{a_name}、焦っていた。順番を飛ばすつもりじゃない。"},
        ]
    if "帰還" in event_name:
        return [
            {"speaker_id": a, "listener_ids": b, "utterance": f"{b_name}、助言は分かるけど、今それを聞く余裕がない。"},
            {"speaker_id": b, "listener_ids": a, "utterance": f"{a_name}、押しつけたかったわけではない。心配だった。"},
        ]
    return [
        {"speaker_id": a, "listener_ids": b, "utterance": f"{b_name}、それは今少しきつい。"},
        {"speaker_id": b, "listener_ids": a, "utterance": f"{a_name}、そんなつもりじゃなかった。"},
    ]


def routine_pair(
    agents: list[dict[str, str]],
    states: list[dict[str, Any]],
    relationship_by_id: dict[str, dict[str, str]],
    step: int,
) -> tuple[str, str] | None:
    states_by_id = {state["agent_id"]: state for state in states}
    for agent in agents:
        current_id = agent_id(agent)
        anchors = parse_ids(first_value(relationship_by_id.get(current_id, {}), "trust_anchor_ids"))
        for target_id in anchors:
            if target_id in states_by_id and states_by_id[target_id]["module_id"] == states_by_id[current_id]["module_id"]:
                return current_id, target_id
    if len(agents) >= 2:
        first = agents[step % len(agents)]
        second = agents[(step + 3) % len(agents)]
        return agent_id(first), agent_id(second)
    return None


def build_conversations(
    step: int,
    run_id: str,
    events_for_step: list[dict[str, str]],
    states: list[dict[str, Any]],
    agents: list[dict[str, str]],
    relationship_by_id: dict[str, dict[str, str]],
    agents_by_id: dict[str, dict[str, str]],
    objects_by_id: dict[str, dict[str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    messages: list[dict[str, Any]] = []
    threads: list[dict[str, Any]] = []
    state_by_agent = {state["agent_id"]: state for state in states}

    for event in events_for_step:
        if event.get("event_type") not in {"conflict", "repair"}:
            continue
        participants = parse_ids(event.get("target", ""))
        if len(participants) < 2:
            continue
        conversation_id = f"conv_{run_id}_step{step:03d}_{event.get('event_id', '')}"
        module_id = event_module(event, objects_by_id)
        event_type = event.get("event_type", "")
        tone = "repair" if event_type == "repair" else "trouble"
        lines = conversation_lines(event, participants, agents_by_id)
        message_ids = [f"msg_{conversation_id}_{index:02d}" for index in range(1, len(lines) + 1)]
        summary, detail = conversation_summary_detail(event, participants, agents_by_id, module_id)
        threads.append({
            "conversation_id": conversation_id,
            "step": step,
            "run_id": run_id,
            "participant_ids": ";".join(participants),
            "module_id": module_id,
            "event_id": event.get("event_id", ""),
            "conversation_type": "repair" if event_type == "repair" else "conflict",
            "status": "repaired" if event_type == "repair" else "open",
            "tone": tone,
            "summary": summary,
            "detail": detail,
            "evidence_message_ids": ";".join(message_ids),
            "summary_source": "scripted_summary",
            "detail_source": "scripted_detail",
        })
        for index, line in enumerate(lines, start=1):
            messages.append({
                "message_id": message_ids[index - 1],
                "conversation_id": conversation_id,
                "step": step,
                "run_id": run_id,
                "speaker_id": line["speaker_id"],
                "listener_ids": parse_ids(line["listener_ids"]),
                "module_id": module_id,
                "event_id": event.get("event_id", ""),
                "tone": tone,
                "utterance": line["utterance"],
                "is_observed": True,
                "source": "scripted",
                "observation_type": "spoken",
            })

    pair = routine_pair(agents, states, relationship_by_id, step)
    if pair:
        a, b = pair
        module_id = state_by_agent.get(a, {}).get("module_id", "common_area")
        if module_id == "crew_quarters":
            module_id = "common_area"
        conversation_id = f"conv_{run_id}_step{step:03d}_routine"
        topic, lines = routine_lines(a, b, module_id, step, run_id, state_by_agent, agents_by_id)
        summary, detail = routine_summary_detail(a, b, module_id, agents_by_id, topic)
        message_ids = [f"msg_{conversation_id}_01", f"msg_{conversation_id}_02"]
        threads.append({
            "conversation_id": conversation_id,
            "step": step,
            "run_id": run_id,
            "participant_ids": f"{a};{b}",
            "module_id": module_id,
            "event_id": "",
            "conversation_type": "routine",
            "status": "closed",
            "tone": "normal",
            "summary": summary,
            "detail": detail,
            "evidence_message_ids": ";".join(message_ids),
            "summary_source": "scripted_summary",
            "detail_source": "scripted_detail",
        })
        for index, line in enumerate(lines, start=1):
            messages.append({
                "message_id": message_ids[index - 1],
                "conversation_id": conversation_id,
                "step": step,
                "run_id": run_id,
                "speaker_id": line["speaker_id"],
                "listener_ids": parse_ids(line["listener_ids"]),
                "module_id": module_id,
                "event_id": "",
                "tone": "normal",
                "utterance": line["utterance"],
                "is_observed": True,
                "source": "scripted",
                "observation_type": "spoken",
            })
    return messages, threads


def build_frames(
    run_id: str,
    agents: list[dict[str, str]],
    places: list[dict[str, str]],
    objects: list[dict[str, str]],
    events: list[dict[str, str]],
    relationship_rows: list[dict[str, str]],
    time_schedule: list[dict[str, str]],
    state_by_step: dict[int, dict[str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    places_by_id = index_by(places, "place_name")
    objects_by_id = index_by(objects, "object_id")
    relationship_by_id = index_by(relationship_rows, "agent_id")
    agents_by_id = {agent_id(agent): agent for agent in agents}
    frames: list[dict[str, Any]] = []
    position_rows: list[dict[str, Any]] = []
    occupancy_rows: list[dict[str, Any]] = []
    sleep_rows: list[dict[str, Any]] = []
    all_messages: list[dict[str, Any]] = []
    all_threads: list[dict[str, Any]] = []
    schedule_by_step = {to_int(row.get("step"), 0): row for row in time_schedule}

    for step in range(1, step_count(time_schedule) + 1):
        events_for_step = active_events(events, step)
        state_row = state_by_step.get(step, {})
        agent_states = []
        for index, agent in enumerate(agents):
            module_id = preferred_module(agent, index, step, events_for_step, objects_by_id)
            place = places_by_id.get(module_id, places[0] if places else {})
            point = place_point(place, index, step)
            stress = stress_score(agent, step, events_for_step, state_row)
            current_agent_id = agent_id(agent)
            active_for_agent = event_ids_for_agent(events_for_step, current_agent_id)
            emotion = emotion_observation(agent, stress, module_id, events_for_step)
            action = action_observation(agent, stress, module_id, events_for_step)
            agent_state = {
                "agent_id": current_agent_id,
                "name": agent_name(agent),
                "module_id": module_id,
                "x": point["x"],
                "y": point["y"],
                "stress": stress,
                "evaluation": evaluation_from_stress(stress),
                "emotion": emotion["label"],
                "emotion_summary": emotion["summary"],
                "emotion_detail": emotion["detail"],
                "emotion_source": emotion["source"],
                "action_category": action["category"],
                "action_summary": action["summary"],
                "action_detail": action["detail"],
                "action_source": action["source"],
                "is_isolated": module_id == "crew_quarters" and stress >= 58,
                "is_selected_candidate": stress >= 78,
                "active_event_ids": active_for_agent,
                "evidence_event_ids": sorted(set(emotion["evidence_event_ids"]) | set(action["evidence_event_ids"])),
                "evidence_conversation_ids": [],
            }
            agent_states.append(agent_state)
            position_rows.append({
                "step": step,
                "agent_id": current_agent_id,
                "module_id": module_id,
                "x": point["x"],
                "y": point["y"],
                "stress": stress,
                "is_isolated": str(agent_state["is_isolated"]).lower(),
                "active_event_ids": ";".join(active_for_agent),
            })

        module_oxygen = module_oxygen_measurements(events_for_step, places, step)
        module_states = module_occupancy(agent_states, places)
        for module_state in module_states:
            module_id = str(module_state.get("module_id", ""))
            oxygen = to_float(module_oxygen.get(module_id, 100.0), 100.0)
            module_state["oxygen_percent"] = round(clamp(oxygen, 0.0, 100.0), 1)
            module_state["oxygen_is_critical"] = oxygen < 50.0
        for module_state in module_states:
            occupancy_rows.append({"step": step, **module_state})

        sleeps = sleep_assignments(agents, step)
        for sleep in sleeps:
            sleep_rows.append({"step": step, **sleep})

        messages, threads = build_conversations(
            step,
            run_id,
            events_for_step,
            agent_states,
            agents,
            relationship_by_id,
            agents_by_id,
            objects_by_id,
        )
        for agent_state in agent_states:
            agent_state["evidence_conversation_ids"] = [
                thread["conversation_id"]
                for thread in threads
                if agent_state["agent_id"] in parse_ids(thread.get("participant_ids", ""))
            ]
        all_messages.extend(messages)
        all_threads.extend(threads)

        incidents = active_incidents(events_for_step, objects_by_id)
        average_stress = round(sum(state["stress"] for state in agent_states) / max(len(agent_states), 1), 1)
        conflict_count = sum(1 for incident in incidents if incident["status"] == "active")
        repair_count = sum(1 for incident in incidents if incident["status"] == "repair")
        isolated_count = sum(1 for state in agent_states if state["is_isolated"])
        crew_capacity = max(1, to_int(places_by_id.get("crew_quarters", {}).get("capacity"), 1))
        private_wait = max(0, sum(1 for state in agent_states if state["module_id"] == "crew_quarters") - crew_capacity)
        repair_rate = round((repair_count / max(conflict_count + repair_count, 1)) * 100)
        frame = {
            "run_id": run_id,
            "step": step,
            "phase": first_value(schedule_by_step.get(step, {}), "phase", fallback=f"step_{step}"),
            "agent_states": agent_states,
            "module_states": module_states,
            "module_oxygen": module_oxygen,
            "active_objects": active_objects(events_for_step, objects_by_id, places_by_id, step),
            "active_events": active_event_payload(events_for_step),
            "active_incidents": incidents,
            "conversation_ids": [thread["conversation_id"] for thread in threads],
            "metrics": {
                "average_stress": average_stress,
                "isolated_agents": isolated_count,
                "conflict_count": conflict_count,
                "repair_count": repair_count,
                "repair_rate": repair_rate,
                "private_room_wait": private_wait,
            },
            "sleep_assignments": sleeps,
            "summary": first_value(schedule_by_step.get(step, {}), "description", fallback=f"step {step}"),
            "detail": (
                f"active_events={len(events_for_step)}、conversations={len(threads)}、"
                f"average_stress={average_stress}。"
                "UI用の派生フレームであり、raw発話はmessages.jsonlを参照する。"
            ),
            "source": "derived",
        }
        frames.append(frame)

    return frames, position_rows, occupancy_rows, sleep_rows, all_messages, all_threads, []


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export habitat UI frames from a domain pack.")
    parser.add_argument("--pack", type=Path, required=True)
    parser.add_argument("--scenario", default="")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--events-tsv", type=Path)
    parser.add_argument("--state-tsv", type=Path)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> None:
    global DOMAIN, schedule_by_step_cache
    args = parse_args()
    DOMAIN = DomainRuntime.load(args.pack, scenario=args.scenario or None)
    habitat_config = DOMAIN.pipeline.get("habitat_ui", {})
    if not isinstance(habitat_config, dict):
        habitat_config = {}

    agents = read_tsv(DOMAIN.data_path("agents"))
    places = read_tsv(DOMAIN.data_path("places"))
    objects = read_tsv(DOMAIN.data_path("objects"))
    relationship_rows = read_tsv(DOMAIN.data_path("relationship_seed"))
    time_schedule = read_tsv(DOMAIN.data_path("time_schedule"))
    schedule_by_step_cache = {to_int(row.get("step"), 0): row for row in time_schedule}
    events_tsv = args.events_tsv or DOMAIN.data_path("events")
    events = read_tsv(events_tsv)
    objects_by_id = index_by(objects, "object_id")
    event_timeline = build_event_timeline(events, objects_by_id)
    nudge_effects = build_nudge_effects(event_timeline, objects_by_id)
    state_by_step = read_state_by_step(args.state_tsv)
    run_id = args.run_id or args.scenario or str(DOMAIN.config.get("scenario_id", "")) or "run"

    frames, positions, occupancy, sleeps, messages, threads, warnings = build_frames(
        run_id,
        agents,
        places,
        objects,
        events,
        relationship_rows,
        time_schedule,
        state_by_step,
    )

    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(output_dir / str(habitat_config.get("output_frames", "habitat_frames.jsonl")), frames)
    write_tsv(
        output_dir / str(habitat_config.get("output_agent_positions", "agent_positions.tsv")),
        positions,
        ["step", "agent_id", "module_id", "x", "y", "stress", "is_isolated", "active_event_ids"],
    )
    write_tsv(
        output_dir / str(habitat_config.get("output_module_occupancy", "module_occupancy.tsv")),
        occupancy,
        ["step", "module_id", "occupancy", "capacity", "crowding_level", "is_over_capacity"],
    )
    write_tsv(
        output_dir / str(habitat_config.get("output_sleep_assignments", "sleep_assignments.tsv")),
        sleeps,
        ["step", "slot_id", "agent_id", "is_temporary"],
    )
    write_jsonl(output_dir / str(DOMAIN.pipeline.get("conversations", {}).get("output_messages", "messages.jsonl")), messages)
    write_tsv(
        output_dir / str(DOMAIN.pipeline.get("conversations", {}).get("output_threads", "conversation_threads.tsv")),
        threads,
        [
            "conversation_id",
            "step",
            "run_id",
            "participant_ids",
            "module_id",
            "event_id",
            "conversation_type",
            "status",
            "tone",
            "summary",
            "detail",
            "evidence_message_ids",
            "summary_source",
            "detail_source",
        ],
    )
    write_tsv(
        output_dir / str(habitat_config.get("output_event_timeline", "event_timeline.tsv")),
        event_timeline,
        [
            "event_id",
            "start_step",
            "end_step",
            "event_type",
            "timeline_type",
            "label",
            "module_id",
            "participant_ids",
            "related_object_id",
            "related_conflict_id",
            "intensity",
            "direction",
            "description",
        ],
    )
    write_tsv(
        output_dir / str(habitat_config.get("output_nudge_effects", "nudge_effects.tsv")),
        nudge_effects,
        [
            "effect_id",
            "object_id",
            "object_name",
            "start_step",
            "module_id",
            "target_event_ids",
            "repaired_event_ids",
            "conflict_event_ids",
            "affected_agent_ids",
            "first_repair_step",
            "status",
            "effect_summary",
            "effect_path",
        ],
    )

    manifest = {
        "kind": "habitat_ui_export",
        "pack": str(args.pack),
        "scenario": args.scenario,
        "run_id": run_id,
        "events_tsv": str(events_tsv or ""),
        "state_tsv": str(args.state_tsv or ""),
        "frame_count": len(frames),
        "message_count": len(messages),
        "conversation_count": len(threads),
        "timeline_event_count": len(event_timeline),
        "nudge_effect_count": len(nudge_effects),
        "warnings": warnings,
        "outputs": [
            str(habitat_config.get("output_frames", "habitat_frames.jsonl")),
            str(habitat_config.get("output_agent_positions", "agent_positions.tsv")),
            str(habitat_config.get("output_module_occupancy", "module_occupancy.tsv")),
            str(habitat_config.get("output_sleep_assignments", "sleep_assignments.tsv")),
            str(DOMAIN.pipeline.get("conversations", {}).get("output_messages", "messages.jsonl")),
            str(DOMAIN.pipeline.get("conversations", {}).get("output_threads", "conversation_threads.tsv")),
            str(habitat_config.get("output_event_timeline", "event_timeline.tsv")),
            str(habitat_config.get("output_nudge_effects", "nudge_effects.tsv")),
        ],
    }
    (output_dir / "habitat_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        f"Wrote {len(frames)} habitat frames, {len(messages)} messages, "
        f"{len(threads)} conversations, {len(event_timeline)} timeline events, "
        f"and {len(nudge_effects)} nudge effects to {output_dir}"
    )


if __name__ == "__main__":
    main()
