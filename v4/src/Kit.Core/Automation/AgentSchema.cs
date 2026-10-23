using System.Text.Json;

namespace PortingKit.Core.Automation;

/// <summary>Builds and enforces the same strict input contracts for IPC, CLI, and MCP.</summary>
public static class AgentSchema
{
    public sealed record Field(string Name, string Type, string Description = "", bool Required = false,
        long? Minimum = null, long? Maximum = null);

    public static JsonElement Object(params Field[] fields) => AgentProtocol.Element(new
    {
        type = "object",
        properties = fields.ToDictionary(field => field.Name, field =>
        {
            var value = new Dictionary<string, object> { ["description"] = field.Description };
            if (field.Type != "any") value["type"] = field.Type;
            if (field.Minimum is { } min) value["minimum"] = min;
            if (field.Maximum is { } max) value["maximum"] = max;
            if (field.Type == "string") value["maxLength"] = 32768;
            return value;
        }),
        required = fields.Where(field => field.Required).Select(field => field.Name).ToArray(),
        additionalProperties = false
    });

    public static void Validate(JsonElement schema, JsonElement input)
    {
        if (input.ValueKind != JsonValueKind.Object)
            throw new ArgumentException("Arguments must be a JSON object.");
        var properties = schema.GetProperty("properties");
        var seen = new HashSet<string>(StringComparer.Ordinal);
        foreach (var property in input.EnumerateObject())
        {
            if (!seen.Add(property.Name))
                throw new ArgumentException($"Duplicate argument: {property.Name}");
            if (!properties.TryGetProperty(property.Name, out var definition))
                throw new ArgumentException($"Unknown argument: {property.Name}");
            if (definition.TryGetProperty("type", out var type))
            {
                var valid = type.GetString() switch
                {
                    "string" => property.Value.ValueKind == JsonValueKind.String,
                    "integer" => property.Value.ValueKind == JsonValueKind.Number && property.Value.TryGetInt64(out _),
                    "boolean" => property.Value.ValueKind is JsonValueKind.True or JsonValueKind.False,
                    "object" => property.Value.ValueKind == JsonValueKind.Object,
                    _ => false
                };
                if (!valid) throw new ArgumentException($"{property.Name} must be {type.GetString()}.");
            }
            if (property.Value.ValueKind == JsonValueKind.String && property.Value.GetString()!.Length > 32768)
                throw new ArgumentException($"{property.Name} is too long.");
            if (definition.TryGetProperty("enum", out var choices) && !choices.EnumerateArray().Any(choice => choice.GetString() == property.Value.GetString()))
                throw new ArgumentException($"Invalid choice for {property.Name}.");
            if (definition.TryGetProperty("minimum", out var min) && property.Value.GetInt64() < min.GetInt64() ||
                definition.TryGetProperty("maximum", out var max) && property.Value.GetInt64() > max.GetInt64())
                throw new ArgumentException($"{property.Name} is outside its permitted range.");
        }
        foreach (var required in schema.GetProperty("required").EnumerateArray())
            if (!input.TryGetProperty(required.GetString()!, out _))
                throw new ArgumentException($"{required.GetString()} is required.");
    }

    public static Dictionary<string, string> ToolValues(JsonElement input)
    {
        if (input.ValueKind != JsonValueKind.Object)
            throw new ArgumentException("Tool arguments must be an object.");
        var result = new Dictionary<string, string>(StringComparer.Ordinal);
        foreach (var field in input.EnumerateObject())
        {
            var text = field.Value.ValueKind switch
            {
                JsonValueKind.String => field.Value.GetString()!,
                JsonValueKind.Number or JsonValueKind.True or JsonValueKind.False => field.Value.GetRawText(),
                _ => throw new ArgumentException($"{field.Name} must be a string, number, or boolean.")
            };
            if (!result.TryAdd(field.Name, text)) throw new ArgumentException($"Duplicate parameter: {field.Name}");
        }
        return result;
    }
}
