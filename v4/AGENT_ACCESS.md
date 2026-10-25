# Source Backport agent access

Use the CLI or MCP for backend work. Neither entry point opens a desktop window.
They share tool discovery, validation, jobs, settings, assets, and outputs with
the desktop. This is native C#/.NET access to V4; it does not invoke the older
Python applications or provide their unimplemented conversion pipelines.

## Build and start

From the repository root:

```powershell
& .\v4\Build.ps1 -AgentsOnly
& .\v4\Backport.cmd status
& .\v4\Backport.cmd list
& .\v4\Backport.cmd describe hotspot/generate
```

The wrappers build missing executables on first use. Rebuild after editing source;
an existing executable is reused for fast subsequent calls. For the lowest startup
overhead, call `v4/src/Kit.Cli/bin/Release/net10.0-windows/s2kit.exe` directly.
The desktop launcher builds both agent entry points as part of its normal build.
MCP alone uses the pinned official `ModelContextProtocol.Core` NuGet package;
the desktop, CLI, core, and included add-ons have no added NuGet dependencies.

## Shared backend and lifecycle

Agent calls connect to the backend serving the selected state directory. If the
updated desktop is open, they use its live queue and services. Otherwise, the
client automatically starts one hidden headless backend. Concurrent clients share
that owner; a state lock prevents duplicate writers. IPC uses a bounded Windows
named pipe restricted to the current Windows user, with no network listener.

The headless process remains available after a CLI command or MCP session ends.
Submitted jobs continue running. `run` waits for completion; Ctrl+C requests job
cancellation and returns exit code 130. For persistent work use `submit`, retain
the returned ID, and poll it. Cancel through `jobs cancel ID` when intended.
Cancelling an MCP wait request only cancels that wait, not the underlying job.

```powershell
& .\v4\Backport.cmd server status
& .\v4\Backport.cmd server stop
```

Stopping is allowed only for an idle headless owner. Active jobs and desktop
owners are refused. Opening the desktop takes over from an idle headless backend,
reloading saved settings and job history. If the headless backend has active work,
finish or cancel it before opening the desktop. The current workspace selection
is session state; recent workspace paths and project markers are persisted.

Backend binaries and add-ons remain loaded until the owner exits. Stop it before
rebuilding; restart the desktop if it owns the backend. An older desktop build
without an agent endpoint produces an actionable error rather than a second
writer. Agent requests are not automatically replayed after a connection failure,
because a submitted mutation may already have happened.

Global `--root V4_FOLDER` and `--state STATE_FOLDER` options precede the command.
Use a separate state directory for isolated experiments; the default is `v4/.state`.
Paths supplied to tools, assets, and workspaces should be absolute. Tool calls use
their declared defaults and supplied inputs, without inheriting saved GUI drafts.

## CLI workflow

```powershell
& .\v4\Backport.cmd validate hotspot/generate --width 1024 --height 512 --layout trim-124
& .\v4\Backport.cmd submit hotspot/generate --width 1024 --height 512 --layout trim-124
& .\v4\Backport.cmd jobs get JOB_UUID
& .\v4\Backport.cmd jobs wait JOB_UUID --wait-seconds 30
& .\v4\Backport.cmd logs --job-id JOB_UUID --count 50
& .\v4\Backport.cmd artifacts list JOB_UUID
& .\v4\Backport.cmd artifacts read JOB_UUID --path sheet.json --utf8 true
```

Replace `JOB_UUID` with the actual returned ID. `run TOOL` is a submit-and-wait
shortcut. A completed job includes
its summary, result data, artifact references, and any failure message.

Every successful response is JSON on stdout. Errors are `{ "error": { "code",
"message" } }` on stderr. Exit codes are 0 for success, 1 for a failed operation or
job, 2 for invalid input, 3 when `jobs wait` reaches its deadline with work still
active, and 130 for cancellation. `jobs get` reports a snapshot successfully even
when that snapshot describes a failed job; inspect `status` as well as the exit
code. Listings are paged. Log reads cover the bounded live tail, not all disk logs.

