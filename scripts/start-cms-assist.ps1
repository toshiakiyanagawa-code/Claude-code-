param(
    [ValidateSet("dev", "prod")]
    [string]$Mode = "dev"
)

$ErrorActionPreference = "Stop"

$RootDir = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RootDir

function Import-DotEnv {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }

    Get-Content -LiteralPath $Path | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#")) {
            return
        }

        $parts = $line -split "=", 2
        if ($parts.Count -ne 2) {
            return
        }

        $name = $parts[0].Trim()
        $value = $parts[1].Trim().Trim('"').Trim("'")
        Set-Item -Path "Env:$name" -Value $value
    }
}

Import-DotEnv -Path ".env"

$LocalCli = Join-Path $RootDir "bin\cms-assist.js"
$UseLocalCli = Test-Path -LiteralPath $LocalCli
$GlobalCli = Get-Command cms-assist -ErrorAction SilentlyContinue
if (-not $UseLocalCli -and -not $GlobalCli) {
    Write-Error "'cms-assist' command was not found. Install the internal CLI/binary and add it to PATH."
}

if ($UseLocalCli -and -not (Get-Command node -ErrorAction SilentlyContinue)) {
    Write-Error "Node.js was not found. Install Node.js 20+ or add it to PATH."
}

$required = @("CMS_BASE_URL", "CMS_API_TOKEN", "CMS_SPACE_ID")
$missing = $required | Where-Object { -not [Environment]::GetEnvironmentVariable($_) }
if ($missing.Count -gt 0) {
    Write-Error "Required environment variables are missing: $($missing -join ', '). Copy .env.example to .env and fill it in."
}

$timeout = if ($env:CMS_TIMEOUT_MS) { $env:CMS_TIMEOUT_MS } else { "15000" }
$argsList = @(
    "run",
    "--base-url", $env:CMS_BASE_URL,
    "--token", $env:CMS_API_TOKEN,
    "--space", $env:CMS_SPACE_ID,
    "--timeout", $timeout
)

if ($Mode -eq "dev") {
    Write-Host "[INFO] Starting CMS assist in development mode"
    $argsList += "--watch"
} else {
    Write-Host "[INFO] Starting CMS assist in production-like mode"
}

if ($UseLocalCli) {
    & node $LocalCli @argsList
} else {
    & cms-assist @argsList
}
exit $LASTEXITCODE
