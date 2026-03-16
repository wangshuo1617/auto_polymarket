# 启动「每 4 小时执行 position_analyze.py」的调度器（后台运行）
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$LogDir = Join-Path $ProjectRoot "logs"
$LogFile = Join-Path $LogDir "position_analyze_4h.log"
$PidFile = Join-Path $LogDir "position_analyze_4h.pid"

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

if (Test-Path $PidFile) {
  $OldPid = Get-Content $PidFile -ErrorAction SilentlyContinue
  if ($OldPid -and (Get-Process -Id $OldPid -ErrorAction SilentlyContinue)) {
    Write-Host "调度器已在运行 (PID: $OldPid)，如需重启请先停止该进程"
    exit 0
  }
  Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
}

Write-Host "启动 position_analyze 每 4 小时调度器..."
$job = Start-Process -FilePath "uv" -ArgumentList "run", "scripts/run_position_analyze_every_4h.py" -WorkingDirectory $ProjectRoot -WindowStyle Hidden -RedirectStandardOutput $LogFile -RedirectStandardError (Join-Path $LogDir "position_analyze_4h_stderr.log") -PassThru
$job.Id | Set-Content $PidFile
Write-Host "✅ 调度器已启动 (PID: $($job.Id))"
Write-Host "日志: $LogFile"
Write-Host "停止: Stop-Process -Id $($job.Id)"