`operations` returns all operation names, descriptions, and input schemas. The
generic `call` command can invoke any operation. Use JSON files to avoid shell
quoting issues, especially for nested objects or settings:

```powershell
& .\v4\Backport.cmd call settings.set --json-file request.json
& .\v4\Backport.cmd run hotspot/generate --json-file inputs.json
Get-Content -Raw inputs.json | & .\v4\Backport.cmd validate hotspot/generate --json-file -
```

For example, a `settings.set` request can contain:

```json
{ "key": "runtime", "value": { "workers": 1, "diskCacheMegabytes": 512 } }
```

Use `runtime` and `appearance` for host settings. The host also reserves
`tools.vrf`, `tools.studiomdl`, and `tools.ffmpeg` for future add-ons; the current
Hotspot editor does not use external executables. Use `addon_id` to read or write
a loaded add-on's own settings.
These calls use the same in-memory store as that add-on. Add-on settings may be
read only when a page opens or at startup; the response conservatively marks them
as requiring a restart. Runtime concurrency/cache budgets also require restart.
Appearance changes update the open desktop. Partial appearance objects use the
same defaults as the desktop; supply the full record when preserving every choice.

Other commands include `addons enable|disable ID`, `workspace create|open DIR`,
`workspace get`, `assets providers|list|read`, and `cache stats|clear`. Run `help`
for shortcuts, or inspect `operations` for precise fields and limits.
Only the Hotspot editor add-on is included. Its five supporting operations remain
available through CLI/MCP but have no separate desktop sidebar forms. The host
retains asset-provider operations for future add-ons; `assets providers` is empty
in the current distribution.

## MCP connection

Build first, then run this to print a ready-to-use stdio server entry with absolute
paths for the current checkout:

```powershell
& .\v4\Get-AgentConfig.ps1
```

Merge its `source-backport` entry into your MCP client's server configuration.
The generated entry uses `dotnet` with the absolute server DLL as an argument,
avoiding Windows client quoting differences for executable paths containing spaces.
`Get-AgentConfig.ps1 -State C:\Scratch\BackportState` selects an isolated backend.
It prints configuration and does not alter other agent settings. The executable
is `v4/src/Kit.Mcp/bin/Release/net10.0-windows/sourcebackport-mcp.exe`, with optional
`--root` and `--state` argument pairs. `Mcp.cmd` is also a stdio launcher; first-use
build output is sent to stderr so stdout contains only protocol messages.

MCP helper names replace operation dots with underscores: `jobs.get` becomes
`jobs_get`, `assets.list` becomes `assets_list`. Each loaded `ITool` also appears
as a typed `run_*` tool. Its stable name includes a short hash of the contribution
ID to prevent collisions. Inspect `tools_list` or `tools_describe` rather than
guessing that name. Adding a tool to an add-on requires no MCP or CLI registration.
Restart the backend after building the add-on, then reconnect or refresh tool
discovery in the MCP client. MCP result data is returned both as structured content
and JSON text for client compatibility.

## Bounds and validation

Inputs reject unknown fields, duplicates, invalid types, and out-of-range values.
Tool values also pass the shared desktop/CLI normalization and choice validation.
Jobs use the existing bounded queue, cancellation, process-tree cleanup, durable
history, and output folders. Backend requests have a 45-second execution deadline;
use jobs for longer work. Up to eight local requests execute concurrently.

Asset-provider lists hold at most 200 entries per page. Reads return at most 64 KiB,
with `nextOffset` and `hasMore` for subsequent chunks. Base64 is authoritative;
optional UTF-8 text can split a multibyte character at a chunk boundary.
Artifact paths are relative to that job's output folder and reject traversal,
links, and alternate stream names. File reads do not open editors or decode assets.
Responses larger than 4 MB return `response_too_large`; request a smaller page or
read an output artifact in chunks. This is a local trusted-user interface, with
the same file and tool access as the desktop, not a sandbox for untrusted add-ons.
