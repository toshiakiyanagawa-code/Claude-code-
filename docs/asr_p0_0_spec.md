# P0-0 計測基盤 実装仕様書 (codex 向け)

`docs/asr_speedup_plan.md` の §3 P0-0 を実装するための仕様。

## ゴール

文字起こしの **速度・精度を数値で評価できる土台** を作る。
これ以降の改善はすべてこの数値で採否を判定する。

## 実装範囲(本仕様)

1. **評価セット構造** をディレクトリで定義
2. **CER (Character Error Rate)** と **固有名詞ヒット率** を計算するユーティリティ
3. **`podedit asr-eval` 新規 CLI コマンド** (評価セットに対して ASR 実行 → 指標を出力)
4. **`podedit kpi-summary` 新規 CLI コマンド** (UI の KPI ログから「編集者の修正クリック数 / 時間音源」を集計)
5. ユニットテスト

範囲外(別仕様で扱う): UI 側の `ui.first_edit.applied` イベント追加、Web UI の集計画面表示。

---

## 1. 評価セットのディレクトリ構造

```
eval/asr/<set_name>/
├── audio.{wav,mp3,m4a}          # 評価音源 (1 ファイル)
├── reference.transcript.json    # 正解 transcript (podedit transcribe と同スキーマ)
├── meta.json                    # case_id, 案件辞書, 注釈
└── runs/                        # asr-eval が結果を書き込む場所
    └── 20260514-1234-small-beam1.json
```

### `meta.json` スキーマ

```json
{
  "case_id": "bun-AI-ep1",
  "title": "文系AI部 第1回",
  "duration_sec": 1788.0,
  "notes": "対談ポッドキャスト、ゲスト1名",
  "glossary": [
    "クロード", "Anthropic", "スタンフォード", "認知バイアス"
  ]
}
```

`glossary` は **固有名詞ヒット率** の計算対象。

---

## 2. メトリクス

新規モジュール `src/podedit/asr_eval.py` に純粋関数として実装。

### 2.1 CER

```python
def compute_cer(reference: str, hypothesis: str) -> float:
    """Character Error Rate using Levenshtein distance / len(reference).

    Both strings are NFKC-normalised + whitespace-stripped before compare
    so full/half-width and space-only differences don't inflate the score.
    Returns 0.0 for exact match, 1.0 for completely different (capped at 1.0
    for the corner case where hyp is much longer than ref).
    """
```

実装ヒント:
- `python-Levenshtein` か pure-python の DP どちらか。依存追加するなら `Levenshtein` (PyPI) が小さくて速い。
- pure-python なら `O(n*m)` の編集距離テーブル。30 分音源 ≒ 1 万文字級なので問題なし。

### 2.2 固有名詞ヒット率

```python
def compute_glossary_recall(
    hypothesis_text: str, glossary: list[str]
) -> tuple[float, list[dict]]:
    """For each glossary term, check if it appears in hypothesis_text.

    Returns:
        recall: fraction of glossary terms found at least once
        details: per-term {term, found: bool, occurrences: int}
    """
```

- 比較は NFKC + lowercase (英数字の表記揺れ吸収)
- 部分一致でなく完全一致(`hypothesis.count(term)` ベース)

### 2.3 transcript 全体テキストの抽出

```python
def transcript_to_text(transcript: dict) -> str:
    """Concatenate every word.text in segment order, separator '' (ja).

    Tolerates the schema used by ``podedit transcribe`` output.
    """
```

---

## 3. `podedit asr-eval` CLI

新規 `@cli.command("asr-eval")` を `src/podedit/cli.py` に追加。

### 引数

```bash
podedit asr-eval <set_name> \
    [--model <tiny|small|...>] \
    [--beam-size <int>] \
    [--preset <fast|balanced|quality>] \
    [--out-dir <path>] \
    [--initial-prompt <str>] \
    [--hotwords <str>] \
    [--no-vad]
```

- `<set_name>`: `eval/asr/<set_name>/` のディレクトリ名
- preset 指定時は `model`/`beam_size` のデフォルトを上書き(現状の preset と同じ定義)
- 各引数のデフォルトは `ASRConfig` の値

### 動作

1. `eval/asr/<set_name>/audio.*` を見つけて読み込む
2. `audio_to_wav_16k_mono` で前処理
3. `transcribe()` を実行(`model_handle` キャッシュ不要、毎回 fresh で OK)
4. wall_sec / RTF / model load 時間を計測
5. 結果を `predicted.transcript.json` として書き出す
6. `reference.transcript.json` と比較して CER + glossary recall を計算
7. ターミナルに Markdown 表で出力
8. JSON レポートを `eval/asr/<set_name>/runs/<timestamp>-<config>.json` に保存

### レポート JSON のスキーマ

