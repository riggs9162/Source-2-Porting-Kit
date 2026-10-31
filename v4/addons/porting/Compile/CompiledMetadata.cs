namespace PortingKit.Addons.Porting;

/// <summary>A compiled bone, with its jiggle record when it has one.</summary>
public sealed record CompiledBone(string Name, int Parent, double[] Position, double[] Quaternion, int Flags, int ProceduralType, int ProceduralOffset, int PhysicsBone)
{
    public string ParentName { get; set; } = "";
    public int? JiggleFlags { get; set; }
    public double[]? JiggleParameters { get; set; }
}

/// <summary>One serialized vertex animation record of a mesh. The name comes from its descriptor.</summary>
public sealed record CompiledMeshFlex(int Descriptor, int VertexDeltas, int Pair, int AnimationType)
{
    public string Name { get; set; } = "";
}

/// <summary>A compiled material mesh and its vertex animation records.</summary>
public sealed record CompiledRenderMesh(int MaterialSlot, int Vertices, List<CompiledMeshFlex> Flexes);

/// <summary>One choice of a bodypart. A blank choice has no meshes.</summary>
public sealed record CompiledChoice(string Name, int MeshCount, int VertexCount, List<CompiledRenderMesh> Meshes);

/// <summary>A compiled bodypart and its choices.</summary>
public sealed record CompiledBodygroup(string Name, int Base, List<CompiledChoice> Choices);

/// <summary>A compiled attachment with its three-by-four local matrix.</summary>
public sealed record CompiledAttachment(string Name, int Flags, int Bone, double[][] Matrix);

/// <summary>A compiled hitbox of one set.</summary>
public sealed record CompiledHitbox(string Set, int Bone, int Group, double[] Minimum, double[] Maximum);

/// <summary>A compiled flex controller.</summary>
public sealed record CompiledFlexController(string Name, string Type, double Minimum, double Maximum);

/// <summary>A compiled flex rule. The name comes from its descriptor.</summary>
public sealed record CompiledFlexRule(int Descriptor, int Operations)
{
    public string Name { get; set; } = "";
}

/// <summary>The v48 or v49 studiohdr, bone, mesh, skin, flex and metadata records of an MDL.</summary>
public sealed class CompiledMetadata
{
    public int Version { get; init; }
    public uint Checksum { get; init; }
    public List<CompiledBone> Bones { get; init; } = [];
    public List<CompiledBodygroup> Bodygroups { get; init; } = [];
    public List<string> Materials { get; init; } = [];
    public List<string> MaterialPaths { get; init; } = [];
    public List<List<string>> Skins { get; init; } = [];
    public List<List<int>> SkinIndices { get; init; } = [];
    public List<string> FlexDescriptors { get; init; } = [];
    public List<CompiledFlexController> FlexControllers { get; init; } = [];
    public List<CompiledFlexRule> FlexRules { get; init; } = [];
    public List<CompiledAttachment> Attachments { get; init; } = [];
    public List<CompiledHitbox> Hitboxes { get; init; } = [];
    public List<CompiledRenderMesh> Meshes { get; init; } = [];
}
