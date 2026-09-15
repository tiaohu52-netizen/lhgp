# LHGP daemon 看门狗（通用版）：探活，死了就拉起来，并记录时间线
# 依赖：lhgp 在 PATH，或设置环境变量 LHGP_BIN；数据根默认 ~/.lhgp（可用 LHGP_DATA_DIR 覆盖）。
# 注册（每 5 分钟；把 <PATH>\lhgp-watchdog.ps1 换成实际路径）：
#   schtasks /Create /SC MINUTE /MO 5 /TN "LHGP-Daemon-Watchdog" ^
#     /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File <PATH>\lhgp-watchdog.ps1" /F
$ErrorActionPreference = "SilentlyContinue"

$lhgp = if ($env:LHGP_BIN) { $env:LHGP_BIN } else { "lhgp" }
$dataRoot = if ($env:LHGP_DATA_DIR) { $env:LHGP_DATA_DIR } else { Join-Path $env:USERPROFILE ".lhgp" }
$log = Join-Path $dataRoot "watchdog.log"
$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

$statusRaw = & $lhgp status 2>$null | Out-String
$running = $false
try {
    $status = $statusRaw | ConvertFrom-Json
    $running = [bool]$status.running
} catch {
    Add-Content -Path $log -Value "$stamp WARN: status parse failed: $($statusRaw.Trim().Substring(0,[Math]::Min(120,$statusRaw.Trim().Length)))"
}

if (-not $running) {
    Add-Content -Path $log -Value "$stamp daemon DOWN -> starting"
    $start = & $lhgp start 2>&1 | Out-String
    Add-Content -Path $log -Value "$stamp start result: $($start.Trim() -replace '\s+',' ')"
} else {
    $min = (Get-Date).Minute
    if ($min -lt 5) { Add-Content -Path $log -Value "$stamp alive (pid=$($status.pid))" }
}
