using System.Buffers.Binary;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;

namespace PortingKit.Addons.Porting;

/// <summary>Expands exact QC material bindings over duplicate Source compiler texture identities.</summary>
/// <remarks>studiomdl can give one resource two texture numbers, and its LOD replacement table only lists the
/// first, so a lower LOD silently renders the wrong material on the other number. The finalizer extends a binding
/// across those duplicates, and only then: anything the QC cannot certify is a compiler defect and is refused,
/// never quietly rewritten. The original table stays in the file unreferenced, so no geometry moves.</remarks>
public static class CompiledMaterials
{
    /// <summary>Requires an existing exact binding before extending it to equivalent texture IDs.</summary>
    private static (List<Dictionary<int, string>> Tables, int Added) ExpandedTables(IReadOnlyList<string> materials, List<RenderLevel> expected, List<Dictionary<int, string>> actual)
    {
        if (expected.Count != actual.Count) throw new CompiledDataError("Compiled and generated LOD material table counts differ");
        var identities = new Dictionary<string, List<int>>(StringComparer.Ordinal);
        for (var index = 0; index < materials.Count; index++)
        {
            var token = Resources.Token(materials[index]);
            if (!identities.TryGetValue(token, out var list)) identities[token] = list = [];
            list.Add(index);
        }
        var output = new List<Dictionary<int, string>>(expected.Count);
        var added = 0;
        for (var lod = 0; lod < expected.Count; lod++)
        {
            var current = actual[lod];
            var bindings = new Dictionary<int, string>();
            foreach (var (source, target) in expected[lod].Materials)
            {
                var indices = identities.GetValueOrDefault(source) ?? [];
                if (indices.Count == 0) throw new CompiledDataError($"LOD {lod} material replacement source is absent: {source}");
                foreach (var index in indices) bindings[index] = target;
            }
            foreach (var (index, target) in current)
                if (bindings.GetValueOrDefault(index) != target) throw new CompiledDataError($"LOD {lod} texture ID {index} has an unexpected material replacement");
            foreach (var (index, target) in bindings)
            {
                if (current.ContainsKey(index)) continue;
                var equivalent = identities[Resources.Token(materials[index])];
                if (equivalent.Count < 2 || !equivalent.Any(other => current.GetValueOrDefault(other) == target)) throw new CompiledDataError($"LOD {lod} texture ID {index} lacks a certified duplicate material binding");
                added++;
            }
            output.Add(bindings);
        }
        return (output, added);
    }

    /// <summary>Appends packed v7 replacement records without relocating any rendered geometry.</summary>
    private static byte[] AppendTables(byte[] original, List<Dictionary<int, string>> tables)
    {
        var data = new List<byte>(original);
        data.AddRange(new byte[(4 - data.Count % 4) % 4]);
        var start = data.Count;
        data.AddRange(new byte[8 * tables.Count]);
        void Write(int offset, Span<byte> bytes)
        {
            for (var index = 0; index < bytes.Length; index++) data[offset + index] = bytes[index];
        }
        Span<byte> scratch = stackalloc byte[8];
        for (var lod = 0; lod < tables.Count; lod++)
        {
            var header = start + 8 * lod;
            var entries = data.Count;
            BinaryPrimitives.WriteInt32LittleEndian(scratch, tables[lod].Count);
            BinaryPrimitives.WriteInt32LittleEndian(scratch[4..], entries - header);
            Write(header, scratch);
            data.AddRange(new byte[6 * tables[lod].Count]);
            var position = 0;
            foreach (var (index, target) in tables[lod].OrderBy(pair => pair.Key))
            {
                if (index is < 0 or > 32767 || target.Contains('\0')) throw new CompiledDataError("LOD material replacement cannot fit the target binary format");
                var record = entries + position++ * 6;
                BinaryPrimitives.WriteInt16LittleEndian(scratch, (short)index);
                BinaryPrimitives.WriteInt32LittleEndian(scratch[2..], data.Count - record);
                Write(record, scratch[..6]);
                data.AddRange(Encoding.UTF8.GetBytes(target));
                data.Add(0);
            }
        }
        if (data.Count >= int.MaxValue) throw new CompiledDataError("Expanded VTX material tables exceed signed 32-bit offsets");
        BinaryPrimitives.WriteInt32LittleEndian(scratch, start);
        Write(24, scratch[..4]);
        return data.ToArray();
    }

