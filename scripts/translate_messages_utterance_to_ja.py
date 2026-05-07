#!/usr/bin/env python3
"""
Translate `utterance` field in a JSONL file to Japanese, while preserving
agent/person names that are written in Latin letters.

Designed to be fast enough for a few thousand lines by batching requests.

Usage:
  python scripts/translate_messages_utterance_to_ja.py \
    --in  outputs/runs/.../messages.jsonl \
    --out outputs/runs/.../messages_ja.jsonl
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple


NAME_PATTERNS: List[re.Pattern[str]] = [
    # "Agent 9", "Agent 0" etc.
    re.compile(r"\bAgent\s+\d+\b"),
    # Latin full names like "Chen Wei", "Aisha", "Henri", "Mission Control"
    re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3}\b"),
    # All-caps short tokens (HAB, LAB, ES/EN)
    re.compile(r"\b[A-Z]{2,5}(?:/[A-Z]{2,5})?\b"),
]

MARK_PREFIX = "<<<MSG_"
MARK_SUFFIX = ">>>"
PH_PREFIX = "<<<PH_"
PH_SUFFIX = ">>>"


@dataclass
class ProtectedText:
    text: str
    placeholders: Dict[str, str]


def protect_latin_names(s: str) -> ProtectedText:
    placeholders: Dict[str, str] = {}

    def _make_key(i: int) -> str:
        return f"{PH_PREFIX}{i:05d}{PH_SUFFIX}"

    # Collect matches with spans, then replace from end to start.
    spans: List[Tuple[int, int, str]] = []
    for pat in NAME_PATTERNS:
        for m in pat.finditer(s):
            spans.append((m.start(), m.end(), m.group(0)))

    # De-duplicate overlaps by preferring longer spans
    spans.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    filtered: List[Tuple[int, int, str]] = []
    last_end = -1
    for start, end, val in spans:
        if start < last_end:
            continue
        filtered.append((start, end, val))
        last_end = end

    out = s
    for idx, (start, end, val) in enumerate(reversed(filtered)):
        key = _make_key(idx)
        placeholders[key] = val
        out = out[:start] + key + out[end:]

    return ProtectedText(text=out, placeholders=placeholders)


def unprotect(s: str, placeholders: Dict[str, str]) -> str:
    # Put back in reverse-length order to be safe
    for k in sorted(placeholders.keys(), key=len, reverse=True):
        s = s.replace(k, placeholders[k])
    return s


def translate_batch(text: str) -> str:
    """
    Try Google first (best quality), then fall back to MyMemory (more tolerant).
    """
    try:
        from deep_translator import GoogleTranslator, MyMemoryTranslator  # type: ignore
        from deep_translator.exceptions import RequestError  # type: ignore
    except Exception as e:  # pragma: no cover
        raise RuntimeError("deep-translator is required. Install with: pip install deep-translator") from e

    try:
        return GoogleTranslator(source="auto", target="ja").translate(text)
    except Exception as e:
        # If Google blocks/rate-limits, MyMemory often still works.
        if isinstance(e, RequestError) or "Request" in e.__class__.__name__:
            return MyMemoryTranslator(source="auto", target="ja-JP").translate(text)
        raise


def chunk_messages(msgs: List[str], max_chars: int = 900) -> List[List[Tuple[int, str]]]:
    """
    Create chunks of (index, utterance) with an upper bound on total chars.
    Uses a conservative bound because the underlying service can be strict.
    """
    chunks: List[List[Tuple[int, str]]] = []
    cur: List[Tuple[int, str]] = []
    cur_len = 0
    for i, u in enumerate(msgs):
        # Add marker line overhead
        overhead = len(MARK_PREFIX) + 8 + len(MARK_SUFFIX) + 2
        add_len = len(u) + overhead
        if cur and cur_len + add_len > max_chars:
            chunks.append(cur)
            cur = []
            cur_len = 0
        cur.append((i, u))
        cur_len += add_len
    if cur:
        chunks.append(cur)
    return chunks


def build_payload(chunk: List[Tuple[int, str]]) -> str:
    parts: List[str] = []
    for idx, utter in chunk:
        parts.append(f"{MARK_PREFIX}{idx:05d}{MARK_SUFFIX}")
        parts.append(utter)
    return "\n".join(parts)


def parse_payload(translated: str) -> Dict[int, str]:
    """
    Parse translated payload back into {index: utterance}.
    We locate markers and take text until next marker.
    """
    out: Dict[int, str] = {}
    # Split while keeping markers
    marker_re = re.compile(rf"{re.escape(MARK_PREFIX)}(\d{{5}}){re.escape(MARK_SUFFIX)}")
    matches = list(marker_re.finditer(translated))
    for j, m in enumerate(matches):
        idx = int(m.group(1))
        start = m.end()
        end = matches[j + 1].start() if j + 1 < len(matches) else len(translated)
        seg = translated[start:end].strip("\n ").strip()
        out[idx] = seg
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="in_path", required=True)
    ap.add_argument("--out", dest="out_path", required=True)
    ap.add_argument("--sleep", type=float, default=0.15, help="Sleep between batches")
    ap.add_argument("--retries", type=int, default=3)
    args = ap.parse_args()

    rows: List[Dict[str, Any]] = []
    utterances: List[str] = []

    with open(args.in_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            rows.append(obj)
            utterances.append(str(obj.get("utterance", "")))

    protected: List[ProtectedText] = [protect_latin_names(u) for u in utterances]
    protected_utterances = [p.text for p in protected]

    chunks = chunk_messages(protected_utterances)
    translated_map: Dict[int, str] = {}

    total_chunks = len(chunks)
    for chunk_i, chunk in enumerate(chunks, start=1):
        payload = build_payload(chunk)
        print(f"[translate] chunk {chunk_i}/{total_chunks} (items={len(chunk)}, chars={len(payload)})", file=sys.stderr)
        last_err: Exception | None = None
        for attempt in range(args.retries):
            try:
                translated = translate_batch(payload)
                parsed = parse_payload(translated)
                # Ensure all indices in this chunk exist; if not, treat as failure.
                want = {idx for idx, _ in chunk}
                got = set(parsed.keys())
                if want != got:
                    raise RuntimeError(f"marker mismatch: want={len(want)} got={len(got)}")
                translated_map.update(parsed)
                last_err = None
                break
            except Exception as e:
                last_err = e
                # backoff
                time.sleep(0.6 * (attempt + 1))
        if last_err is not None:
            raise last_err
        time.sleep(args.sleep)

    # Apply translations back
    for i, obj in enumerate(rows):
        t = translated_map.get(i, protected_utterances[i])
        t = unprotect(t, protected[i].placeholders)
        obj["utterance"] = t

    with open(args.out_path, "w", encoding="utf-8") as f:
        for obj in rows:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

