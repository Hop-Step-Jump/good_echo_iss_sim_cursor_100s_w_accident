"""
Compare two ISS simulation runs (A/B) using lightweight interaction KPIs.

Usage:
  python analyze_iss_pair.py \
    --run-a outputs/spatial/output_iss_claude_run_a \
    --run-b outputs/spatial/output_iss_claude_run_b
"""

import argparse
import csv
import json
import os
from collections import Counter, defaultdict
from typing import Dict, Hashable, List, Set, Tuple


HELP_KEYWORDS = (
    "help",
    "thanks",
    "safe",
    "together",
    "share",
    "listen",
    "talk",
    "care",
    "support",
    "sorry",
)

CONFLICT_KEYWORDS = (
    "angry",
    "upset",
    "blame",
    "fault",
    "unfair",
    "annoy",
    "irritat",
    "frustrat",
    "stop",
    "だめ",
    "怒",
    "苛立",
    "不公平",
    "責任",
)

REPAIR_KEYWORDS = (
    "sorry",
    "thank",
    "thanks",
    "understand",
    "let's",
    "together",
    "help",
    "support",
    "appreciate",
    "ごめん",
    "ありがとう",
    "一緒",
    "助け",
    "理解",
)


def _read_jsonl(path: str) -> List[Dict]:
    if not os.path.exists(path):
        return []
    rows: List[Dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _read_tsv(path: str) -> List[Dict[str, str]]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def _to_int(value) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _agent_turns_to_messages(agent_turn_rows: List[Dict[str, str]]) -> List[Dict]:
    """Fallback adapter for closed-loop outputs without messages.jsonl.

    We treat each agent row as one communication artifact by preferring
    `social_post` then `private_talk` then `thought`.
    """
    messages: List[Dict] = []
    for row in agent_turn_rows:
        sender = row.get("agent_id")
        step = _to_int(row.get("step"))
        if not sender or step is None:
            continue
        msg = (
            row.get("social_post", "")
            or row.get("private_talk", "")
            or row.get("thought", "")
        )
        if not msg:
            continue
        messages.append({
            "step": step,
            "from": sender,
            "to": "ALL",
            "message": msg,
        })
    return messages


def _contains_help_signal(message: str) -> bool:
    text = (message or "").lower()
    return any(keyword in text for keyword in HELP_KEYWORDS)


def _contains_any_keyword(message: str, keywords: Tuple[str, ...]) -> bool:
    text = (message or "").lower()
    return any(keyword in text for keyword in keywords)


def _gini(values: List[int]) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(v for v in values if v >= 0)
    n = len(sorted_values)
    if n == 0:
        return 0.0
    total = sum(sorted_values)
    if total == 0:
        return 0.0
    weighted = 0.0
    for i, value in enumerate(sorted_values, start=1):
        weighted += i * value
    return (2.0 * weighted) / (n * total) - (n + 1.0) / n


def _in_step_window(row: Dict, start_step: int | None, end_step: int | None) -> bool:
    step = row.get("step")
    if not isinstance(step, int):
        return start_step is None and end_step is None
    if start_step is not None and step < start_step:
        return False
    if end_step is not None and step > end_step:
        return False
    return True


def summarize_run(
    run_dir: str,
    start_step: int | None = None,
    end_step: int | None = None,
) -> Dict:
    messages = _read_jsonl(os.path.join(run_dir, "messages.jsonl"))
    memory_reasoning = _read_jsonl(os.path.join(run_dir, "memory_reasoning.jsonl"))
    data_source = "messages_jsonl"
    fallback_used = False
    derived_metrics = False
    message_source_counts: Counter = Counter()
    if not messages:
        # closed-loop outputs use agent_turns.tsv instead of messages.jsonl
        agent_turn_rows = _read_tsv(os.path.join(run_dir, "agent_turns.tsv"))
        if agent_turn_rows:
            messages = _agent_turns_to_messages(agent_turn_rows)
            data_source = "agent_turns_tsv_proxy"
            fallback_used = True
            derived_metrics = True

    messages = [row for row in messages if _in_step_window(row, start_step, end_step)]
    memory_reasoning = [
        row for row in memory_reasoning if _in_step_window(row, start_step, end_step)
    ]
    message_source_counts = Counter(str(row.get("source", "unknown")) for row in messages)
    scripted_message_count = message_source_counts.get("scripted", 0)
    llm_message_count = message_source_counts.get("llm", 0)
    if message_source_counts and (scripted_message_count > 0 or set(message_source_counts) <= {"unknown"}):
        derived_metrics = True

    sent_by_agent: Counter = Counter()
    received_by_agent: Counter = Counter()
    unique_pairs: set[Tuple[Hashable, Hashable]] = set()
    directional_pairs: set[Tuple[Hashable, Hashable]] = set()
    undirected_pair_directions: Dict[Tuple[Hashable, Hashable], Set[Tuple[Hashable, Hashable]]] = defaultdict(set)
    partners_by_agent: Dict[Hashable, Set[Hashable]] = defaultdict(set)
    help_signals = 0
    conflict_events: List[Tuple[int, Tuple[int, int]]] = []
    repair_messages: List[Tuple[int, Tuple[int, int]]] = []

    for row in messages:
        sender = row.get("from", row.get("speaker_id"))
        receiver = row.get("to")
        if receiver is None:
            listener_ids = row.get("listener_ids")
            if isinstance(listener_ids, list) and listener_ids:
                receiver = listener_ids[0]
            elif isinstance(listener_ids, str):
                receiver = next((item for item in listener_ids.replace(",", ";").split(";") if item), None)
        step = row.get("step")
        msg = row.get("message", row.get("utterance", ""))
        tone = str(row.get("tone", ""))
        if isinstance(sender, (int, str)):
            sent_by_agent[sender] += 1
        if isinstance(receiver, (int, str)):
            received_by_agent[receiver] += 1
        if isinstance(sender, (int, str)) and isinstance(receiver, (int, str)):
            unique_pairs.add((sender, receiver))
            directional_pairs.add((sender, receiver))
            sender_s = str(sender)
            receiver_s = str(receiver)
            undirected = (
                sender if sender_s <= receiver_s else receiver,
                receiver if sender_s <= receiver_s else sender,
            )
            undirected_pair_directions[undirected].add((sender, receiver))
            partners_by_agent[sender].add(receiver)
            partners_by_agent[receiver].add(sender)
            is_conflict = tone in {"trouble", "caution"} or _contains_any_keyword(msg, CONFLICT_KEYWORDS)
            is_repair = tone == "repair" or _contains_any_keyword(msg, REPAIR_KEYWORDS)
            if is_conflict and isinstance(step, int):
                conflict_events.append((step, undirected))
            if is_repair and isinstance(step, int):
                repair_messages.append((step, undirected))
        if _contains_help_signal(msg):
            help_signals += 1

    active_agents = set(sent_by_agent.keys()) | set(received_by_agent.keys())
    numeric_agent_ids = [agent_id for agent_id in active_agents if isinstance(agent_id, int)]
    inferred_num_agents = (max(numeric_agent_ids) + 1) if numeric_agent_ids else 0

    isolates = []
    if inferred_num_agents:
        for agent_id in range(inferred_num_agents):
            if sent_by_agent[agent_id] == 0 and received_by_agent[agent_id] == 0:
                isolates.append(agent_id)

    reasoning_density = defaultdict(int)
    for row in memory_reasoning:
        agent_id = row.get("id")
        reasoning = (row.get("reasoning") or "").strip()
        if isinstance(agent_id, (int, str)) and reasoning:
            reasoning_density[agent_id] += 1

    reciprocal_pairs = 0
    for directions in undirected_pair_directions.values():
        if len(directions) >= 2:
            reciprocal_pairs += 1
    reciprocity_rate = (
        reciprocal_pairs / len(undirected_pair_directions)
        if undirected_pair_directions
        else 0.0
    )

    repaired_conflicts = 0
    repair_window = 3
    repairs_by_pair: Dict[Tuple[int, int], List[int]] = defaultdict(list)
    for step, pair in repair_messages:
        repairs_by_pair[pair].append(step)
    for steps in repairs_by_pair.values():
        steps.sort()
    for conflict_step, pair in conflict_events:
        repaired = any(
            repair_step > conflict_step and repair_step <= conflict_step + repair_window
            for repair_step in repairs_by_pair.get(pair, [])
        )
        if repaired:
            repaired_conflicts += 1
    repair_after_conflict_rate = (
        repaired_conflicts / len(conflict_events) if conflict_events else 0.0
    )

    bridge_agents = []
    for agent_id, peers in partners_by_agent.items():
        sent = sent_by_agent.get(agent_id, 0)
        received = received_by_agent.get(agent_id, 0)
        if len(peers) >= 4 and sent >= 2 and received >= 2:
            bridge_agents.append(agent_id)
    bridge_agents.sort()

    all_agent_ids = sorted(active_agents)
    sent_loads = [sent_by_agent.get(agent_id, 0) for agent_id in all_agent_ids]
    load_fairness = 1.0 - _gini(sent_loads)

    summary = {
        "run_dir": run_dir,
        "data_source": data_source,
        "proxy_metrics": derived_metrics,
        "fallback_used": fallback_used,
        "message_source_counts": dict(sorted(message_source_counts.items())),
        "llm_message_count": llm_message_count,
        "scripted_message_count": scripted_message_count,
        "step_window": {
            "start_step": start_step if start_step is not None else "",
            "end_step": end_step if end_step is not None else "",
        },
        "total_messages": len(messages),
        "unique_interaction_pairs": len(unique_pairs),
        "help_signal_messages": help_signals,
        "reciprocity_rate": round(reciprocity_rate, 4),
        "repair_after_conflict_rate": round(repair_after_conflict_rate, 4),
        "conflict_events": len(conflict_events),
        "bridge_agent_count": len(bridge_agents),
        "bridge_agents": bridge_agents,
        "load_fairness": round(load_fairness, 4),
        "active_agents": len(active_agents),
        "isolated_agents": isolates,
        "sent_by_agent": dict(sorted(sent_by_agent.items())),
        "received_by_agent": dict(sorted(received_by_agent.items())),
        "reasoning_entries_by_agent": dict(sorted(reasoning_density.items())),
    }
    if fallback_used:
        summary["note"] = (
            "messages.jsonl が見つからなかったため、agent_turns.tsv から "
            "social_post/private_talk/thought を代理メッセージとして集計。"
        )
    elif derived_metrics and llm_message_count:
        summary["note"] = (
            "messages.jsonl に source=llm と source=scripted が混在しているため、"
            "KPIは部分的にプロキシを含む。全threadをLLM化したrunで本集計する。"
        )
    elif derived_metrics:
        summary["note"] = (
            "messages.jsonl は存在するが source=scripted のUI派生発話を集計しているため、"
            "実LLM会話ではなくプロキシKPIとして扱う。"
        )
    return summary


def _delta(a: Dict, b: Dict) -> Dict:
    return {
        "messages_diff_b_minus_a": b["total_messages"] - a["total_messages"],
        "pairs_diff_b_minus_a": b["unique_interaction_pairs"] - a["unique_interaction_pairs"],
        "help_signals_diff_b_minus_a": b["help_signal_messages"] - a["help_signal_messages"],
        "reciprocity_rate_diff_b_minus_a": round(
            b["reciprocity_rate"] - a["reciprocity_rate"], 4
        ),
        "repair_after_conflict_rate_diff_b_minus_a": round(
            b["repair_after_conflict_rate"] - a["repair_after_conflict_rate"], 4
        ),
        "bridge_agent_count_diff_b_minus_a": (
            b["bridge_agent_count"] - a["bridge_agent_count"]
        ),
        "load_fairness_diff_b_minus_a": round(
            b["load_fairness"] - a["load_fairness"], 4
        ),
        "isolates_diff_b_minus_a": len(b["isolated_agents"]) - len(a["isolated_agents"]),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare ISS run A/B metrics.")
    parser.add_argument("--run-a", required=True, help="Run A output directory")
    parser.add_argument("--run-b", required=True, help="Run B output directory")
    parser.add_argument(
        "--start-step",
        type=int,
        default=None,
        help="Optional inclusive start step filter (e.g. 45)",
    )
    parser.add_argument(
        "--end-step",
        type=int,
        default=None,
        help="Optional inclusive end step filter (e.g. 50)",
    )
    parser.add_argument(
        "--out",
        default="",
        help="Optional output JSON path. If omitted, only prints to stdout.",
    )
    args = parser.parse_args()

    if (
        args.start_step is not None
        and args.end_step is not None
        and args.start_step > args.end_step
    ):
        raise SystemExit("--start-step must be <= --end-step")

    summary_a = summarize_run(args.run_a, args.start_step, args.end_step)
    summary_b = summarize_run(args.run_b, args.start_step, args.end_step)
    comparison = {
        "run_a": summary_a,
        "run_b": summary_b,
        "delta": _delta(summary_a, summary_b),
    }

    print(json.dumps(comparison, ensure_ascii=False, indent=2))

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(comparison, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
