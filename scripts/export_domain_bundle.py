#!/usr/bin/env python3
"""Export a clean, domain-pack-driven handoff bundle."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sim_core.domain_pack import resolve_domain_pack


DEFAULT_BUNDLE_ROOT = ROOT / "outputs" / "runs" / "domain_bundles"


def copy_file(src: Path, dst: Path, copied: list[str], dry_run: bool) -> None:
    copied.append(str(src))
    if dry_run:
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)


def destination_for(pack_dir: Path, bundle_pack_dir: Path, path: Path, key: str) -> Path:
    if path.is_relative_to(pack_dir):
        return bundle_pack_dir / path.relative_to(pack_dir)
    safe_key = key.replace(".", "__")
    return bundle_pack_dir / "_external_refs" / safe_key / path.name


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bundle resolved domain-pack files for handoff.")
    parser.add_argument("--pack", type=Path, required=True)
    parser.add_argument("--scenario", default="")
    parser.add_argument("--bundle-root", type=Path, default=DEFAULT_BUNDLE_ROOT)
    parser.add_argument("--bundle-name", default="")
    parser.add_argument("--run-dir", type=Path, action="append", default=[], help="Optional run directory to copy.")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    resolved = resolve_domain_pack(args.pack, scenario=args.scenario or None)
    if not resolved.validation.ok:
        joined = "\n".join(f"- {item}" for item in resolved.validation.errors)
        raise SystemExit(f"Domain pack validation failed:\n{joined}")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bundle_name = args.bundle_name.strip() or f"{resolved.pack_id}_{stamp}"
    bundle_dir = args.bundle_root / bundle_name
    bundle_pack_dir = bundle_dir / "domain_pack"
    copied: list[str] = []
    missing: list[str] = []
    seen_sources: set[Path] = set()

    domain_yaml = resolved.pack_dir / "domain.yaml"
    copy_file(domain_yaml, bundle_pack_dir / "domain.yaml", copied, args.dry_run)
    if args.scenario and resolved.config.get("scenario_file"):
        scenario_path = Path(str(resolved.config["scenario_file"]))
        copy_file(
            scenario_path,
            bundle_pack_dir / "scenarios" / scenario_path.name,
            copied,
            args.dry_run,
        )

    for key, paths in sorted(resolved.files.items()):
        for path in paths:
            if path in seen_sources:
                continue
            seen_sources.add(path)
            if not path.exists():
                missing.append(str(path))
                continue
            dst = destination_for(resolved.pack_dir, bundle_pack_dir, path, key)
            copy_file(path, dst, copied, args.dry_run)

    for run_dir in args.run_dir:
        if not run_dir.exists():
            missing.append(str(run_dir))
            continue
        for path in sorted(item for item in run_dir.rglob("*") if item.is_file()):
            dst = bundle_dir / "runs" / run_dir.name / path.relative_to(run_dir)
            copy_file(path, dst, copied, args.dry_run)

    manifest: dict[str, Any] = {
        "kind": "domain_bundle",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "pack_id": resolved.pack_id,
        "scenario": args.scenario,
        "bundle_dir": str(bundle_dir),
        "copied_files": copied,
        "missing_files": missing,
        "validation": resolved.validation.as_dict(),
        "resolved_files": {
            key: [str(path) for path in paths]
            for key, paths in sorted(resolved.files.items())
        },
    }
    if not args.dry_run:
        bundle_dir.mkdir(parents=True, exist_ok=True)
        (bundle_dir / "bundle_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
