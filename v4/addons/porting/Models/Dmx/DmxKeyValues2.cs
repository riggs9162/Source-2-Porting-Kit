using System.Collections;
using System.Globalization;
using System.Text;

namespace PortingKit.Addons.Porting;

/// <summary>Reads and writes the keyvalues2 text encoding.</summary>
/// <remarks>Quoted values carry no escapes: a backslash in a quoted value is a literal backslash, which is
/// how Source 2 resource paths arrive. References to elements that have not been read yet are resolved once
/// the whole file is known, and a reference with no matching element stays a placeholder.</remarks>
internal static class DmxKeyValues2
{
    private const string Header = "<!-- dmx encoding keyvalues2 {0} format {1} {2} -->\n";
    private const string PrefixType = "$prefix_element$";

    private sealed class Tokens(string text)
    {
        private int position;

        public int Line => text.AsSpan(0, Math.Min(position, text.Length)).Count('\n') + 1;

        /// <summary>Whether the last token was quoted. A bare type word is how a null value is written.</summary>
        public bool Quoted { get; private set; }

        private void Space()
        {
            while (position < text.Length)
            {
                if (char.IsWhiteSpace(text[position]) || text[position] == ',') position++;
                else if (string.CompareOrdinal(text, position, "<!--", 0, 4) == 0)
                {
                    var end = text.IndexOf("-->", position, StringComparison.Ordinal);
                    position = end < 0 ? text.Length : end + 3;
                }
                else return;
            }
        }

        public bool AtEnd
        {
            get
            {
                Space();
                return position >= text.Length;
            }
        }

        /// <summary>Whether the next token is a structural character, without consuming it.</summary>
        public bool Peek(char mark)
        {
            Space();
            return position < text.Length && text[position] == mark;
        }

        /// <summary>Returns the next quoted value, bare word or single bracket.</summary>
        public string Next()
        {
            Space();
            if (position >= text.Length) throw new EndOfStreamException("Unexpected EOF");
            var first = text[position];
            Quoted = first == '"';
            if (first == '"')
            {
                var end = text.IndexOf('"', position + 1);
                if (end < 0) throw new EndOfStreamException("Unterminated string");
                var value = text[(position + 1)..end];
                position = end + 1;
                return value;
            }
            if (first is '{' or '}' or '[' or ']')
            {
                position++;
                return first.ToString();
            }
            var start = position;
            while (position < text.Length && !char.IsWhiteSpace(text[position]) && text[position] is not ('"' or '{' or '}' or '[' or ']' or ',')) position++;
            return text[start..position];
        }

        public void Expect(string token)
        {
            var value = Next();
            if (value != token) throw new InvalidDataException($"Expected '{token}', found '{value}'.");
        }
    }

    private sealed class Reader(DmxDocument document, string text)
    {
        private readonly Tokens tokens = new(text);
        private readonly Dictionary<Guid, List<(DmxElement Owner, string Name, int Index)>> users = [];
        private readonly Dictionary<Guid, DmxElement> known = [];

        private DmxElement? Reference(DmxElement owner, string name, int index, string value)
        {
            if (value.Length == 0) return null;
            var id = Guid.Parse(value);
            if (known.TryGetValue(id, out var element)) return element;
            if (!users.TryGetValue(id, out var list)) users[id] = list = [];
            list.Add((owner, name, index));
            return null;
        }

        private static object? Scalar(DmxKind kind, string value)
        {
            double[] Numbers() => value.Split(' ', StringSplitOptions.RemoveEmptyEntries).Select(item => double.Parse(item, CultureInfo.InvariantCulture)).ToArray();
            return kind switch
            {
                DmxKind.String => value,
                DmxKind.Int => int.Parse(value, CultureInfo.InvariantCulture),
                DmxKind.UInt8 => byte.Parse(value, CultureInfo.InvariantCulture),
                DmxKind.UInt64 => value.StartsWith("0x", StringComparison.OrdinalIgnoreCase) ? ulong.Parse(value.AsSpan(2), NumberStyles.HexNumber, CultureInfo.InvariantCulture) : ulong.Parse(value, CultureInfo.InvariantCulture),
                DmxKind.Float => (float)double.Parse(value, CultureInfo.InvariantCulture),
                DmxKind.Bool => int.Parse(value, CultureInfo.InvariantCulture) != 0,
                DmxKind.Time => new DmxTime(double.Parse(value, CultureInfo.InvariantCulture)),
                DmxKind.Vector2 => Numbers() is [var x, var y] ? new V2(x, y) : throw new InvalidDataException("Expected 2 values"),
                DmxKind.Vector3 => Numbers() is [var x, var y, var z] ? new V3(x, y, z) : throw new InvalidDataException("Expected 3 values"),
                DmxKind.Vector4 => Numbers() is [var x, var y, var z, var w] ? new DmxVector4(x, y, z, w) : throw new InvalidDataException("Expected 4 values"),
                DmxKind.Quaternion => Numbers() is [var x, var y, var z, var w] ? new Q4(x, y, z, w) : throw new InvalidDataException("Expected 4 values"),
                DmxKind.Angle => Numbers() is [var x, var y, var z] ? new DmxAngle(x, y, z) : throw new InvalidDataException("Expected 3 values"),
                DmxKind.Color => Numbers() is [var r, var g, var b, var a] ? new DmxColor((byte)r, (byte)g, (byte)b, (byte)a) : throw new InvalidDataException("Expected 4 values"),
                DmxKind.Matrix => new M4(Numbers()),
                DmxKind.Binary => Convert.FromHexString(string.Concat(value.Where(c => !char.IsWhiteSpace(c)))),
                DmxKind.ObjectId => Guid.Parse(value),
                _ => throw new InvalidDataException($"Cannot read attributes of type {kind}")
            };
        }

