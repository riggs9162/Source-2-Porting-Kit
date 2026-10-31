using System.Text;

namespace PortingKit.Addons.Porting;

/// <summary>Writes static and skinned reference SMDs from a processed mesh.</summary>
/// <remarks>Input is already scaled, axis-converted and sanitized; nothing here converts coordinates. The two
/// writers differ on purpose. A static SMD has one bone, so no weight block; a skinned one separates its weight
/// block with a tab, because StudioMDL CE reads it as its own tab-delimited field.</remarks>
public static class SmdExporter
{
    private static readonly V3 DefaultNormal = new(0.0, 0.0, 1.0);

    /// <summary>Applies the UV mode to a mesh's coordinates, or returns null when there are none to use.</summary>
    public static V2[]? ResolveUvs(ModelMesh mesh, string uvMode, bool forceUvZero)
    {
        if (forceUvZero || mesh.Uvs is not { } raw || raw.Length != mesh.Vertices.Length) return null;
        switch (uvMode)
        {
            case "wrap":
                return Array.ConvertAll(raw, uv => new V2(uv.X - Math.Floor(uv.X), uv.Y - Math.Floor(uv.Y)));
            case "clamp":
                return Array.ConvertAll(raw, uv => new V2(Math.Clamp(uv.X, 0.0, 1.0), Math.Clamp(uv.Y, 0.0, 1.0)));
            case "normalize":
                double minU = double.MaxValue, minV = double.MaxValue, maxU = double.MinValue, maxV = double.MinValue;
                foreach (var (u, v) in raw)
                {
                    minU = Math.Min(minU, u);
                    minV = Math.Min(minV, v);
                    maxU = Math.Max(maxU, u);
                    maxV = Math.Max(maxV, v);
                }
                double rangeU = maxU - minU, rangeV = maxV - minV;
                return rangeU > 1e-6 && rangeV > 1e-6 ? Array.ConvertAll(raw, uv => new V2((uv.X - minU) / rangeU, (uv.Y - minV) / rangeV)) : raw;
            default:
                return raw;
        }
    }

    /// <summary>Chooses one face's material, falling back when the per-face data does not cover it.</summary>
    public static string PickMaterial(int face, int[]? faceMaterials, List<string>? names, string fallback)
    {
        if (faceMaterials is null || names is null || face >= faceMaterials.Length) return fallback;
        var material = faceMaterials[face];
        return material >= 0 && material < names.Count ? names[material] : fallback;
    }

    /// <summary>Lists the faces that survive index and finiteness checks, by their original position.</summary>
    /// <returns>The surviving face indices, or null with the reason every face was rejected.</returns>
    private static (List<int>? Faces, string Error) ValidFaces(ModelMesh mesh, V3[]? normals)
    {
        var vertices = mesh.Vertices;
        var faces = mesh.Faces;
        var valid = new List<int>(mesh.FaceCount);
        for (var face = 0; face < mesh.FaceCount; face++)
        {
            int a = faces[face * 3], b = faces[face * 3 + 1], c = faces[face * 3 + 2];
            if (a >= 0 && b >= 0 && c >= 0 && a < vertices.Length && b < vertices.Length && c < vertices.Length) valid.Add(face);
        }
        if (valid.Count == 0) return (null, "No valid faces after index validation");
        var finite = valid.Where(face => Enumerable.Range(0, 3).All(corner =>
        {
            var index = faces[face * 3 + corner];
            return vertices[index].IsFinite && (normals is null || normals[index].IsFinite);
        })).ToList();
        return finite.Count == 0 ? (null, "No valid faces after filtering") : (finite, "");
    }

    private static V3[]? Normals(ModelMesh mesh, V3[]? overrideNormals)
    {
        if (overrideNormals is not null) return overrideNormals;
        try
        {
            return mesh.VertexNormals();
        }
        catch (IndexOutOfRangeException)
        {
            return null;
        }
    }

    /// <summary>Writes one vertex's position, normal and UV, tab-separated with six decimals.</summary>
    private static void Corner(StringBuilder line, int bone, V3 position, V3 normal, double u, double v)
    {
        line.Append(Fmt.Int(bone)).Append('\t')
            .Append(Fmt.F6(position.X)).Append(' ').Append(Fmt.F6(position.Y)).Append(' ').Append(Fmt.F6(position.Z)).Append('\t')
            .Append(Fmt.F6(normal.X)).Append(' ').Append(Fmt.F6(normal.Y)).Append(' ').Append(Fmt.F6(normal.Z)).Append('\t')
            .Append(Fmt.F6(u)).Append(' ').Append(Fmt.F6(v));
    }

