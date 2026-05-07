#!/usr/bin/env python3
"""Generate habitat UI conversations with an LLM and prior-step history.

This is a post-processor for ``scripts/export_habitat_frames.py`` output. It
keeps the stable UI artifacts, but replaces selected conversation/message rows
with LLM-generated spoken rows that can see earlier conversations.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Iterable

import yaml

ROOT = Path(__file__).resolve().parents[1]
SPATIAL_DEMO = ROOT / "examples" / "spatial_demo"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SPATIAL_DEMO) not in sys.path:
    sys.path.insert(0, str(SPATIAL_DEMO))

from llm_backends import create_llm_client
from sim_core.domain_runtime import DomainRuntime


MESSAGE_FIELDS = [
    "message_id",
    "conversation_id",
    "step",
    "run_id",
    "speaker_id",
    "listener_ids",
    "module_id",
    "event_id",
    "tone",
    "utterance",
    "is_observed",
    "source",
    "observation_type",
]

THREAD_FIELDS = [
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
]


def read_tsv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def write_tsv(path: Path, rows: Iterable[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    with tmp_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, delimiter="\t", fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})
    tmp_path.replace(path)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp_path.replace(path)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp_path.replace(path)


def is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def copy_static_files(input_dir: Path, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    if output_dir == input_dir:
        return
    skip = {"messages.jsonl", "conversation_threads.tsv", "habitat_manifest.json"}
    for source in input_dir.iterdir():
        if source.name in skip:
            continue
        resolved_source = source.resolve()
        if resolved_source == output_dir or is_within(output_dir, resolved_source):
            continue
        target = output_dir / source.name
        if source.is_file():
            shutil.copy2(source, target)
        elif source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)


def original_messages_for_threads(
    threads: Iterable[dict[str, Any]],
    messages_by_conversation: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for thread in threads:
        rows.extend(messages_by_conversation.get(str(thread.get("conversation_id", "")), []))
    return rows


def write_generation_outputs(
    output_dir: Path,
    input_dir: Path,
    messages: list[dict[str, Any]],
    threads: list[dict[str, Any]],
    manifest_base: dict[str, Any],
    args: argparse.Namespace,
    generated: int,
    failed: int,
    status: str,
) -> None:
    write_jsonl(output_dir / "messages.jsonl", messages)
    write_tsv(output_dir / "conversation_threads.tsv", threads, THREAD_FIELDS)
    manifest = dict(manifest_base)
    manifest.update({
        "conversation_generation": {
            "kind": "llm_history" if not args.mock else "mock_noop",
            "status": status,
            "source_dir": str(input_dir),
            "generated_thread_count": generated,
            "failed_thread_count": failed,
            "start_step": args.start_step,
            "end_step": args.end_step,
            "history_size": args.history_size,
            "llm_config": str(args.llm_config or ""),
        },
        "message_count": len(messages),
        "conversation_count": len(threads),
    })
    write_json(output_dir / "habitat_manifest.json", manifest)


def parse_ids(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "")
    text = text.replace(",", ";").replace("[", "").replace("]", "").replace('"', "").replace("'", "")
    return [item.strip() for item in text.split(";") if item.strip()]


def to_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return fallback


def first_value(row: dict[str, Any], *keys: str, fallback: str = "") -> str:
    for key in keys:
        value = row.get(key, "")
        if value != "":
            return str(value)
    return fallback


def agent_id(row: dict[str, str], domain: DomainRuntime) -> str:
    for column in domain.agent_id_columns():
        if row.get(column):
            return row[column]
    return first_value(row, "agent_id")


def agent_name(row: dict[str, str]) -> str:
    return first_value(row, "name", fallback=first_value(row, "agent_id"))


def index_by(rows: list[dict[str, str]], key: str) -> dict[str, dict[str, str]]:
    return {row[key]: row for row in rows if row.get(key)}


def load_llm_config(path: Path | None) -> dict[str, Any]:
    if not path:
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"YAML root must be a mapping: {path}")
    llm = payload.get("llm", {})
    if not isinstance(llm, dict):
        raise ValueError(f"Missing llm mapping in config: {path}")
    return llm


def extract_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.S)
        if not match:
            raise
        payload = json.loads(match.group(0))
    if not isinstance(payload, dict):
        raise ValueError("LLM response JSON must be an object")
    return payload


def compact_agent(
    agent: dict[str, str],
    state: dict[str, Any] | None,
    relationship: dict[str, str] | None,
) -> dict[str, Any]:
    state = state or {}
    relationship = relationship or {}
    return {
        "agent_id": first_value(agent, "agent_id"),
        "name": agent_name(agent),
        "age": first_value(agent, "age"),
        "region": first_value(agent, "region"),
        "religion": first_value(agent, "religion"),
        "role": first_value(agent, "iss_role", "layer"),
        "persona": first_value(agent, "persona"),
        "communication_style": first_value(agent, "communication_style"),
        "privacy_need": first_value(agent, "privacy_need"),
        "vulnerability_note": first_value(agent, "vulnerability_note"),
        "trust_anchor_ids": first_value(relationship, "trust_anchor_ids"),
        "friction_anchor_ids": first_value(relationship, "friction_anchor_ids"),
        "current_module_id": state.get("module_id", ""),
        "current_stress": state.get("stress", ""),
        "current_evaluation": state.get("evaluation", ""),
        "current_emotion": state.get("emotion", ""),
        "emotion_summary": state.get("emotion_summary", ""),
        "action_summary": state.get("action_summary", ""),
        "is_isolated": state.get("is_isolated", ""),
    }


def compact_message(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "step": row.get("step", ""),
        "conversation_id": row.get("conversation_id", ""),
        "speaker_id": row.get("speaker_id", row.get("from", "")),
        "listener_ids": parse_ids(row.get("listener_ids", row.get("to", ""))),
        "tone": row.get("tone", ""),
        "utterance": row.get("utterance", row.get("message", "")),
        "source": row.get("source", ""),
    }


def relevant_history(
    history: list[dict[str, Any]],
    participant_ids: list[str],
    step: int,
    limit: int,
) -> list[dict[str, Any]]:
    participants = set(participant_ids)
    rows = []
    for row in history:
        row_step = to_int(row.get("step"), 0)
        if row_step >= step:
            continue
        speaker = str(row.get("speaker_id", row.get("from", "")))
        listeners = set(parse_ids(row.get("listener_ids", row.get("to", ""))))
        if speaker in participants or participants.intersection(listeners):
            rows.append(row)
    return [compact_message(row) for row in rows[-limit:]]


def active_thread_context(
    thread: dict[str, str],
    frame: dict[str, Any] | None,
) -> dict[str, Any]:
    frame = frame or {}
    event_id = thread.get("event_id", "")
    active_events = frame.get("active_events", []) or []
    event = next((row for row in active_events if row.get("event_id") == event_id), {})
    return {
        "frame_summary": frame.get("summary", ""),
        "frame_detail": frame.get("detail", ""),
        "active_objects": frame.get("active_objects", []) or [],
        "active_events": active_events,
        "matched_event": event,
        "metrics": frame.get("metrics", {}),
    }


def build_prompt(
    thread: dict[str, str],
    existing_messages: list[dict[str, Any]],
    history: list[dict[str, Any]],
    frame: dict[str, Any] | None,
    agents_by_id: dict[str, dict[str, str]],
    relationships_by_id: dict[str, dict[str, str]],
    max_history: int,
) -> str:
    participant_ids = parse_ids(thread.get("participant_ids", ""))
    state_by_id = {
        row.get("agent_id"): row
        for row in (frame or {}).get("agent_states", [])
        if isinstance(row, dict) and row.get("agent_id")
    }
    participants = [
        compact_agent(
            agents_by_id.get(participant_id, {"agent_id": participant_id}),
            state_by_id.get(participant_id),
            relationships_by_id.get(participant_id),
        )
        for participant_id in participant_ids
    ]
    context = active_thread_context(thread, frame)
    recent_history = relevant_history(history, participant_ids, to_int(thread.get("step"), 0), max_history)
    draft_messages = [compact_message(row) for row in existing_messages]
    payload = {
        "thread": {
            "conversation_id": thread.get("conversation_id", ""),
            "step": thread.get("step", ""),
            "module_id": thread.get("module_id", ""),
            "event_id": thread.get("event_id", ""),
            "conversation_type": thread.get("conversation_type", ""),
            "tone": thread.get("tone", ""),
            "current_summary": thread.get("summary", ""),
            "current_detail": thread.get("detail", ""),
        },
        "participants": participants,
        "current_context": context,
        "previous_related_messages": recent_history,
        "scripted_draft_do_not_copy": draft_messages,
    }
    return f"""
