#!/usr/bin/env python3
"""Translate `utterance` fields in a messages.jsonl to Japanese, preserving Latin agent names."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

from deep_translator import GoogleTranslator

# Latin (or mixed) agent display names from domain pack + common variants in logs.
# Longer strings first so "Chen Wei" wins over "Wei" if we ever add short tokens.
_PROTECTED_LITERALS: list[str] = [
    "Makoto Tanaka",
    "Chen Wei",
    "Sofía",
    "Sofia",
    # `-san` must be protected with the name so we do not split `<<<PH>>>-san`.
    "Marcus-san",
    "Makoto-san",
    "Linh-san",
    "Amir-san",
    "Aisha-san",
    "Sofia-san",
    "Sofía-san",
    "Fatima-san",
    "Priya-san",
    "Henri-san",
    "Agents Three-san",
    "Agents Eight-san",
    "Agents Three",
    "Agents Eight",
    "Marcus",
    "Priya",
    "Fatima",
    "Aisha",
    "Henri",
    "Amir",
    "Linh",
    "Makoto",
]

# Google sometimes returns "<<<PH_6 >>>" (extra spaces) — match flexibly.
_PH_TOKEN_RE = re.compile(r"<<<\s*PH\s*_(\d+)\s*>>>")
_AGENT_RE = re.compile(r"\bAgent\s+\d+\b", re.IGNORECASE)
_AGENTS_WORD_RE = re.compile(
    r"\bAgents\s+(?:One|Two|Three|Four|Five|Six|Seven|Eight|Nine|Ten)(?:-san)?\b",
    re.IGNORECASE,
)


def _build_protect_map(text: str) -> tuple[str, dict[str, str]]:
    """Replace protected spans and Agent N with placeholders."""
    mapping: dict[str, str] = {}
    out = text
    idx = 0

    def add_placeholder(original: str) -> str:
        nonlocal idx
        token = f"<<<PH_{idx}>>>"
        mapping[token] = original
        idx += 1
        return token

    # Agent 5, agent 12, ... (right-to-left so indices stay valid)
    for m in reversed(list(_AGENT_RE.finditer(out))):
        orig = m.group(0)
        normalized = "Agent " + orig.split()[-1]
        ph = add_placeholder(normalized)
        out = out[: m.start()] + ph + out[m.end() :]

    # "Agents Three-san", etc. (word agents, not digits)
    for m in reversed(list(_AGENTS_WORD_RE.finditer(out))):
        ph = add_placeholder(m.group(0))
        out = out[: m.start()] + ph + out[m.end() :]

    # Longest agent-name literals first (e.g. Makoto Tanaka before Makoto).
    for lit in sorted(_PROTECTED_LITERALS, key=len, reverse=True):
        if not lit:
            continue
        pattern = re.compile(re.escape(lit), re.IGNORECASE)
        pos = 0
        parts: list[str] = []
        for m in pattern.finditer(out):
            parts.append(out[pos : m.start()])
            parts.append(add_placeholder(m.group(0)))
            pos = m.end()
        parts.append(out[pos:])
        out = "".join(parts)

    return out, mapping


def _restore_placeholders(translated: str, mapping: dict[str, str]) -> str:
    def repl(m: re.Match[str]) -> str:
        key = f"<<<PH_{m.group(1)}>>>"
        return mapping.get(key, m.group(0))

    return _PH_TOKEN_RE.sub(repl, translated)


def _split_plain_for_api(plain: str, max_chars: int) -> list[str]:
    """Split plaintext (no placeholders) under max_chars; prefer sentence boundaries."""
    plain = plain.strip()
    if not plain:
        return []
    if len(plain) <= max_chars:
        return [plain]
    sentences = re.split(r"(?<=[.!?。！？…])\s+", plain)
    chunks: list[str] = []
    buf = ""
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        sep = " " if buf and buf[-1].isascii() and s[:1].isascii() else ""
        cand = f"{buf}{sep}{s}" if buf else s
        if len(cand) <= max_chars:
            buf = cand
            continue
        if buf:
            chunks.append(buf)
            buf = ""
        if len(s) <= max_chars:
            buf = s
            continue
        i = 0
        while i < len(s):
            j = min(i + max_chars, len(s))
            if j < len(s):
                cut = max(s.rfind(" ", i + 24, j), s.rfind(".", i + 24, j), s.rfind("。", i + 24, j))
                j = cut + 1 if cut > i else j
            piece = s[i:j].strip()
            if piece:
                chunks.append(piece)
            i = j
    if buf:
        chunks.append(buf)
    return chunks


def _masked_translate_units(masked: str, *, max_plain_chars: int = 130) -> list[str]:
    """Alternating units: either a placeholder token (do not send to API) or plain text to translate."""
    parts = re.split(r"(<<<PH_\d+>>>)", masked)
    units: list[str] = []
    for p in parts:
        if not p:
            continue
        if p.startswith("<<<PH_") and p.endswith(">>>"):
            units.append(p)
        else:
            units.extend(_split_plain_for_api(p, max_plain_chars))
    return units


def _translate_chunk(translator: GoogleTranslator, text: str, retries: int = 4) -> str:
    text = text.strip()
    if not text:
        return text
    delay = 1.5
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            return translator.translate(text)
        except Exception as e:  # noqa: BLE001 — vendor errors vary
            last_err = e
            time.sleep(delay)
            delay = min(delay * 2, 30)
    print(f"WARN: translate failed after retries: {last_err}", file=sys.stderr)
    return text


def _translate_chunk_auto_then_en(
    translator_auto: GoogleTranslator,
    chunk: str,
    *,
    min_ascii_retry_len: int = 32,
) -> str:
    """`auto` often returns English unchanged for mixed JA/EN; fall back to `en` for that chunk."""
    out = _translate_chunk(translator_auto, chunk)
    if (
        out.strip() == chunk.strip()
        and chunk[:1].isascii()
        and len(chunk) >= min_ascii_retry_len
    ):
        out = _translate_chunk(GoogleTranslator(source="en", target="ja"), chunk)
    return out


def translate_utterance_to_ja(
    utterance: str,
    *,
    translator: GoogleTranslator | None = None,
    between_chunks_sleep: float = 0.12,
) -> str:
    """Mask names, translate plain segments only (placeholders never hit the API), restore names."""
    utterance = utterance.strip()
    if not utterance:
        return utterance
    masked, ph_map = _build_protect_map(utterance)
    tr = translator or GoogleTranslator(source="auto", target="ja")
    units = _masked_translate_units(masked)
    parts: list[str] = []
    for k, unit in enumerate(units):
        if unit.startswith("<<<PH_") and _PH_TOKEN_RE.fullmatch(unit):
            parts.append(unit)
        else:
            parts.append(_translate_chunk_auto_then_en(tr, unit))
        if k + 1 < len(units):
            time.sleep(between_chunks_sleep)
    ja = "".join(parts)
    ja = _restore_placeholders(ja, ph_map)
    if ja.strip() == utterance.strip() and utterance[:1].isascii():
        parts2: list[str] = []
        for unit in units:
            if unit.startswith("<<<PH_") and _PH_TOKEN_RE.fullmatch(unit):
                parts2.append(unit)
            else:
                parts2.append(_translate_chunk(GoogleTranslator(source="en", target="ja"), unit))
        ja = _restore_placeholders("".join(parts2), ph_map)
    return ja


def _repair_indices(current_utterances: list[str], backup_utterances: list[str]) -> list[int]:
    need: list[int] = []
    for i, (uo, ub) in enumerate(zip(current_utterances, backup_utterances)):
        if uo == ub:
            need.append(i)
        elif _PH_TOKEN_RE.search(uo) or "<<<PH" in uo:
            need.append(i)
    return need


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--sleep", type=float, default=0.12, help="Seconds between API calls")
    p.add_argument(
        "--repair-with-backup",
        type=Path,
        metavar="BACKUP_EN_JSONL",
        help="Re-translate lines that match backup (still English) or contain leaked <<<PH_>>> tokens; uses backup utterance as source.",
    )
    args = p.parse_args()

    raw = args.input.read_text(encoding="utf-8")
    cur_lines = [ln for ln in raw.splitlines() if ln.strip()]

    if args.repair_with_backup:
        bak_raw = args.repair_with_backup.read_text(encoding="utf-8")
        bak_lines = [ln for ln in bak_raw.splitlines() if ln.strip()]
        if len(bak_lines) != len(cur_lines):
            print(
                f"ERR: line count mismatch current={len(cur_lines)} backup={len(bak_lines)}",
                file=sys.stderr,
            )
            return 1
        cur_u: list[str] = []
        bak_u: list[str] = []
        cur_rows: list[dict] = []
        for cl, bl in zip(cur_lines, bak_lines, strict=True):
            try:
                cr = json.loads(cl)
                br = json.loads(bl)
            except json.JSONDecodeError as e:
                print(f"ERR JSON: {e}", file=sys.stderr)
                return 1
            cu = cr.get("utterance")
            bu = br.get("utterance")
            if not isinstance(cu, str) or not isinstance(bu, str):
                print("ERR: missing utterance", file=sys.stderr)
                return 1
            cur_rows.append(cr)
            cur_u.append(cu)
            bak_u.append(bu)
        fix_i = _repair_indices(cur_u, bak_u)
        tr = GoogleTranslator(source="auto", target="ja")
        print(f"repair: {len(fix_i)} lines", file=sys.stderr)
        for n, i in enumerate(fix_i):
            cur_rows[i]["utterance"] = translate_utterance_to_ja(
                bak_u[i], translator=tr, between_chunks_sleep=args.sleep
            )
            time.sleep(args.sleep)
            if (n + 1) % 5 == 0:
                print(f"  … {n + 1}/{len(fix_i)}", file=sys.stderr)
        out_lines = [json.dumps(r, ensure_ascii=False) for r in cur_rows]
    else:
        translator = GoogleTranslator(source="auto", target="ja")
        out_lines = []
        for line_no, line in enumerate(cur_lines, 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                print(f"ERR line {line_no}: {e}", file=sys.stderr)
                return 1
            utt = row.get("utterance")
            if not isinstance(utt, str):
                out_lines.append(json.dumps(row, ensure_ascii=False))
                continue
            masked, ph_map = _build_protect_map(utt)
            ja = _translate_chunk(translator, masked)
            row["utterance"] = _restore_placeholders(ja, ph_map)
            out_lines.append(json.dumps(row, ensure_ascii=False))
            time.sleep(args.sleep)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
