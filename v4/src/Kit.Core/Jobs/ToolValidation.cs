using System.Collections.ObjectModel;
using System.Globalization;

namespace PortingKit.Core;

public static class ToolValidation
{
    public static IReadOnlyDictionary<string, string> Normalize(ToolDefinition definition,
        IReadOnlyDictionary<string, string> supplied)
    {
        var known = definition.Parameters.Select(parameter => parameter.Key).ToHashSet(StringComparer.Ordinal);
        var unknown = supplied.Keys.FirstOrDefault(key => !known.Contains(key));
        if (unknown is not null)
            throw new ArgumentException($"Unknown parameter: {unknown}");
        var values = new Dictionary<string, string>(StringComparer.Ordinal);
        foreach (var parameter in definition.Parameters)
        {
            var value = supplied.TryGetValue(parameter.Key, out var input) ? input.Trim() : parameter.Default;
            if (value.Length > 32768)
                throw new ArgumentException($"{parameter.Label} is too long.");
            if (parameter.Required && string.IsNullOrWhiteSpace(value))
                throw new ArgumentException($"{parameter.Label} is required.");
            if (parameter.Kind == ParameterKind.Integer &&
                (!int.TryParse(value, NumberStyles.Integer, CultureInfo.InvariantCulture, out var number) ||
                number < parameter.Minimum || number > parameter.Maximum))
                throw new ArgumentException($"{parameter.Label} must be between {parameter.Minimum} and {parameter.Maximum}.");
            if (parameter.Kind == ParameterKind.Boolean && !bool.TryParse(value, out _))
                throw new ArgumentException($"{parameter.Label} must be true or false.");
            if (parameter.Kind == ParameterKind.Choice && !(parameter.Choices ?? []).Contains(value, StringComparer.Ordinal))
                throw new ArgumentException($"Invalid choice for {parameter.Label}.");
            values.Add(parameter.Key, value);
        }
        return new ReadOnlyDictionary<string, string>(values);
    }
}
