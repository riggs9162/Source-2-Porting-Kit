namespace PortingKit.Addons.Porting;

/// <summary>Registers the Source 2 model and material porting tools.</summary>
public sealed class PortingAddon : IAddon
{
    public void Initialize(IAddonContext context)
    {
        context.Register<ITool>("materials", new MaterialPortTool());
    }

    public ValueTask DisposeAsync() => ValueTask.CompletedTask;
}
