# Source 2 Porting Kit v3

A ground-up rebuild of the kit on [Flet](https://flet.dev) 0.86. `v2` (the
PySide6 app under [v2/](../v2/)) is untouched and still runs; this tree is
independent and shares no code with it.

```console
python v3/run.py            # choose Desktop or Browser (localhost)
python v3/run.py --desktop  # launch the desktop app directly
python v3/run.py --web      # launch in the default browser, loopback only
python v3/run.py --check    # environment report, no window
python v3/run.py --self-test  # headless integration test
```

Browser mode runs the same tools on this PC at `127.0.0.1`; keep the Python
launcher running while using it. Browser security hides full filesystem paths,
so Browse and Save controls ask for local paths in this mode. Paste the full
file or folder path; outputs are written locally, not downloaded by the browser.
Use Desktop for native file dialogs. `--port` selects a browser server port.

## What this is

The point of v3 is that **every tool page behaves the same**. A tool declares a
parameter schema and one function; the framework supplies the rest:

| Tab | What it gives every tool, for free |
| --- | --- |
| **Run** | The form, built from the schema: validation, dependent fields, path pickers with last-directory memory, presets, reset, last-used values restored |
| **Output** | Stat tiles, sortable/filterable result tables with CSV copy, and the list of files the run produced |
| **Preview** | Texture, model, sound and text viewers for anything the run wrote - or anything you open by hand |
| **Log** | Levelled, colour-coded, searchable, level-filterable, follow-tail, copy, save to disk |
| **History** | Every past run: parameters, metrics, artefacts and the stored log, restorable and repeatable |
| **Settings** | The tool's own preferences (when it declares any), stored per tool |
| **Help** | The tool's markdown notes |

Plus, app-wide: a searchable sidebar with categories and favourites, a command
palette (`Ctrl+K`), a Preferences page covering *every* option in the app, live
theming, and a status bar.

## Running a tool

Work happens on a worker thread and talks back through a queue that the UI
drains in batches, so a job that emits ten thousand log lines does not stall the
window and cancellation is always responsive.

## Writing a tool

One file in [s2kit/tools/](s2kit/tools/); it is picked up automatically.

```python
from s2kit.core.jobs import JobContext
from s2kit.core.params import BoolParam, PathParam
from s2kit.core.registry import tool
from s2kit.ui.tool_page import ToolPage


@tool(id="my-tool", title="My Tool", category="Utility", icon="BUILD",
      description="What it does, in one line.")
class MyTool(ToolPage):
    PARAMS = [
        PathParam("input", "Input folder", mode="directory", required=True),
        BoolParam("dry_run", "Dry run", default=True, group="Safety"),
    ]
    PREFERENCES = [BoolParam("verbose", "Verbose logging", default=False)]
    HELP = "### My Tool\n\nMarkdown shown on the Help tab."

    def run_job(self, ctx: JobContext) -> None:
        ctx.info(f"Working on {ctx.params['input']}")
        ctx.progress(1, 2)
        ctx.metric("Files", 42, tone="good")
        ctx.table("Results", ["File", "Status"], [["a.vmt", "ok"]])
        ctx.artifact("path/to/output.png")     # appears on Preview
        ctx.summary("42 files converted")
```

`run_job` runs off the UI thread and may only talk to its `JobContext`
(`log/info/warn/error/success`, `progress`, `status`, `metric`, `table`,
`artifact`, `summary`, `raise_if_cancelled`). It must never touch a Flet
control - that is what keeps the pipelines testable and reusable from a CLI.

Every entry in `PARAMS` and `PREFERENCES` is automatically persisted,
validated, restorable from history, searchable in Preferences and resettable.

## Layout

```text
v3/
  run.py                 bootstrapper: version check, dependency audit,
                         logging, crash handlers, then launch
  s2kit/
    bootstrap.py         dependency probing/installing, logging, excepthooks
    app.py               Flet entry point, window state, self-test
    core/                framework-free: settings, preferences, params, jobs,
                         history, registry, paths, process
    media/               framework-free decode/render: images (incl. VTF),
                         audio (waveforms), meshes (software rasteriser)
    pipelines/           framework-free asset work: VRF export, glTF->SMD/QC,
                         the SMD writers, glTF animation readers, audio ops,
                         texture I/O, PBR baking, VMT/VMAT, material building
    ui/                  shell, tool page, preferences page, widgets, viewers
    tools/               one module per tool - drop a file in, it appears
  config/                settings.json, per-tool history (generated)
  logs/                  rotating app log + one file per run (generated)
```

