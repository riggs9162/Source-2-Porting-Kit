param([switch]$NoBuild)

$ErrorActionPreference = 'Stop'
$application = Join-Path $PSScriptRoot 'src/Kit.Desktop/bin/Release/net10.0-windows/Source2PortingKit.exe'
$running = Get-Process -Name Source2PortingKit -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $application }
if ($running) {
    Write-Host 'V4 is already running.'
    return
}
if (!$NoBuild) {
    & (Join-Path $PSScriptRoot 'Build.ps1')
}
if (!(Test-Path -LiteralPath $application -PathType Leaf)) {
    throw 'Build V4 before launching it with -NoBuild.'
}
Start-Process -FilePath $application -WorkingDirectory $PSScriptRoot -WindowStyle Normal
