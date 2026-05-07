# ISS 善性オブジェクト実験パック（軽量）

このパックは、`docs/ISS/` で設計した ISS 実験を `domain_packs` 配下で管理するための軽量雛形。

目的:

- 実験設計（ペルソナ、環境、オブジェクト、ゴール）をパック単位で再利用できるようにする
- Run A（対照群）/ Run B（介入群）を同じ枠組みで比較できるようにする
- 発表・引き継ぎ時に「どの設定で回したか」を明確に残す

重要:

- `sim_core` の `domain.yaml` 解決・検証フローには接続済み
- 実行エンジンは `domain.yaml` の `runtime` / `pipeline` に書き、入口は中立scriptを使う
- 旧closed-loopブリッジは `scripts/legacy/` に退避済み。ISS正本データの主線ではない
- 実行時に必要なデータは `iss_benevolence/data/` 内で完結させる

## 実行エントリ

- Claude smoke A: `examples/spatial_demo/configs/config.iss.claude.smoke.run_a.yaml`
- Claude smoke B: `examples/spatial_demo/configs/config.iss.claude.smoke.run_b.yaml`
- Claude full A: `examples/spatial_demo/configs/config.iss.claude.run_a.yaml`
- Claude full B: `examples/spatial_demo/configs/config.iss.claude.run_b.yaml`
- Codex smoke A: `examples/spatial_demo/configs/config.iss.codex.smoke.run_a.yaml`
- Codex smoke B: `examples/spatial_demo/configs/config.iss.codex.smoke.run_b.yaml`
- Codex full A: `examples/spatial_demo/configs/config.iss.codex.run_a.yaml`
- Codex full B: `examples/spatial_demo/configs/config.iss.codex.run_b.yaml`
- Cursor smoke A: `examples/spatial_demo/configs/config.iss.cursor.smoke.run_a.yaml`
- Cursor smoke B: `examples/spatial_demo/configs/config.iss.cursor.smoke.run_b.yaml`
- Cursor full A: `examples/spatial_demo/configs/config.iss.cursor.run_a.yaml`
- Cursor full B: `examples/spatial_demo/configs/config.iss.cursor.run_b.yaml`

## 最小手順

```bash
# 1) domain packの検証
python -m sim_core validate --pack domain_packs/iss_benevolence --scenario run_a

# 2) providerごとにログイン（いずれか1つ）
claude login
# codex login
# cursor agent login

# 3) domain runtime profileを実行
python scripts/run_profile.py --pack domain_packs/iss_benevolence --scenario run_a --profile claude_smoke_a
python scripts/run_profile.py --pack domain_packs/iss_benevolence --scenario run_b --profile claude_smoke_b

# 4) A/B比較
python examples/spatial_demo/analyze_iss_pair.py \
  --run-a outputs/spatial/output_iss_claude_run_a \
  --run-b outputs/spatial/output_iss_claude_run_b
```

## 中立実行インターフェース

domain packのprofileを指定して実行します。

```bash
python scripts/run_profile.py --pack domain_packs/iss_benevolence --scenario run_a --profile claude_smoke_a
python scripts/run_profile.py --pack domain_packs/iss_benevolence --scenario run_b --profile claude_smoke_b
python scripts/run_profile.py --pack domain_packs/iss_benevolence --scenario run_b --profile cursor_full_b
```

## プロファイル切り替え例

```bash
# Codex smoke B を指定実行
python scripts/run_profile.py \
  --pack domain_packs/iss_benevolence \
  --scenario run_b \
  --profile codex_smoke_b

# Cursor smoke A を指定実行
python scripts/run_profile.py \
  --pack domain_packs/iss_benevolence \
  --scenario run_a \
  --profile cursor_smoke_a

# Cursor full B を指定実行
python scripts/run_profile.py \
  --pack domain_packs/iss_benevolence \
  --scenario run_b \
  --profile cursor_full_b
```

## agent観測ランナー接続

```bash
./venv/bin/python scripts/run_agents.py \
  --pack domain_packs/iss_benevolence \
  --scenario run_b \
  --start-step 1 \
  --steps 1 \
  --agent-ids ISS00,ISS01 \
  --output-dir outputs/runs/iss_agent_probe
```

状態行とauto eventを先に作る場合:

