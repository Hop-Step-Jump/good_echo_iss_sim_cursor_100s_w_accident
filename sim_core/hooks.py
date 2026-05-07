from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


HOOK_STAGES = (
    "pre_run",
    "pre_step",
    "pre_observation",
    "post_observation",
    "aggregate",
    "feedback",
    "post_step",
    "export_viewer",
    "audit",
)


@dataclass(slots=True)
class HookSpec:
    stage: str
    hook_id: str
    enabled: bool = True
    module: str = ""
    function: str = ""
    config: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "hook_id": self.hook_id,
            "enabled": self.enabled,
            "module": self.module,
            "function": self.function,
            "config": self.config,
        }


def normalize_hooks(config: dict[str, Any]) -> list[HookSpec]:
    """Normalize hooks from config.

    Supported shapes:
    hooks:
      pre_observation:
        - id: foo
          module: pkg.mod
          function: run
      export_viewer:
        - domain_viewer_export
    """

    raw_hooks = config.get("hooks", {})
    if not isinstance(raw_hooks, dict):
        return []

    hooks: list[HookSpec] = []
    for stage, entries in raw_hooks.items():
        if stage not in HOOK_STAGES:
            continue
        if entries is None:
            continue
        if not isinstance(entries, list):
            entries = [entries]
        for index, entry in enumerate(entries):
            if isinstance(entry, str):
                hooks.append(HookSpec(stage=stage, hook_id=entry))
                continue
            if not isinstance(entry, dict):
                hooks.append(HookSpec(stage=stage, hook_id=f"{stage}_{index}", enabled=False))
                continue
            hook_id = str(entry.get("id") or entry.get("hook_id") or f"{stage}_{index}")
            hooks.append(HookSpec(
                stage=stage,
                hook_id=hook_id,
                enabled=bool(entry.get("enabled", True)),
                module=str(entry.get("module", "")),
                function=str(entry.get("function", "")),
                config=dict(entry.get("config", {}) or {}),
            ))
    return hooks


def hook_warnings(config: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    raw_hooks = config.get("hooks", {})
    if not isinstance(raw_hooks, dict):
        if raw_hooks:
            warnings.append("hooks must be a mapping")
        return warnings
    for stage in raw_hooks:
        if stage not in HOOK_STAGES:
            warnings.append(f"Unknown hook stage: {stage}")
    for hook in normalize_hooks(config):
        if hook.enabled and (not hook.module or not hook.function):
            warnings.append(f"Enabled hook lacks module/function: {hook.stage}.{hook.hook_id}")
    return warnings
