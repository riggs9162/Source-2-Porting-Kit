namespace PortingKit.Addons.Utilities;

public sealed class UtilitiesAddon : IAddon
{
    public void Initialize(IAddonContext context)
    {
        context.Register<ITool>("environment", new EnvironmentTool());
        context.Register<ITool>("inventory", new InventoryTool());
    }

    public ValueTask DisposeAsync() => ValueTask.CompletedTask;
}
