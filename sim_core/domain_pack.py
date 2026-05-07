from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .hooks import hook_warnings, normalize_hooks


ROOT = Path(__file__).resolve().parents[1]
DEFAULTS_DIR = Path(__file__).resolve().parent / "defaults"


@dataclass(slots=True)
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": self.errors,
            "warnings": self.warnings,
        }


@dataclass(slots=True)
class ResolvedDomainPack:
    pack_dir: Path
    config: dict[str, Any]
    files: dict[str, list[Path]]
    validation: ValidationReport

    @property
    def pack_id(self) -> str:
        return str(self.config.get("pack_id", self.pack_dir.name))


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"YAML root must be a mapping: {path}")
    return payload


def deep_merge(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        merged = dict(base)
        for key, value in override.items():
            if value is None:
                continue
            merged[key] = deep_merge(merged.get(key), value)
        return merged
    if override is None:
        return base
    return override


def replace_tokens(value: Any, *, pack_dir: Path, scenario_dir: Path | None = None) -> Any:
    if isinstance(value, dict):
        return {key: replace_tokens(item, pack_dir=pack_dir, scenario_dir=scenario_dir) for key, item in value.items()}
    if isinstance(value, list):
        return [replace_tokens(item, pack_dir=pack_dir, scenario_dir=scenario_dir) for item in value]
    if not isinstance(value, str):
        return value
    replacements = {
        "${root}": str(ROOT),
        "${default}": str(DEFAULTS_DIR),
        "${pack}": str(pack_dir),
        "${scenario}": str(scenario_dir or pack_dir),
    }
    resolved = value
    for token, replacement in replacements.items():
        resolved = resolved.replace(token, replacement)
    return resolved


def load_default_config(inherits: str | None) -> dict[str, Any]:
    default_name = inherits or "default_v1"
    path = DEFAULTS_DIR / f"{default_name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Default config not found: {path}")
    return load_yaml(path)


def scenario_path(pack_config: dict[str, Any], pack_dir: Path, scenario: str | None) -> Path | None:
    if not scenario:
        return None
    scenarios = pack_config.get("scenarios", {})
    if not isinstance(scenarios, dict) or scenario not in scenarios:
        raise KeyError(f"Scenario '{scenario}' is not defined in {pack_dir / 'domain.yaml'}")
    raw_path = str(scenarios[scenario])
    raw_path = raw_path.replace("${pack}", str(pack_dir)).replace("${root}", str(ROOT))
    path = Path(raw_path)
    if not path.is_absolute():
        path = pack_dir / path
    return path


def iter_file_refs(config: dict[str, Any]) -> dict[str, list[Path]]:
    refs: dict[str, list[Path]] = {}

    def add(name: str, value: Any) -> None:
        if value is None:
            return
        if isinstance(value, list):
            refs[name] = [Path(str(item)) for item in value]
            return
        refs[name] = [Path(str(value))]

    data = config.get("data", {})
    if isinstance(data, dict):
        for key, value in data.items():
            add(f"data.{key}", value)
    prompts = config.get("prompts", {})
    if isinstance(prompts, dict):
        for key, value in prompts.items():
            add(f"prompts.{key}", value)
    viewer = config.get("viewer", {})
    if isinstance(viewer, dict):
        for key, value in viewer.items():
            if key.endswith("_path") or key in {"config"}:
                add(f"viewer.{key}", value)
    return refs


def read_tsv_header(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle, delimiter="\t")
        return next(reader, [])


def alias_values(config: dict[str, Any], canonical: str, fallback: list[str]) -> list[str]:
    aliases = config.get("column_aliases", {})
    value = aliases.get(canonical) if isinstance(aliases, dict) else None
    if value is None:
        return fallback
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def has_any(header: list[str], candidates: list[str]) -> bool:
    return any(candidate in header for candidate in candidates)


