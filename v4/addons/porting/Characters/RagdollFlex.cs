using System.Buffers.Binary;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace PortingKit.Addons.Porting;

/// <summary>What a flex source's indexed geometry reduced to.</summary>
public sealed record FlexGeometry(int SourceCorners, int Vertices, bool Indexed);

/// <summary>Writes native Source 1 DMX flex rigs on authored bodygroup submodels.</summary>
public static partial class RagdollFlex
{
    private sealed class ReferenceBone(string name, int parent)
    {
        public string Name { get; } = name;
        public int Parent { get; } = parent;
        public V3? Position { get; set; }
        public V3 Rotation { get; set; }
    }

    private readonly record struct ReferenceCorner(V3 Position, V3 Normal, V2 Uv, (int Joint, double Weight)[] Links);

    [GeneratedRegex(@"\A\s*(\d+)\s+""([^""]+)""\s+(-?\d+)\s*\z")]
    private static partial Regex NodeLine();

    private static readonly Encoding StrictUtf8 = new UTF8Encoding(false, true);

    /// <summary>Reads one checked NUL-terminated UTF-8 studio-model string.</summary>
    private static string MdlString(ReadOnlySpan<byte> data, int start)
    {
        if (start < 0 || start >= data.Length) throw new InvalidDataException("A compiled flex table references a string outside the model");
        var length = data[start..].IndexOf((byte)0);
        if (length < 0) throw new InvalidDataException("A compiled flex string has no terminator");
        return StrictUtf8.GetString(data.Slice(start, length));
    }

    /// <summary>Translates the supported source stack program to Source 1 studio opcodes.</summary>
    /// <remarks>Source 1 has no negate or absolute opcode, so they become a subtraction from zero and a maximum with it.</remarks>
    private static List<(int Opcode, uint Operand)> CompiledOperations(JsonArray operations, IReadOnlyDictionary<string, int> controllers)
    {
        var opcodes = new Dictionary<string, int> { ["CONST"] = 1, ["FETCH1"] = 2, ["ADD"] = 4, ["SUB"] = 5, ["MUL"] = 6, ["DIV"] = 7, ["MAX"] = 13, ["MIN"] = 14 };
        var stack = new List<List<(int, uint)>>();
        foreach (var operation in operations)
        {
            var opcode = operation.Need("opcode").Str();
            var operand = unchecked((uint)operation.Need("operand").Lng());
            if (opcode == "CONST") stack.Add([(1, operand)]);
            else if (opcode == "FETCH1")
            {
                var controller = operation.Need("controller").Str();
                if (!controllers.TryGetValue(controller, out var index)) throw new KeyNotFoundException($"'{controller}'");
                stack.Add([(2, (uint)index)]);
            }
            else if (opcode is "NEG" or "ABS")
            {
                if (stack.Count == 0) throw new InvalidDataException("A compiled source flex rule has an invalid stack");
                var value = stack[^1];
                stack.RemoveAt(stack.Count - 1);
                List<(int, uint)> negative = [(1, 0), .. value, (5, 0)];
                stack.Add(opcode == "NEG" ? negative : [.. value, .. negative, (13, 0)]);
            }
            else if (opcodes.TryGetValue(opcode, out var code))
            {
                if (stack.Count < 2) throw new InvalidDataException("A compiled source flex rule has an invalid stack");
                var (second, first) = (stack[^1], stack[^2]);
                stack.RemoveRange(stack.Count - 2, 2);
                stack.Add([.. first, .. second, (code, 0)]);
            }
            else throw new InvalidDataException($"Unsupported source flex opcode {opcode}");
        }
        if (stack.Count != 1) throw new InvalidDataException("A compiled source flex rule does not produce one value");
        return stack[0];
    }

    private static void Extend(List<byte> data, int count) => data.AddRange(new byte[count]);
    private static void Align(List<byte> data) => Extend(data, -data.Count & 3);
    private static void PutInt(List<byte> data, int offset, int value) => BinaryPrimitives.WriteInt32LittleEndian(CollectionsMarshal.AsSpan(data)[offset..], value);
    private static void PutFloat(List<byte> data, int offset, float value) => BinaryPrimitives.WriteSingleLittleEndian(CollectionsMarshal.AsSpan(data)[offset..], value);

