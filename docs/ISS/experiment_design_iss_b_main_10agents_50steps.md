# ISS実験 実行仕様（Bメイン）
## 10 agents / 50 steps / Run B中心

> **2026/05/06 更新:** ドメインパックの正本は **100 steps** に拡張済み（Day50デブリ、Day56 HAB再開、Day60 HAB酸素漏れ、Day65– LAB酸素低下）。本文中の「50 steps」は履歴表記として残す場合があります。実行規模は `domain_packs/iss_benevolence/domain.yaml` の `time.steps` を参照してください。

最終更新: 2026/05/04（追記 2026/05/06）

---

## この文書の目的

この文書は、`iss_benevolence` の現行実装に合わせた**実行用仕様**です。  
`experiment_design_iss_20agents_100steps.md` は、共同検討・拡張用の上位設計として残します。

---

## 実行方針

- メイン比較: **Run B（善性オブジェクトあり）**
- 比較対象: Run A（オブジェクトなし）
- 実行規模: 10 agents / 50 steps
- 重点観測: 終盤トラブル（Day45-50）での修復・協力挙動

---

## 現行データソース（実装準拠）

- ペルソナ（10人）: `domain_packs/iss_benevolence/data/agents.tsv`
- 場所定義: `docs/ISS/places_iss_design.md` を基に実装済みconfigを使用
- オブジェクト候補: `docs/ISS/iss_objects_menu.md`
- 時間設計: `domain_packs/iss_benevolence/data/time_schedule.tsv`
- 慢性ストレス設計（archive）: `docs/ISS/archive/iss_benevolence_non_main_data_2026-05-04/chronic_stress_event_templates.tsv`
- 終盤トラブル設計（archive）: `docs/ISS/archive/iss_benevolence_non_main_data_2026-05-04/tail_trouble_design_protocol.tsv`

---

## Bメインで使う評価指標

`examples/spatial_demo/analyze_iss_pair.py` の指標を主に使う。

- `reciprocity_rate`
- `repair_after_conflict_rate`
- `bridge_agent_count`
- `load_fairness`
- `isolated_agents`

終盤重点比較は `--start-step 45 --end-step 50` を付けて実行する。

---

## 実行コマンド（Bメイン）

### 1) Bメイン本体（smoke）

```bash
./venv/bin/python scripts/run_profile.py \
  --pack domain_packs/iss_benevolence \
  --scenario run_b \
  --profile cursor_smoke_b
```

### 2) Bメイン本体（full）

```bash
./venv/bin/python scripts/run_profile.py \
  --pack domain_packs/iss_benevolence \
  --scenario run_b \
  --profile cursor_full_b
```

### 3) state / agent rows を中立runnerで作る

```bash
./venv/bin/python scripts/build_state.py \
  --pack domain_packs/iss_benevolence \
  --scenario run_b \
  --output-dir outputs/runs/iss_state_run_b

./venv/bin/python scripts/run_agents.py \
  --pack domain_packs/iss_benevolence \
  --scenario run_b \
  --start-step 45 \
  --steps 6 \
  --state-tsv outputs/runs/iss_state_run_b/societal_state.tsv \
  --auto-events-tsv outputs/runs/iss_state_run_b/auto_events.tsv \
  --output-dir outputs/runs/iss_agents_run_b_tail
```

### 4) 終盤窓比較（Day45-50）

```bash
./venv/bin/python examples/spatial_demo/analyze_iss_pair.py \
  --run-a outputs/runs/iss_tail_branches/tail_low \
  --run-b outputs/runs/iss_tail_branches/tail_high \
  --start-step 45 \
  --end-step 50
```

### 4.5) プロンプト比較（neutral v2）

```bash
./venv/bin/python scripts/run_agents.py \
  --pack domain_packs/iss_benevolence \
  --scenario run_b \
  --start-step 45 \
  --steps 6 \
  --prompt-style neutral_v2 \
  --output-dir outputs/runs/iss_prompt_neutral_tail
```

### 5) UI/分析チーム向けデータバンドル出力

```bash
./venv/bin/python scripts/export_domain_bundle.py \
  --pack domain_packs/iss_benevolence \
  --scenario run_b \
  --bundle-name iss_clean_handoff
```

出力先: `outputs/runs/domain_bundles/iss_clean_handoff/`

---

## 役割分離ルール（チーム運用）

- 他メンバーは従来ドキュメント（4ファイル）を編集・検証してOK
- あなたはこの文書を基準に Bメイン実行と結果比較を進める
- 将来 20x100 へ戻す時は、上位設計書へ再統合する

---

## 実験後の改善メモ（次フェーズ）

現行ジョブ完了後、`20 agents / 100 steps` に合わせて以下を実施する。

- 参照正本を固定: `docs/ISS/experiment_design_iss_20agents_100steps.md`、`docs/ISS/personas_iss_10agents.md`、`docs/ISS/places_iss_design.md`、`docs/ISS/iss_objects_menu.md`
- ペルソナ同期: 実行TSV（`agents.tsv`）と正本ドキュメントの年齢・属性差分を解消
- 規模変更: ステップ数を100に拡張し、20名パネルを成立させる（必要なら `agent_panel.tsv` の20名版を追加）
- 文脈分離: `scripts/` 側のドメイン語彙（AGI/若者/日本固定）を削減し、ドメイン差分は `domain_packs/` へ移管
- 出力運用: 生データ（raw）と正規化データ（normalized）を分けてバンドルし、UI引き渡しを安定化

