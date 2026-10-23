param(
    [ValidateSet('Debug', 'Release')]
    [string]$Configuration = 'Release'
)

$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath $PSScriptRoot
try {
    $projects = @('src/Kit.Desktop/Kit.Desktop.csproj', 'src/Kit.Cli/Kit.Cli.csproj')
    foreach ($project in $projects) {
        & dotnet build $project -c $Configuration --nologo -v quiet
        if ($LASTEXITCODE -ne 0) { throw "Could not build $project" }
    }

    $addonsRoot = Join-Path $PSScriptRoot 'addons'
    if (!(Test-Path -LiteralPath $addonsRoot -PathType Container)) { return }
    foreach ($addonDirectory in Get-ChildItem -LiteralPath $addonsRoot -Directory | Sort-Object Name) {
        $manifestPath = Join-Path $addonDirectory.FullName 'addon.json'
        if (!(Test-Path -LiteralPath $manifestPath -PathType Leaf)) { continue }
        $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
        if (!$manifest.project) { continue }
        $project = [IO.Path]::GetFullPath((Join-Path $addonDirectory.FullName $manifest.project))
        $addonRoot = $addonDirectory.FullName + [IO.Path]::DirectorySeparatorChar
        if (!$project.StartsWith($addonRoot, [StringComparison]::OrdinalIgnoreCase) -or [IO.Path]::GetExtension($project) -ne '.csproj') {
            throw "The project for $($manifest.id) must be inside its add-on folder."
        }
        Write-Host "Building add-on: $($manifest.id)"
        & dotnet build $project -c $Configuration --nologo -v quiet
        if ($LASTEXITCODE -ne 0) { throw "Could not build add-on: $($manifest.id)" }
    }
}
finally {
    Pop-Location
}
