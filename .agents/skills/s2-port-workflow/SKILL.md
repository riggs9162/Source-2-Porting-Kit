---
name: s2-port-workflow
description: The end-to-end Source 2 → Source 1/GMod asset porting pipeline this kit implements (VRF export → SMD/QC → PBR-to-VMT → studiomdl), including ragdoll/physics rules. Use when porting a model/material, debugging a broken port, or extending a pipeline stage.
---

# Source 2 → Source 1 porting workflow

The kit ports assets in stages; each stage maps to a tool in the app. Deep-dive documentation for every stage lives in **`v2/manuals/`** (QC_GUIDE, QC_BONE/SEQUENCE/ANIMATION guides, COLLISION_MESH, FAKE_PBR_EXPLAINED, HOTSPOT_TEXTURING, soundscape guides, VTF_MIGRATION_GUIDE) — consult those before inventing an answer.

## Pipeline stages

1. **Extract** — *VRF Batch Export* tool (or `v2/app/cli/vrf_export_cli.py`) drives **Source2Viewer-CLI.exe** (path saved in settings as `vrf_cli_path`) to decompile `.vmdl_c` / `.vmat_c` / `.vtex_c` from loose files or VPKs. Output is staged into a Source 1 sibling layout: `modelsrc/` (GLTF/GLB + a separate `<name>_physics.glb`) and `materialsrc/` (VMATs + PNGs). Implementation: `v2/app/utils/vrf_runner.py`.
2. **Model conversion** — *GLTF Batch SMD* tool (`gltf_smd_batch_tool.py`) pairs render + physics GLBs, handles Source2 native units (inches, axis swap baked into the root bone), writes skinned reference SMD + per-clip animation SMDs, and scaffolds a QC (`$texturegroup` skins, static `$collisionmodel`, or ragdoll `$collisionjoints` — see below).
3. **Materials** — *VMAT PBR Batch Converter* (`vmat_pbr_tool.py`) converts `.vmat` → `.vmt` + VTFs; the FakePBR family (`fake_pbr_tool.py`, `exo_pbr_tool.py`, `texture_pbr_batch_tool.py`) composite PBR maps into Source 1 "fake PBR" (phong/envmap tricks). VTF encoding goes through `v2/app/utils/vtf_encoder.py` (sourcepp) — oversized inputs are pre-resized in Python because sourcepp's internal downscale corrupts normals.
4. **Compile** — user compiles the QC with **studiomdl** and tests in-game. Decompile checks use **Crowbar ≥ 0.68** (older versions collapse multi-convex physics into one piece).
5. **Sounds** — soundscape porter + OGG/loop-point/quad-to-stereo tools (need FFmpeg).

## Physics / ragdoll rules (hard-won, verified)

- **Rebuild, not convert**: Source 2 Rubikon physics never transfers to Source 1 VPhysics. Mesh + skeleton survive; physics is re-authored.
- VRF's `<name>_physics.glb` merges all hulls into ONE unskinned mesh per collision-group×surfaceprop (3 unshared verts per triangle, no bone info). Recovering per-bone hulls from it is heuristic and has failed in-game; the ground truth is the **PHYS block text dump** (`Source2Viewer-CLI -b PHYS`) — exact per-bone shapes, joints with limits, surfaceprop, density.
- Ragdolls: one convex hull per simulated bone, every vert weighted 1.0 to a single bone, max **32** simulated parts, `$collisionjoints` + `$jointconstrain` (constrain only the axes that need it). Animated props also use `$collisionjoints` with bone-named solids. Static props use `$collisionmodel` + `$concave`.
- Ragdoll code lives in `v2/app/tools/ragdoll_physics.py`, presets in `v2/app/core/ragdoll_presets.py`, Source2→ValveBiped bone map in `v2/app/core/bone_maps.py`. Humanoids retarget to ValveBiped; non-humanoids keep the native skeleton. Target legacy VPhysics only (not VPhysics-Jolt).

## Debugging a broken port

- Model invisible/corrupted textures → check VMT paths against `default_material_path` (settings) and VTF dimensions (VTF Clamp tool; Source 1 wants powers of two).
- Ragdoll collapses to a blob → collision mesh wasn't per-bone convex, or Crowbar < 0.68 was involved.
- Normal maps look corrupted → an oversized PNG hit sourcepp's internal downscale; ensure the FakePBR resolution clamps (Preferences) stayed enabled.
- Check `v2/logs/app.log` for the failing tool's stack trace.
