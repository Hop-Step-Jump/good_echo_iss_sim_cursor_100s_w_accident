# iss_benevolence 本線移行メモ

## 現在の状態

- ISS正本ドキュメントを基準に、主線agentデータを `data/agents.tsv` へ統合済み
- `pipeline.agents.sources` は `agents` のみを参照
- Run A は `data/events_run_a.tsv`、Run B は `data/events_run_b.tsv` を参照
- `data/agent_panel.tsv`、`data/relationship_seed.tsv`、`data/places_iss_10.tsv`、`data/objects_menu.tsv` は `ISS00`〜`ISS09` のID体系に同期済み
- `scripts/run_agents.py` は clean event schema (`event_id/start_step/end_step/...`) を読める
- pack指定時に旧デフォルトauto eventsを勝手に混ぜないよう修正済み

## 旧互換として残っているもの

旧互換TSVは `docs/ISS/archive/iss_benevolence_legacy_data_2026-05-04/` へ退避済み。`domain_packs/iss_benevolence/` の主線dataからは外している。

## 次段階 TODO

1. `examples/spatial_demo/configs/config.iss.*.yaml` を `data/agents.tsv` / `places_iss_10.tsv` / `objects_menu.tsv` から生成する仕組みに寄せる
2. 旧closed-loopを参照する場合は `scripts/legacy/` から明示的に呼ぶ
3. 本物の `messages.jsonl` をagent観測側でも出す
4. 20 agents / 100 steps 版へ拡張する場合は、正本ペルソナ20人を `agents.tsv` の同一schemaで追加する
