# Porting add-on

Native C# porting of Source 2 models and materials into Source 1 addons. It adds
**Models > Model port** and **Materials > Material port** to the sidebar, and
`porting/models` and `porting/materials` to the CLI and MCP.

| Folder | Holds |
| --- | --- |
| `Foundation/` | KV3 and JSON reading, double-precision maths, number formatting, resource paths |
| `Gltf/`, `Metadata/`, `Smd/` | Reading exported models and their authored data, writing skeletons |
| `Export/` | Source2Viewer-CLI, block dumps and the sidecars later stages read |
| `Models/` | Static and skinned sources, LODs, skins, bodygroups, DMX, props |
| `Physics/` | Collision repair and decomposition, kinematic and ragdoll physics, `.phy` audits |
| `Characters/` | Ragdoll appearance, flexes, morphs, procedural bones, runtime drivers |
| `Materials/`, `Textures/` | VMAT to VMT conversion for models and brushes, PNG, TGA, VTF and DXT |
| `Playermodels/` | ValveBiped rigs, retargeted player animations, twist helpers and their audits |
| `Compile/` | studiomdl, the repairs it needs, and compiled-model audits |
| `Workflow/` | Resumable stages, addon packaging and safe installation |

Stages talk through files, in the same layout V3 used: `extracted/<key>/` from the
export, `modelsrc/<model>/` with a `build.json` describing the sources,
`staged/<key>/addon/` holding one model's verified payload, and `addon/` built from
those payloads. A static variant keeps its sources in `modelsrc/<model>/static/`.

A model ships only when its compile, repairs and audits all pass. Studiomdl drops
morphs and LOD material replacements and rebuilds collision hulls, so `Compile/`
restores them in the compiled files and then proves the result against the source.

Number formats, single-precision steps and rounding here are deliberate: they match
what the Source tools read and what V3 produced. Change them only against a real
compiled comparison.