**Nothing in `core/`, `media/` or `pipelines/` imports Flet.** Decoding,
rendering and every pipeline stays reachable from a plain interpreter, a test,
or a headless CLI.

## Ported tools

### One-click ragdoll addons

Open **Porting Workflow**, leave **GMod ragdoll addon** selected, and paste
Source 2 Viewer VPK model URLs one per line. Choose a project folder outside
the source installation and a lowercase output namespace. Set 32-bit StudioMDL CE and
its compile game folder here or in Preferences, then press **Port**. These
settings and the batch inputs are saved.

Exact collision serialization uses the compiler's VPhysics library and a small
local helper built with the Windows .NET Framework C# compiler. Enable .NET
Framework 4.8 if that compiler is unavailable. The completed GMod addon contains
the finalized collision data and needs neither helper nor compiler at runtime.

The default produces native ragdolls. **Include original Alyx animations**,
**Also create humanoid playermodels**, and **Install completed addon into GMod**
are independent options, initially off. The player option builds a separate
`_pm` companion with the source character's dimensions, a complete retargeted
GMod animation library, weapon attachments and uninterrupted limb IK chains.
The published animation sources are cached at revision
`9a588af0cb1a2d53de35496ba1737305a877e50c`, with provenance beside the sources.
Headcrabs keep their native rig.

The project contains:

| Path | Contents |
| --- | --- |
| `addon/` | Validated model, material and Lua runtime files ready for GMod |
| `modelsrc/` | Editable SMD, DMX and QC sources, animation library and feature audits |
| `extracted/` | Exported glTF, textures and original authoring metadata |
| `semantic-materials/` | Hash-verified per-VMAT texture contexts for compiled inputs |
| `port-report.md`, `port-report.json` | Per-model outcomes and links to detailed audits |
| `workflow-queue.json` | Versioned checkpoints and output hashes for resume |
| `addon-manifest.json` | Exact packaged file list and SHA-256 hashes |

Extraction, conversion, materials, compilation and packaging run serially.
Independent models continue after a failure. **Cancel** retains completed
stages; **Port** verifies their hashes and resumes the same project. Use a
separate project when changing batch settings. **Plan and check** prepares the
queue without running conversion. Installation copies only manifest-owned
files into the selected addons folder and verifies their hashes.

Reports distinguish preserved, adapted, generated and unsupported features.
The native path retains authored bodygroups, skin material identities, LOD
membership, supported morph rigs, procedural corrections and genuine jiggle
bones. Original morph animation uses a model-scoped GMod flex callback;
unsupported Source 2 simulation remains documented alongside its source data.

Authored bone helpers with exact native equivalents use `.vrd` sources and
`$proceduralbones`. Compiled validation checks their procedural types, control
frames, flags and quaternion triggers. Each constraint has one owner across the
native and addon evaluators. Partial blends, multi-target constraints, RBF
correctives and helpers whose original animation moves their origins retain
the source-aware addon evaluator.

Playermodel motion keeps the source bind frames for the torso, neck, head and
clavicles. Arms, hands and legs use canonical GMod motion directions at their native
joint offsets, preserving standard weapon grips and movement without forcing the
source neck and shoulders into donor bind angles. Reports identify each bone's
transfer mode and measure reference-pose adaptations. The published blend, activity,
gesture and IK graph stays intact.

Compiled morph validation checks the rendered vertices of every material and
LOD against the retained source deformations. When the compiler shares vertices
that need different morph deltas, finalization separates their storage while
keeping vertex attributes and triangle topology unchanged. Reports measure the
extra vertex-buffer space, compiler welding and Source 1 morph precision.

Lower LODs with different materials use stable compiler slots and explicit
per-LOD, per-skin QC material replacements. Generated VMT aliases reuse the
original shader bytes and textures. Compiled validation compares material and
triangle coverage for every bodygroup choice and LOD before packaging.

Compiled-input materials are decompiled individually into isolated project caches.
Distinct compiled textures can share an authored PNG name, so materials use their
own shader-aware channel exports rather than shared extraction aliases. The material
report records original compiled texture references, exact semantic map paths and
hashes, and validated channel selections for omitted builtin defaults. Cache reuse
requires a complete verified file inventory; cancellation cannot certify a partial
export. Explicit material-source overrides and exported-folder inputs retain their
direct input semantics. Packaging still rejects conflicting shared payload bytes.