    /// <summary>Writes a one-bone SMD.</summary>
    /// <param name="rootBind">Authors the root bone's frame. Needed when this SMD is the collision model of an
    /// animated model whose root has a non-identity bind: the physics root must match so that studiomdl's
    /// inverse-physics-root times main-root cancels at runtime instead of double-rotating the hull.</param>
    /// <returns>Whether it wrote, and a reason when it did not.</returns>
    public static (bool Success, string Warning) WriteStatic(ModelMesh? mesh, string outPath, string materialName, bool flipV = true, bool forceUvZero = false,
        V3[]? overrideNormals = null, string uvMode = "preserve", (V3 Position, (double Pitch, double Yaw, double Roll) Angles)? rootBind = null)
    {
        if (mesh is null || mesh.FaceCount == 0) return (false, "Mesh has no faces");
        if (mesh.Vertices.Length == 0) return (false, "Mesh has no vertices");
        var normals = Normals(mesh, overrideNormals);
        var (faces, error) = ValidFaces(mesh, normals);
        if (faces is null) return (false, error);
        var uvs = ResolveUvs(mesh, uvMode, forceUvZero);
        try
        {
            using var output = Fmt.OpenText(outPath);
            output.Write("version 1\nnodes\n0 \"root\" -1\nend\nskeleton\ntime 0\n");
            var (x, y, z) = rootBind?.Position ?? V3.Zero;
            var (pitch, yaw, roll) = rootBind?.Angles ?? (0.0, 0.0, 0.0);
            output.Write($"0 {Fmt.F6(x)} {Fmt.F6(y)} {Fmt.F6(z)}  {Fmt.F6(Fmt.Radians(Fmt.WrapDegrees(roll)))} {Fmt.F6(Fmt.Radians(Fmt.WrapDegrees(pitch)))} {Fmt.F6(Fmt.Radians(Fmt.WrapDegrees(yaw)))}\n");
            output.Write("end\ntriangles\n");
            var line = new StringBuilder(128);
            foreach (var face in faces)
            {
                var material = PickMaterial(face, mesh.FaceMaterials, mesh.MaterialNames, materialName);
                if (!Ascii.IsValid(material)) throw new InvalidDataException($"Material name is not ASCII: {material}");
                output.Write(material);
                output.Write('\n');
                for (var corner = 0; corner < 3; corner++)
                {
                    var index = mesh.Faces[face * 3 + corner];
                    var uv = uvs is null ? default : uvs[index];
                    line.Clear();
                    Corner(line, 0, mesh.Vertices[index], normals is null ? DefaultNormal : normals[index], uv.X, flipV ? 1.0 - uv.Y : uv.Y);
                    line.Append('\n');
                    output.Write(line);
                }
            }
            output.Write("end\n");
        }
        catch (Exception exception) when (exception is IOException or InvalidDataException or UnauthorizedAccessException)
        {
            return (false, $"SMD write failed: {exception.Message}");
        }
        return (true, "");
    }

