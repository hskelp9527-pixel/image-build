$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
try {
    $nodeCheck = Get-Command node -ErrorAction SilentlyContinue
    $codexCheck = Get-Command codex.cmd -ErrorAction SilentlyContinue
    $setupMarker = Join-Path $PSScriptRoot '.venv\codex-ready'
    if (-not $nodeCheck -or -not $codexCheck -or -not (Test-Path -LiteralPath $setupMarker)) {
        Write-Host 'First launch: preparing Node.js, Codex CLI and login...' -ForegroundColor Cyan
        & (Join-Path $PSScriptRoot 'setup.ps1')
        if ($LASTEXITCODE -ne 0) { throw 'Run start.bat to finish installation.' }
    }
    $pythonCommand = $null
    foreach ($candidate in @('py', 'python', 'python3')) {
        if (Get-Command $candidate -ErrorAction SilentlyContinue) {
            & $candidate -c 'import sys; assert sys.version_info >= (3, 10)' 2>$null
            if ($LASTEXITCODE -eq 0) { $pythonCommand = $candidate; break }
        }
    }
    if (-not $pythonCommand) {
        & (Join-Path $PSScriptRoot 'setup.ps1')
        if ($LASTEXITCODE -ne 0) { throw 'Run start.bat to finish installing Python.' }
        $pythonCommand = 'python'
    }
    $venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython)) {
        Write-Host 'Preparing the local workspace (first launch only)...'
        & $pythonCommand -m venv (Join-Path $PSScriptRoot '.venv')
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python environment.' }
    }
    $requirements = Join-Path $PSScriptRoot 'requirements.txt'
    $marker = Join-Path $PSScriptRoot '.venv\requirements.sha256'
    $hash = (Get-FileHash -LiteralPath $requirements -Algorithm SHA256).Hash
    $installed = if (Test-Path -LiteralPath $marker) { (Get-Content -LiteralPath $marker -Raw).Trim() } else { '' }
    if ($installed -ne $hash) {
        Write-Host 'Installing image tools (internet needed on first launch)...'
        & $venvPython -m pip install --disable-pip-version-check -r $requirements
        if ($LASTEXITCODE -ne 0) { throw 'Installation failed. Check your internet connection and try again.' }
        Set-Content -LiteralPath $marker -Value $hash
    }
    Write-Host 'Opening Image Build. Keep this window open; Ctrl+C stops the workspace.'
    Write-Host 'Date archiving runs with this process, even when browser tabs are closed.'
    & $venvPython (Join-Path $PSScriptRoot 'app.py')
    if ($LASTEXITCODE -ne 0) { throw 'Workspace stopped unexpectedly.' }
} catch {
    Write-Host $_ -ForegroundColor Red
    exit 1
}