Source 1 imposes differences that the report measures. Capsule and sphere
tessellation targets at most 0.05 inches surface error and 1% volume error.
Native convex-polyhedron serialization retains those prepared shapes after
compilation, avoiding the compiler's coarse convex rebuilding. The decoded PHY
audit checks individual shape separation, surface bounds, volume, body masses
and constraint values. Analytic tessellation and required thickness repairs
remain separately documented.
Spheres and capsules can use widened temporary compiler inputs; finalization
restores their original dimensions and records both meshes in the audit.
Coupled swing cones become independent-axis envelopes, motors become calibrated
friction where possible, and weighted attachments receive an explicit binding
adaptation. The Alyx metrocop choreo model has no authored PHYS; its labelled
generated fallback uses its 18 AO capsules and a humanoid joint preset.

Joint collision flags are preserved. Alyx PHYS does not supply a complete body-pair
table; the converter previously inferred contact between every non-neighbor body.
Recognized humanoids instead use a tested Source 1 support-contact policy to avoid
internal pinching and violent solver motion. Reports list every retained and excluded
inferred pair separately from authored flags. Unclassified rigs keep the original
inference. World contact and collisions with other entities remain unchanged.

Source 1 skinning supports three bone influences per vertex. The port retains
the strongest three and renormalizes them; reports measure affected vertices,
discarded weights and normalization changes separately for primary and authored
LOD meshes. The extracted sources retain every original influence.

Only successful compiled models with complete materials enter the addon.
Binary audits check skeleton binds, bodygroups, skins, material resources,
LODs, attachments, hitboxes, flex data and physical bodies. Reports keep actual
GMod rendering, movement and physics validation separate from compiler checks.
Source models are limited to 256 total bones and 32 simulated bodies.

### Native model porting

**Model Porter** now has a native mode alongside the ValveBiped character path.
VRF Batch Export writes `.metadata.json` sidecars with authored attachments,
hitboxes, animation frame rates and flags, bone masks and pose parameters.
Pose-parameter ranges are declared in QC; sequence blend graphs still need
manual authoring. Native mode writes the original skeleton, normals, UVs and sequence names,
including distinct `@` names, and handles Source 2 Viewer exports in either
inches or baked metres. It refuses unsupported bind or animated scaling, additive
clips and Source 2 physics joints instead of silently changing them.
Model-space attachments receive an explicit stationary helper bone while all
authored bones retain their identities and hierarchy.

Filtered VPK model exports automatically preserve supported authored lower LOD
DMX meshes and write `.lods.json` sidecars with their switch values. Multi-mesh
LOD groups produce a warning and require manual composition. Large meshes are divided into corresponding parts at every
LOD so StudioMDL CE cannot leave high-detail pieces behind. StudioMDL can remove
degenerate faces and weld nearby LOD vertices. Source 1 switch distances need
asset-specific tuning because Source 2 uses screen coverage.

The main model converts rigid collision geometry from the exported physics GLB.
Bone-driven convex collision comes directly from the PHYS sidecar, preserving up
to 32 named parts and the separate hulls within each part. It uses
`$concaveperjoint` and collision smoothing groups to keep touching hulls distinct.
Thin hulls receive documented repairs to meet the Source 1 minimum thickness.
Use `prop_dynamic` bone followers for animated collision. These kinematic sources
are not ragdolls: no explicit joint constraints are invented, and compiler
zero-limit defaults apply only where a physical parent survives compilation.
Mass is calculated from Source 1 surface properties; authored
Rubikon mass and solver behavior are not preserved. A missing physics companion
leaves the main model without collision. Selecting ragdoll bones creates an
explicitly generated companion with separate hulls and adjustable mass and joint
limits.

Enable **Dynamic prop only** to retain authored animations and supported collision
without adding a synthetic ragdoll sequence or generating a ragdoll companion.
The Python API exposes the same choice as `dynamic_prop=True`. Bone-driven
collision still uses the engine's bone followers.

The Alyx scanner verified this path with 62 bones, 52 source animation names,
eight attachments, two authored hitboxes and four mesh detail levels. Material
conversion also carries literal texture constants, glass tint and phase-driven
waveform emission. Source 1 lighting is an approximation; Source 2 animation
events, particles and animation graphs still require separate translation.