あなたはISS閉鎖空間実験の会話観測器です。
出力はJSONだけ。コードブロックは禁止。

目的:
- scripted_draft_do_not_copy は置き換え対象の薄い下書きです。文面をコピーしないでください。
- previous_related_messages を踏まえ、前ステップまでの会話の余韻、警戒、修復、避け方を反映してください。
- 発話は「実際に観測された短い会話」として自然にしてください。説明調、標語、発表用コピーにしないでください。
- 同じ定型文を繰り返さないでください。
- 参加者の personality / communication_style / stress / isolation / module を反映してください。
- conflict は少し刺さる言い方、repair は完全解決ではなく距離の戻し方を出してください。
- routine は作業・食事・静穏時間・個室・運動・地球観測・ナッジ周辺の具体的な短いやり取りにしてください。
- 発話者 speaker_id は participants 内だけ。listener_ids も participants 内だけ。
- 2発話を基本に、必要なら3発話まで。utterance は各80字以内。

入力:
{json.dumps(payload, ensure_ascii=False, indent=2)}

返すJSON:
{{
  "summary": "UI一覧に出す20〜45字の要約",
  "detail": "関係、前ステップ履歴、場所、感情を含む80〜160字の説明",
  "status": "closed/open/repaired/unresolved のどれか",
  "messages": [
    {{
      "speaker_id": "ISS00",
      "listener_ids": ["ISS01"],
      "tone": "normal/nudge/caution/trouble/repair",
      "utterance": "観測発話",
      "observation_type": "spoken"
    }}
  ]
}}
""".strip()


def normalize_llm_payload(
    payload: dict[str, Any],
    thread: dict[str, str],
    existing_messages: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    participant_ids = parse_ids(thread.get("participant_ids", ""))
    participant_set = set(participant_ids)
    default_tone = thread.get("tone", "normal") or "normal"
    messages_raw = payload.get("messages", [])
    if not isinstance(messages_raw, list) or not messages_raw:
        raise ValueError("LLM payload must contain non-empty messages list")

    allowed_tones = {"normal", "caution", "trouble", "repair", "nudge"}
    thread_tone = default_tone if default_tone in allowed_tones else "normal"
    event_id = thread.get("event_id", "")
    if thread.get("conversation_type") == "routine" and not event_id:
        permitted_tones = {"normal"}
    elif thread_tone == "repair" or event_id.startswith(("REP", "REPA", "REPB")):
        permitted_tones = {"repair", "normal"}
    elif thread_tone == "trouble" or event_id.startswith("CONF"):
        permitted_tones = {"trouble", "caution", "normal"}
    else:
        permitted_tones = {thread_tone, "normal"}

    messages: list[dict[str, Any]] = []
    for index, item in enumerate(messages_raw[:3], start=1):
        if not isinstance(item, dict):
            continue
        speaker = str(item.get("speaker_id", "")).strip()
        if speaker not in participant_set:
            continue
        listeners = [item for item in parse_ids(item.get("listener_ids", [])) if item in participant_set and item != speaker]
        if not listeners:
            listeners = [item for item in participant_ids if item != speaker][:1]
        utterance = str(item.get("utterance", "")).strip().replace("\n", " ")
        if not utterance:
            continue
        tone = str(item.get("tone") or thread_tone)
        if tone not in permitted_tones:
            tone = thread_tone if thread_tone in permitted_tones else "normal"
        messages.append({
            "message_id": f"msg_{thread['conversation_id']}_{index:02d}",
            "conversation_id": thread["conversation_id"],
            "step": to_int(thread.get("step"), 0),
            "run_id": thread.get("run_id", ""),
            "speaker_id": speaker,
            "listener_ids": listeners,
            "module_id": thread.get("module_id", ""),
            "event_id": thread.get("event_id", ""),
            "tone": tone,
            "utterance": utterance[:160],
            "is_observed": True,
            "source": "llm",
            "observation_type": str(item.get("observation_type") or "spoken"),
        })
    if not messages:
        raise ValueError("LLM payload did not contain usable participant messages")

    summary = str(payload.get("summary") or thread.get("summary") or "").strip()
    detail = str(payload.get("detail") or thread.get("detail") or "").strip()
    status = str(payload.get("status") or thread.get("status") or "closed").strip()
    patched_thread = dict(thread)
    patched_thread.update({
        "status": status,
        "summary": summary[:120],
        "detail": detail[:400],
        "evidence_message_ids": ";".join(row["message_id"] for row in messages),
        "summary_source": "llm_summary",
        "detail_source": "llm_detail",
    })
    return patched_thread, messages


def should_generate_step(step: int, start_step: int, end_step: int, limit_threads: int | None, generated_count: int) -> bool:
    if step < start_step or step > end_step:
        return False
    if limit_threads is not None and generated_count >= limit_threads:
        return False
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate habitat conversations with LLM history.")
    parser.add_argument("--pack", type=Path, required=True)
    parser.add_argument("--scenario", default="")
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--llm-config", type=Path, default=None, help="spatial_demo config YAML with llm section")
    parser.add_argument("--start-step", type=int, default=1)
    parser.add_argument("--end-step", type=int, default=50)
    parser.add_argument("--limit-threads", type=int, default=None, help="Generate only N threads for smoke testing")
    parser.add_argument("--history-size", type=int, default=12)
    parser.add_argument("--mock", action="store_true", help="Do not call LLM; copy scripted rows and mark manifest only")
    parser.add_argument("--fail-on-error", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_dir = args.input_dir.resolve()
    output_dir = (args.output_dir or args.input_dir).resolve()
    if not input_dir.exists():
        raise SystemExit(f"--input-dir does not exist: {input_dir}")
    missing = [
        name
        for name in ("habitat_frames.jsonl", "conversation_threads.tsv", "messages.jsonl")
        if not (input_dir / name).exists()
    ]
    if missing:
        raise SystemExit(f"--input-dir is missing required files: {', '.join(missing)}")

    domain = DomainRuntime.load(args.pack, scenario=args.scenario or None)
    agents = read_tsv(domain.data_path("agents") or Path())
    relationships = read_tsv(domain.data_path("relationship_seed") or Path())
    agents_by_id = {
        agent_id(row, domain): row
        for row in agents
        if agent_id(row, domain)
    }
    relationships_by_id = index_by(relationships, "agent_id")

    frames = read_jsonl(input_dir / "habitat_frames.jsonl")
    frames_by_step = {
        to_int(frame.get("step"), 0): frame
        for frame in frames
    }
    threads = read_tsv(input_dir / "conversation_threads.tsv")
    messages = read_jsonl(input_dir / "messages.jsonl")
    messages_by_conversation: dict[str, list[dict[str, Any]]] = {}
    for row in messages:
        messages_by_conversation.setdefault(str(row.get("conversation_id", "")), []).append(row)
    ordered_threads = sorted(threads, key=lambda row: (to_int(row.get("step"), 0), row.get("conversation_id", "")))
    manifest_path = input_dir / "habitat_manifest.json"
    manifest_base = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    copy_static_files(input_dir, output_dir)

    client = None
    if not args.mock:
        llm_config = load_llm_config(args.llm_config)
        if not llm_config:
            raise SystemExit("--llm-config is required unless --mock is used")
        client = create_llm_client(llm_config)
        if not client.check_connection():
            raise SystemExit("Configured LLM backend is not available")

    new_threads: list[dict[str, Any]] = []
    new_messages: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []
    generated = 0
    failed = 0

    def checkpoint(index: int, status: str) -> None:
        remaining_threads = ordered_threads[index + 1:]
        checkpoint_threads = [*new_threads, *remaining_threads]
        checkpoint_messages = [
            *new_messages,
            *original_messages_for_threads(remaining_threads, messages_by_conversation),
        ]
        write_generation_outputs(
            output_dir,
            input_dir,
            checkpoint_messages,
            checkpoint_threads,
            manifest_base,
            args,
            generated,
            failed,
            status,
        )

    checkpoint(-1, "initialized")

    for index, thread in enumerate(ordered_threads):
        step = to_int(thread.get("step"), 0)
        existing = messages_by_conversation.get(thread.get("conversation_id", ""), [])
        generated_this_thread = False
        if not args.mock and should_generate_step(step, args.start_step, args.end_step, args.limit_threads, generated):
            prompt = build_prompt(
                thread,
                existing,
                history,
                frames_by_step.get(step),
                agents_by_id,
                relationships_by_id,
                args.history_size,
            )
            try:
                assert client is not None
                response = client.generate(prompt)
                payload = extract_json(response)
                patched_thread, patched_messages = normalize_llm_payload(payload, thread, existing)
                new_threads.append(patched_thread)
                new_messages.extend(patched_messages)
                history.extend(patched_messages)
                generated += 1
                generated_this_thread = True
                print(f"generated {thread.get('conversation_id')} step {step}", flush=True)
            except Exception as exc:
                failed += 1
                print(f"failed {thread.get('conversation_id')} step {step}: {exc}", file=sys.stderr, flush=True)
                if args.fail_on_error:
                    new_threads.append(thread)
                    new_messages.extend(existing)
                    history.extend(existing)
                    checkpoint(index, "failed")
                    raise

        if not generated_this_thread:
            new_threads.append(thread)
            new_messages.extend(existing)
            history.extend(existing)
        checkpoint(index, "partial")

    write_generation_outputs(
        output_dir,
        input_dir,
        new_messages,
        new_threads,
        manifest_base,
        args,
        generated,
        failed,
        "complete",
    )
    print(
        f"Wrote {len(new_messages)} messages and {len(new_threads)} threads "
        f"to {output_dir} (generated={generated}, failed={failed})"
    )


if __name__ == "__main__":
    main()