    /// <summary>Restores authored controllers, ranges, postfix rules and UI in a compiled MDL.</summary>
    /// <remarks>Valve's Source 1 DMX importer understands morph geometry and combination controls but lacks Source 2
    /// DmeFlexRules, so it creates a temporary direct rig. This replaces only that rig's controller, rule and UI tables;
    /// morph description indices, geometry offsets and companion checksums remain unchanged. Tables are appended and all
    /// pointers remain relative to their owning structure, as defined by Valve's public studio.h format.</remarks>
    public static JsonObject InstallSourceFlexRig(string modelPath, JsonObject rig)
    {
        var controllers = rig.Get("controllers").Arr();
        var rules = rig.Get("rules").Obj();
        if (controllers.Count == 0 && rules.Count == 0) return new() { ["installed"] = false, ["controllers"] = 0, ["rules"] = 0 };
        if (controllers.Count > 96) throw new InvalidDataException("Source flex rig exceeds Source 1's 96-controller limit");
        var original = File.ReadAllBytes(modelPath);
        if (original.Length < 408 || !original.AsSpan(0, 4).SequenceEqual("IDST"u8) || BinaryPrimitives.ReadInt32LittleEndian(original.AsSpan(4)) is not (48 or 49))
            throw new InvalidDataException("Source flex installation requires a Source 1 MDL version 48 or 49");
        if (BinaryPrimitives.ReadInt32LittleEndian(original.AsSpan(76)) != original.Length) throw new InvalidDataException("Compiled MDL length does not match its header");
        int descriptorCount = BinaryPrimitives.ReadInt32LittleEndian(original.AsSpan(260)), descriptorOffset = BinaryPrimitives.ReadInt32LittleEndian(original.AsSpan(264));
        if (descriptorCount < 0 || descriptorOffset < 0 || descriptorOffset + (long)descriptorCount * 4 > original.Length) throw new InvalidDataException("Compiled morph-description table is outside the model");
        var descriptors = new Dictionary<string, int>(StringComparer.Ordinal);
        for (var index = 0; index < descriptorCount; index++)
        {
            var at = descriptorOffset + index * 4;
            var name = MdlString(original, at + BinaryPrimitives.ReadInt32LittleEndian(original.AsSpan(at)));
            if (!descriptors.TryAdd(name, index)) throw new InvalidDataException($"Compiled model has duplicate morph description {name}");
        }
        var missing = rules.Select(pair => pair.Key).Where(name => !descriptors.ContainsKey(name)).Order(StringComparer.Ordinal).ToList();
        if (missing.Count > 0) throw new InvalidDataException("Compiled model omitted authored morphs: " + string.Join(", ", missing));
        var names = controllers.Select(controller => controller.Need("m_szName").Str()).ToList();
        if (names.Distinct(StringComparer.Ordinal).Count() != names.Count) throw new InvalidDataException("Authored flex-controller names must be unique");
        var indices = new Dictionary<string, int>(StringComparer.Ordinal);
        for (var index = 0; index < names.Count; index++) indices[names[index]] = index;
        var programs = rules.Select(pair => (Descriptor: descriptors[pair.Key], Operations: CompiledOperations(pair.Value.Arr(), indices))).ToList();
        var data = new List<byte>(original);
        Align(data);
        var controllerOffset = data.Count;
        Extend(data, 20 * controllers.Count);
        var ruleOffset = data.Count;
        Extend(data, 12 * programs.Count);
        var uiOffset = data.Count;
        Extend(data, 20 * controllers.Count);
        var strings = new Dictionary<string, int>(StringComparer.Ordinal);
        int AddString(string value)
        {
            if (strings.TryGetValue(value, out var known)) return known;
            strings[value] = data.Count;
            data.AddRange(Encoding.UTF8.GetBytes(value));
            data.Add(0);
            return strings[value];
        }
        for (var index = 0; index < controllers.Count; index++)
        {
            var controller = controllers[index];
            var record = controllerOffset + index * 20;
            var name = controller.Need("m_szName").Str();
            var typeOffset = AddString(controller.Has("m_szType") ? controller.Get("m_szType").Str() : "default");
            var nameOffset = AddString(name);
            double low = controller.Has("min") ? controller.Get("min").Dbl() : 0.0, high = controller.Has("max") ? controller.Get("max").Dbl() : 1.0;
            if (!double.IsFinite(low) || !double.IsFinite(high) || low > high) throw new InvalidDataException($"Invalid source flex-controller range for {name}");
            PutInt(data, record, typeOffset - record);
            PutInt(data, record + 4, nameOffset - record);
            PutInt(data, record + 8, -1);
            PutFloat(data, record + 12, (float)low);
            PutFloat(data, record + 16, (float)high);
            var interfaceRecord = uiOffset + index * 20;
            PutInt(data, interfaceRecord, nameOffset - interfaceRecord);
            PutInt(data, interfaceRecord + 4, record - interfaceRecord);
        }
        Align(data);
        for (var index = 0; index < programs.Count; index++)
        {
            var (descriptor, operations) = programs[index];
            var record = ruleOffset + index * 12;
            PutInt(data, record, descriptor);
            PutInt(data, record + 4, operations.Count);
            PutInt(data, record + 8, data.Count - record);
            foreach (var (opcode, operand) in operations)
            {
                var at = data.Count;
                Extend(data, 8);
                PutInt(data, at, opcode);
                PutInt(data, at + 4, unchecked((int)operand));
            }
        }
        PutInt(data, 268, controllers.Count);
        PutInt(data, 272, controllerOffset);
        PutInt(data, 276, programs.Count);
        PutInt(data, 280, ruleOffset);
        PutInt(data, 384, controllers.Count);
        PutInt(data, 388, uiOffset);
        PutInt(data, 76, data.Count);
        var temporary = modelPath + ".flex.tmp";
        File.WriteAllBytes(temporary, data.ToArray());
        File.Move(temporary, modelPath, true);
        return new()
        {
            ["installed"] = true, ["controllers"] = controllers.Count, ["rules"] = programs.Count, ["morph_descriptions"] = descriptorCount,
            ["controller_names"] = JsonKeys.Strings(names), ["preserved_checksum"] = BinaryPrimitives.ReadInt32LittleEndian(original.AsSpan(8))
        };
    }

