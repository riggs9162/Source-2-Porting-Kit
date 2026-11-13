using PortingKit.Desktop;

namespace PortingKit.Addons.Soundscapes;

/// <summary>Registers the soundscape porter: its desktop page and the two operations behind it.</summary>
public sealed class SoundscapesAddon : IAddon
{
    public void Initialize(IAddonContext context)
    {
        context.Register<ITool>("list", new SoundscapeListTool());
        context.Register<ITool>("port", new SoundscapePortTool());
        context.Register<IDesktopPage>("porter", new SoundscapePage(context));
    }

    public ValueTask DisposeAsync() => ValueTask.CompletedTask;
}
