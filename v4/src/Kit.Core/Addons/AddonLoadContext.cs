using System.Reflection;
using System.Runtime.Loader;

namespace PortingKit.Core;

/// <summary>Shares host contracts while resolving each add-on's private managed and native dependencies.</summary>
internal sealed class AddonLoadContext(string path) : AssemblyLoadContext(isCollectible: true)
{
    private readonly AssemblyDependencyResolver resolver = new(path);

    protected override Assembly? Load(AssemblyName name)
    {
        if (name.Name is "Kit.Contracts" or "Kit.DesktopSdk")
            return Default.LoadFromAssemblyName(name);
        var resolved = resolver.ResolveAssemblyToPath(name);
        return resolved is null ? null : LoadFromAssemblyPath(resolved);
    }

    protected override nint LoadUnmanagedDll(string name)
    {
        var resolved = resolver.ResolveUnmanagedDllToPath(name);
        return resolved is null ? 0 : LoadUnmanagedDllFromPath(resolved);
    }
}
