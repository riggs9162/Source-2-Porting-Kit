namespace PortingKit.Addons.Porting;

/// <summary>A DMX element: an ordered set of named attributes with a name, a type and an identifier.</summary>
/// <remarks>
/// Attribute values use these types. Single values are <see cref="DmxElement"/> (or null), int, float,
/// bool, string (or null), byte[] for binary, <see cref="DmxTime"/>, <see cref="DmxColor"/>, <see cref="V2"/>,
/// <see cref="V3"/>, <see cref="DmxVector4"/>, <see cref="DmxAngle"/>, <see cref="Q4"/>, <see cref="M4"/>,
/// ulong, byte and Guid. An array attribute is a List of the same type.
/// </remarks>
public sealed class DmxElement
{
    private readonly List<string> order = [];
    private readonly Dictionary<string, object?> attributes = new(StringComparer.Ordinal);

    internal DmxElement(string? name, string type, Guid id, bool placeholder)
    {
        Name = name;
        Type = type;
        Id = id;
        IsPlaceholder = placeholder;
    }

    /// <summary>The element's name, where an empty name reads as none.</summary>
    public string? Name
    {
        get;
        set => field = string.IsNullOrEmpty(value) ? null : value;
    }

    public string Type { get; set; }
    public Guid Id { get; }

    /// <summary>A reference to an element stored elsewhere, which has no attributes of its own here.</summary>
    public bool IsPlaceholder { get; }

    public IReadOnlyList<string> Keys => order;
    public int Count => order.Count;

    /// <summary>Reads or replaces an attribute; reading one that is absent is a data fault and throws.</summary>
    public object? this[string name]
    {
        get => attributes.TryGetValue(name, out var value) ? value : throw new DmxAttributeException($"No attribute \"{name}\" on {this}");
        set => Set(name, value);
    }

    public bool Contains(string name) => attributes.ContainsKey(name);

    /// <summary>Reads an attribute, or the fallback when it is absent.</summary>
    public object? Get(string name, object? fallback = null) => attributes.TryGetValue(name, out var value) ? value : fallback;

    /// <summary>Stores an attribute, keeping its original position when it already exists.</summary>
    public void Set(string name, object? value)
    {
        if (name is "name" or "id") throw new ArgumentException($"\"{name}\" is a reserved name");
        DmxTypes.Classify(value);
        if (!attributes.ContainsKey(name)) order.Add(name);
        attributes[name] = value;
    }

    /// <summary>Reads a single attribute as its exact stored type.</summary>
    public T Value<T>(string name)
    {
        var value = this[name];
        return value is T typed ? typed : throw new InvalidDataException($"Attribute \"{name}\" on {this} is not {typeof(T).Name}.");
    }

    /// <summary>Reads an element attribute that may be null.</summary>
    public DmxElement? Element(string name) => this[name] switch
    {
        null => null,
        DmxElement element => element,
        _ => throw new InvalidDataException($"Attribute \"{name}\" on {this} is not an element.")
    };

    /// <summary>Reads a string attribute that may be null.</summary>
    public string? Text(string name) => this[name] switch
    {
        null => null,
        string text => text,
        _ => throw new InvalidDataException($"Attribute \"{name}\" on {this} is not text.")
    };

    /// <summary>Reads an array attribute with its element type.</summary>
    public List<T> Array<T>(string name)
    {
        var value = this[name];
        return value as List<T> ?? throw new InvalidDataException($"Attribute \"{name}\" on {this} is not a {typeof(T).Name} array.");
    }

    /// <summary>Reads an element array attribute.</summary>
    public List<DmxElement?> Elements(string name) => Array<DmxElement?>(name);

    /// <summary>Reads a numeric array of any stored width as doubles, for streams whose width varies by exporter.</summary>
    public double[] Numbers(string name) => this[name] switch
    {
        List<float> values => values.Select(value => (double)value).ToArray(),
        List<int> values => values.Select(value => (double)value).ToArray(),
        List<byte> values => values.Select(value => (double)value).ToArray(),
        _ => throw new InvalidDataException($"Attribute \"{name}\" on {this} is not a numeric array.")
    };

    public override string ToString() => $"<Datamodel element \"{Name}\" ({Type})>";
}
