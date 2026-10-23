param(
    [ValidateSet('Debug', 'Release')]
    [string]$Configuration = 'Release',
    [switch]$ContinueOnAddonError,
    [switch]$AgentsOnly
)

$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath $PSScriptRoot
try {
    $projects = @('src/Kit.Cli/Kit.Cli.csproj', 'src/Kit.Mcp/Kit.Mcp.csproj')
    if (!$AgentsOnly) { $projects = @('src/Kit.Desktop/Kit.Desktop.csproj') + $projects }
    foreach ($project in $projects) {
        & dotnet build $project -c $Configuration --nologo -v quiet
        if ($LASTEXITCODE -ne 0) { throw "Could not build $project" }
    }

    $addonsRoot = Join-Path $PSScriptRoot 'addons'
    if (!(Test-Path -LiteralPath $addonsRoot -PathType Container)) { return }
    foreach ($addonDirectory in Get-ChildItem -LiteralPath $addonsRoot -Directory | Sort-Object Name) {
        $manifestPath = Join-Path $addonDirectory.FullName 'addon.json'
        if (!(Test-Path -LiteralPath $manifestPath -PathType Leaf)) { continue }
        $errorPath = Join-Path $addonDirectory.FullName 'obj/v4-build-error.txt'
        try {
            $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
            if ($manifest.project) {
                $project = [IO.Path]::GetFullPath((Join-Path $addonDirectory.FullName $manifest.project))
                $addonRoot = $addonDirectory.FullName + [IO.Path]::DirectorySeparatorChar
                if (!$project.StartsWith($addonRoot, [StringComparison]::OrdinalIgnoreCase) -or [IO.Path]::GetExtension($project) -ne '.csproj') {
                    throw "The project for $($manifest.id) must be inside its add-on folder."
                }
                Write-Host "Building add-on: $($manifest.id)"
                & dotnet build $project -c $Configuration --nologo -v quiet
                if ($LASTEXITCODE -ne 0) { throw "Could not build add-on: $($manifest.id). See the build output for compiler errors." }
            }
            if (Test-Path -LiteralPath $errorPath) { Remove-Item -LiteralPath $errorPath }
        }
        catch {
            New-Item -ItemType Directory -Path (Split-Path -Parent $errorPath) -Force | Out-Null
            [IO.File]::WriteAllText($errorPath, $_.Exception.Message)
            if (!$ContinueOnAddonError) { throw }
            Write-Warning $_.Exception.Message
        }
    }
}
finally {
    Pop-Location
}