The crafting station extends that coverage to 186 authored bones, 50 animation
names, 41 attachments and 75 collision hulls on 30 bones. Its `vr_complex`
emission uses exposure stops, so a brightness of zero means one-times intensity.

A port runs down two tracks. VRF Batch Export splits a Source 2 VPK into
`modelsrc/` and `materialsrc/`; the model track turns the first into something
studiomdl compiles, and the material track turns the second into the VMTs and
VTFs that compiled model looks for.

### Models

1. **VRF Batch Export** - Source 2 VPK -> `modelsrc/` + `materialsrc/`.
2. **glTF Batch SMD** - `modelsrc/*.glb` -> SMD meshes, animation SMDs and a
   QC that studiomdl compiles. For props.
3. **Model Porter** - skinned GLB -> native bones and authored clips, or a
   ValveBiped character using a donor animation library. Native mode can also
   create a separate generated ragdoll.

See [Porting a character](#porting-a-character) for the two skeleton workflows
and their physics limits.

### Materials / PBR

1. **VMAT Converter** - `materialsrc/*.vmat` -> VMT + VTF, carrying the source's
   transparency, glow, tint and PBR constants across.
2. **Texture Set Converter** - the same conversion for loose textures with no
   vmat, grouped into material sets by filename.
3. **Material Builder** - one material, maps picked by hand. The tuning tool.
4. **Material Extractor** - a built material back into PBR PNGs.

### Materials

**Texture Converter**, **VTF Resizer**, **Alpha Mask**, and the **Hotspot
Editor** below.

### Hotspot texturing

Hotspot texturing is one texture full of trim and panel regions plus a list of
where those regions are. Apply the material in Hammer++ or Strata, press
**Fit**, and it snaps each face's UVs onto whichever rectangle best matches its
size and aspect: one material, one draw call, no manual aligning. Valve leaned on
it heavily for Half-Life: Alyx's modular geometry.

The Hotspot Editor authors those rectangle lists. Its Run tab is a canvas rather
than a form - the one place this app breaks its own shape, because there is no
honest way to draw a rectangle on a texture with a parameter field. Save is still
a real job, so it logs, lands in history, and previews what it wrote.

Three things worth knowing:

* **Two incompatible formats share the `.rect` extension.** Source 2 writes KV3
  with coordinates as a fraction of the texture (32768 = the full extent, so the
  pixel size is nowhere in the file); Hammer++ writes plain text in pixels and is
  referenced from the VMT as `%rectanglemap` - percent, not dollar. **Open** reads
  either and converts, so a Half-Life: Alyx sheet's rectangles can be imported
  rather than redrawn by eye. Saving reports anything the target format cannot
  express instead of dropping it silently.
* **Detection is hierarchical.** With alpha, each connected opaque island becomes
  a rectangle. Without it, the sheet is cut along rows and columns that are one
  flat colour - the gutters between panels - and each half is then cut again. That
  recursion is what handles a sheet that is bands-then-cells; a single pass only
  ever finds the seams that happen to span the whole texture.
* **Overlap is never reported as a problem.** Authoring several candidate
  rectangles over the same art is how Fit gets something to choose between, and
  Valve's own sheets overlap heavily. Coverage is measured per pixel rather than
  by summing areas, so overlap does not read as 160% of the texture.

Editing keys are what you would expect - Delete, arrows to nudge by a pixel
(Shift by a cell), Ctrl+A/D/Z/Y/S, F to fit, G for the grid - and they stand
down while a text field has focus, since Flet hands key events to the page
rather than to whatever is focused.

The grid is configurable under the tool's **Settings** - style (lines, dots or
crosses), colour, strength and how often a line is emphasised - and the changes
land on the texture as you make them. It is not decoration: white lines vanish
on a blown-out tile sheet, which is the moment you need them. Whatever the cell
size, the grid only draws while the lines are far enough apart to read, so a
4 px grid is safe to leave switched on.

The **Fit preview** answers the question a trim sheet exists to answer: type a
face size and it ranks the rectangles Hammer would pick. If nothing scores well
for a long thin wall, the sheet needs a long thin rectangle - and you find that
out here rather than in the map.

### Sounds

**Sound Optimizer** (see below), **Sound Converter**, **Loop Point
Converter**, **Quad to Stereo**, **Soundscape Porter** (see below).

