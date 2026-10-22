---
name: s2-valvebiped-conform
description: The verified Source 2 to ValveBiped playermodel conversion recipe (donor-exact bind, anatomical re-skin, naming/QC rules, validation protocol). Use when porting any S2 model to a GMod playermodel/NPC on an included anim library, or when touching valvebiped_conform.py / debugging a "scrambled mesh" port.
---

# Source 2 → ValveBiped conform — the working recipe

Every rule below was root-caused and user-verified on the HLA `zombie_classic`
playermodel port (July 2026). The kit implementation lives in
`v2/app/tools/valvebiped_conform.py` (+ `v2/app/utils/mdl_bones.py`,
map in `v2/app/core/bone_maps.py`); wired into the glTF→SMD batch tool via
the "Anim donor (.mdl)" field. Full forensic history:
`~/.Codex/.../memory/gmod-playermodel-conversion-rules.md`.

## The five load-bearing rules

1. **Donor-exact bind.** `CalcVirtualAnimation` initializes every mapped bone
   from the anim library's bind (`pSeqbone`) — the target's bind locals for
   shared bones are discarded at runtime. Compiled bind must EQUAL the donor
   (pos + quat per shared bone, verify ≤1e-5). Custom proportions go only in
   a `$proportions` delta autoplay.
2. **Single-case bone naming.** The compiler stricmp-sorts `bonetablebyname`;
   mixed-case names break case-sensitive binary searches (silently dropped
   bones → partial skeleton). Unmapped bones → `z_` + lowercase. Map the
   crab/companion roots explicitly (`mini_root` → `ValveBiped.HC_Body_Bone`)
   or their subtrees die in ValveBiped-only prunes.
3. **`rotate -90` on every locally-authored `$sequence`/`$animation`.**
   studiomdl yaws sequence-animation roots +90° (XSI legacy) but not
   `$definebone`. NPCs mask it (uniform yaw); playermodels mix local +
   library sequences and scramble without the compensation.
4. **Mirror the donor's `$poseparameter` list verbatim, donor order.**
   Library sequences run always-on pose-param-driven autolayers
   (z_anm: `body_rot_z` ±60° spine twist); an empty local table leaves them
   unresolved in tools.
5. **Anatomical re-skin — never full-frame deltas.** Per bone:
   translation to the new joint head + swing (old child-segment direction →
   new) + **forward-anchored twist**: rotate about the new segment axis so
   the swing-transported projection of CHARACTER-FORWARD (toe direction
   `ball − ankle`, z-flattened, averaged L/R, per rig) aligns with the target
   rig's forward projection; anchor on character-UP when |axis·fwd| > 0.8;
   leaves inherit the parent rotation. Full-frame deltas
   (`new_world @ inv(old_world)`) swing mirrored-side vertex clusters 180°
   around their joints (ValveBiped right-side frames are flipped vs S2).
   Wrap-window twist heuristics also fail — real inter-rig rolls cluster AT
   ±90°. Fold rule: weights AND deltas resolve pruned bones through the same
   skeleton-tree fold (never raw glTF node parents — cloth/helper chains
   parent elsewhere and fragment the mesh).

## Also required

- ≤128 compiled bones (prune near-zero-weight leaves, fold weights up).
- Compile with StudioMDL CE for >65k-vert models (`$maxverts` supported);
  bodygroup SMDs each ≤65,535 verts.
- Attachment helper bones may be definebone-only (harmless, cleared as a
  suspect), but keep definebones == SMD skeleton values (QAngle order:
  pitch,yaw,roll = ry,rz,rx of the R=Rz@Ry@Rx euler).

## Validation protocol (do this BEFORE asking anyone to look)

1. Scatter-render the **current** build's compiled `.vvd` (never trust old
   renders — a stale verification hid the fragmentation for days).
2. Decode the compiled reference sequence vs the bone table (parser:
   scratchpad `port/anim_decode.py` pattern) — must differ by **0 channels**.
3. Diff compiled bind vs donor bind ≤1e-5; bonetable sorted under strcmp AND
   stricmp with zero binary-search misses.
4. Engine ground truth beats HLMV++: headless
   `gmod.exe -dedicated -console -condebug +map gm_construct` + an
   autorun/server Lua ent probe (`LookupSequence`/`SetCycle`/
   `GetBonePosition`), grep `console.log`. HLMV++ is closed-source and was
   wrong about nothing — but spawnicon/HLMV verdicts can be confounded by
   pose params, autoplay layers, and GMod's session-long model cache:
   **restart viewers after every deploy, judge local sequences without
   autoplay layers first**.
5. Fast iteration: single-variant test builds (zombie: `zombie_type 5` only,
   `TEST_ZOMBIE_TYPE_ONLY` in the scratch builder) — 3 s compiles vs 10 min.

## Debugging order for a "scrambled/messed up" port

Weights → fold consistency → re-skin deltas (mirror flips! roll branch!) →
sequence-vs-table rotation → poseparams/autolayers → THEN tool quirks.
Single-file minimal pairs (one body, one sequence, no definebones) isolate
faster than full builds. Modular meshes viewed as one bodygroup show
legitimate "floating hands" (sleeves live in other bodygroups) — check the
full model before diagnosing.