    /// <summary>Writes a skinned reference SMD with the skin's bind pose.</summary>
    /// <param name="vertexJoints">Four joint indices per vertex.</param>
    /// <param name="vertexWeights">Four weights per vertex, in step with the joints.</param>
    /// <returns>Whether it wrote, and a reason when it did not.</returns>
    public static (bool Success, string Warning) WriteSkinned(ModelMesh? mesh, GltfSkin skin, int[]? vertexJoints, float[]? vertexWeights, string outPath, string materialName,
        CoordinateMode coordinates, bool flipV = false, bool forceUvZero = false, V3[]? overrideNormals = null, string uvMode = "preserve")
    {
        if (mesh is null || mesh.FaceCount == 0) return (false, "Mesh has no faces");
        if (mesh.Vertices.Length == 0) return (false, "Mesh has no vertices");
        if (vertexJoints is null || vertexWeights is null) return (false, "Missing JOINTS_0/WEIGHTS_0 — cannot write skinned SMD");
        if (vertexJoints.Length != mesh.Vertices.Length * 4 || vertexWeights.Length != mesh.Vertices.Length * 4)
            return (false, $"Skin attribute length mismatch (verts={mesh.Vertices.Length} joints={vertexJoints.Length / 4} weights={vertexWeights.Length / 4})");
        var normals = Normals(mesh, overrideNormals);
        var (faces, error) = ValidFaces(mesh, normals);
        if (faces is null) return (false, error);
        var uvs = ResolveUvs(mesh, uvMode, forceUvZero);
        var order = SmdSkeleton.NodeOrder(skin);
        var links = VertexLinks(mesh.Vertices.Length, vertexJoints, vertexWeights, skin);
        try
        {
            using var output = Fmt.OpenText(outPath);
            SmdSkeleton.WriteSkeleton(output, skin, [skin.BindPose()], coordinates);
            output.Write("triangles\n");
            var line = new StringBuilder(160);
            foreach (var face in faces)
            {
                var material = PickMaterial(face, mesh.FaceMaterials, mesh.MaterialNames, materialName);
                if (!Ascii.IsValid(material)) throw new InvalidDataException($"Material name is not ASCII: {material}");
                output.Write(material);
                output.Write('\n');
                for (var corner = 0; corner < 3; corner++)
                {
                    var index = mesh.Faces[face * 3 + corner];
                    var uv = uvs is null ? default : uvs[index];
                    var vertexLinks = links[index];
                    line.Clear();
                    Corner(line, order[vertexLinks[0].Joint], mesh.Vertices[index], normals is null ? DefaultNormal : normals[index], uv.X, flipV ? 1.0 - uv.Y : uv.Y);
                    if (vertexLinks.Count > 1)
                    {
                        line.Append('\t').Append(Fmt.Int(vertexLinks.Count));
                        foreach (var (joint, weight) in vertexLinks) line.Append(' ').Append(Fmt.Int(order[joint])).Append(' ').Append(Fmt.F6(weight));
                    }
                    line.Append('\n');
                    output.Write(line);
                }
            }
            output.Write("end\n");
        }
        catch (Exception exception) when (exception is IOException or InvalidDataException or UnauthorizedAccessException)
        {
            return (false, $"Skinned SMD write failed: {exception.Message}");
        }
        return (true, "");
    }

    /// <summary>Builds each vertex's positive weights, renormalized, with unweighted vertices falling back to the root.</summary>
    private static List<(int Joint, double Weight)>[] VertexLinks(int count, int[] joints, float[] weights, GltfSkin skin)
    {
        var result = new List<(int, double)>[count];
        for (var vertex = 0; vertex < count; vertex++)
        {
            var links = new List<(int Joint, double Weight)>(4);
            for (var slot = 0; slot < 4; slot++)
            {
                double weight = weights[vertex * 4 + slot];
                var joint = joints[vertex * 4 + slot];
                if (weight > 0.0 && joint >= 0 && joint < skin.Joints.Count) links.Add((joint, weight));
            }
            if (links.Count == 0) links.Add((skin.RootIndex, 1.0));
            else
            {
                var total = links.Sum(link => link.Weight);
                if (total > 0) links = links.ConvertAll(link => (link.Joint, link.Weight / total));
            }
            result[vertex] = links;
        }
        return result;
    }

    /// <summary>Concatenates JOINTS_0 and WEIGHTS_0 across primitives in glTF order, renormalizing each row.</summary>
    /// <remarks>Walks every mesh in JSON order, which is what matches the concatenated geometry of a raw
    /// Source-units file; only looking at the first mesh broke multi-mesh assets. Joint indices are not remapped,
    /// because Source 2 Viewer repeats one skeleton across <c>skins[]</c> so each mesh node can have a private
    /// one. A primitive with no skin data makes the whole model read as unskinned rather than guess.</remarks>
    /// <returns>Four joints and weights per vertex, or nulls when the model has no usable skin streams.</returns>
    public static (int[]? Joints, float[]? Weights) ParseSkinVertexData(GltfDocument document)
    {
        if (document.Meshes.Count == 0) return (null, null);
        var joints = new List<int>();
        var weights = new List<float>();
        foreach (var mesh in document.Meshes)
            foreach (var primitive in mesh.Get("primitives").Arr())
            {
                var attributes = primitive.Get("attributes");
                if (!attributes.Has("JOINTS_0") || !attributes.Has("WEIGHTS_0")) return (null, null);
                var jointData = document.Accessor(attributes.Get("JOINTS_0").Int());
                var weightData = document.Accessor(attributes.Get("WEIGHTS_0").Int());
                if (jointData.Width != 4 || weightData.Width != 4 || jointData.Count != weightData.Count) return (null, null);
                joints.AddRange(jointData.Integers());
                weights.AddRange(weightData.Values.Select(value => (float)value));
            }
        if (joints.Count == 0) return (null, null);
        var values = weights.ToArray();
        for (var row = 0; row < values.Length; row += 4)
        {
            var sum = values[row] + values[row + 1] + values[row + 2] + values[row + 3];
            if (sum <= 1e-6f) continue;
            for (var slot = 0; slot < 4; slot++) values[row + slot] /= sum;
        }
        return (joints.ToArray(), values);
    }
}