### Utility

**Filename Sanitizer**, **Search & Replace** (see below), **Asset Inspector**.

The remaining v2 tools port the same way: move the Qt-free logic into
`pipelines/`, then write a schema and a `run_job`.

### Porting a character

Use **Model Porter** after VRF Batch Export. Choose the mode according to the
animation system the character needs:

* **Native model and animations** keeps the exported skeleton and authored
  clips, with metadata from the adjacent `.metadata.json`. It supports rigid
  source collision, bone-driven convex hulls from the `_phys.txt` sidecar, and
  an optional separate ragdoll generated from selected bone weights. Animated
  collision requires `prop_dynamic` bone followers. Source 2 physics joints
  still require explicit joint-limit conversion.
* **ValveBiped character** uses a donor `.mdl` library's bind, proportions and
  animations, with one global mesh alignment and required auxiliary bones.
  Pose parameters come from `$includemodel`. This path does not import source
  physics or generate articulated collision; physics and NPC/playermodel
  registration require separate authoring.

Native mode handles Source 2 Viewer exports in Source inches or baked metres.
ValveBiped mode currently refuses baked-metre Source 2 Viewer exports; use an
export in Source units for that path. The Scale field applies only to glTFs
without Source units in ValveBiped mode. Compile with a compatible StudioMDL,
then verify animation, materials and collision in the target game.

### Faking PBR on Source 1

Source 2 authors materials the modern way - albedo, normal, roughness, metallic,
AO, each its own map. Source 1 has no metallic workflow at all: it has Phong, a
cubemap and a rim term, and each reads one channel of one texture. Converting
means baking the PBR inputs down into that layout:

| File | RGB | Alpha |
| --- | --- | --- |
| `_color.vtf` | albedo, AO and metal darkening baked in | opacity or tint mask |
| `_normal.vtf` | tangent normal | Phong mask |
| `_phong.vtf` | R exponent, G metallic | - |
| `_envmask.vtf` | reflection tint | - |

The approximation is not energy-conserving and cannot be - Source 1 lights
diffuse and specular independently, so the metal energy split has to be faked by
darkening the albedo and pushing the difference into Phong and the cubemap.
Three knobs decide where that faked energy lands: **metal diffuse suppression**,
**coloured metal relief** (so gold and copper keep their hue), and **Phong tint
compensation** (`$phongalbedotint` makes the engine multiply Phong by the albedo
at runtime, so the mask divides it back out).

The **Exo PBR** mode targets the ExoPBR screenspace shader instead, which does
real PBR from a packed ARM map. Better output, but the shader has to be
installed on the target.

Four things the material tools do that are worth knowing:

