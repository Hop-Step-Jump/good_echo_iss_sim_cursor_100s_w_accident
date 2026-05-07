#!/usr/bin/env python3
"""Convert spatial_demo run output into ISS habitat UI artifacts.

Reads:
  - spatial_step_snapshots.jsonl (preferred; written by simulation.py after each step)
  - messages.jsonl, memory_reasoning.jsonl (spatial_demo formats)
  - The same spatial_demo YAML used for the run (--spatial-config)

Writes (under --output-dir):
  habitat_frames.jsonl, agent_positions.tsv, module_occupancy.tsv,
  messages.jsonl, conversation_threads.tsv, event_timeline.tsv,
  nudge_effects.tsv, sleep_assignments.tsv, habitat_manifest.json

event_timeline / nudge_effects reuse logic from export_habitat_frames.py (domain pack).
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import math
import sys
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import yaml

from sim_core.domain_runtime import DomainRuntime


def load_export_habitat_module():
    spec = importlib.util.spec_from_file_location(
        "export_habitat_frames",
        SCRIPTS / "export_habitat_frames.py",
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_tsv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, delimiter="\t", fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def iss_id(spatial_id: int) -> str:
    return f"ISS{int(spatial_id):02d}"


def load_snapshots_by_step(path: Path) -> dict[int, dict[str, Any]]:
    by_step: dict[int, dict[str, Any]] = {}
    for row in read_jsonl(path):
        step = int(row.get("step", 0))
        if step > 0:
            by_step[step] = row
    return by_step


def memory_reasoning_index(path: Path) -> dict[tuple[int, int], str]:
    """Map (step, spatial_agent_id) -> combined memory+reasoning text."""
    out: dict[tuple[int, int], str] = {}
    for row in read_jsonl(path):
        step = int(row.get("step", 0))
        aid = int(row.get("id", -1))
        if step < 0 or aid < 0:
            continue
        mem = str(row.get("memory", ""))
        rea = str(row.get("reasoning", ""))
        out[(step, aid)] = f"{mem}\n{rea}".strip()
    return out


def infer_max_step(
    snapshots: dict[int, dict[str, Any]],
    messages: list[dict[str, Any]],
    duration: int,
) -> int:
    m = duration
    if snapshots:
        m = max(m, max(snapshots))
    if messages:
        m = max(m, max(int(x.get("step", 0)) for x in messages))
    return max(1, m)


def stress_heuristic(
    step: int,
    spatial_id: int,
    memory_text: str,
    exh: Any,
    agent_row: dict[str, str],
    events_for_step: list[dict[str, str]],
) -> float:
    base = 22.0 + (len(memory_text) % 48) + (step % 7) * 2.4 + (spatial_id % 3) * 1.5
    try:
        row_stress = exh.stress_score(agent_row, step, events_for_step, {})
    except Exception:
        row_stress = 50.0
    blended = base * 0.45 + float(row_stress) * 0.55
    return round(clamp(blended, 12.0, 96.0), 1)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--spatial-dir", type=Path, required=True, help="e.g. outputs/spatial/output_iss_cursor_run_b")
    p.add_argument("--spatial-config", type=Path, required=True, help="YAML passed to spatial_demo (e.g. config.iss.cursor.run_b.yaml)")
    p.add_argument("--pack", type=Path, default=ROOT / "domain_packs" / "iss_benevolence")
    p.add_argument("--scenario", default="run_b", help="Domain scenario for events/timeline (run_a or run_b)")
    p.add_argument("--run-id", default="spatial_linked", help="run_id embedded in habitat rows")
    p.add_argument("--output-dir", type=Path, required=True)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    spatial_dir: Path = args.spatial_dir
    cfg_path: Path = args.spatial_config
    out_dir: Path = args.output_dir
    run_id = str(args.run_id)

    if not spatial_dir.is_dir():
        raise SystemExit(f"Not a directory: {spatial_dir}")
    if not cfg_path.is_file():
        raise SystemExit(f"Missing spatial config: {cfg_path}")

    with cfg_path.open("r", encoding="utf-8") as handle:
        spatial_cfg = yaml.safe_load(handle) or {}

    sim_cfg = spatial_cfg.get("simulation") or {}
    duration = int(sim_cfg.get("duration", 50))
    agent_cfg = spatial_cfg.get("agents") or {}
    num_spatial = int(agent_cfg.get("num_agents", 10))
    exh = load_export_habitat_module()
    DOMAIN = DomainRuntime.load(args.pack, scenario=args.scenario or None)
    exh.DOMAIN = DOMAIN
    agents = exh.read_tsv(DOMAIN.data_path("agents"))
    num_agents = min(num_spatial, len(agents)) if agents else num_spatial
    places = exh.read_tsv(DOMAIN.data_path("places"))
    objects = exh.read_tsv(DOMAIN.data_path("objects"))
    relationship_rows = exh.read_tsv(DOMAIN.data_path("relationship_seed"))
    time_schedule = exh.read_tsv(DOMAIN.data_path("time_schedule"))
    events = exh.read_tsv(DOMAIN.data_path("events"))
    exh.schedule_by_step_cache = {exh.to_int(r.get("step"), 0): r for r in time_schedule}

    places_by_id = exh.index_by(places, "place_name")
    objects_by_id = exh.index_by(objects, "object_id")
    relationship_by_id = exh.index_by(relationship_rows, "agent_id")
    agents_by_id = {exh.agent_id(a): a for a in agents}

    snap_path = spatial_dir / "spatial_step_snapshots.jsonl"
    snapshots = load_snapshots_by_step(snap_path)
    degraded = not snapshots
    if degraded:
        print(
            "WARNING: spatial_step_snapshots.jsonl not found. "
            "Position/module data will be approximated (common_area + layout). "
            "Re-run spatial_demo with an updated simulation.py to record snapshots.",
            file=sys.stderr,
        )

    messages_spatial = read_jsonl(spatial_dir / "messages.jsonl")
    memory_idx = memory_reasoning_index(spatial_dir / "memory_reasoning.jsonl")

    max_step = infer_max_step(snapshots, messages_spatial, duration)

    timeline = exh.build_event_timeline(events, objects_by_id)
    nudge_effects = exh.build_nudge_effects(timeline, objects_by_id)

    habitat_frames: list[dict[str, Any]] = []
    position_rows: list[dict[str, Any]] = []
    occupancy_rows: list[dict[str, Any]] = []
    sleep_rows: list[dict[str, Any]] = []
    ui_messages: list[dict[str, Any]] = []
    ui_threads: list[dict[str, Any]] = []

    spatial_id_to_agent_row: dict[int, dict[str, str]] = {}
    for row in agents:
        aid = exh.agent_id(row)
        if aid.startswith("ISS") and len(aid) >= 4:
            try:
                n = int(aid[3:5])
                spatial_id_to_agent_row[n] = row
            except ValueError:
                continue

    msg_seq = 0

    for step in range(1, max_step + 1):
        events_for_step = exh.active_events(events, step)
        snap = snapshots.get(step, {})
        snap_agents = {int(a["id"]): a for a in snap.get("agents", []) if "id" in a}

        agent_states: list[dict[str, Any]] = []

        for spatial_id in range(num_agents):
            iss = iss_id(spatial_id)
            agent_row = spatial_id_to_agent_row.get(spatial_id, agents[spatial_id] if spatial_id < len(agents) else {})
            mem_text = memory_idx.get((step, spatial_id), "")

            if spatial_id in snap_agents:
                sa = snap_agents[spatial_id]
                raw_place = (sa.get("current_place") or "").strip()
                if sa.get("in_place") and raw_place and raw_place in places_by_id:
                    module_id = raw_place
                elif sa.get("in_place") and raw_place:
                    module_id = raw_place if raw_place in places_by_id else "common_area"
                else:
                    module_id = "common_area"
                place_dict = places_by_id.get(module_id, places[0] if places else {})
                point = exh.place_point(place_dict, spatial_id, step)
                x, y = point["x"], point["y"]
            else:
                module_id = "common_area"
                place_dict = places_by_id.get(module_id, places[0] if places else {})
                point = exh.place_point(place_dict, spatial_id, step)
                x, y = point["x"], point["y"]

            stress = stress_heuristic(step, spatial_id, mem_text, exh, agent_row, events_for_step)
            emotion = exh.emotion_observation(agent_row, stress, module_id, events_for_step)
            action = exh.action_observation(agent_row, stress, module_id, events_for_step)
            active_for_agent = exh.event_ids_for_agent(events_for_step, iss)

            is_isolated = module_id == "crew_quarters" and stress >= 58

            agent_state: dict[str, Any] = {
                "agent_id": iss,
                "name": exh.agent_name(agent_row),
                "module_id": module_id,
                "x": x,
                "y": y,
                "stress": stress,
                "evaluation": exh.evaluation_from_stress(stress),
                "emotion": emotion["label"],
                "emotion_summary": emotion["summary"],
                "emotion_detail": emotion["detail"],
                "emotion_source": "spatial_derived",
                "action_category": action["category"],
                "action_summary": action["summary"],
                "action_detail": action["detail"],
                "action_source": "spatial_derived",
                "is_isolated": is_isolated,
                "is_selected_candidate": stress >= 78,
                "active_event_ids": active_for_agent,
                "evidence_event_ids": sorted(
                    set(emotion["evidence_event_ids"]) | set(action["evidence_event_ids"])
                ),
                "evidence_conversation_ids": [],
            }
            agent_states.append(agent_state)
            position_rows.append({
                "step": step,
                "agent_id": iss,
                "module_id": module_id,
                "x": x,
                "y": y,
                "stress": stress,
                "is_isolated": str(is_isolated).lower(),
                "active_event_ids": ";".join(active_for_agent),
            })

        module_states = exh.module_occupancy(agent_states, places)
        for ms in module_states:
            occupancy_rows.append({"step": step, **ms})

        for sleep in exh.sleep_assignments(agents, step):
            sleep_rows.append({"step": step, **sleep})

        for row in messages_spatial:
            if int(row.get("step", 0)) != step:
                continue
            try:
                fid = int(row.get("from"))
                tid = int(row.get("to"))
            except (TypeError, ValueError):
                continue
            utterance = str(row.get("message", "")).strip()
            if not utterance:
                continue
            reasoning = str(row.get("reasoning", "")).strip()
            speaker = iss_id(fid)
            listener = iss_id(tid)
            mod = next(
                (s["module_id"] for s in agent_states if s["agent_id"] == speaker),
                "common_area",
            )
            msg_seq += 1
            cid = f"conv_spatial_{run_id}_s{step:04d}_{fid}_{tid}_{msg_seq:05d}"
            mid = f"msg_{cid}"
            ui_messages.append({
                "message_id": mid,
                "conversation_id": cid,
                "step": step,
                "run_id": run_id,
                "speaker_id": speaker,
                "listener_ids": [listener],
                "module_id": mod,
                "event_id": "",
                "tone": "normal",
                "utterance": utterance,
                "is_observed": True,
                "source": "spatial_llm",
                "observation_type": "spoken",
            })
            ui_threads.append({
                "conversation_id": cid,
                "step": step,
                "run_id": run_id,
                "participant_ids": f"{speaker};{listener}",
                "module_id": mod,
                "event_id": "",
                "conversation_type": "spatial_message",
                "status": "closed",
                "tone": "normal",
                "summary": utterance[:200],
                "detail": reasoning[:800] if reasoning else utterance[:800],
                "evidence_message_ids": mid,
                "summary_source": "spatial_llm",
                "detail_source": "spatial_llm",
            })

        for st in agent_states:
            st["evidence_conversation_ids"] = [
                t["conversation_id"]
                for t in ui_threads
                if t["step"] == step
                and st["agent_id"] in exh.parse_ids(t.get("participant_ids", ""))
            ]

        incidents = exh.active_incidents(events_for_step, objects_by_id)
        average_stress = round(
            sum(s["stress"] for s in agent_states) / max(len(agent_states), 1), 1
        )
        conflict_count = sum(1 for inc in incidents if inc.get("status") == "active")
        repair_count = sum(1 for inc in incidents if inc.get("status") == "repair")
        isolated_count = sum(1 for s in agent_states if s["is_isolated"])
        crew_cap = max(1, exh.to_int(places_by_id.get("crew_quarters", {}).get("capacity"), 1))
        private_wait = max(
            0,
            sum(1 for s in agent_states if s["module_id"] == "crew_quarters") - crew_cap,
        )
        repair_rate = round((repair_count / max(conflict_count + repair_count, 1)) * 100)

        phase = exh.first_value(
            exh.schedule_by_step_cache.get(step, {}),
            "phase",
            fallback=f"ISS step {step}",
        )
        desc = exh.first_value(
            exh.schedule_by_step_cache.get(step, {}),
            "description",
            fallback=f"spatial_linked step {step}",
        )

        habitat_frames.append({
            "run_id": run_id,
            "step": step,
            "phase": phase,
            "agent_states": agent_states,
            "module_states": module_states,
            "active_objects": exh.active_objects(events_for_step, objects_by_id, places_by_id, step),
            "active_events": exh.active_event_payload(events_for_step),
            "active_incidents": incidents,
            "conversation_ids": [
                t["conversation_id"] for t in ui_threads if t["step"] == step
            ],
            "metrics": {
                "average_stress": average_stress,
                "isolated_agents": isolated_count,
                "conflict_count": conflict_count,
                "repair_count": repair_count,
                "repair_rate": repair_rate,
                "private_room_wait": private_wait,
            },
            "sleep_assignments": exh.sleep_assignments(agents, step),
            "summary": desc,
            "detail": (
                f"spatial_linked: snapshots={'yes' if not degraded else 'no'}, "
                f"messages_step={sum(1 for m in messages_spatial if int(m.get('step',0))==step)}, "
                f"avg_stress={average_stress}。"
            ),
            "source": "spatial_linked",
        })

    out_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(out_dir / "habitat_frames.jsonl", habitat_frames)
    write_tsv(
        out_dir / "agent_positions.tsv",
        position_rows,
        ["step", "agent_id", "module_id", "x", "y", "stress", "is_isolated", "active_event_ids"],
    )
    write_tsv(
        out_dir / "module_occupancy.tsv",
        occupancy_rows,
        ["step", "module_id", "occupancy", "capacity", "crowding_level", "is_over_capacity"],
    )
    write_tsv(
        out_dir / "sleep_assignments.tsv",
        sleep_rows,
        ["step", "slot_id", "agent_id", "is_temporary"],
    )
    write_jsonl(out_dir / "messages.jsonl", ui_messages)
    write_tsv(
        out_dir / "conversation_threads.tsv",
        ui_threads,
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
        out_dir / "event_timeline.tsv",
        timeline,
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
        out_dir / "nudge_effects.tsv",
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
        "kind": "habitat_ui_spatial_bridge",
        "spatial_dir": str(spatial_dir),
        "spatial_config": str(cfg_path),
        "pack": str(args.pack),
        "scenario": args.scenario,
        "run_id": run_id,
        "snapshots": str(snap_path),
        "had_snapshots": not degraded,
        "steps": max_step,
        "message_rows": len(ui_messages),
        "degraded_geometry": degraded,
    }
    (out_dir / "habitat_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        f"Wrote habitat UI bundle to {out_dir} "
        f"({max_step} steps, {len(ui_messages)} messages, snapshots={'yes' if not degraded else 'no'})"
    )


if __name__ == "__main__":
    main()