        private void ElementArray(DmxElement owner, string name)
        {
            var items = new List<DmxElement?>();
            owner.Set(name, items);
            tokens.Expect("[");
            while (!tokens.Peek(']'))
            {
                var first = tokens.Next();
                if (tokens.Peek('{'))
                {
                    items.Add(ElementBody(first));
                    continue;
                }
                items.Add(null);
                items[^1] = Reference(owner, name, items.Count - 1, tokens.Next());
            }
            tokens.Next();
        }

        private void ValueArray(DmxElement owner, string name, DmxKind kind)
        {
            var items = DmxTypes.NewArray(kind);
            tokens.Expect("[");
            while (!tokens.Peek(']')) items.Add(Scalar(kind, tokens.Next()));
            tokens.Next();
            owner.Set(name, items);
        }

        private void Attribute(DmxElement owner, string name)
        {
            var type = tokens.Next();
            if (type == "element_array")
            {
                ElementArray(owner, name);
                return;
            }
            if (type.EndsWith("_array", StringComparison.Ordinal) && DmxTypes.TryKind(type[..^"_array".Length], out var arrayKind))
            {
                ValueArray(owner, name, arrayKind);
                return;
            }
            if (tokens.Peek('{'))
            {
                owner.Set(name, ElementBody(type));
                return;
            }
            var valued = tokens.Quoted;
            if (type == "element")
            {
                owner.Set(name, Reference(owner, name, -1, valued ? tokens.Next() : ""));
                return;
            }
            if (!DmxTypes.TryKind(type, out var kind)) throw new InvalidDataException($"Unknown attribute type {type}");
            owner.Set(name, valued ? Scalar(kind, tokens.Next()) : null);
        }

        private DmxElement? ElementBody(string type)
        {
            tokens.Expect("{");
            var prefix = type == PrefixType;
            DmxElement? element = prefix ? document.PrefixAttributes : null;
            string? pendingName = null;
            while (!tokens.Peek('}'))
            {
                var key = tokens.Next();
                if (key == "id")
                {
                    tokens.Expect("elementid");
                    var id = Guid.Parse(tokens.Next());
                    if (prefix) continue;
                    element = document.AddElement(pendingName, type, id);
                    known[id] = element;
                }
                else if (key == "name")
                {
                    tokens.Expect("string");
                    var name = tokens.Quoted ? tokens.Next() : null;
                    if (prefix) continue;
                    if (element is not null) element.Name = name;
                    else pendingName = name;
                }
                else if (element is null) throw new InvalidDataException($"Attribute {key} appears before the element's id.");
                else Attribute(element, key);
            }
            tokens.Next();
            return prefix ? null : element;
        }

        public void Read()
        {
            while (!tokens.AtEnd)
            {
                var line = tokens.Line;
                try
                {
                    ElementBody(tokens.Next());
                }
                catch (Exception error) when (error is not DmxParseException)
                {
                    throw new DmxParseException($"Parsing failed near line {line}", error);
                }
            }
            foreach (var (id, list) in users)
            {
                var element = known.GetValueOrDefault(id) ?? document.AddElement("Missing element", id: id, placeholder: true);
                foreach (var (owner, name, index) in list)
                {
                    if (index < 0) owner.Set(name, element);
                    else owner.Elements(name)[index] = element;
                }
            }
        }
    }

    public static void Read(DmxDocument document, string text, string source)
    {
        try
        {
            new Reader(document, text).Read();
        }
        catch (DmxParseException error)
        {
            throw new DmxParseException($"Parsing of {source} failed: {error.Message}", error.InnerException);
        }
    }

