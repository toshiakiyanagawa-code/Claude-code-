# test_b1 validation memo — 2026-05-17

## Goal

Validate the B1 route for generating both long and short `combined.mp4` outputs from:

- video ID: `AHqwNShdSGI`
- source mock: `output/test_b1/source_mock.json`
- runner: `output/test_b1/codespace_run.sh`

## Local Result

Actual mp4 generation could not be completed in this workspace because the required source inputs were not present:

```text
output/test_b1/cookies.txt
output/test_b1/extract/AHqwNShdSGI_long/source.mp4
output/test_b1/extract/AHqwNShdSGI_long/source.ja.srt
```

Preflight command:

```powershell
uv run clipgen config-check --job-dir output/test_b1/extract/AHqwNShdSGI_long
```

Observed result:

```text
情報: python バージョン: Python 3.14.5
情報: ffmpeg バージョン: ffmpeg version N-124279-g0f6ba39122-20260430
情報: yt-dlp バージョン: 2026.03.17
警告: YOUTUBE_API_KEY is not set
エラー: ジョブディレクトリが見つかりません: output\test_b1\extract\AHqwNShdSGI_long
```

## What Is Ready

- `output/test_b1/codespace_run.sh` is ready to run once `cookies.txt` is uploaded.
- The runner is idempotent and skips existing `source.mp4`, `source.ja.srt`, `parts`, and `combined.mp4` when fresh enough.
- `clipgen config-check --job-dir ...` now provides the first preflight gate before running cut/combine.
- `clipgen run-job` has been expanded to generate candidates, plans, extract manifests, review reports, and digest artifacts for both `short` and `long`.

## Next Action

Upload `output/test_b1/cookies.txt` or provide:

```text
output/test_b1/extract/AHqwNShdSGI_long/source.mp4
output/test_b1/extract/AHqwNShdSGI_long/source.ja.srt
```

Then run:

```bash
bash output/test_b1/codespace_run.sh
```

Expected final artifacts:

```text
output/test_b1/extract/AHqwNShdSGI_long/combined.mp4
output/test_b1/extract/AHqwNShdSGI_short/combined.mp4
```
