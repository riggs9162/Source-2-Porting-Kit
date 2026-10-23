param(
    [ValidateSet('Cli', 'Mcp')]
    [string]$Mode = 'Cli',
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
)

$ErrorActionPreference = 'Stop'
$relative = if ($Mode -eq 'Mcp') { 'src/Kit.Mcp/bin/Release/net10.0-windows/sourcebackport-mcp.exe' } else { 'src/Kit.Cli/bin/Release/net10.0-windows/s2kit.exe' }
$executable = Join-Path $PSScriptRoot $relative
if (!(Test-Path -LiteralPath $executable -PathType Leaf)) {
    & (Join-Path $PSScriptRoot 'Build.ps1') -AgentsOnly *>&1 | ForEach-Object { [Console]::Error.WriteLine($_.ToString()) }
}
& $executable --root $PSScriptRoot @Arguments
exit $LASTEXITCODE