    private sealed class Writer(DmxDocument document, Dictionary<DmxElement, int> users)
    {
        private readonly StringBuilder output = new();
        private string indent = "";

        private void In() => indent += "\t";
        private void Out() => indent = indent[..^1];

        private static string Text(object? value) => value switch
        {
            null => "",
            bool flag => flag ? "1" : "0",
            float number => DmxTypes.FloatText(number),
            byte[] bytes => Convert.ToHexStringLower(bytes),
            DmxElement element => element.Id.ToString(),
            DmxTime time => DmxTypes.FloatText(time.Seconds),
            DmxColor color => $"{color.R} {color.G} {color.B} {color.A}",
            V2 two => $"{DmxTypes.FloatText(two.X)} {DmxTypes.FloatText(two.Y)}",
            V3 three => $"{DmxTypes.FloatText(three.X)} {DmxTypes.FloatText(three.Y)} {DmxTypes.FloatText(three.Z)}",
            DmxVector4 four => $"{DmxTypes.FloatText(four.X)} {DmxTypes.FloatText(four.Y)} {DmxTypes.FloatText(four.Z)} {DmxTypes.FloatText(four.W)}",
            DmxAngle angle => $"{DmxTypes.FloatText(angle.X)} {DmxTypes.FloatText(angle.Y)} {DmxTypes.FloatText(angle.Z)}",
            Q4 rotation => $"{DmxTypes.FloatText(rotation.X)} {DmxTypes.FloatText(rotation.Y)} {DmxTypes.FloatText(rotation.Z)} {DmxTypes.FloatText(rotation.W)}",
            M4 matrix => string.Join(' ', Enumerable.Range(0, 16).Select(index => DmxTypes.FloatText(matrix[index / 4, index % 4]))),
            IFormattable number => number.ToString(null, CultureInfo.InvariantCulture),
            _ => value.ToString() ?? ""
        };

        private void Line(string name, string type, string? value, bool isArray = false)
        {
            if (value is null) output.Append(indent).Append($"\"{name}\" {type}\n");
            else if (isArray) output.Append(indent).Append($"\"{name}\" \"{type}\" {value}\n");
            else output.Append(indent).Append($"\"{name}\" \"{type}\" \"{value}\"\n");
        }

        private void ElementArray(string name, List<DmxElement?> items)
        {
            if (items.Count == 0)
            {
                Line(name, "element_array", "[ ]", true);
                return;
            }
            output.Append(indent).Append($"\"{name}\" \"element_array\" \n{indent}[\n");
            In();
            for (var index = 0; index < items.Count; index++)
            {
                output.Append(indent);
                var item = items[index];
                if (item is not null && users.GetValueOrDefault(item) == 1) Element(item);
                else output.Append($"\"element\" \"{item?.Id.ToString() ?? ""}\"");
                output.Append(index + 1 < items.Count ? ",\n" : "\n");
            }
            Out();
            output.Append(indent).Append("]\n");
        }

        public void Element(DmxElement element)
        {
            output.Append($"\"{element.Type}\"\n{indent}{{\n");
            In();
            Line("id", "elementid", element.Id.ToString());
            Line("name", "string", element.Name);
            foreach (var name in element.Keys)
            {
                var value = element[name];
                var (kind, isArray) = DmxTypes.Classify(value);
                if (value is null) Line(name, "element", null);
                else if (value is DmxElement child && users.GetValueOrDefault(child) < 2)
                {
                    output.Append(indent).Append($"\"{name}\" ");
                    Element(child);
                    output.Append('\n');
                }
                else if (value is List<DmxElement?> items) ElementArray(name, items);
                else if (isArray)
                {
                    var list = ((IList)value).Cast<object?>().Select(item => $"\"{Text(item)}\"").ToList();
                    Line(name, DmxTypes.NameOf(kind) + "_array", list.Count == 0 ? "[ ]" : "[" + string.Join(", ", list) + "]", true);
                }
                else Line(name, DmxTypes.NameOf(kind), Text(value));
            }
            Out();
            output.Append(indent).Append('}');
        }

        public string Write(int version)
        {
            output.Append(string.Format(Header, version, document.Format, document.FormatVersion));
            var root = document.Root ?? throw new InvalidDataException("A DMX document needs a root element.");
            Element(root);
            output.Append("\n\n");
            foreach (var (element, count) in users)
            {
                if (count <= 1 || element == root) continue;
                Element(element);
                output.Append("\n\n");
            }
            return output.ToString();
        }
    }

    public static string Write(DmxDocument document, int version) => new Writer(document, document.CountUsers()).Write(version);
}