```json
{
  "run_id": "20260514-1234-small-beam1",
  "set_name": "bun-AI-ep1",
  "config": {
    "model": "small", "beam_size": 1, "vad_filter": true,
    "device": "cpu", "compute_type": "int8",
    "initial_prompt": "日本語のポッドキャスト...",
    "hotwords": null
  },
  "timing": {
    "audio_duration_sec": 1788.0,
    "wall_sec_total": 720.5,
    "wall_sec_ffmpeg": 1.2,
    "wall_sec_asr": 719.3,
    "rtf": 0.402
  },
  "accuracy": {
    "cer": 0.087,
    "glossary_recall": 0.83,
    "glossary_details": [
      {"term": "クロード", "found": true, "occurrences": 12},
      {"term": "Anthropic", "found": false, "occurrences": 0}
    ]
  }
}
```

### ターミナル出力例

```
podedit asr-eval bun-AI-ep1 --model small --beam-size 1

Source : eval/asr/bun-AI-ep1/audio.mp3 (1788.0s)
Model  : small / beam=1 / vad=on / cpu/int8

| Metric                 | Value       |
|------------------------|-------------|
| wall (total)           | 720.5s      |
| wall (ASR only)        | 719.3s      |
| RTF                    | 0.402       |
| CER                    | 8.7%        |
| Glossary recall        | 83% (5/6)   |

Glossary misses: Anthropic
Report written: eval/asr/bun-AI-ep1/runs/20260514-1234-small-beam1.json
```

---

## 4. `podedit kpi-summary` CLI

新規 `@cli.command("kpi-summary")` を追加。

### 引数

```bash
podedit kpi-summary <kpi.jsonl> [--audio-duration-sec <float>]
```

- `<kpi.jsonl>`: `.podedit/work/<audio>.kpi.jsonl`
- `--audio-duration-sec`: 集計の正規化に使う(未指定なら KPI ログ内の `ui.loaded` から推定)

### 動作

1. KPI JSONL を読み込み(壊れた行は skip)
2. 編集系イベントを集計:
   - `ui.op.delete` の合計件数
   - `ui.op.move` の合計件数
   - `ui.click.word` / `ui.dblclick.word` の合計
   - `ui.drag.select` の合計
   - `ui.annotation.fillers.added` の累積件数
3. 「**修正クリック数**」を以下と定義(計画書の主 KPI):
   ```
   correction_clicks = ui.op.delete + ui.op.move + ui.annotation.fillers.added
   ```
4. **時間音源あたり** に正規化: `correction_clicks / (audio_duration_sec / 3600)`
5. セッション wall: 最初の `ui.loaded` から最後のイベントまでの秒数
6. ターミナル + JSON で出力

### 出力例

```
podedit kpi-summary .podedit/work/episode1.kpi.jsonl

KPI file       : .podedit/work/episode1.kpi.jsonl
Audio duration : 1788.0s (29.8 min)
Session wall   : 1923.4s (32.1 min)

| Metric                          | Value           |
|---------------------------------|-----------------|
| ops.delete                      | 41              |
| ops.move                        | 3               |
| ops.fillers.added (auto)        | 23              |
| **correction clicks**           | **67**          |
| **per hour of audio**           | **134.9 / hr**  |
| word clicks (seek)              | 218             |
| drag selections                 | 14              |
```

JSON 出力先: `<kpi.jsonl のディレクトリ>/<audio_stem>.kpi-summary.json`

---

## 5. テスト

新規 `tests/test_asr_eval.py`:

- `test_compute_cer_exact_match` — 同じ文字列で CER = 0
- `test_compute_cer_full_substitution` — 全文字違いで CER = 1.0
- `test_compute_cer_nfkc_normalises_widths` — 全角・半角の数字が同じ扱い
- `test_glossary_recall_empty` — glossary 空でも落ちない
- `test_glossary_recall_partial` — 一部だけ見つかる
- `test_transcript_to_text_handles_missing_words` — `words` が無い segment で落ちない

新規 `tests/test_kpi_summary.py`:
- `test_correction_clicks_count` — JSONL から正しくカウント
- `test_per_hour_normalisation` — 30 分音源 → 2x される
- `test_corrupted_lines_skipped` — 壊れた JSON 行があっても完走

---

## 6. 受け入れ条件

1. `uv run pytest tests/test_asr_eval.py tests/test_kpi_summary.py` が全 green
2. `uv run podedit asr-eval --help` が表示される
3. `uv run podedit kpi-summary --help` が表示される
4. `eval/asr/` 以下に最低 1 セット (`samples/speech_ja.wav` ベースで小さい正解 transcript 1 個)を同梱
5. その 1 セットに対して `uv run podedit asr-eval <set>` が正常終了し JSON レポートが書かれる
6. 既存テストが全て green
7. ドキュメント: `docs/asr_speedup_plan.md` の §4 に「測定方法」セクションを足し、`podedit asr-eval` と `kpi-summary` の使い方を1段落で記述

---

## 7. 進め方

1. codex がパッチ形式 (新規ファイル + 既存ファイルへの diff) で生成
2. Claude が出力を確認 + 必要なら手直しして書き戻し
3. テスト走らせる → green になったらコミット
4. 1セット作って実走 → 数値が出る画面を編集者に見せる
5. ここから先の改善 (I, H, A-prep 等) は全部この数値で評価する
