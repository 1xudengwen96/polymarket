# Live launch script
# =============================================================
# One-time preparation before first use:
#   1. Fund USDC on Polymarket (suggest 95-100, keep Polygon gas headroom)
#   2. config/live.yaml: allow_live: true
#   3. Fill your EOA private key in the quotes on line 9 below
#      (NEVER commit this file to git; keep it out of shared folders)
# =============================================================

$env:POLYMARKET_PRIVATE_KEY = '__REPLACE_WITH_YOUR_EOA_PRIVATE_KEY__'

if ($env:POLYMARKET_PRIVATE_KEY -like '__REPLACE*') {
    Write-Host "ERROR: fill your EOA private key in launch_live.ps1 line 9 first." -ForegroundColor Red
    exit 1
}

$env:PM5HFT_MODE = 'live'
$env:PM5HFT_LIVE = 'true'
$env:PM5HFT_DB_URL = 'sqlite+aiosqlite:///./data/pm5hft-live.db'
$env:PYTHONIOENCODING = 'utf-8'

$logFile = "logs\live-" + (Get-Date -Format 'yyyyMMdd-HHmm') + ".log"
Write-Host "live log -> $logFile"
python -m pm5hft.main --log-level INFO 2>&1 | Tee-Object -FilePath $logFile
