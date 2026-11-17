$AgentDir = $PSScriptRoot
$env:PYTHONPATH = $AgentDir
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
try {
    $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    [Console]::InputEncoding = $utf8NoBom
    [Console]::OutputEncoding = $utf8NoBom
    $OutputEncoding = $utf8NoBom
} catch {}

# Check if backend is already running
$backendRunning = $false
try {
    $connection = Test-NetConnection -ComputerName localhost -Port 8081 -InformationLevel Quiet -WarningAction SilentlyContinue -ErrorAction SilentlyContinue
    $backendRunning = $connection
} catch {
    $backendRunning = $false
}

if ($backendRunning) {
    Write-Host "WSA backend already running at http://localhost:8081/v1" -ForegroundColor Green
} else {
    Write-Host "Starting WSA backend server in background..." -ForegroundColor Cyan
    Start-Process -WindowStyle Hidden -FilePath "$AgentDir\.venv\Scripts\python.exe" -ArgumentList "-m", "wsa_server" -WorkingDirectory $AgentDir
    Start-Sleep -Seconds 2
    Write-Host "WSA backend started at http://localhost:8081/v1" -ForegroundColor Green
}

Write-Host ""
Write-Host "Starting wsa..." -ForegroundColor Cyan
& "$AgentDir\.venv\Scripts\python.exe" -m wsa_agent @args
exit $LASTEXITCODE