def validate_domain_pack(resolved: ResolvedDomainPack) -> ValidationReport:
    report = ValidationReport()
    config = resolved.config

    for key in ("schema_version", "pack_id", "display_name", "time", "data", "prompts"):
        if key not in config:
            report.errors.append(f"Missing required config key: {key}")

    report.warnings.extend(hook_warnings(config))

    required_files = [
        "data.agents",
        "data.events",
        "data.emotion_dictionary",
        "data.action_dictionary",
        "data.feedback_channels",
        "data.evaluation_metrics",
        "prompts.agent_observation",
    ]
    for key in required_files:
        paths = resolved.files.get(key)
        if not paths:
            report.errors.append(f"Missing required file reference: {key}")
            continue
        for path in paths:
            if not path.exists():
                report.errors.append(f"Referenced file does not exist: {key} -> {path}")

    agent_id_aliases = alias_values(config, "agent_id", ["agent_id"])
    population_aliases = alias_values(config, "population_weight", ["population_weight"])
    for path in resolved.files.get("data.agents", []):
        if not path.exists():
            continue
        header = read_tsv_header(path)
        if not has_any(header, agent_id_aliases):
            report.errors.append(f"Agent file lacks agent_id column: {path}")
        if not has_any(header, population_aliases):
            report.warnings.append(f"Agent file lacks population_weight column: {path}")

    event_id_aliases = alias_values(config, "event_id", ["event_id"])
    start_step_aliases = alias_values(config, "start_step", ["start_step"])
    for path in resolved.files.get("data.events", []):
        if not path.exists():
            continue
        header = read_tsv_header(path)
        if not has_any(header, event_id_aliases):
            report.errors.append(f"Event file lacks event_id column: {path}")
        if not has_any(header, start_step_aliases):
            report.warnings.append(f"Event file lacks start_step column: {path}")

    return report


def resolve_domain_pack(
    pack_dir: Path,
    *,
    scenario: str | None = None,
    cli_overrides: dict[str, Any] | None = None,
) -> ResolvedDomainPack:
    pack_dir = pack_dir.resolve()
    domain_path = pack_dir / "domain.yaml"
    if not domain_path.exists():
        raise FileNotFoundError(f"domain.yaml not found: {domain_path}")

    domain_config = load_yaml(domain_path)
    default_config = load_default_config(str(domain_config.get("inherits", "default_v1")))
    resolved = deep_merge(default_config, domain_config)

    scenario_dir: Path | None = None
    path = scenario_path(domain_config, pack_dir, scenario)
    if path:
        if not path.exists():
            raise FileNotFoundError(f"Scenario file not found: {path}")
        scenario_dir = path.parent
        scenario_config = load_yaml(path)
        resolved = deep_merge(resolved, scenario_config)
        resolved["scenario_id"] = scenario
        resolved["scenario_file"] = str(path)

    if cli_overrides:
        resolved = deep_merge(resolved, cli_overrides)

    resolved = replace_tokens(resolved, pack_dir=pack_dir, scenario_dir=scenario_dir)
    files = iter_file_refs(resolved)
    resolved_pack = ResolvedDomainPack(pack_dir=pack_dir, config=resolved, files=files, validation=ValidationReport())
    resolved_pack.validation = validate_domain_pack(resolved_pack)
    return resolved_pack


def load_domain_pack(pack_dir: Path, scenario: str | None = None) -> ResolvedDomainPack:
    return resolve_domain_pack(pack_dir, scenario=scenario)


def write_resolved_snapshot(resolved: ResolvedDomainPack, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pack_dir": str(resolved.pack_dir),
        "pack_id": resolved.pack_id,
        "validation": resolved.validation.as_dict(),
        "files": {key: [str(path) for path in paths] for key, paths in sorted(resolved.files.items())},
        "hooks": [hook.as_dict() for hook in normalize_hooks(resolved.config)],
        "config": resolved.config,
    }
    (output_dir / "resolved_config.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "validation_report.json").write_text(
        json.dumps(resolved.validation.as_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Domain pack resolver and validator.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_pack_args(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--pack", type=Path, required=True)
        subparser.add_argument("--scenario")
        subparser.add_argument("--steps", type=int)
        subparser.add_argument("--output-dir", type=Path)

    validate_parser = subparsers.add_parser("validate")
    add_pack_args(validate_parser)

    resolve_parser = subparsers.add_parser("resolve")
    add_pack_args(resolve_parser)

    args = parser.parse_args(argv)
    cli_overrides: dict[str, Any] = {}
    if args.steps is not None:
        cli_overrides["time"] = {"steps": args.steps}

    resolved = resolve_domain_pack(args.pack, scenario=args.scenario, cli_overrides=cli_overrides)
    if args.output_dir:
        write_resolved_snapshot(resolved, args.output_dir)

    print(json.dumps({
        "pack_id": resolved.pack_id,
        "ok": resolved.validation.ok,
        "errors": resolved.validation.errors,
        "warnings": resolved.validation.warnings,
        "files": {key: [str(path) for path in paths] for key, paths in sorted(resolved.files.items())},
    }, ensure_ascii=False, indent=2))
    return 0 if resolved.validation.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
