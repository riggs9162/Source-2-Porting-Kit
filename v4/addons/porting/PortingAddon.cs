namespace PortingKit.Addons.Porting;

/// <summary>Registers the Source 2 model and material porting tools.</summary>
public sealed class PortingAddon : IAddon
{
    public void Initialize(IAddonContext context)
    {
        context.Register<ITool>("materials", new MaterialPortTool());
        context.Register<ITool>("models", new ModelPortTool());
    }

    public ValueTask DisposeAsync() => ValueTask.CompletedTask;
}
