# V4 add-ons

An add-on owns one directory under `v4/addons/`. Keep its manifest, C# project,
UI, processing logic, parsers, assets, and documentation inside that directory.
Only code shared by unrelated add-ons belongs in the host or SDK.

The model follows Helix's directory discovery, metadata, local state, hooks,
registration, and lifecycle. It uses explicit C# contracts instead of Lua globals
or filename realms. Add-ons are trusted local code with the application's full
filesystem/process privileges; assembly isolation is not a security sandbox.

## Folder

```text
addons/my-converter/
  addon.json
  MyConverter.csproj
  Addon.cs
  Tools/
  UI/
  Assets/
  README.md
```

```json
{
  "id": "my-converter",
  "name": "My converter",
  "version": "1.0.0",
  "description": "Converts selected assets.",
  "author": "Your name",
  "apiVersion": 1,
  "assembly": "bin/{configuration}/net10.0/Kit.Addons.MyConverter.dll",
  "entryPoint": "PortingKit.Addons.MyConverter.Addon",
  "project": "MyConverter.csproj",
  "dependencies": []
}
```

IDs use lowercase letters, digits, and hyphens, beginning with a letter. Versions
are numeric .NET versions, for example `1.0.0`. API version 1 is the compatibility
boundary; dependency version ranges are not implemented. Dependencies name other
add-on IDs and load first. Their assemblies remain private; communicate through
host contracts, registered extensions, and typed hooks.

`Build.ps1` finds each immediate add-on directory's manifest and builds its project.
The launcher uses its continue-on-add-on-error mode so a broken add-on cannot
prevent the desktop from opening. A failed build records an ignored error marker
under that add-on's `obj/`; the loader reports the failure and avoids loading a
stale DLL. The next successful build clears the marker.
For a prebuilt distribution, omit `project` and set `assembly` to the DLL's relative
path inside the folder. Include the DLL, `.deps.json`, private dependencies, native
libraries, and runtime assets in that same folder. `{configuration}` resolves to
the host's Debug/Release configuration. Project and entry-assembly paths must stay
inside the owning folder.

## Registration and lifecycle

```csharp
public sealed class Addon : IAddon
{
    public void Initialize(IAddonContext context)
    {
        context.Register<ITool>("convert", new ConversionTool());
        context.Subscribe<WorkspaceChanged>((message, token) =>
        {
            context.Host.Log.Write(LogLevel.Info, context.Manifest.Id,
                $"Workspace: {message.Workspace.Name}");
            return ValueTask.CompletedTask;
        });
    }

    public ValueTask DisposeAsync() => ValueTask.CompletedTask;
}
```

Registration prefixes every local ID with the add-on ID. The tool above becomes
`my-converter/convert`. Contributions are keyed by interface and full ID; duplicate
registrations fail. Register against the intended contract explicitly, such as
`Register<ITool>` or `Register<IAssetProvider>`.

`context.Settings` stores add-on-local options in `.state/addons/<id>/settings.json`.
`context.DataDirectory` provides persistent local data. `context.Directory` points
to the add-on's read-only source/assets location; initialization must not write
runtime state there. Global settings, cache, jobs, processes, workspaces, and the
extension catalog are available through `context.Host`.

Initialization exceptions roll back that add-on's contributions and hook
subscriptions, dispose the instance, and report Failed in the add-on manager.
Missing, disabled, failed, duplicate, incompatible, and cyclic dependencies do not
prevent independent add-ons from loading. Hooks registered through the context are
automatically removed during cleanup. A handler failure is logged and dispatch
continues to the next handler.

Desktop shutdown disposes the active page, cancels/drains jobs, then releases
add-ons in reverse load order. Each add-on has a collectible AssemblyLoadContext
with private dependency resolution; host contract assemblies are shared to
preserve interface identity. Unloading is cooperative, and references retained
by add-on static state or background threads can delay collection. Enable/disable
and code changes currently apply on restart. There is no live code reload.

## Tools

Implement `ITool` and register a `ToolDefinition` with parameter schemas. The host
generates a native form and exposes the same operation to the CLI. Inputs validate
before entering the queue. Unknown keys are rejected. Integer ranges, booleans,
required values, and choices are enforced centrally. Filesystem and format-specific
validation belongs to the tool.

`RunAsync` receives `ToolContext`, normalized `ToolArguments`, and a cancellation
token. Use `context.Report` for progress and `context.Log` for job output. Write
durable results to `context.OutputDirectory` or a user-selected project output,
then return a summary and artifact paths. Large data belongs in files, not in
`ToolResult.Data`, hook messages, or logs.

Check cancellation during loops. Use `context.Host.Processes.RunAsync` for native
programs and inspect their exit codes and produced files. Never construct a shell
command from user inputs. Bound your own internal parallelism; one queued job
must not silently launch dozens of converters.

## Custom desktop pages

Use `Kit.DesktopSdk` only when the tool needs a custom workspace. Set the project
target to `net10.0-windows` and `UseWindowsForms` to true. Reference the contracts
and desktop SDK with `Private="false"` and set `EnableDynamicLoading` to true.
The included asset browser is a complete example.

Implement `IDesktopPage` and register it through the same context:

```csharp
context.Register<IDesktopPage>("editor", new SoundscapePage());
```

The host calls `Create(DesktopContext)` on the UI thread only when selected. The
returned Control is disposed when navigating away. A page must cancel pending
loads, dispose subscriptions, release cache leases, audio players, GPU objects,
and preview sessions during disposal. An edit session that should survive page
navigation belongs in add-on-owned state, not an undisposed hidden Control.

Keep processing in UI-independent classes inside the same add-on folder. Opening
a page must not decode an entire archive. Use providers, small asset references,
paged metadata, asynchronous loading, and explicit resource ownership.

## Validation

Build with `Build.ps1`, verify discovery through `s2kit addons`, inspect the schema
with `s2kit describe <addon>/<tool>`, and run a real small input through both the
CLI and desktop. Verify failures and cancellation as well as success. Local test
projects belong in ignored `v4/tests/` and must not be committed.