---

## ドメイン依存除去バックログ（優先順）

`scripts/` を「抽象クラス + パイプライン実行器」に寄せるための除去対象。

### P0（最優先: 実行構造の前提を外す）

- エージェント入力の2分割前提を撤廃  
  - 現状: `agents.tsv`（単一）を主線化済み
  - 旧 `youth_agents.tsv` + `working_agents.tsv` は `docs/ISS/archive/iss_benevolence_legacy_data_2026-05-04/` に退避し、ISS domain pack主線からは外す
- ID接頭辞（`A*`,`W*`,`YG_*`）で層判定するロジックを撤廃  
  - 目標: レイヤーはデータ列（または pack schema）から解決
- `scenario_mode` の固定デフォルトを撤廃  
  - 現状: `baseline/control/all` の中立モードに変更し、pack設定から注入

### P1（高優先: 語彙・列名の固定を外す）

- 評価カテゴリ `良好/中立/注意/危険` をコード固定から切り離し  
  - 現状: `domain_packs/<pack>/domain.yaml` の `pipeline.labels` から注入
- `ACTION_CATEGORIES` をコード固定から切り離し  
  - 現状: pack側の `pipeline.labels.action_categories` を主線化。script内fallbackは汎用値のみ
- 組織関連列（`youth_training_policy`, `expected_youth_impact` 等）を中立名へ移行  
  - 目標: `schema aliases` で旧列互換を維持

### P2（中優先: 日本中心の中間表現を抽象化）

- `japan_state.tsv` / `日本社会状態` を抽象名へ移行  
  - 現状: `societal_state.tsv` / `state_summary` 系へ移行済み
- `world_to_japan` フェーズ名を抽象化  
  - 例: `world_to_society`
- `country -> society -> organization -> agent -> feedback` の各フェーズ定義を pack 側へ

### P3（中優先: 実行時構成を1箇所化）

- `config/active_domain.yaml` を導入し、pack/scenario/profile を一元管理  
- 実行コマンドは `active_domain.yaml` を既定参照（1箇所変更で切替）

### P4（低優先: 互換運用と引き渡し品質）

- 旧列名（例: `若者への入力文`）は正規化スクリプトで吸収し、段階的に撤去
  - 現状: 主線出力からは撤去済み。旧互換scriptは `scripts/legacy/`、旧互換TSVは `docs/ISS/archive/iss_benevolence_legacy_data_2026-05-04/` に隔離
- 出力は `raw/` と `normalized/` の2系統を標準化
- UI引き渡し用に `schema_map.yaml`（旧->新列対応）をバンドルに同梱

### 実装メモ（2026-05-04）