* **The normal's green channel is flipped.** Source 2 authors tangent normals
  green-up (OpenGL, `+Y`) and Source 1 wants them green-down (DirectX). Copying
  one across unchanged leaves every bump on the model reading inside out - the
  most common way a ported material looks subtly wrong without looking broken.
  The flip is on by default. A map whose filename already says `_dx` is taken at
  its word and left alone, and **Normal green channel** forces it either way.
  The Material Extractor flips back, so convert -> extract round-trips.

  Per [VDC](https://developer.valvesoftware.com/wiki/Bump_map): *"Source and
  Source 2 (even with DX11) have opposite interpretations of green channels in
  normal maps, with Source taking the DirectX format and Source 2 taking the
  OpenGL format."* Source 2's own texture compiler names the opposite setting
  *"Legacy Source 1 inverted normals"*, which is the same statement from the
  other side.

* **Nothing is emitted speculatively.** A material with no roughness or metallic
  gets no Phong block and no `_phong.vtf`; a flat normal gets no `$bumpmap`; a
  mask that bakes to pure black is dropped along with the parameter that would
  have referenced it. The Output tab lists every drop and why. Writing a
  flat-default map instead fakes a response the source never asked for.
* **The engine branch gates the parameters.** `$phongalbedotint`,
  `$blendtintbybasealpha`, float `$envmapfresnel` and Phong on
  `LightmappedGeneric` all postdate the HL2 shaders, and a branch that lacks one
  ignores it silently rather than erroring. The branch table refuses to write
  them, which is the difference between a material that looks wrong and one that
  looks wrong *for a discoverable reason*.
* **Ambiguity is reported, not resolved.** Two files claiming one role equally
  strongly - `foo_normal.png` beside `foo_nrm.png` - is a real question about
  which map the artist meant. The set records a warning and leaves the role
  empty rather than picking by filename length.

### Sound optimisation

The Sound Optimizer applies the passes the GMod content optimisers converged
on - Vorbis re-encode at `-q:a 4`, sample-rate capping to Source's native
44.1/22.05/11.025 kHz, mono downmix, `loudnorm=I=-14:TP=-1.5:LRA=11`,
`-map_metadata -1`, and a -55 dBFS trailing-silence trim with a 200 ms fade.
On a typical WAV folder that is an 80-90% saving.

Two things it adds on top:

* **Loop metadata is sacred.** A WAV carrying a `smpl` or `cue` chunk is how
  Source knows where an ambience loops, and Ogg Vorbis cannot carry it. Those
  files are detected and skipped rather than silently broken - which is also
  why the Loop Point Converter exists as its own tool.
* **References are rewritten.** Renaming `foo.wav` to `foo.ogg` breaks every
  `EmitSound`/`sound.Add` naming it, so matching paths in `.lua`, `.txt`,
  `.json`, `.res` and `.cfg` are updated to match.

The dry run projects the saving from the target bitrate rather than encoding
(within a few percent in practice), and byte-identical duplicates are reported
separately - the same footstep copied into six folders is usually the single
biggest win in a scraped addon.

Techniques adapted from [fOptimizer](https://github.com/fxington/fOptimizer),
[gm_content_optimizer](https://github.com/wrefgtzweve/gm_content_optimizer) and
[GMod-Optimization-Tool](https://github.com/bleonheart/GMod-Optimization-Tool).

### Soundscape porting

Half-Life: Alyx keeps its soundscapes in `scripts/soundscapes_*.txt` - the same
KeyValues container Source 1 uses, and never compiled. What changed is the body:
where Source 1 names a `.wav` inside a `playlooping` rule, Source 2 writes a
`playevent` naming a *sound event*, and that event lives in a KV3 `.vsndevts`
file which resolves - usually through a `base` chain and a set of children - to
the actual `.vsnd` files.

The Soundscape Porter is a browser over that. Point it at a game, press
**Load**, and it indexes every soundscape the game ships (472 for Alyx) with
what each one plays; tick the ones you want and port. Everything picked lands in
one script file, with its audio, as an addon folder you can drop into
`garrysmod/addons/`.

| Source 2 | Source 1 |
| --- | --- |
| A looping event with layered stems | one `playlooping` per stem, volumes multiplied |
| A random ambience | one `playrandom` with the takes in an `rndwave` block |
| A quad or surround bed | the front pair only |
| Rules already in Source 1 form | passed through untouched |

Four things about Valve's data drove the format layer, all of them checked
against the shipped corpus (21 soundscape files, 129 sound-event files, 12,521
events):

* **Both formats are mixed inside one file.** `soundscapes_a2.txt` holds 266
  `playevent` blocks and 40 Source 1 rules, so the Source 1 ones are passed
  through rather than round-tripped. The HL2 files that came along for the ride
  (canals, citadel, prison, ...) are pure Source 1 and make a free regression
  corpus - the port must leave them structurally identical.
* **Valve comment blocks out in place.** A parser that ignores `//` emits
  sounds the game never plays.
* **`base` inheritance is load-bearing.** 2,692 events inherit, chains up to
  five deep; following them recovers about 56,000 fields - the volumes and file
  lists a non-inheriting parser silently drops.
* **Sound events are referenced by several key spellings** - `soundevent_01`,
  the plural `soundevents` (string *or* array), `random_soundevent_01_name`.
  `limiter_event_name` also names an event but is a ducking reference, not
  audio that plays, so it is deliberately not followed.

Three traps are worth stating outright, because each fails silently in-engine
rather than erroring:

* **Paths.** Source 2 audio lives under `sounds/` while a Source 1 `wave` is
  relative to `sound/`, so carrying the prefix through asks for
  `sound/sounds/...` and plays nothing.
* **Namespacing.** Half-Life 2 already owns `sound/ambient/` and Alyx reuses
  many of the same names, and soundscape names are global too. Audio is written
  under `sound/<namespace>/` and every soundscape takes a name prefix, so an
  installed addon cannot replace stock ambience.
* **The manifest.** Garry's Mod loads soundscapes from a single
  `scripts/soundscapes_manifest.txt` listing 89 files. An addon shipping its own
  *replaces* that list and disables every stock soundscape, so the default reads
  the stock manifest out of `garrysmod_dir.vpk` and writes it back with the new
  file appended - 90 entries, not 1.

Looping needs a WAV: Alyx stores audio as MP3 and Source only loops a wave
carrying a `smpl` chunk, so anything a `playlooping` uses is transcoded and
given a loop point while one-shots are left as they decoded.

What does not survive: Source 2's dynamic mixing has no Source 1 equivalent, so
opvar-driven volume curves, HRTF, occlusion and falloff curves are dropped. The
result is the bed, not the mix.

`pipelines/soundscape.py` is the whole format layer and is Flet-free;
`tests/test_soundscape.py` pins it, and will additionally run over Valve's own
files if `S2KIT_SOUNDSCAPE_CORPUS` points at an extracted `scripts/` +
`soundevents/` tree.

### Bulk renaming

Search & Replace applies one search term to file names, folder names and file
contents in a single pass. v2 split that across two tools that shared most of
their code and disagreed about the rest, so renaming `hlvr_` to `hla_` across a
port meant two runs, two blacklists and two undo buttons that each knew about
half of it.

Four things it does that v2's pair did not, each a real way to lose work:

* **Folders are renamed deepest-first.** v2 renamed top-down, so renaming a
  parent invalidated every child path still queued behind it.
* **Contents are read and written as bytes.** `Path.write_text` translates `\n`
  to `os.linesep`, so v2 rewrote every LF file in a tree to CRLF the moment one
  line matched. Line endings and BOMs now survive untouched.
* **Collisions are a decision.** `Path.rename` overwrites on POSIX and raises on
  Windows; the tool skips or suffixes, and a case-only rename is not mistaken
  for a collision.
* **Undo survives a restart.** Every run that changes something writes a journal
  to the cache with the before/after of each rename and the original bytes of
  each rewritten file. It replays in reverse, which is what makes a file that
  was edited *and* renamed *and* moved with its folder recoverable.

The exclude patterns keep v2's shorthand - a pattern with `*`, `?` or `[` is a
glob, anything else is a case-insensitive substring - but they now match every
folder on the way down, so excluding `backup_*` skips the whole subtree instead
of just the folder itself.

### Behaviour changes from v2

* **`$mass` is in kilograms now.** The density table is specific gravity
  (g/cm3) but the volume is in cubic inches, and v2 multiplied the two
  directly - overstating every mass by ~61x, so a 16-inch wooden crate came
  out at 3082 kg. QCs regenerated here will differ from v2's.
* **Collision normals** are computed with one vectorised numpy pass instead of
  a per-vertex Python dict; same output, much faster on dense hulls.
* **Conversions run in parallel** (see the tool's Settings tab), and the
  per-model orchestration is a plain function rather than a QThread, so it is
  callable from a test or a CLI.
* **The material tools are renamed and merged.** v2's names described the
  technique; these describe what goes in and what comes out.

  | v2 | v3 |
  | --- | --- |
  | VMAT PBR Batch | VMAT Converter |
  | Fake PBR *(Automate tab)* + Texture Folder PBR Batch | Texture Set Converter |
  | Fake PBR *(Manual tab)* + Manual PBR Converter | Material Builder |
  | Fake PBR Reverse | Material Extractor |
  | VTF Clamp | VTF Resizer |

  Exo PBR is no longer a separate tool - it is a mode on each converter, which
  is what it always was underneath. v2's two folder scanners did the same job
  with different keyword tables and only one understood packed ORM maps; they
  are now one scanner that does.
* **Search & Replace (Files) and Search & Replace (Folder) are one tool.** They
  were one operation on three kinds of target all along - see *Bulk renaming*
  above for what the merge fixed.
* **Packed ORM never touches disk.** v2 split an ORM into three temporary PNGs
  under a `__tmp_orm` folder and read them back. The builder accepts arrays
  directly, so the split happens in memory.
* **Per-output resolution caps are per-tool preferences**, not a global setting
  block. They still lower the *working* resolution as well as the output size,
  since computing above the largest cap that will be written buys nothing.

## Running external programs

`core/process.run_streamed(args, on_line, cancel=...)` is the one way tools
shell out. It forwards stdout+stderr line by line, hides the console window on
Windows, and - the part that matters - kills the child (and its children, via
`taskkill /T`) the moment the job's cancel event is set, so Cancel is never
blocked waiting on a quiet pipe.

For batch work, `ctx.map(items, worker)` runs a bounded thread pool that
honours the `max_parallel` preference, reports per-item progress, isolates
per-item failures and stops cleanly on cancel.

## Viewers

* **Texture** - anything Pillow reads, plus VTF via `sourcepp`: mip and frame
  selection, channel isolation (R/G/B/A/luminance), alpha-checker background,
  pan/zoom, log-scale histogram, full header info.
* **Model** - SMD parsed natively (bones and materials included), GLB/OBJ/STL/
  PLY/DAE via `trimesh`. Rendered by a numpy z-buffer rasteriser with orbit and
  zoom, shaded/wireframe/normals/depth modes. No GPU context needed. Triangles
  are bucketed by screen size and each bucket is rasterised in one vectorised
  pass, which is ~11x faster than per-triangle work on a dense mesh (60k faces:
  2.4 s -> 0.2 s) and pixel-identical to it.
* **Sound** - waveform with an RMS envelope, container/codec/loop-point
  metadata, transport. WAV needs nothing; other formats use ffmpeg.
* **Text / data** - QC, VMT, VMAT, KV3, SMD, logs with line numbers and search;
  CSV/TSV/JSON render as a sortable table.

## Guided port projects

The **Porting Workflow** tool plans a port from exported GLB/glTF or VMAT
sources, a compiled Source 2 asset, or a filtered VPK. Choose model sources,
compiled models, or materials as the target, and a project folder outside the
input tree. **Plan and check** reports suggested stages, missing resources,
skeleton structure/weight issues, and compiler setup. Checks that require
extraction are explicitly deferred until **Run / resume**.

The serial queue carries outputs through the existing exporters and converters.
`workflow-queue.json` saves each item's outcome. Resume verifies output hashes,
skips intact successes, and retries failed or interrupted items; a failed item
blocks its dependencies while independent items continue. Source size or
modification-time changes rebuild the queue. Changed settings require a new
project folder. Repeated extraction preserves the old tree in an
`extracted-previous-*` folder, preventing stale files from entering the new run.

`port-report.md` and `port-report.json` list findings, results, files, and checks
still needed. Materials are staged in the project; compiling writes models to
the selected game. This workflow preserves native skeletons. Use Model Porter
in ValveBiped mode for donor-library playermodel work. Material binding, animation alignment, and
in-game appearance/collision still require review.

Headless regression tests: `python -m pytest v3/tests/test_workflow.py -q`
(from the repository root). They exercise real synthetic GLB conversion plus
checkpoint, cancellation, locking, preflight, and resume behavior without
opening the app.

## Launch flags

| Flag | Effect |
| --- | --- |
| `--check` | Print the environment report and exit |
| `--install` | pip-install missing required packages first |
| `--install-optional` | Also install the optional packages |
| `--self-test` | Headless: mount every page, run a real job, drive the viewers |
| `--headless` | Run with a hidden window |
| `--web` / `--port N` | Serve the UI in a browser instead |
| `--tool <id>` | Open straight into a tool |
| `--log-level` | Console log level (the file log always gets DEBUG) |

`S2KIT_SELFTEST_DIR` points the self-test's scan at a folder of real assets;
`S2KIT_SELFTEST_SHOT` saves a screenshot of the result.

## Flet 0.86 notes

Three framework behaviours are load-bearing here and are easy to trip over
again (the first two are documented in
[s2kit/ui/controls.py](s2kit/ui/controls.py)):

1. **Custom controls subclass `ft.Container`, never `ft.Column`/`ft.Row`.** A
   `@ft.control` subclass of a flex control does not apply `expand` to its
   children, so layouts silently overflow. Put a plain `Column` in `content`.
2. **Python-side fields need `state()`** (`metadata={"skip": True}`) or the
   first update fails to serialise them.
3. **Give form controls a stable `key`.** Swap one schema's controls for
   another's at the same position and Flutter recycles the widget - which then
   reports its state to the *old* param's handler. That silently wrote
   preferences nobody chose until every field got a `ValueKey`.

And one Flet API trap worth remembering: `SegmentedButton.selected` must be a
**list**, not a set. A set cannot be msgpack-encoded, and the failure takes the
whole UI patch with it - the page silently keeps rendering its previous
contents.