    /// <summary>Keeps exact originals and rolls back the complete VTX cohort if replacement fails.</summary>
    private static void ReplaceFiles(Dictionary<string, (byte[] Original, byte[] Rewritten)> prepared, string backupDir, PortContext? context)
    {
        var backups = new Dictionary<string, string>();
        var temporary = new Dictionary<string, string>();
        var replaced = new List<string>();
        Directory.CreateDirectory(backupDir);
        try
        {
            foreach (var (path, (original, rewritten)) in prepared)
            {
                context?.Check();
                if (!File.ReadAllBytes(path).AsSpan().SequenceEqual(original)) throw new CompiledDataError($"Compiled VTX changed during material finalization: {path}");
                var digest = Convert.ToHexStringLower(SHA256.HashData(original))[..16];
                var backup = Path.Combine(backupDir, $"{Path.GetFileName(path)}.{digest}.original");
                if (File.Exists(backup) && !File.ReadAllBytes(backup).AsSpan().SequenceEqual(original)) throw new CompiledDataError("Compiled material backup differs from its certified original");
                if (!File.Exists(backup)) File.WriteAllBytes(backup, original);
                backups[path] = backup;
                var staging = path + ".s2kit-materials.tmp";
                File.WriteAllBytes(staging, rewritten);
                temporary[path] = staging;
            }
            context?.Check();
            foreach (var (path, staging) in temporary)
            {
                File.Move(staging, path, true);
                replaced.Add(path);
                if (!File.ReadAllBytes(path).AsSpan().SequenceEqual(prepared[path].Rewritten)) throw new CompiledDataError($"Written VTX material bindings differ from validated bytes: {path}");
            }
        }
        catch (Exception)
        {
            for (var index = replaced.Count - 1; index >= 0; index--)
            {
                var recovery = replaced[index] + ".s2kit-materials.rollback";
                File.WriteAllBytes(recovery, File.ReadAllBytes(backups[replaced[index]]));
                File.Move(recovery, replaced[index], true);
            }
            throw;
        }
        finally
        {
            foreach (var staging in temporary.Values)
                if (File.Exists(staging)) File.Delete(staging);
        }
    }

    private static bool SameTables(List<Dictionary<int, string>> a, List<Dictionary<int, string>> b) =>
        a.Count == b.Count && a.Zip(b).All(pair => pair.First.Count == pair.Second.Count && pair.First.All(entry => pair.Second.GetValueOrDefault(entry.Key) == entry.Value));

    /// <summary>Restores identical-resource duplicate VTX bindings and rejects unrelated compiler defects.</summary>
    /// <param name="mdl">The compiled model.</param>
    /// <param name="qc">The QC that produced it.</param>
    /// <param name="backupDir">Where original VTX files are kept, by default beside the model.</param>
    /// <param name="context">Cancellation and logging.</param>
    public static JsonObject FinalizeLodMaterialReplacements(string mdl, string qc, string? backupDir = null, PortContext? context = null)
    {
        var path = PathText.Native(mdl);
        var components = Studiomdl.CheckCompiledModel(path);
        if (!components.Ok) throw new CompiledDataError("Material finalization requires matching compiled components: " + string.Join("; ", components.Problems()));
        var metadata = CompiledModelAudit.ReadCompiledModel(path);
        var expected = CompiledRenderCoverage.ReadRenderLayout(qc).Levels;
        var prepared = new Dictionary<string, (byte[] Original, byte[] Rewritten)>();
        var files = new JsonArray();
        var total = 0;
        foreach (var suffix in Studiomdl.VtxSuffixes)
        {
            context?.Check();
            var vtx = Path.ChangeExtension(path, null) + suffix;
            if (!File.Exists(vtx)) continue;
            var original = File.ReadAllBytes(vtx);
            var actual = CompiledRenderCoverage.ReadLodMaterialReplacementIds(original, metadata.Materials);
            var (tables, added) = ExpandedTables(metadata.Materials, expected, actual);
            var rewritten = added > 0 ? AppendTables(original, tables) : original;
            if (!SameTables(CompiledRenderCoverage.ReadLodMaterialReplacementIds(rewritten, metadata.Materials), tables)) throw new CompiledDataError("Expanded VTX material bindings failed numeric round-trip validation");
            if (!rewritten.AsSpan(0, 24).SequenceEqual(original.AsSpan(0, 24)) || !rewritten.AsSpan(28, original.Length - 28).SequenceEqual(original.AsSpan(28))) throw new CompiledDataError("Material finalization changed original VTX geometry bytes");
            if (BinaryPrimitives.ReadUInt32LittleEndian(rewritten.AsSpan(16)) != metadata.Checksum) throw new CompiledDataError("Material finalization changed the compiled checksum");
            if (added > 0) prepared[vtx] = (original, rewritten);
            total += added;
            files.Add(new JsonObject
            {
                ["path"] = vtx, ["added_bindings"] = added, ["added_bytes"] = rewritten.Length - original.Length,
                ["original_sha256"] = Convert.ToHexStringLower(SHA256.HashData(original)), ["sha256"] = Convert.ToHexStringLower(SHA256.HashData(rewritten))
            });
        }
        var directory = backupDir ?? Path.Combine(CompiledText.Parent(path), ".s2kit-material-originals");
        if (prepared.Count > 0)
        {
            ReplaceFiles(prepared, directory, context);
            context?.Log($"Expanded {total} exact duplicate-ID LOD material bindings");
        }
        return new()
        {
            ["ok"] = true, ["errors"] = new JsonArray(), ["status"] = prepared.Count > 0 ? "adapted" : "preserved", ["files"] = files, ["added_bindings"] = total,
            ["backup_directory"] = prepared.Count > 0 ? directory : null, ["geometry_bytes_unchanged"] = true, ["checksums_unchanged"] = true
        };
    }
}
