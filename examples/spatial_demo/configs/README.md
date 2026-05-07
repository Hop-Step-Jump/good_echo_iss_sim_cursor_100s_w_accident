# ISS実験用 configs

このディレクトリは、`main.py` で動かす ISS 実験向け設定ファイル置き場。

ISS実験の実行設定は `domain_packs/iss_benevolence/domain.yaml` と `domain_packs/iss_benevolence/scenarios/` を基準に管理する。

ルール:

- ルート直下に `config*.yaml` を増やさない
- 実行時は `examples/spatial_demo/` に `cd` してから `python main.py --config configs/config.xxx.yaml` のように指定する
- 実験出力先は `outputs/spatial/`
- 実験ログは `logs/spatial/`

残す設定:

| ファイル | 用途 |
|---|---|
| `config.yaml` | 汎用の最小設定 |
| `config.smoke.yaml` | Ollamaの小規模確認 |
| `config.claude.smoke.yaml` | Claude CLIの小規模確認 |
| `config.codex.smoke.yaml` | Codex CLIの小規模確認 |
| `config.cursor.smoke.yaml` | Cursor Agent CLIの小規模確認 |
| `config.gemini.smoke.yaml` | Gemini CLIの小規模確認 |
| `config.iss.claude.smoke.run_a.yaml` | ISS 10人版の対照群（Claude / smoke） |
| `config.iss.claude.smoke.run_b.yaml` | ISS 10人版の介入群（Claude / smoke） |
| `config.iss.claude.run_a.yaml` | ISS 10人版の対照群（Claude / 50step） |
| `config.iss.claude.run_b.yaml` | ISS 10人版の介入群（Claude / 50step） |
| `config.iss.codex.smoke.run_a.yaml` | ISS 10人版の対照群（Codex / smoke） |
| `config.iss.codex.smoke.run_b.yaml` | ISS 10人版の介入群（Codex / smoke） |
| `config.iss.codex.run_a.yaml` | ISS 10人版の対照群（Codex / 50step） |
| `config.iss.codex.run_b.yaml` | ISS 10人版の介入群（Codex / 50step） |
| `config.iss.cursor.smoke.run_a.yaml` | ISS 10人版の対照群（Cursor / smoke） |
| `config.iss.cursor.smoke.run_b.yaml` | ISS 10人版の介入群（Cursor / smoke） |
| `config.iss.cursor.run_a.yaml` | ISS 10人版の対照群（Cursor / 50step） |
| `config.iss.cursor.run_b.yaml` | ISS 10人版の介入群（Cursor / 50step） |
