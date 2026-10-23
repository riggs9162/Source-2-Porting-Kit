param([string]$State)

$arguments = @((Join-Path $PSScriptRoot 'src/Kit.Mcp/bin/Release/net10.0-windows/sourcebackport-mcp.dll'), '--root', $PSScriptRoot)
if ($State) { $arguments += @('--state', [IO.Path]::GetFullPath($State)) }
@{
    mcpServers = @{
        'source-backport' = @{
            command = 'dotnet'
            args = $arguments
        }
    }
} | ConvertTo-Json -Depth 5
