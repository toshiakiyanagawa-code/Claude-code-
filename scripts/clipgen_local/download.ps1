# clipgen Windows fetcher
# C:\clipgen\queue\urls.txt の URL を yt-dlp で取得し OneDrive にアップロード
$ErrorActionPreference = "Continue"
$Root = "C:\clipgen"
$Queue = Join-Path $Root "queue\urls.txt"
$Out = Join-Path $Root "out"
$Log = Join-Path $Root "log\fetch.log"
$Synced = Join-Path $Root "synced.txt"
$Failed = Join-Path $Root "failed.txt"
$CookiesTxt = Join-Path $Root "cookies.txt"
$OneDriveRemote = "onedrive:clipgen/inbox"

function Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$ts $msg" | Tee-Object -FilePath $Log -Append | Out-Host
}

if (-not (Test-Path $Queue)) {
    Log "ERROR: $Queue がありません"
    exit 1
}

$syncedIds = @{}
if (Test-Path $Synced) {
    Get-Content $Synced | ForEach-Object { $syncedIds[$_] = $true }
}

$urls = Get-Content $Queue | Where-Object { $_ -match "youtube\.com/watch\?v=|youtu\.be/" }
Log "fetch開始: $($urls.Count) 件の URL"

foreach ($url in $urls) {
    $vid = $null
    if ($url -match "[?&]v=([A-Za-z0-9_-]{11})") {
        $vid = $matches[1]
    }
    elseif ($url -match "youtu\.be/([A-Za-z0-9_-]{11})") {
        $vid = $matches[1]
    }
    else {
        Log "SKIP: 動画 ID 抽出失敗: $url"
        continue
    }

    if ($syncedIds[$vid]) {
        Log "SKIP: 既に sync 済み: $vid"
        continue
    }

    $jobDir = Join-Path $Out $vid
    New-Item -ItemType Directory -Path $jobDir -ErrorAction SilentlyContinue | Out-Null
    $mp4 = Join-Path $jobDir "source.mp4"

    if ((Test-Path $mp4) -and ((Get-Item $mp4).Length -gt 100MB)) {
        Log "INFO: $vid は既にDL済み、アップロードのみ"
    }
    else {
        $cookieArg = @()
        if (Test-Path $CookiesTxt) {
            $cookieArg = @("--cookies", $CookiesTxt)
            Log "INFO: $vid 取得 (cookies.txt 使用)"
        }
        else {
            $cookieArg = @("--cookies-from-browser", "chrome")
            Log "INFO: $vid 取得 (Chrome cookies、Chrome 終了が必要)"
        }
        $outTemplate = Join-Path $jobDir "source.%(ext)s"
        $fmt = "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]"
        & yt-dlp @cookieArg -f $fmt --merge-output-format mp4 --write-auto-subs --sub-lang ja --convert-subs srt -o $outTemplate $url
        if ($LASTEXITCODE -ne 0) {
            Log "FAIL: $vid yt-dlp exit $LASTEXITCODE"
            Add-Content $Failed "$vid $url"
            continue
        }
        if (-not (Test-Path $mp4) -or (Get-Item $mp4).Length -lt 100MB) {
            Log "FAIL: $vid source.mp4 が無いかサイズ不足"
            Add-Content $Failed "$vid $url"
            continue
        }
    }

    Log "INFO: $vid を OneDrive にアップロード..."
    & rclone copy $jobDir "$OneDriveRemote/$vid" --ignore-existing --progress
    if ($LASTEXITCODE -ne 0) {
        Log "FAIL: $vid rclone exit $LASTEXITCODE"
        Add-Content $Failed "$vid $url"
        continue
    }
    Add-Content $Synced $vid
    Log "OK: $vid 完了"
}

Log "fetch終了"
