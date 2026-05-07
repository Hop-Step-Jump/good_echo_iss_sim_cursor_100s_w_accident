# ISS実験 実行ガイド（spatial_demo）

このディレクトリは、ISS A/B実験を実行するための参照実装です。
主線ドメインは [`domain_packs/iss_benevolence/`](../../domain_packs/iss_benevolence/) です。

本線（制度反応波及シミュレーター）の使い方は [リポジトリルートの README](../../README.md) を参照してください。

## 位置づけ

`scripts/run_profile.py` と `configs/config.iss.*.yaml` を使い、domain pack の runtime profile として実行します。

## 構成

| ファイル | 役割 |
|---|---|
| `main.py` | シミュレーション実行器 |
| `simulation.py` / `agent.py` / `visualization.py` | コア実装 |
| `llm_backends.py` / `ollama_client.py` | LLMバックエンド（Ollama / Claude / Codex / Cursor / Gemini） |
| `utils.py` | ユーティリティ |
| `setup_mac.sh` / `setup_win.bat` | venv セットアップ補助 |
| [`configs/`](configs/) | シナリオ YAML 一式（[configs/README.md](configs/README.md)） |

## 動かす場合の手順

import がカレントディレクトリ前提のため、このディレクトリに `cd` してから実行します。

```bash
cd examples/spatial_demo
python main.py --config configs/config.smoke.yaml
```

LLMバックエンドは `configs/config.*.smoke.yaml` で切り替えます。

| 設定 | バックエンド |
|---|---|
| `config.smoke.yaml` | Ollama |
| `config.claude.smoke.yaml` | Claude Code CLI |
| `config.codex.smoke.yaml` | Codex CLI |
| `config.cursor.smoke.yaml` | Cursor Agent CLI |
| `config.gemini.smoke.yaml` | Gemini CLI |

出力は `outputs/spatial/`、ログは `logs/spatial/` に書かれます。

## ISSセットアップ（Run A/B比較）

ISSの10人版（対照群A / 介入群B）をそのまま実行できる設定を追加しています。

### Claude CLI

```bash
cd examples/spatial_demo
claude login

# smoke
python main.py --config configs/config.iss.claude.smoke.run_a.yaml
python main.py --config configs/config.iss.claude.smoke.run_b.yaml

# full (50 steps)
python main.py --config configs/config.iss.claude.run_a.yaml
python main.py --config configs/config.iss.claude.run_b.yaml
```

### Codex CLI

```bash
cd examples/spatial_demo
codex login

# smoke
python main.py --config configs/config.iss.codex.smoke.run_a.yaml
python main.py --config configs/config.iss.codex.smoke.run_b.yaml

# full (50 steps)
python main.py --config configs/config.iss.codex.run_a.yaml
python main.py --config configs/config.iss.codex.run_b.yaml
```

### Cursor Agent CLI

```bash
cd examples/spatial_demo
cursor agent login

# generic smoke
python main.py --config configs/config.cursor.smoke.yaml

# ISS smoke
python main.py --config configs/config.iss.cursor.smoke.run_a.yaml
python main.py --config configs/config.iss.cursor.smoke.run_b.yaml
```

## domain profileとして実行（推奨）

`scripts/run_profile.py` を使うと、`domain.yaml` に定義したprofileを同じCLI形で実行できます。

```bash
# 事前ログイン（使うproviderのみ）
claude login
# codex login
# cursor agent login

# profileを指定
python ../../scripts/run_profile.py --pack ../../domain_packs/iss_benevolence --scenario run_a --profile claude_smoke_a
python ../../scripts/run_profile.py --pack ../../domain_packs/iss_benevolence --scenario run_b --profile claude_smoke_b
python ../../scripts/run_profile.py --pack ../../domain_packs/iss_benevolence --scenario run_b --profile cursor_smoke_b
```

### A/B比較（最小KPI）

```bash
cd examples/spatial_demo
python analyze_iss_pair.py \
  --run-a outputs/spatial/output_iss_claude_run_a \
  --run-b outputs/spatial/output_iss_claude_run_b
```

`total_messages`、`unique_interaction_pairs`、`help_signal_messages`、`isolated_agents` を比較します。
加えて `reciprocity_rate`、`repair_after_conflict_rate`、`bridge_agent_count`、`load_fairness` も比較できます。
