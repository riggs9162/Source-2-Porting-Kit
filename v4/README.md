# Source Backport (V4)

**Source 2 → Source 1 asset porting.**

Source Backport is the V4 Windows desktop application built with C# and .NET 10. It has its own
backend and directory-based add-ons. It does not load the Python applications.

This first milestone provides the architecture, desktop shell, and working
utility/asset tools. The native VRF map/model renderer, soundscape editor,
material pipelines, and model/ragdoll conversion pipelines are not implemented
in V4 yet. Those features belong in add-ons built on this foundation.

## Run

Install the [.NET 10 SDK](https://dotnet.microsoft.com/download/dotnet/10.0), then
double-click `Start.cmd`. It builds the host and discovered add-on projects before
launching the desktop application. Subsequent builds are incremental.
An add-on build failure is shown in the add-on manager while independent add-ons
remain usable. Running `Build.ps1` directly stops at the first build error.

```powershell
.\Build.ps1
.\Start.ps1 -NoBuild
```

The desktop, CLI, backend, and included add-ons use the libraries shipped with
the .NET SDK. The optional MCP entry point uses the pinned official C# MCP SDK.
Native asset export requires the
separate Source2Viewer-CLI executable. Configure it in Tool settings or supply a tool
override. StudioMDL and FFmpeg paths are also available for future add-ons.

## Included add-ons

| Folder | Contributions |
| --- | --- |
| `addons/asset-browser/` | Native asset browser, folder and VPK providers, paged listing tool |
| `addons/utilities/` | Environment report and streaming file inventory with optional SHA-256 |
| `addons/vrf/` | Cancellable VRF export and resource-block inspection |

## Personalize your workshop

Open **Appearance** in the sidebar or toolbar. Choose Graphite, Blueprint, or
Paper, then set an accent color, interface font, text size, spacing, and personal
workspace name. The RAM readout can also be hidden. **Apply appearance** saves
these preferences and updates the shell and add-on controls immediately, without
restarting. Preferences return at the next launch. **Reset choices** restores the
form defaults; apply them to save.

The interface uses native drawing and small reusable vector marks. Themes add no
browser runtime, background images, or animated effects. Very low-contrast custom
accents are adjusted to keep controls readable. The build/output filenames and
add-on IDs retain their V4 names for compatibility with existing scripts.

The [visual rules](DESIGN.md) describe the identity; the [add-on guide](ADDONS.md)
documents the shared styling API.

The browser reads VPK v1/v2 directory metadata without retaining the entire index
or decoding textures, audio, or geometry. It supports embedded and split-archive
asset streams. Each page holds at most 100 rows. Large searches may scan the
directory again; this deliberately trades repeated metadata reads for bounded
retained memory. CRC/signature verification and nested VPK browsing are not
provided by this initial provider.

VRF exports require a path filter for VPK inputs, preventing an accidental
whole-game export. Each export gets a new job output folder. This is a direct
VRF export, not the complete V3 source-layout/material/physics pipeline.

## Architecture

```text
v4/
  addons/                   # Independently built, automatically discovered folders
    asset-browser/          # Manifest, project, providers, UI, tool, and documentation
    utilities/
    vrf/
  src/
    Kit.Contracts/          # UI-independent add-on, tool, asset, and service APIs
    Kit.Core/               # Loader, jobs, hooks, settings, workspaces, cache, processes
    Kit.DesktopSdk/         # Optional native-page interface and shared controls
    Kit.Desktop/            # Windows Forms shell and schema-driven tool pages
    Kit.Cli/                # Headless access to the same tools and job queue
    Kit.Mcp/                # Official MCP stdio transport over the shared agent API
  .state/                   # Ignored local settings, cache, logs, job history, outputs
```

The desktop creates an add-on page only when selected and disposes the old page
on navigation. Add-on constructors and initialization should register lightweight
factories; they must not load game libraries or decode assets at startup.

Tools run on a bounded queue away from the UI thread. The default is one worker;
Settings permits up to four. Running executables uses separate argument tokens,
concurrent stdout/stderr drainage, bounded output lines/tails, optional timeouts,
and process-tree cancellation. A nonzero exit code is returned to the tool so it
can apply its own tool-specific success criteria.

Settings use serialized, atomic JSON writes. An exclusive state-directory lock
prevents two hosts from overwriting each other's settings and job records. Each
add-on receives its own settings store and data directory.

The job list retains the latest 100 finished records. Jobs left queued/running
after an unclean exit become Interrupted when reopened. Cancellation is
cooperative for C# tools; tools must check their token. External processes are
terminated as a process tree. No automatic retries or silent job resumption occur.
Job output folders are durable and are not deleted when old history records are
pruned. Remove unneeded outputs yourself after reviewing them.

The live log tail holds 500 records; the asynchronous disk log rotates at 4 MB
with three archives. Pending disk log writes are bounded and may drop old entries
under extreme logging pressure. The disk preview cache has a configurable budget
and lease-based eviction: active files remain pinned until their consumers
dispose the lease. This disk budget is not a cap on total application RAM/VRAM.

Asset references are small provider/container/path records. Asset providers open
streams on demand. `IPreviewSession` defines disposable ownership for future
viewer resources. Native renderer integration and its map/texture memory budget
still need their own measured prototype.

## Agent access: MCP and CLI

Use [AGENT_ACCESS.md](AGENT_ACCESS.md) for agent workflows, the full operation
catalog, backend lifecycle, and MCP configuration. `Get-AgentConfig.ps1` prints
a ready-to-use stdio server entry for this checkout. New add-on tools appear
automatically in both interfaces.

```powershell
$cli = '.\src\Kit.Cli\bin\Release\net10.0-windows\s2kit.exe'
& $cli list
& $cli addons
& $cli describe utilities/inventory
& $cli doctor
& $cli run utilities/inventory --directory 'A:\My assets' --hash true
& $cli jobs
& $cli assets providers
& $cli operations
```

The CLI and MCP share the running desktop's backend. When it is closed, they
automatically start a hidden headless backend that keeps submitted jobs alive
after clients disconnect. The CLI prints JSON; `run` waits and `submit` returns
a job ID immediately. Ctrl+C cancels a job awaited by `run`. Use a separate
`--state` directory before the command for isolated work. The Windows Desktop
runtime is required to resolve add-ons with native UI extensions; their pages
are not created by headless access. `Backport.cmd` and `Mcp.cmd` are launchers.

## Add a tool

```powershell
.\New-Addon.ps1 -Id my-converter -Name 'My converter'
.\Build.ps1
```

Edit the generated folder and restart V4. No central project or sidebar registry
needs editing. See [ADDONS.md](ADDONS.md) for the contract, lifecycle, hooks,
dependency rules, native workspaces, and deployment layout.
