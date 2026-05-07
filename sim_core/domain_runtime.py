from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .domain_pack import ResolvedDomainPack, resolve_domain_pack


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _split_csv(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _nested(config: dict[str, Any], keys: Iterable[str], fallback: Any = None) -> Any:
    current: Any = config
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return fallback
        current = current[key]
    return current


@dataclass(slots=True)
class DomainRuntime:
    resolved: ResolvedDomainPack | None = None

    @classmethod
    def load(cls, pack: Path | None, scenario: str | None = None) -> "DomainRuntime":
        if not pack:
            return cls(None)
        resolved = resolve_domain_pack(pack, scenario=scenario)
        if not resolved.validation.ok:
            joined = "\n".join(f"- {item}" for item in resolved.validation.errors)
            raise SystemExit(f"Domain pack validation failed:\n{joined}")
        return cls(resolved)

    @property
    def config(self) -> dict[str, Any]:
        return self.resolved.config if self.resolved else {}

    @property
    def data(self) -> dict[str, Any]:
        value = self.config.get("data", {})
        return value if isinstance(value, dict) else {}

    @property
    def runtime(self) -> dict[str, Any]:
        value = self.config.get("runtime", {})
        return value if isinstance(value, dict) else {}

    @property
    def pipeline(self) -> dict[str, Any]:
        value = self.config.get("pipeline", {})
        return value if isinstance(value, dict) else {}

    def data_path(self, key: str, fallback: Path | None = None) -> Path | None:
        value = self.data.get(key)
        if value is None:
            return fallback
        return Path(str(value))

    def data_paths(self, keys: Iterable[str]) -> list[Path]:
        paths: list[Path] = []
        for key in keys:
            value = self.data.get(key)
            for item in _as_list(value):
                if item:
                    paths.append(Path(str(item)))
        return paths

    def default_ids(self, key: str) -> list[str]:
        return _split_csv(self.runtime.get(key))

    def scenario_mode_default(self, fallback: str = "all") -> str:
        return str(
            _nested(
                self.pipeline,
                ("scenario_modes", "default"),
                self.runtime.get("default_scenario_mode", fallback),
            )
        )

    def agent_source_keys(self) -> list[str]:
        keys = _split_csv(_nested(self.pipeline, ("agents", "sources")))
        if keys:
            return keys
        if self.data.get("agent_sources"):
            return _split_csv(self.data.get("agent_sources"))
        if self.data.get("agents"):
            return ["agents"]
        return []

    def agent_source_paths(self, explicit: Iterable[Path] | None = None) -> list[Path]:
        explicit_paths = [path for path in explicit or [] if path]
        if explicit_paths:
            return explicit_paths
        return self.data_paths(self.agent_source_keys())

    def panel_path(self, fallback: Path | None = None) -> Path | None:
        keys = _split_csv(_nested(self.pipeline, ("agents", "panel_keys")))
        if not keys:
            keys = ["agent_panel"]
        for key in keys:
            path = self.data_path(key)
            if path:
                return path
        return fallback

    def event_input_fields(self) -> list[str]:
        fields = _split_csv(_nested(self.pipeline, ("events", "input_text_fields")))
        return fields or ["description"]

    def event_state_field(self) -> str:
        return str(_nested(self.pipeline, ("events", "state_summary_field"), "state_summary"))

    def legacy_event_state_fields(self) -> list[str]:
        return _split_csv(_nested(self.pipeline, ("events", "legacy_state_summary_fields")))

    def state_config(self) -> dict[str, Any]:
        value = self.pipeline.get("state", {})
        return value if isinstance(value, dict) else {}

    def state_fields(self) -> list[str]:
        fields = self.state_config().get("fields", [])
        if isinstance(fields, list) and fields and isinstance(fields[0], dict):
            return [str(item["id"]) for item in fields if item.get("id")]
        return _split_csv(fields)

    def state_field_labels(self) -> dict[str, str]:
        labels: dict[str, str] = {}
        fields = self.state_config().get("fields", [])
        if isinstance(fields, list):
            for item in fields:
                if isinstance(item, dict) and item.get("id"):
                    labels[str(item["id"])] = str(item.get("label", item["id"]))
        labels.update({
            str(key): str(value)
            for key, value in (self.state_config().get("labels") or {}).items()
        })
        return labels

    def state_buffer_field(self) -> str:
        fields = self.state_fields()
        return str(self.state_config().get("buffer_field") or (fields[-1] if fields else "buffer"))

    def state_negative_fields(self) -> list[str]:
        configured = _split_csv(self.state_config().get("negative_fields"))
        if configured:
            return configured
        buffer_field = self.state_buffer_field()
        return [field for field in self.state_fields() if field != buffer_field]

    def state_output_name(self) -> str:
        return str(self.state_config().get("output") or "societal_state.tsv")

    def feedback_state_output_name(self) -> str:
        return str(self.state_config().get("feedback_output") or "societal_state_feedback.tsv")

    def legacy_state_output_names(self) -> list[str]:
        return _split_csv(self.state_config().get("legacy_outputs"))

    def state_context_field(self) -> str:
        return str(self.state_config().get("context_field") or "context_for_agents")

    def state_dominant_field(self) -> str:
        return str(self.state_config().get("dominant_field") or "dominant_pressure")

    def state_high_risk_field(self) -> str:
        return str(self.state_config().get("high_risk_field") or "high_risk_factors")

    def spillover_weight_column(self) -> str:
        return str(self.state_config().get("spillover_weight_column") or "spillover_weight")

    def phase_dir(self, phase: str, fallback: str) -> str:
        return str(_nested(self.pipeline, ("phases", phase, "directory"), fallback))

    def phase_script(self, phase: str, fallback: str) -> str:
        return str(_nested(self.pipeline, ("phases", phase, "script"), fallback))

    def output_name(self, key: str, fallback: str) -> str:
        return str(_nested(self.pipeline, ("outputs", key), fallback))

    def evaluation_labels(self) -> dict[str, list[str]]:
        labels = _nested(self.pipeline, ("labels", "evaluation"), {})
        if isinstance(labels, dict):
            return {
                str(label): [str(item) for item in _as_list(emotions)]
                for label, emotions in labels.items()
            }
        return {}

    def default_evaluation(self, fallback: str = "neutral") -> str:
        return str(_nested(self.pipeline, ("labels", "default_evaluation"), fallback))

    def default_emotion(self, evaluation: str, fallback: str = "neutral") -> str:
        labels = self.evaluation_labels()
        emotions = labels.get(evaluation) or labels.get(self.default_evaluation())
        return str(emotions[0]) if emotions else fallback

    def action_categories(self) -> list[str]:
        return _split_csv(_nested(self.pipeline, ("labels", "action_categories")))

    def column_aliases(self, canonical: str, fallback: Iterable[str] | None = None) -> list[str]:
        aliases = self.config.get("column_aliases", {})
        values = aliases.get(canonical) if isinstance(aliases, dict) else None
        fields = _split_csv(values)
        if fields:
            return fields
        return [str(item) for item in (fallback or [canonical]) if str(item)]

    def layer_column_candidates(self) -> list[str]:
        return _split_csv(_nested(self.pipeline, ("agents", "layer_columns"))) or ["layer"]

    def agent_id_columns(self) -> list[str]:
        return self.column_aliases("agent_id", ["agent_id"])

    def population_weight_columns(self) -> list[str]:
        return self.column_aliases("population_weight", ["population_weight"])

    def agent_layer_prefixes(self) -> dict[str, str]:
        value = _nested(self.pipeline, ("agents", "legacy_id_prefix_layers"), {})
        if not isinstance(value, dict):
            return {}
        return {str(prefix): str(layer) for prefix, layer in value.items()}

    def agent_layer_age_bands(self) -> list[dict[str, Any]]:
        value = _nested(self.pipeline, ("agents", "age_layer_bands"), [])
        return [item for item in value if isinstance(item, dict)]

    def prompt_block(self, key: str, style: str = "neutral_v2") -> dict[str, Any]:
        prompts = self.pipeline.get("prompts", {})
        if not isinstance(prompts, dict):
            return {}
        block = prompts.get(key, {})
        if not isinstance(block, dict):
            return {}
        style_block = block.get(style, block.get("default", {}))
        return style_block if isinstance(style_block, dict) else {}

    def unit_output_fields(self) -> list[str]:
        fields = _split_csv(_nested(self.pipeline, ("units", "output_fields")))
        return fields

    def pressure_fields(self) -> list[str]:
        return self.state_fields()
