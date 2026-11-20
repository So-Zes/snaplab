# Builds installer\dist\SnapLab-Setup.exe with the C# compiler that ships with Windows (.NET Framework 4.x).
# Nothing to install: run `powershell -ExecutionPolicy Bypass -File installer\build.ps1`.
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $here
$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $csc)) { throw "The .NET Framework 4 C# compiler was not found at $csc." }
$dist = Join-Path $here 'dist'
New-Item -ItemType Directory -Force -Path $dist | Out-Null
$out = Join-Path $dist 'SnapLab-Setup.exe'
$cscArgs = @(
    '/nologo', '/target:winexe', '/optimize+', '/codepage:65001', '/platform:anycpu',
    "/out:$out",
    "/win32manifest:$(Join-Path $here 'app.manifest')",
    "/win32icon:$(Join-Path $root 'assets\snaplab.ico')",
    "/resource:$(Join-Path $root 'web\icons\icon-192.png'),logo.png",
    '/reference:System.dll', '/reference:System.Core.dll', '/reference:System.Drawing.dll', '/reference:System.Windows.Forms.dll',
    (Join-Path $here 'SnapLabSetup.cs')
)
& $csc @cscArgs
if ($LASTEXITCODE -ne 0) { throw "The compiler reported errors (exit $LASTEXITCODE)." }
Write-Host "Built $out ($([math]::Round((Get-Item $out).Length / 1KB)) KB)"
