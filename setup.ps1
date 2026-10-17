param([switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
function Refresh-TaskPath {
    $env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')
}
function Install-PackageIfMissing([string]$Command, [string]$Package) {
    $existing = Get-Command $Command -ErrorAction SilentlyContinue
    if ($existing -and -not ($Command -eq 'python' -and $existing.Source -like '*WindowsApps*')) { return }
    if ($CheckOnly) { throw "Missing $Command. Double-click start.bat to install." }
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { throw "Windows App Installer (winget) is required. Install Python 3.10+, Node.js LTS and Codex CLI, then retry." }
    Write-Host "Installing $Package..." -ForegroundColor Cyan
    & winget install --id $Package --exact --source winget --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "Could not install $Package." }
    Refresh-TaskPath
}
try {
    Install-PackageIfMissing 'python' 'Python.Python.3.12'
    Install-PackageIfMissing 'node' 'OpenJS.NodeJS.LTS'
    $npmCommand = Get-Command npm.cmd -ErrorAction Stop
    $codexCommand = Get-Command codex.cmd -ErrorAction SilentlyContinue
    if (-not $codexCommand) {
        if ($CheckOnly) { throw 'Codex CLI is missing. Double-click start.bat.' }
        & $npmCommand.Source install -g '@openai/codex'
        if ($LASTEXITCODE -ne 0) { throw 'Codex CLI installation failed.' }
        Refresh-TaskPath
        $codexCommand = Get-Command codex.cmd -ErrorAction Stop
    }
    & $codexCommand.Source --version
    if (-not $CheckOnly) {
        & $codexCommand.Source features enable image_generation
        if ($LASTEXITCODE -ne 0) {
            & $npmCommand.Source install -g '@openai/codex@latest'
            if ($LASTEXITCODE -ne 0) { throw 'Codex CLI update failed.' }
            & $codexCommand.Source features enable image_generation
            if ($LASTEXITCODE -ne 0) { throw 'Codex CLI does not support image generation.' }
        }
    }
    & $codexCommand.Source login status
    if ($LASTEXITCODE -ne 0) {
        if ($CheckOnly) { throw 'Codex is not logged in. Double-click start.bat.' }
        Write-Host 'Complete the Codex sign-in in your browser.' -ForegroundColor Cyan
        & $codexCommand.Source login
        if ($LASTEXITCODE -ne 0) { throw 'Codex login was not completed.' }
    }
    if (-not $CheckOnly) {
        $venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
        if (-not (Test-Path -LiteralPath $venvPython)) { & python -m venv (Join-Path $PSScriptRoot '.venv') }
        if ($LASTEXITCODE -ne 0) { throw 'Python environment setup failed.' }
        & $venvPython -m pip install --disable-pip-version-check -r (Join-Path $PSScriptRoot 'requirements.txt')
        if ($LASTEXITCODE -ne 0) { throw 'Image dependency installation failed.' }
        Set-Content -LiteralPath (Join-Path $PSScriptRoot '.venv\codex-ready') -Value 'ready'
    }
    Write-Host 'Environment ready. Double-click start.bat to open the workbench.' -ForegroundColor Green
} catch { Write-Host $_ -ForegroundColor Red; exit 1 }