    private static string[] Fields(string line) => line.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries);
    private static double Number(string value) => double.Parse(value, NumberStyles.Float, CultureInfo.InvariantCulture);

    /// <summary>The text between a marker and the next line that starts with <c>end</c>.</summary>
    private static string Block(string text, string marker)
    {
        var at = text.IndexOf(marker, StringComparison.Ordinal);
        if (at < 0) throw new InvalidDataException($"Reference SMD has no {marker.TrimEnd()} block");
        var start = at + marker.Length;
        var end = text.IndexOf("\nend", start, StringComparison.Ordinal);
        return end < 0 ? text[start..] : text[start..end];
    }

    /// <summary>Reads a generated reference SMD without welding or changing corner order.</summary>
    private static (List<ReferenceBone> Bones, List<ReferenceCorner> Corners, List<string> Materials) ReadReference(string path)
    {
        var text = PythonText.Read(path);
        var bones = new List<ReferenceBone>();
        foreach (var line in PythonText.SplitLines(Block(text, "nodes\n")))
        {
            var match = NodeLine().Match(line);
            if (!match.Success || int.Parse(match.Groups[1].ValueSpan, CultureInfo.InvariantCulture) != bones.Count) throw new InvalidDataException("Reference SMD requires ordered bone indices");
            bones.Add(new(match.Groups[2].Value, int.Parse(match.Groups[3].ValueSpan, CultureInfo.InvariantCulture)));
        }
        foreach (var line in PythonText.SplitLines(Block(text, "skeleton\n")))
        {
            if (line.StartsWith("time ", StringComparison.Ordinal) || line.Trim().Length == 0) continue;
            var values = Fields(line);
            var bone = bones[int.Parse(values[0], CultureInfo.InvariantCulture)];
            bone.Position = new V3(Number(values[1]), Number(values[2]), Number(values[3]));
            bone.Rotation = new(Number(values[4]), Number(values[5]), Number(values[6]));
        }
        var body = PythonText.SplitLines(Block(text, "triangles\n"));
        var corners = new List<ReferenceCorner>(body.Count / 4 * 3);
        var materials = new List<string>(body.Count / 4);
        for (var offset = 0; offset < body.Count; offset += 4)
        {
            if (offset + 4 > body.Count) throw new InvalidDataException("Truncated reference SMD triangle");
            materials.Add(body[offset]);
            for (var line = offset + 1; line < offset + 4; line++)
            {
                var values = Fields(body[line]);
                var count = values.Length > 9 ? int.Parse(values[9], CultureInfo.InvariantCulture) : 0;
                var links = new (int, double)[count > 0 ? count : 1];
                if (count == 0) links[0] = (int.Parse(values[0], CultureInfo.InvariantCulture), 1.0);
                for (var index = 0; index < count; index++) links[index] = (int.Parse(values[10 + index * 2], CultureInfo.InvariantCulture), Number(values[11 + index * 2]));
                corners.Add(new(new(Number(values[1]), Number(values[2]), Number(values[3])), new(Number(values[4]), Number(values[5]), Number(values[6])),
                    new(Number(values[7]), Number(values[8])), links));
            }
        }
        return (bones, corners, materials);
    }

    /// <summary>Converts SMD XYZ radians into DMX XYZW orientation.</summary>
    private static Q4 Quaternion(V3 rotation)
    {
        double x = rotation.X * 0.5, y = rotation.Y * 0.5, z = rotation.Z * 0.5;
        double sx = Math.Sin(x), sy = Math.Sin(y), sz = Math.Sin(z), cx = Math.Cos(x), cy = Math.Cos(y), cz = Math.Cos(z);
        return new(sx * cy * cz - cx * sy * sz, cx * sy * cz + sx * cy * sz, cx * cy * sz - sx * sy * cz, cx * cy * cz + sx * sy * sz);
    }

    /// <summary>Creates a DMX local transform with explicit identity defaults.</summary>
    private static DmxElement Transform(DmxDocument document, string name, V3? position = null, Q4? orientation = null)
    {
        var element = document.AddElement(name, "DmeTransform");
        element["position"] = position ?? V3.Zero;
        element["orientation"] = orientation ?? Q4.Identity;
        return element;
    }

    /// <summary>Writes Source 1 joint lists and indexed weights from the current SMD bind.</summary>
    private static void SetSkeleton(DmxDocument document, DmxElement model, DmxElement meshDag, List<ReferenceBone> bones, List<ReferenceCorner> corners, DmxElement vertexData)
    {
        var elements = new List<DmxElement?>();
        var transforms = new List<DmxElement?>();
        foreach (var bone in bones)
        {
            var element = document.AddElement(bone.Name, "DmeJoint");
            var position = bone.Position ?? throw new KeyNotFoundException("'position'");
            var quaternion = Quaternion(bone.Rotation);
            element["transform"] = Transform(document, bone.Name, position, quaternion);
            element["children"] = new List<DmxElement?>();
            elements.Add(element);
            transforms.Add(Transform(document, bone.Name + "_bind", position, quaternion));
        }
        var roots = new List<DmxElement?>();
        for (var index = 0; index < bones.Count; index++)
        {
            if (bones[index].Parent < 0) roots.Add(elements[index]);
            else elements[bones[index].Parent]!.Elements("children").Add(elements[index]);
        }
        roots.Add(meshDag);
        transforms.Add(Transform(document, "mesh_bind"));
        model["children"] = roots;
        model["jointList"] = new List<DmxElement?>([.. elements, meshDag]);
        model["jointTransforms"] = new List<DmxElement?>([.. elements.Select(element => element!.Element("transform")), meshDag.Element("transform")]);
        var bind = document.AddElement("bind", "DmeTransformList");
        bind["transforms"] = transforms;
        model["baseStates"] = new List<DmxElement?> { bind };
        var sourceIndices = vertexData.Contains("s2kitSourceCornerIndices") ? vertexData.Array<int>("s2kitSourceCornerIndices") : Enumerable.Range(0, corners.Count).ToList();
        if (sourceIndices.Exists(index => index < 0 || index >= corners.Count)) throw new InvalidDataException("Indexed flex mesh references an absent reference-SMD corner");
        var limited = sourceIndices.ConvertAll(index => RagdollAppearance.Source1SkinLinks(corners[index].Links));
        var count = limited.Max(links => links.Count);
        var weights = new List<float>(limited.Count * count);
        var indices = new List<int>(limited.Count * count);
        foreach (var links in limited)
            for (var slot = 0; slot < count; slot++)
            {
                var (joint, weight) = slot < links.Count ? links[slot] : (0, 0.0);
                indices.Add(joint);
                weights.Add((float)weight);
            }
        vertexData["jointCount"] = count;
        vertexData["jointWeights"] = weights;
        vertexData["jointIndices"] = indices;
    }

    /// <summary>Writes only nonzero delta entries while preserving original corner indices.</summary>
    private static void DeltaStream(DmxElement target, string name, IReadOnlyList<V3> values)
    {
        var indices = new List<int>();
        var kept = new List<V3>();
        for (var index = 0; index < values.Count; index++)
        {
            var value = values[index];
            if (Math.Abs(value.X) <= 1e-12 && Math.Abs(value.Y) <= 1e-12 && Math.Abs(value.Z) <= 1e-12) continue;
            indices.Add(index);
            kept.Add(value);
        }
        target[name] = kept;
        target[name + "Indices"] = indices;
    }

    private static V3 Single(V3 value) => new((float)value.X, (float)value.Y, (float)value.Z);
    private static V2 Single(V2 value) => new((float)value.X, (float)value.Y);

    /// <summary>Numbers each corner by which class of identical corners it belongs to, refining one field at a time.</summary>
    /// <remarks>numpy compares the raw single-precision bytes of every field, so a negative zero differs from a zero.
    /// Refining a class number keeps memory proportional to the morphs rather than to corners times targets.</remarks>
    private sealed class CornerClasses(int count)
    {
        public int[] Numbers { get; private set; } = new int[count];
        public int Next { get; private set; } = 1;

        public void Refine<T>(Func<int, T> field) where T : notnull
        {
            var known = new Dictionary<(int, T), int>();
            for (var corner = 0; corner < Numbers.Length; corner++)
            {
                var key = (Numbers[corner], field(corner));
                if (!known.TryGetValue(key, out var number)) known[key] = number = known.Count;
                Numbers[corner] = number;
            }
            Next = known.Count;
        }

        public void RefineSparse(IReadOnlyList<int> corners, Func<int, (int, int, int)> field)
        {
            var known = new Dictionary<(int, (int, int, int)), int>();
            for (var index = 0; index < corners.Count; index++)
            {
                var key = (Numbers[corners[index]], field(index));
                if (!known.TryGetValue(key, out var number)) known[key] = number = Next++;
                Numbers[corners[index]] = number;
            }
        }
    }

    private static (int, int, int) Bits(V3 value) =>
        (BitConverter.SingleToInt32Bits((float)value.X), BitConverter.SingleToInt32Bits((float)value.Y), BitConverter.SingleToInt32Bits((float)value.Z));

    /// <summary>Welds only vertices whose geometry, skin links and every morph channel are identical.</summary>
    private static FlexGeometry CompactGeometry(DmxElement mesh, DmxElement vertexData)
    {
        if (vertexData.Contains("s2kitSourceCornerIndices"))
            return new(vertexData.Value<int>("s2kitSourceCornerCount"), vertexData.Array<V3>("positions").Count, true);
        var count = vertexData.Array<V3>("positions").Count;
        foreach (var name in new[] { "positions", "normals", "textureCoordinates" })
            if (!vertexData.Array<int>(name + "Indices").SequenceEqual(Enumerable.Range(0, count))) throw new InvalidDataException("Flex geometry compaction requires generated reference-corner streams");
        var positions = vertexData.Array<V3>("positions").ConvertAll(Single);
        var normals = vertexData.Array<V3>("normals").ConvertAll(Single);
        var coordinates = vertexData.Array<V2>("textureCoordinates").ConvertAll(Single);
        var jointCount = vertexData.Value<int>("jointCount");
        var joints = vertexData.Array<int>("jointIndices");
        var weights = vertexData.Array<float>("jointWeights");
        var classes = new CornerClasses(count);
        classes.Refine(corner => Bits(positions[corner]));
        classes.Refine(corner => Bits(normals[corner]));
        classes.Refine(corner => (BitConverter.SingleToInt32Bits((float)coordinates[corner].X), BitConverter.SingleToInt32Bits((float)coordinates[corner].Y)));
        classes.Refine(corner =>
        {
            var key = new StringBuilder();
            for (var slot = 0; slot < jointCount; slot++)
                key.Append(joints[corner * jointCount + slot]).Append(':').Append(BitConverter.SingleToInt32Bits(weights[corner * jointCount + slot])).Append(',');
            return key.ToString();
        });
        var deltaStreams = new List<(DmxElement Delta, List<(string Channel, List<int> Indices, List<V3> Values)> Channels)>();
        foreach (var delta in mesh.Elements("deltaStates"))
        {
            var channels = new List<(string, List<int>, List<V3>)>();
            foreach (var channel in new[] { "positions", "normals" })
            {
                var indices = delta!.Array<int>(channel + "Indices");
                var values = delta.Array<V3>(channel).ConvertAll(Single);
                if (indices.Count != values.Count || indices.Exists(index => index < 0 || index >= count)) throw new InvalidDataException("Flex delta references an absent source corner");
                if (indices.Count == 0) continue;
                channels.Add((channel, indices, values));
                classes.RefineSparse(indices, index => Bits(values[index]));
            }
            deltaStreams.Add((delta!, channels));
        }
        var numbers = classes.Numbers;
        var firstOf = new Dictionary<int, int>();
        var first = new List<int>();
        var inverse = new int[count];
        for (var corner = 0; corner < count; corner++)
        {
            if (!firstOf.TryGetValue(numbers[corner], out var welded))
            {
                firstOf[numbers[corner]] = welded = first.Count;
                first.Add(corner);
            }
            inverse[corner] = welded;
        }
        var identity = Enumerable.Range(0, first.Count).ToList();
        vertexData["positions"] = first.ConvertAll(corner => positions[corner]);
        vertexData["positionsIndices"] = identity;
        vertexData["normals"] = first.ConvertAll(corner => normals[corner]);
        vertexData["normalsIndices"] = new List<int>(identity);
        vertexData["textureCoordinates"] = first.ConvertAll(corner => coordinates[corner]);
        vertexData["textureCoordinatesIndices"] = new List<int>(identity);
        var keptWeights = new List<float>(first.Count * jointCount);
        var keptJoints = new List<int>(first.Count * jointCount);
        foreach (var corner in first)
            for (var slot = 0; slot < jointCount; slot++)
            {
                keptWeights.Add(weights[corner * jointCount + slot]);
                keptJoints.Add(joints[corner * jointCount + slot]);
            }
        vertexData["jointWeights"] = keptWeights;
        vertexData["jointIndices"] = keptJoints;
        foreach (var faceSet in mesh.Elements("faceSets"))
            faceSet!["faces"] = faceSet.Array<int>("faces").ConvertAll(index => index >= 0 ? inverse[index] : index);
        var scratch = new V3[count];
        foreach (var (delta, channels) in deltaStreams)
            foreach (var (channel, indices, values) in channels)
            {
                for (var index = 0; index < indices.Count; index++) scratch[indices[index]] = values[index];
                DeltaStream(delta, channel, first.ConvertAll(corner => scratch[corner]));
                foreach (var corner in indices) scratch[corner] = V3.Zero;
            }
        vertexData["s2kitSourceCornerIndices"] = first;
        vertexData["s2kitSourceCornerCount"] = count;
        return new(count, first.Count, true);
    }

    /// <summary>Writes shared morph descriptions without Source 2-only DMX expression elements.</summary>
    /// <remarks>Only the first source defines temporary direct controllers. Every subsequent bodygroup and LOD references
    /// the same morph descriptions through an operator with no controller definitions. Install the authored rig after
    /// compiling. Corner streams come back out of the reference SMD's text, rounding included, so a weld matches the file
    /// the compiler reads, and the weights stay in step with whatever physics later remaps there.</remarks>
    /// <param name="morphTransform">A rigid or uniformly scaled mesh transform replacing the source coordinate conversion.</param>
    /// <param name="defineControls">Whether this source defines the temporary direct controllers.</param>
    public static FlexGeometry WriteFlexDmx(string path, string reference, IReadOnlyList<AppearancePrimitive> primitives, CoordinateMode coord, JsonObject rig,
        M4? morphTransform = null, bool defineControls = true)
    {
        var (bones, corners, materials) = ReadReference(reference);
        var stem = PathText.Stem(path);
        var document = new DmxDocument("model", 18);
        var root = document.AddElement("root", "DmElement");
        document.Root = root;
        var model = document.AddElement(stem, "DmeModel");
        model["transform"] = Transform(document, "model");
        root["model"] = model;
        root["skeleton"] = model;
        var mesh = document.AddElement(stem, "DmeMesh");
        mesh["visible"] = true;
        var dag = document.AddElement(stem, "DmeDag");
        dag["shape"] = mesh;
        dag["transform"] = Transform(document, "mesh");
        var vertexData = document.AddElement("bind", "DmeVertexData");
        vertexData["vertexFormat"] = new List<string?> { "positions", "normals", "textureCoordinates", "jointWeights", "jointIndices" };
        vertexData["flipVCoordinates"] = true;
        var identity = Enumerable.Range(0, corners.Count).ToList();
        vertexData["positions"] = corners.ConvertAll(corner => corner.Position);
        vertexData["positionsIndices"] = identity;
        vertexData["normals"] = corners.ConvertAll(corner => corner.Normal);
        vertexData["normalsIndices"] = new List<int>(identity);
        vertexData["textureCoordinates"] = corners.ConvertAll(corner => corner.Uv);
        vertexData["textureCoordinatesIndices"] = new List<int>(identity);
        mesh["bindState"] = vertexData;
        mesh["currentState"] = vertexData;
        mesh["baseStates"] = new List<DmxElement?> { vertexData };
        SetSkeleton(document, model, dag, bones, corners, vertexData);
        var faceSets = new List<DmxElement?>();
        foreach (var material in materials.Distinct(StringComparer.Ordinal))
        {
            var faceSet = document.AddElement(material, "DmeFaceSet");
            var materialElement = document.AddElement(material, "DmeMaterial");
            materialElement["mtlName"] = material;
            faceSet["material"] = materialElement;
            var faces = new List<int>();
            for (var triangle = 0; triangle < materials.Count; triangle++)
                if (materials[triangle] == material) faces.AddRange([triangle * 3, triangle * 3 + 1, triangle * 3 + 2, -1]);
            faceSet["faces"] = faces;
            faceSets.Add(faceSet);
        }
        mesh["faceSets"] = faceSets;
        var names = new List<string>();
        var seen = new HashSet<string>(StringComparer.Ordinal);
        if (defineControls)
            foreach (var (name, _) in rig.Need("expressions").Obj())
                if (seen.Add(name)) names.Add(name);
        foreach (var primitive in primitives)
            foreach (var name in primitive.Targets.Keys)
                if (seen.Add(name)) names.Add(name);
        if (defineControls && names.Count > 96) throw new InvalidDataException("Morph import exceeds Source 1's 96-controller limit");
        var cornerTotal = primitives.Sum(primitive => primitive.Indices.Length);
        var deltaStates = new List<DmxElement?>();
        var positions = new V3[cornerTotal];
        var normals = new V3[cornerTotal];
        var scratch = new V3[primitives.Count > 0 ? primitives.Max(primitive => primitive.Vertices.Length) : 0];
        if (morphTransform is not null && names.Count > 0) RagdollAppearance.RequireUniformScale(morphTransform);
        foreach (var name in names)
        {
            Array.Clear(positions);
            Array.Clear(normals);
            var at = 0;
            foreach (var primitive in primitives)
            {
                primitive.Targets.TryGetValue(name, out var target);
                WriteCorners(target?.Position, primitive, at, positions, scratch, value => RagdollAppearance.PositionDelta(value, coord, morphTransform));
                WriteCorners(target?.Normal, primitive, at, normals, scratch, value => RagdollAppearance.NormalDelta(value, coord, morphTransform));
                at += primitive.Indices.Length;
            }
            var delta = document.AddElement(name, "DmeVertexDeltaData");
            delta["vertexFormat"] = new List<string?> { "positions", "normals" };
            DeltaStream(delta, "positions", positions);
            DeltaStream(delta, "normals", normals);
            deltaStates.Add(delta);
        }
        mesh["deltaStates"] = deltaStates;
        mesh["deltaStateWeights"] = Enumerable.Repeat(new V2(0, 0), deltaStates.Count).ToList();
        mesh["deltaStateWeightsLagged"] = Enumerable.Repeat(new V2(0, 0), deltaStates.Count).ToList();
        var combination = document.AddElement("combinationOperator", "DmeCombinationOperator");
        var controls = new List<DmxElement?>();
        if (defineControls)
            foreach (var name in names)
            {
                var control = document.AddElement(name, "DmeCombinationInputControl");
                control["rawControlNames"] = new List<string?> { name };
                control["stereo"] = false;
                control["eyelid"] = false;
                control["flexMin"] = 0.0f;
                control["flexMax"] = 1.0f;
                control["wrinkleScales"] = new List<float> { 0.0f };
                controls.Add(control);
            }
        combination["controls"] = controls;
        combination["controlValues"] = Enumerable.Repeat(new V3(0, 0, 0.5), controls.Count).ToList();
        combination["controlValuesLagged"] = Enumerable.Repeat(new V3(0, 0, 0.5), controls.Count).ToList();
        combination["usesLaggedValues"] = false;
        combination["dominators"] = new List<DmxElement?>();
        combination["targets"] = new List<DmxElement?> { mesh };
        root["combinationOperator"] = combination;
        var compacted = CompactGeometry(mesh, vertexData);
        document.Save(path, "binary", 5);
        return compacted;
    }

    /// <summary>Spreads one primitive's morph over its triangle corners, starting at a corner offset.</summary>
    private static void WriteCorners(MorphDelta? delta, AppearancePrimitive primitive, int start, V3[] destination, V3[] scratch, Func<V3, V3> transform)
    {
        if (delta is null || delta.Indices.Length == 0) return;
        for (var index = 0; index < delta.Indices.Length; index++) scratch[delta.Indices[index]] = transform(delta.Values[index]);
        var indices = primitive.Indices;
        for (var corner = 0; corner < indices.Length; corner++) destination[start + corner] = scratch[indices[corner]];
        delta.Collapse(scratch);
    }

    /// <summary>Rebuilds DMX skeleton and vertex links after physics or a playermodel remaps its paired SMD.</summary>
    public static void SyncFlexSkeleton(string path, string reference)
    {
        var document = DmxDocument.Load(path);
        var (bones, corners, _) = ReadReference(reference);
        var model = document.Root!.Element("model")!;
        var dag = document.Elements.First(element => element.Type == "DmeDag");
        var vertexData = dag.Element("shape")!.Element("currentState")!;
        var sourceCount = vertexData.Contains("s2kitSourceCornerCount") ? vertexData.Value<int>("s2kitSourceCornerCount") : vertexData.Array<V3>("positions").Count;
        if (sourceCount != corners.Count) throw new InvalidDataException("Remapped SMD changed flex-DMX corner count");
        SetSkeleton(document, model, dag, bones, corners, vertexData);
        document.Save(path, "binary", 5);
    }
}
