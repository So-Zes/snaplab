param(
    [string]$InstallDir = (Join-Path $HOME '.snaplab\app'),
    [string]$DataDir = $(if ($env:SNAPLAB_DATA) { $env:SNAPLAB_DATA } else { Join-Path $HOME '.snaplab\data' }),
    [string]$Repository = 'https://github.com/So-Zes/snaplab.git',
    [int]$Port = 18445,
    [switch]$SetupWorker
)
$ErrorActionPreference = 'Stop'
function Find-Python {
    foreach ($candidate in @('python', 'python3', 'py')) {
        if (Get-Command $candidate -ErrorAction SilentlyContinue) {
            $previousPreference = $ErrorActionPreference
            try {
                $ErrorActionPreference = 'Continue'
                & $candidate -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>$null
                if ($LASTEXITCODE -eq 0) { return (Get-Command $candidate).Source }
            } finally { $ErrorActionPreference = $previousPreference }
        }
    }
    return $null
}
function Install-PackageForUser([string]$id, [string]$label) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "Install $label from its official website, then run this command again. Windows App Installer (winget) was not found."
    }
    Write-Host "Installing $label for this Windows user using winget ($id). Existing installations will not be upgraded."
    & winget install --id $id --exact --source winget --scope user --no-upgrade --accept-package-agreements --accept-source-agreements --disable-interactivity
    if ($LASTEXITCODE -ne 0) { throw "Could not install $label. Install it from its official website, then run this command again." }
    $env:PATH = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')
}
try {
    Write-Host 'SnapLab installs only missing requirements. Its Python packages go in the app folder; existing Python packages and settings are kept.'
    $pythonExe = Find-Python
    if (-not $pythonExe) { Install-PackageForUser 'Python.Python.3.13' 'Python 3.13'; $pythonExe = Find-Python }
    if (-not $pythonExe) { throw 'Python is installed but not available yet. Open a new PowerShell window and run this command again.' }
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Install-PackageForUser 'Git.Git' 'Git' }
    if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
        Write-Host 'Node.js is not installed. SnapLab works without it, but MCP servers that start with npx need it.'
        try { Install-PackageForUser 'OpenJS.NodeJS.LTS' 'Node.js LTS' } catch { Write-Host $_.Exception.Message }
    }
    # A checked-out script uses its own repository. A downloaded script clones to a new directory.
    if ($PSScriptRoot -and (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'tools\launch.py'))) { $InstallDir = $PSScriptRoot }
    elseif (-not (Test-Path -LiteralPath $InstallDir)) {
        Write-Host "Downloading SnapLab into $InstallDir."
        & git clone -- $Repository $InstallDir
        if ($LASTEXITCODE -ne 0) { throw 'Could not download SnapLab. Check repository access and your connection. If a partial folder was created, choose another -InstallDir.' }
    }
    elseif (-not (Test-Path -LiteralPath (Join-Path $InstallDir 'tools\launch.py'))) { throw 'The installation folder already contains other files. Choose another -InstallDir; nothing was replaced.' }
    $venv = Join-Path $InstallDir '.venv'
    if (-not (Test-Path -LiteralPath $venv)) {
        Write-Host "Creating a Python environment in $venv."
        & $pythonExe -m venv $venv
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python environment. Check write access and available disk space, then try again.' }
    }
    $venvPython = Join-Path $venv 'Scripts\python.exe'
    & $venvPython -c "import importlib.util, sys; sys.exit(0 if all(importlib.util.find_spec(m) for m in ('cryptography', 'PIL', 'pypdf')) else 1)"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Installing cryptography, Pillow and pypdf in $venv (notifications, photos without camera metadata, PDF text)."
        & $venvPython -m pip install cryptography pillow pypdf
        if ($LASTEXITCODE -ne 0) { throw 'Could not install the Python package. Check your internet connection and run this command again.' }
    }
    if ($SetupWorker) {
        Write-Host 'Preparing a dedicated WSL2 Linux workspace. Existing Windows and Docker settings will be kept.'
        & $venvPython (Join-Path $InstallDir 'tools\setup_wsl.py') --resume
        if ($LASTEXITCODE -ne 0) { Write-Host 'The workspace is not ready. Chat is still available; SnapCode will stay disabled.' }
    }
    & $venvPython (Join-Path $InstallDir 'tools\launch.py') --data $DataDir --port $Port
    if ($LASTEXITCODE -ne 0) { throw 'The launcher could not finish. Follow the message above and run this command again.' }
} catch {
    Write-Host "SnapLab setup stopped. $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
