#!/usr/bin/env python3
"""Run a spatial_demo config referenced by a domain pack runtime section."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sim_core.domain_pack import resolve_domain_pack


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run examples/spatial_demo from domain pack runtime profiles."
    )
    parser.add_argument(
        "--pack",
        type=Path,
        required=True,
        help="Domain pack directory (e.g. domain_packs/iss_benevolence)",
    )
    parser.add_argument(
        "--scenario",
        default="",
        help="Optional scenario id from domain.yaml scenarios",
    )
    parser.add_argument(
        "--profile",
        default="",
        help="Runtime profile key from domain.yaml runtime.profiles",
    )
    parser.add_argument(
        "--python-bin",
        default=sys.executable,
        help="Python executable for running spatial demo main.py",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print resolved command only",
    )
    return parser.parse_args()


def _as_dict(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"'{field_name}' must be a mapping in domain.yaml")
    return value


def main() -> None:
    args = parse_args()
    scenario = args.scenario or None
    resolved = resolve_domain_pack(args.pack, scenario=scenario)

    if not resolved.validation.ok:
        joined = "\n".join(f"- {item}" for item in resolved.validation.errors)
        raise SystemExit(f"Domain pack validation failed:\n{joined}")

    runtime = _as_dict(resolved.config.get("runtime", {}), "runtime")
    profiles = _as_dict(runtime.get("profiles", {}), "runtime.profiles")
    entrypoint_raw = runtime.get("entrypoint")
    if not entrypoint_raw:
        raise SystemExit("runtime.entrypoint is required")

    profile = args.profile or str(runtime.get("default_profile", "")).strip()
    if not profile:
        raise SystemExit("No profile selected. Set --profile or runtime.default_profile")
    if profile not in profiles:
        available = ", ".join(sorted(profiles))
        raise SystemExit(
            f"Unknown profile '{profile}'. Available profiles: {available}"
        )

    entrypoint = Path(str(entrypoint_raw))
    config_path = Path(str(profiles[profile]))
    if not entrypoint.exists():
        raise SystemExit(f"entrypoint does not exist: {entrypoint}")
    if not config_path.exists():
        raise SystemExit(f"profile config does not exist: {config_path}")

    cmd = [args.python_bin, str(entrypoint), "--config", str(config_path)]
    print("$ " + " ".join(cmd))
    if args.dry_run:
        return

    completed = subprocess.run(cmd, cwd=ROOT, check=False)
    raise SystemExit(completed.returncode)


if __name__ == "__main__":
    main()
