param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-z][a-z0-9-]{0,63}$')]
    [string]$Id,
    [Parameter(Mandatory = $true)]
    [string]$Name
)

$ErrorActionPreference = 'Stop'
$directory = Join-Path (Join-Path $PSScriptRoot 'addons') $Id
if (Test-Path -LiteralPath $directory) { throw "The add-on folder already exists: $directory" }
$className = (($Id -split '-') | ForEach-Object { $_.Substring(0, 1).ToUpperInvariant() + $_.Substring(1) }) -join ''
$manifest = [ordered]@{
    id = $Id
    name = $Name
    version = '1.0.0'
    description = 'A custom Source 2 Porting Kit add-on.'
    author = ''
    apiVersion = 1
    assembly = "bin/{configuration}/net10.0/Kit.Addons.$className.dll"
    entryPoint = "PortingKit.Addons.$className.Addon"
    project = "$className.csproj"
    dependencies = @()
}
$project = @'
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <AssemblyName>Kit.Addons.__CLASS__</AssemblyName>
    <EnableDynamicLoading>true</EnableDynamicLoading>
  </PropertyGroup>
  <ItemGroup>
    <ProjectReference Include="../../src/Kit.Contracts/Kit.Contracts.csproj" Private="false" />
  </ItemGroup>
</Project>
'@
$source = @'
namespace PortingKit.Addons.__CLASS__;

public sealed class Addon : IAddon
{
    public void Initialize(IAddonContext context)
    {
        context.Register<ITool>("run", new AddonTool());
    }

    public ValueTask DisposeAsync() => ValueTask.CompletedTask;
}

public sealed class AddonTool : ITool
{
    public ToolDefinition Definition { get; } = new("New add-on tool", "Custom", "Edit this tool to add your processing workflow.", []);

    public Task<ToolResult> RunAsync(ToolContext context, ToolArguments arguments, CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        context.Log("The add-on loaded and its job ran successfully.");
        return Task.FromResult(new ToolResult("Add-on ready"));
    }
}
'@
New-Item -ItemType Directory -Path $directory | Out-Null
[IO.File]::WriteAllText((Join-Path $directory 'addon.json'), ($manifest | ConvertTo-Json -Depth 5))
[IO.File]::WriteAllText((Join-Path $directory "$className.csproj"), $project.Replace('__CLASS__', $className))
[IO.File]::WriteAllText((Join-Path $directory 'Addon.cs'), $source.Replace('__CLASS__', $className))
Write-Host "Created $directory. Run Build.ps1 and restart V4 to load it."