```bash
python scripts/build_state.py \
  --pack domain_packs/iss_benevolence \
  --scenario run_b \
  --output-dir outputs/runs/iss_state_probe
```

Habitat UI用frameを作る場合:

```bash
python scripts/export_habitat_frames.py \
  --pack domain_packs/iss_benevolence \
  --scenario run_b \
  --state-tsv outputs/runs/iss_state_probe/societal_state.tsv \
  --output-dir outputs/runs/iss_habitat_probe
```

引き渡し用bundle:

```bash
python scripts/export_domain_bundle.py \
  --pack domain_packs/iss_benevolence \
  --scenario run_b \
  --bundle-name iss_clean_handoff
```

`runtime.default_agent_ids` により、`--agent-ids` 未指定時は
`ISS00`〜`ISS09` の10名が自動で使われます。

Run A は `events_run_a.tsv`、Run B は `events_run_b.tsv` を読みます。
Run B には善性オブジェクトイベントが含まれます。

## 参照ドキュメント

- `docs/ISS/experiment_design_iss_20agents_100steps.md`
- `docs/ISS/personas_iss_10agents.md`
- `docs/ISS/places_iss_design.md`
- `docs/ISS/iss_objects_menu.md`

## このパックに置いたもの

- `domain.yaml`: `sim_core` 互換のパック定義 + runtimeプロファイル
- `viewer/viewer_config.yaml`: ISS用viewer設定
- `scenarios/run_a.yaml`, `scenarios/run_b.yaml`: A/Bのデフォルト実行プロファイル
- `prompts/system_context.md`, `prompts/agent_observation.md`: 最小観測プロンプト
- `data/agents.tsv`: 正本ペルソナ10人に同期した単一agent表
- `data/events.tsv`, `data/events_run_a.tsv`, `data/events_run_b.tsv`: ISS閉鎖空間イベントとA/B差分
- `data/agent_panel.tsv`: 表示/代表重み
- `data/personas_10.tsv`: 10人版ペルソナ要約
- `data/places_iss_10.tsv`: 10人版ISS場所定義
- `data/objects_menu.tsv`: ISS善性オブジェクト一覧
- `data/relationship_seed.tsv`: 信頼/摩擦の初期アンカー（創発観測の初期条件）
- `data/message_schema.tsv`: 発話rawログの正準schema
- `data/conversation_thread_schema.tsv`: 会話スレッドの要約/詳細/evidence schema
- `data/agent_state_schema.tsv`: 感情/行動の要約・詳細・source・evidence schema
- `data/habitat_frame_schema.tsv`: ISS habitat UI用のstep別frame schema
- `data/event_timeline_schema.tsv`, `data/nudge_effect_schema.tsv`: UIタイムラインとナッジ効果表示用schema
- `data/time_schedule.tsv`: 静穏時間/共同食/個室ローテを含む運用スケジュール
- `data/action_dictionary.tsv`, `data/emotion_dictionary.tsv`, `data/state_variables.tsv`: ISS用の行動・感情・状態辞書
- `data/evaluation_metrics.tsv`, `data/feedback_channels.tsv`, `data/interventions.tsv`, `data/perception_channels.tsv`: ISS用の評価・フィードバック・介入・知覚チャネル辞書

旧互換TSVは `docs/ISS/archive/iss_benevolence_legacy_data_2026-05-04/` に退避済みです。`domain_packs/iss_benevolence/` の引き渡し対象には含めません。

主線実行に使わないtail実験・補助設計TSVは `docs/ISS/archive/iss_benevolence_non_main_data_2026-05-04/` に退避済みです。

## 創発観測向けの追加KPI

`examples/spatial_demo/analyze_iss_pair.py` は次の指標も出力します。

- `reciprocity_rate`: 双方向やり取りが成立したペア比率
- `repair_after_conflict_rate`: 摩擦語彙後3step以内に修復語彙が出た比率
- `bridge_agent_count`: 接点が広く送受信も担う橋渡しエージェント数
- `load_fairness`: 発話負荷の公平性（1に近いほど偏りが小さい）

例:

```bash
python examples/spatial_demo/analyze_iss_pair.py \
  --run-a outputs/spatial/output_iss_cursor_smoke_run_a \
  --run-b outputs/spatial/output_iss_cursor_smoke_run_b
```