- `domain_packs/iss_benevolence/domain.yaml` に `pipeline` 設定を追加し、agent sources / scenario mode / state schema / event schema / labels / organization outputs / prompt blocks を pack 側定義へ移動
- `sim_core/domain_runtime.py` を追加し、scripts は domain pack の `pipeline` を読む実行器として扱う
- 正準状態ファイルを `societal_state.tsv` / `societal_state_feedback.tsv` に変更。ISS pack主線では `japan_*` legacy output は参照しない
- 中立入口として `scripts/run_profile.py` / `scripts/build_state.py` / `scripts/run_agents.py` / `scripts/build_feedback.py` / `scripts/export_domain_bundle.py` を追加
- 旧closed-loop / country / organization / tail実験scriptは `scripts/legacy/` へ退避
- `scripts/agent_turn_runner.py` / `scripts/feedback_builder.py` は `agents.tsv` を主線として、pack の `pipeline.agents.sources` を読む
- feedback の signal / delta / event rules を `pipeline.feedback` へ移動
- 検証: `py_compile`、`python3 -m sim_core validate --pack domain_packs/iss_benevolence`、scenario run_a/run_b validate、spatial demo dry-run、既存 `iss_prompt_v1_tail` 入力による変換器実行を通過
- `domain_packs/iss_benevolence/data/` 直下をISS正本主線へ整理。旧互換TSVは `docs/ISS/archive/iss_benevolence_legacy_data_2026-05-04/` へ退避
- `action_dictionary.tsv` / `emotion_dictionary.tsv` / `state_variables.tsv` / viewer設定をISS pack側へ追加し、sim_core defaultの旧文脈を踏まない構成へ変更
- 引き渡し用は `python3 scripts/export_domain_bundle.py --pack domain_packs/iss_benevolence --scenario run_b --bundle-name <name>` で、pack参照ファイルを束ねられる
- 主線TSVのカラムを中立化。`agent_panel.tsv` / `time_schedule.tsv` / `auto_events.tsv` は英語正準列へ統一し、旧 `エージェントへの入力文` などの互換列は出力しない
- ISS内の摩擦/言い合い/修復イベントを `events_run_a.tsv` / `events_run_b.tsv` に正規データとして追加。UIデモの `incidentPlans` と同じ6件を入力データにも反映し、Run Bでは短い摩擦+早い修復になるようにした
- 会話UIに合わせて、会話を一次データとして扱う設計を追加。`messages.jsonl` は発話単位、`conversation_threads.tsv` は会話スレッド単位とし、`speaker_id` / `listener_ids` / `conversation_id` / `module_id` / `event_id` / `tone` を必須の中核列にする。スキーマは `domain_packs/iss_benevolence/data/message_schema.tsv` / `domain_packs/iss_benevolence/data/conversation_thread_schema.tsv` と `domain.yaml` の `pipeline.conversations` に記録
- `visualization/iss_habitat_demo.html` を当面の目標UIとして固定し、リアルに見せるためのUI frame設計を `docs/ISS/iss_habitat_ui_realism_design.md` に追加。`habitat_frames.jsonl` / `agent_positions.tsv` / `module_occupancy.tsv` / `sleep_assignments.tsv` をUI用正規化出力として扱う
- `scripts/export_habitat_frames.py` を追加し、Run A / Run B のUI用成果物を生成確認済み。`visualization/iss_habitat_demo.html` はローカルサーバー経由で `outputs/runs/iss_habitat_run_a` / `outputs/runs/iss_habitat_run_b` を読める場合、生成データを優先表示する
- イベントタイムラインとナッジ効果トレースをUI/バックエンド双方へ追加。`event_timeline.tsv` は nudge/conflict/repair の時系列、`nudge_effects.tsv` はOBJごとの関連摩擦・修復・影響agentを出力する
- 会話・感情・行動のUI表示を `raw / summary / detail / source / evidence` に分離。`message_schema.tsv` / `conversation_thread_schema.tsv` / `agent_state_schema.tsv` を追加し、要約は後段LLMで差し替え可能な派生層として扱う
- `domain_packs/iss_benevolence/data/` を主線実行データへ整理。旧tail実験・補助設計TSVは `docs/ISS/archive/iss_benevolence_non_main_data_2026-05-04/` へ退避し、`domain.yaml` の主線dataから外した
- `scripts/generate_habitat_conversations.py` を追加し、UI用の `messages.jsonl` / `conversation_threads.tsv` を前ステップ会話履歴つきLLM生成へ差し替え可能にした。生成中も各thread後に途中保存し、保存前クラッシュでrowデータが失われないようにする
- 50step smoke のLLM会話成果物を保存済み:
  - Run A（ナッジなし）: `outputs/runs/iss_no_nudge_smoke_ui_llm`
  - Run B（ナッジあり）: `outputs/runs/iss_nudge_smoke_ui_llm`
  - A/B比較: `outputs/runs/iss_habitat_llm_ab_50step.json`
- `visualization/iss_habitat_demo.html` は `_llm` 出力を優先して読む。存在しない場合のみ従来のscripted UI出力へfallbackする
- LLM会話row検証結果: Run A は 249 messages / 83 threads、Run B は 210 messages / 70 threads。両方とも `source=llm`、schema外カラムなし、routine tone混入なし

残り:

- `scripts/legacy/` 配下には旧互換実装が残存。本線入口からは外している
- `domain_packs/iss_benevolence/data/agents.tsv` は正本10人版に同期済み。20x100へ進む場合は同一schemaで20人版へ拡張する
- LLM本実行では、`private_talk` を会話ログとして扱わない。実際の相互作用は `messages.jsonl` に `speaker_id` / `listener_ids` 付きで別出力する
- 20x100へ拡張する前に、`generate_habitat_conversations.py` のLLM生成コスト/所要時間と、会話品質のサンプルレビュー手順を決める

---

## 実行ToDo（2026-05-04記録）

### 直近（最優先）

- [x] `iss_prompt_ablation_tail_45_50.json` が全指標0の原因を特定する  
  - 対象: `examples/spatial_demo/analyze_iss_pair.py` と `outputs/runs/iss_prompt_v1_tail/agent_turns.tsv` / `outputs/runs/iss_prompt_v2_tail/agent_turns.tsv`
- [x] `analyze_iss_pair.py` の参照列と `agent_turns.tsv` 実列を一致させ、終盤窓(45-50)で非ゼロKPIを再取得する
- [ ] 再取得後、`legacy_v1` vs `neutral_v2` の差分要約をこの文書へ反映する

### 次フェーズ（ドメイン依存除去の実装順）

- [x] `scripts/run_agents.py` / `scripts/agent_turn_runner.py`  
  - レイヤ判定・評価カテゴリ・行動カテゴリ語彙を `domain_packs` 側定義に移管
- [x] `scripts/build_state.py`  
  - state variables / events / schedule から中立状態行を生成
- [x] `scripts/build_feedback.py` / `scripts/feedback_builder.py`  
  - feedback signal / delta / event rules を pack 側から読む
- [x] `scripts/export_domain_bundle.py`  
  - pack参照ファイルと任意run dirを汎用bundle化
- [x] 旧country / organization / closed-loop / tail入口  
  - `scripts/legacy/` へ退避し、本線入口から除外
