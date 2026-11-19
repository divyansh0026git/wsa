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

# Resolve Python interpreter (.venv preferred; auto-create .venv or use system Python with bootstrap)
$VenvPy = Join-Path $AgentDir ".venv\Scripts\python.exe"
if (Test-Path $VenvPy) {
    $PythonExe = $VenvPy
} else {
    $SysPy = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $SysPy) { $SysPy = (Get-Command python3 -ErrorAction SilentlyContinue).Source }
    if (-not $SysPy) {
        Write-Host "[WSA Setup Error] Python 3.10+ was not found on PATH. Install Python from https://python.org" -ForegroundColor Red
        exit 1
    }
    & $SysPy -c "import sys; sys.path.insert(0, r'$AgentDir'); from wsa_agent.bootstrap import check_missing_wsa_packages; raise SystemExit(1 if check_missing_wsa_packages() else 0)" 2>$null
    if ($LASTEXITCODE -eq 0) {
        $PythonExe = $SysPy
    } else {
        Write-Host "[WSA Setup] Creating local .venv and installing WSA dependencies..." -ForegroundColor Cyan
        & $SysPy -m venv (Join-Path $AgentDir ".venv")
        if (Test-Path $VenvPy) {
            $PythonExe = $VenvPy
        } else {
            $PythonExe = $SysPy
        }
    }
}
& $PythonExe -c "import sys; sys.path.insert(0, r'$AgentDir'); from wsa_agent.bootstrap import ensure_wsa_dependencies; res = ensure_wsa_dependencies(); raise SystemExit(0 if res.get('ok') else 1)"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

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
    Start-Process -WindowStyle Hidden -FilePath $PythonExe -ArgumentList "-m", "wsa_server" -WorkingDirectory $AgentDir
    Start-Sleep -Seconds 2
    Write-Host "WSA backend started at http://localhost:8081/v1" -ForegroundColor Green
}

Write-Host ""
Write-Host "Starting wsa..." -ForegroundColor Cyan
& $PythonExe -m wsa_agent @args
exit $LASTEXITCODE
