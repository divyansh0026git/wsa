$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$python = Get-Command py -ErrorAction SilentlyContinue
if (-not $python) { throw 'Install Python 3.10 or newer from python.org, then rerun this script.' }
& py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)"
if ($LASTEXITCODE -ne 0) { throw 'Python 3.10 or newer is required.' }
& py -3 -m venv (Join-Path $root '.venv')
if ($LASTEXITCODE -ne 0) { throw 'Could not create the virtual environment.' }
$exe = Join-Path $root '.venv\Scripts\python.exe'
& $exe -m pip install -e $root
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
$bin = Join-Path $root '.venv\Scripts'
Write-Host "WSA installed. Add this directory to your user PATH, then open a new terminal:"
Write-Host $bin
Write-Host "You can also run it directly: & '$bin\wsa.exe'"
