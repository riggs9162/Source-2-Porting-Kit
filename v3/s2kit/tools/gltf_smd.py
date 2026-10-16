"""
glTF Batch SMD.

Stage two of the port: turns the `modelsrc/` tree VRF Batch Export wrote into
SMD meshes, animation SMDs and a QC that studiomdl can compile. Point it at a
folder, and every glTF/GLB under it becomes a compilable model.

The conversion itself lives in :mod:`s2kit.pipelines.gltf_smd`; this file is
the parameter schema plus the batch orchestration.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Optional

from s2kit.core.jobs import JobCancelled, JobContext
from s2kit.core.params import (
    BoolParam, ChoiceParam, FloatParam, IntParam, PathParam, TextParam, options,
)
from s2kit.core.registry import tool
from s2kit.media.probe import human_size
from s2kit.pipelines import gltf_smd as pipeline
from s2kit.ui.tool_page import ToolPage

UV_MODES = options(
    ("preserve", "Preserve (as authored)"),
    ("wrap", "Wrap into 0-1"),
    ("clamp", "Clamp to 0-1"),
    ("normalize", "Normalise to 0-1"),
)


@tool(
    id="gltf-smd",
    title="glTF Batch SMD",
    category="Models",
    icon="CHANGE_CIRCLE",
    description="Convert exported glTF/GLB models into Source 1 SMD meshes, "
                "animations and a QC.",
    keywords=("gltf", "glb", "smd", "qc", "studiomdl", "convert", "compile",
              "physics", "collision", "animation"),
    order=20,
)
class GltfBatchSmd(ToolPage):
    """Batch glTF -> SMD/QC conversion."""

    RUN_LABEL = "Convert"
    RUN_ICON = "CHANGE_CIRCLE"

    PARAMS = [
        PathParam("input", "Model source folder", mode="directory", required=True,
                  group="Input",
                  help="Usually the modelsrc/ folder VRF Batch Export wrote."),
        BoolParam("recursive", "Include subfolders", default=True, group="Input"),
        TextParam("filter", "Only these models", default="", group="Input",
                  hint="crate*, *_door, combine_*",
                  help="Comma-separated glob patterns matched against the model "
                       "name. Empty converts everything found."),

        PathParam("output", "Output folder", mode="directory", required=True,
                  must_exist=False, group="Output"),
        BoolParam("preserve_folders", "Mirror the input folder structure",
                  default=True, group="Output"),
        BoolParam("replace_existing", "Overwrite existing SMD/QC", default=True,
                  group="Output"),
        BoolParam("dry_run", "Dry run (report what would be written)",
                  default=False, group="Output",
                  help="Scans and resolves every output path without loading "
                       "meshes or writing files."),

        BoolParam("generate_smd", "Write SMD meshes", default=True,
                  group="What to write"),
        BoolParam("generate_qc", "Write QC", default=True,
                  group="What to write"),
        BoolParam("export_physics", "Write a collision mesh", default=True,
                  group="What to write",
                  help="Uses the _physics glTF when present, otherwise the "
                       "render mesh, so the prop still gets a real "
                       "$collisionmodel."),
        BoolParam("export_animations", "Write animation SMDs", default=True,
                  group="What to write",
                  help="Skinned models keep their skeleton either way; this "
                       "controls the per-clip animation SMDs."),
        FloatParam("animation_fps", "Animation FPS", default=30.0, min=1.0,
                   max=120.0, step=1.0, decimals=1, group="What to write",
                   depends_on=("export_animations", True)),
        BoolParam("auto_loop_detect", "Detect looping clips by name",
                  default=True, group="What to write",
                  depends_on=("export_animations", True)),

        FloatParam("scale", "Scale", default=40.0, min=0.001, max=10000.0,
                   step=1.0, decimals=4, group="Geometry",
                   help="glTF metres to Source inches. Ignored for Source 2 "
                        "Viewer exports already in Source units, unless you "
                        "force it below."),
        BoolParam("axis_conversion", "Convert Y-up to Z-up", default=True,
                  group="Geometry"),
        ChoiceParam("uv_mode", "UV handling", default="preserve",
                    choices=UV_MODES, group="Geometry"),
        BoolParam("flip_v", "Flip V", default=False, group="Geometry"),
        BoolParam("force_manual_scale", "Force the manual scale", default=False,
                  group="Geometry", advanced=True,
                  help="Applies the scale even to Source-units glTFs. Their "
                       "orientation is left alone either way."),

        TextParam("modelname_prefix", "$modelname prefix", default="",
                  group="QC", hint="props/myaddon",
                  help="Empty derives it from the output subfolder."),
        TextParam("cdmaterials", "$cdmaterials", default="models/", group="QC"),
        BoolParam("concave", "$concave collision", default=True, group="QC"),
        BoolParam("auto_surfaceprop", "Detect $surfaceprop", default=True,
                  group="QC",
                  help="Reads the surface property Source 2 recorded for the "
                       "collision - world.metal_combine, world.plastic - and "
                       "maps it to the Source 1 equivalent. Only a model that "
                       "declares nothing usable falls back to keywords in its "
                       "name."),
        TextParam("static_surfaceprop", "$surfaceprop", default="metal",
                  group="QC", depends_on=("auto_surfaceprop", False)),
        TextParam("surfaceprop_override", "Force $surfaceprop", group="QC",
                  lines=2, hint="metal      or      *door*=metal, *glass*=glass",
                  help="Wins over both the detector and the fixed value above. "
                       "A bare value forces itself on every model; "
                       "'pattern=prop' entries force one only where the model "
                       "name matches, so the automatic choice can stand "
                       "everywhere it is right. Patterns are case-insensitive "
                       "substrings, or globs when they carry * or ?, separated "
                       "by commas or newlines, first match wins."),
        BoolParam("auto_mass", "Calculate $mass from volume", default=True,
                  group="QC",
                  help="studiomdl's own $automass rule: collision volume times "
                       "the surfaceprop density, or surface area times its "
                       "shell thickness for hollow materials like metal and "
                       "glass. Kilograms."),
        FloatParam("mass_modifier", "Mass multiplier", default=1.0, min=0.001,
                   max=1000.0, step=0.1, decimals=3, group="QC",
                   depends_on=("auto_mass", True)),
        FloatParam("static_mass", "$mass", default=10.0, min=0.001, max=10000.0,
                   step=1.0, decimals=2, group="QC",
                   depends_on=("auto_mass", False)),

        BoolParam("decompose_collision", "Cut concave collision up",
                  default=True, group="QC",
                  help="Source 2 collision is often one concave triangle "
                       "soup; Source 1 hulls each connected element, so a "
                       "doorframe compiles as a solid slab with the doorway "
                       "filled in. This cuts the concave parts into pieces "
                       "whose hulls fit the shape."),
        IntParam("max_convex_pieces", "Convex piece budget", default=40,
                 min=1, max=256, group="QC", advanced=True,
                 depends_on=("decompose_collision", True),
                 help="studiomdl's own default is 40. Above it the compiler "
                      "warns about a costly collision model and the QC needs "
                      "$maxconvexpieces, which is written for you."),

        BoolParam("remove_2d_collision", "$remove2d (drop thin pieces)",
                  default=False, group="QC", advanced=True,
                  help="Discards any convex piece under half an inch thick. "
                       "Rarely does anything now: pieces that thin are "
                       "inflated to 0.55in first, because leaving one in makes "
                       "studiomdl hull the entire model. Removing them instead "
                       "deletes the collision for thin panels, signs and "
                       "grates, so this stays off by default."),

        BoolParam("collision_from_render", "Invent collision from the render mesh",
                  default=True, group="Physics guards", advanced=True,
                  help="For a model the source gives no collision at all. "
                       "Source 2 leaves those non-solid - they are decoration "
                       "there - but a prop placed in a map usually has to hold "
                       "a player up. Off writes no $collisionmodel for them, "
                       "which is the source's own answer. Either way, a physics "
                       "glTF that failed to *read* still falls back."),
        BoolParam("skip_physics_for_large", "Skip collision on huge models",
                  default=True, group="Physics guards", advanced=True,
                  help="vphysics can crash on collision meshes the size of a "
                       "skybox or the Citadel."),
        FloatParam("large_model_threshold", "Size threshold",
                   default=1024.0, min=64.0, max=100000.0, step=64.0,
                   decimals=0, group="Physics guards", advanced=True,
                   suffix="units", depends_on=("skip_physics_for_large", True)),
    ]

    PREFERENCES = [
        IntParam("workers", "Models converted in parallel", default=4, min=1,
                 max=32, group="Performance",
                 help="Mesh loading is the slow part; 4 is a good balance on "
                      "most machines. Set 1 if a model crashes the loader and "
                      "you want a clean log."),
        BoolParam("verbose", "Verbose per-model logging", default=False,
                  group="Logging"),
        BoolParam("list_files", "List every written file in a table",
                  default=True, group="Report"),
        BoolParam("preview_results", "Add converted models to Preview",
                  default=True, group="Report",
                  help="SMD opens in the model viewer, QC in the text viewer."),
        IntParam("preview_limit", "Preview at most", default=6, min=0, max=100,
                 suffix="models", group="Report"),
    ]

    HELP = """
### glTF Batch SMD

Takes the glTF/GLB files VRF Batch Export produced and writes the Source 1
source files studiomdl needs.

#### What a model set is

A render mesh plus an optional collision companion, matched by name:

```text
crate.glb            <- render
crate_physics.glb    <- collision  (optional)
crate.skins.json     <- $texturegroup skins  (optional, written by VRF export)
```

Each set produces `crate.smd`, `crate_physics.smd` and `crate.qc`, plus one
`crate_anim_<clip>.smd` per animation on a skinned model.

#### Things it handles for you

* **Source 2 Viewer units.** Up to 19.2, S2V wrote raw inches and put the
  metres-and-Y-up conversion on a root node as a matrix - which trimesh
  applies for static models but drops for skinned ones. Those files are
  detected and neither scaled nor rotated. **Version 20 bakes that conversion
  into the render mesh instead**, while still exporting physics meshes the old
  way, so a model that took the exporter at its word compiled 39.37x too small
  inside a correctly sized collision hull. The space is now read per file from
  the matrix rather than from the version, and a baked mesh is scaled by
  39.3701 and turned back through S2V's own basis. *Force the manual scale*
  overrides only the scale, never the orientation.
* **Concave collision.** Source 2 stores a lot of collision as one
  `m_meshes` triangle soup - arbitrary concave geometry, which Rubikon takes
  as-is for anything static. Source 1 has no such shape: studiomdl hulls each
  connected element of the collision SMD, so a doorframe compiles as a solid
  slab with the doorway filled in and nothing in the compile mentions it.
  *Cut concave collision up* finds the axis-aligned cuts that give the most
  of that volume back, spending up to the piece budget on the parts that pay
  best. Pieces are pushed past their cut plane so they overlap rather than
  touch - touching pieces share vertices, studiomdl welds those, and the two
  hulls would fuse back into the one hull the cut existed to avoid. A
  component that was already convex keeps its own triangles.
* **Openings.** Part of what a Source 2 collision surface describes is
  absence: the reveal around a doorway is four strips forming an open tube,
  and the doorway is the space between them. Hulling that tube fills the
  doorway in, and cutting it up does not help - halving a frame leaves two
  halves whose hulls still cover the opening, so the volume a cut saves is
  zero and the search correctly refuses to make it. A hollow open surface with
  few enough flat faces is therefore rebuilt as one thin slab per face,
  following the surface instead of searching for it, and the opening is what
  is left between them.
* **Collision pieces too thin to be solids.** One piece under half an inch
  thick makes studiomdl abandon the per-element decomposition entirely and
  hull the whole mesh into a single convex piece - a doorway filled in, a
  window bricked up - reporting nothing but `WARNING: Model has 2-dimensional
  geometry`. Source 2 collision is full of them, because an `m_meshes` shape
  is a *surface*: a zero-thickness quad is a real barrier there and can be a
  pane of glass. Those pieces are inflated to 0.55 inches along their thin
  axis, which keeps the collision `$remove2d` would have deleted.
* **Models with no collision.** Source 2 Viewer writes a stub physics glTF for
  a model that has no collision data - Half-Life Alyx leaves those non-solid,
  because they are decoration sitting on map brushwork. A prop placed in a
  Hammer map usually has to hold a player up, so collision is built from the
  render mesh instead, and it goes through the same decomposition a real
  collision mesh does - hulling a shell per connected component is how you get
  a prop that is a box around itself. Turn *Invent collision from the render
  mesh* off for the source's own answer: no `$collisionmodel` at all.
* **Collision normals.** Physics meshes arrive pre-split into disconnected
  convex pieces with hard edges. Normals are re-averaged across shared
  *positions*, which is what Source reads to decompose the hull.
* **Huge props.** Anything past the size threshold gets no `$collisionmodel`;
  vphysics can crash the engine on collision that large.
* **Duplicate clips.** S2V exports some animations twice under an
  `@`-prefixed alias. The duplicate is skipped, because studiomdl rejects
  duplicate `$animation` names.

#### Choosing $surfaceprop

Source 2 records a surface property for the collision itself - the exporter
names each physics group `physics_group_world.metal_combine` and repeats the
property in the node's extras - so the material is read off the model rather
than guessed at. `world.metal_combine` becomes `combine_metal`, `world.plastic`
becomes `plastic`, and so on through the alias table.

Only a model that declares nothing usable falls back to keywords in its name,
and a name is a poor witness: `combine_hall_doorframe_b_windowed_wrapped`
contains "window", so it reads as `glass`. That is not just the wrong footstep -
`glass` is a shell material, so `$automass` switches from volume to surface area
and the compiled mass moves by a factor of five. A model whose collision
declares several properties resolves to the one covering the most vertices,
and says which ones it dropped, because Source 1 carries only one.

**Force $surfaceprop** overrides all of that:

* a bare value - `metal` - forces every model in the run;
* `pattern=prop` entries force only the models whose name matches:

```
*doorframe*=metal, *grate*=metalgrate, *_glass_*=glass
```

Patterns are case-insensitive substrings, or globs when they carry `*` or `?`.
Separate entries with commas or newlines; the first match wins, and a bare
value is the fallback for everything no pattern claimed. A value the kit has no
density for is still written, with a warning - the game may know it, but
`$mass` cannot be right for it.

#### $mass and the collision block

`$mass` follows studiomdl's own `$automass` rule rather than a guess. The
collision mesh is split into connected components and each is convex-hulled -
exactly what the compiler does - and then:

* **solid materials** (wood, concrete, flesh, rock) weigh `volume x density`;
* **shell materials** weigh `surface area x thickness x density`. Metal, glass,
  plastic, cardboard and tile are all hollow shells in Source's
  `surfaceproperties.txt` (metal is a 0.1 inch skin), so a drum or locker
  weighs what its walls weigh, not what a solid billet would.

Both branches were checked against Valve's own props: reproducing this on the
244 `$automass` models in the Half-Life 2 sources matches the shipped `.phy`
volume to a median 0.00002% and the shipped mass to within 2% on 90% of them.

studiomdl floors every solid at 1 kg, so smaller masses are reported rather
than silently ignored. `$concave` is written only when the mesh really has more
than one convex piece, and `$maxconvexpieces` only when the count exceeds the
compiler's default of 40 - which is also logged, because that is the point
where the prop becomes expensive to simulate. `$inertia`, `$damping` and
`$rotdamping` are left out entirely, matching 1587 of Valve's 1593 static
props; the compiler's defaults are 1.0 / 0 / 0.
"""

    # ------------------------------------------------------------------

    def run_job(self, ctx: JobContext) -> None:
        params = ctx.params
        input_root = Path(str(params["input"]))
        output_root = Path(str(params["output"]))
        if not input_root.is_dir():
            raise ValueError(f"Not a folder: {input_root}")

        ctx.status(f"Scanning {input_root}...")
        model_sets = pipeline.ModelSetScanner(
            input_root, recursive=bool(params["recursive"])).find_sets()
        ctx.info(f"Found {len(model_sets)} model set(s) under {input_root}")

        patterns = _patterns(str(params["filter"] or ""))
        if patterns:
            before = len(model_sets)
            model_sets = [ms for ms in model_sets if _matches(ms.name, patterns)]
            ctx.info(f"Filter {', '.join(patterns)} kept "
                     f"{len(model_sets)} of {before}")
        if not model_sets:
            ctx.warn("Nothing to convert.")
            ctx.summary("No model sets matched")
            return

        model_sets.sort(key=lambda ms: str(ms.render_path).lower())
        for model_set in model_sets:
            for warning in model_set.warnings:
                ctx.warn(f"{model_set.name}: {warning}")

        conversion = self._options(ctx)
        if conversion.dry_run:
            ctx.warn("Dry run - nothing will be written.")

        def convert(model_set):
            return pipeline.convert_model_set(
                model_set, conversion,
                input_root=input_root, output_root=output_root,
                on_log=ctx.info, cancel=ctx.cancel_event)

        workers = int(ctx.prefs.get("workers", 4))
        try:
            outcome = ctx.map(model_sets, convert, workers=workers,
                              label=lambda ms: ms.name, unit="model",
                              log_each=False)
        except pipeline.ConversionCancelled as exc:
            raise JobCancelled() from exc

        results = [r for r in outcome.results if r is not None]
        self._report(ctx, results, outcome, output_root)

    # ------------------------------------------------------------------

    def _options(self, ctx: JobContext) -> pipeline.ConversionOptions:
        """Parameters -> the pipeline's options object."""
        params = ctx.params
        return pipeline.ConversionOptions(
            scale=float(params["scale"]),
            axis_conversion=bool(params["axis_conversion"]),
            force_manual_scale=bool(params["force_manual_scale"]),
            flip_v=bool(params["flip_v"]),
            uv_mode=str(params["uv_mode"]),
            generate_smd=bool(params["generate_smd"]),
            generate_qc=bool(params["generate_qc"]),
            export_physics=bool(params["export_physics"]),
            export_animations=bool(params["export_animations"]),
            animation_fps=float(params["animation_fps"]),
            auto_loop_detect=bool(params["auto_loop_detect"]),
            modelname_prefix=str(params["modelname_prefix"]),
            cdmaterials=str(params["cdmaterials"]),
            concave=bool(params["concave"]),
            auto_mass=bool(params["auto_mass"]),
            mass_modifier=float(params["mass_modifier"]),
            static_mass=float(params["static_mass"]),
            auto_surfaceprop=bool(params["auto_surfaceprop"]),
            surfaceprop_override=str(params["surfaceprop_override"] or ""),
            static_surfaceprop=str(params["static_surfaceprop"]),
            decompose_collision=bool(params["decompose_collision"]),
            max_convex_pieces=int(params["max_convex_pieces"]),
            remove_2d_collision=bool(params["remove_2d_collision"]),
            collision_from_render=bool(params["collision_from_render"]),
            skip_physics_for_large=bool(params["skip_physics_for_large"]),
            large_model_threshold=float(params["large_model_threshold"]),
            preserve_folders=bool(params["preserve_folders"]),
            replace_existing=bool(params["replace_existing"]),
            dry_run=bool(params["dry_run"]),
            verbose=bool(ctx.prefs.get("verbose", False)),
        )

    def _report(self, ctx: JobContext, results: list, outcome,
                output_root: Path) -> None:
        converted = [r for r in results if r.status == "converted"]
        dry = [r for r in results if r.status == "dry-run"]
        skipped = [r for r in results if r.status == "skipped"]
        failed = [r for r in results if r.status == "failed"]
        written = [path for r in results for path in r.files]

        ctx.metric("Converted", len(converted) or len(dry),
                   tone="good" if (converted or dry) else "neutral")
        if skipped:
            ctx.metric("Skipped", len(skipped), tone="warn")
        if failed or outcome.failed:
            ctx.metric("Failed", len(failed) + outcome.failed, tone="bad")
        ctx.metric("Triangles", f"{sum(r.triangles for r in results):,}")
        animated = [r for r in results if r.bones]
        if animated:
            ctx.metric("Skinned", len(animated), tone="good")
            ctx.metric("Clips", sum(r.clips for r in results))
        if written:
            ctx.metric("Written",
                       human_size(sum(_size(p) for p in written)))

        ctx.table(
            "Models",
            ["Model", "Result", "Tris", "Bones", "Clips", "Collision",
             "Mass (kg)", "Surfaceprop", "Time", "Detail"],
            [[r.name, r.status, r.triangles, r.bones or "", r.clips or "",
              _collision(r), _mass(r), r.surfaceprop or "",
              f"{r.elapsed:.2f}s", r.reason or "; ".join(r.warnings)]
             for r in sorted(results, key=lambda r: r.name.lower())],
            numeric_columns=(2, 3, 4, 6),
        )

        if bool(ctx.prefs.get("list_files", True)) and written:
            ctx.table(
                f"Written files ({len(written)})",
                ["File", "Kind", "Size", "Path"],
                [[p.name, _kind(p), human_size(_size(p)), str(p)]
                 for p in sorted(written, key=lambda p: str(p).lower())],
                path_column=3,
            )

        for result in failed:
            ctx.error(f"{result.name}: {result.reason}")

        if bool(ctx.prefs.get("preview_results", True)):
            limit = int(ctx.prefs.get("preview_limit", 6))
            for result in converted[:limit]:
                for path in result.files:
                    if path.suffix.lower() in (".smd", ".qc"):
                        ctx.artifact(path, label=path.name)

        verb = "would convert" if dry else "converted"
        count = len(dry) if dry else len(converted)
        summary = f"{verb} {count}/{len(results)} model(s)"
        if skipped:
            summary += f", {len(skipped)} skipped"
        if failed:
            summary += f", {len(failed)} failed"
        (ctx.error if failed and not converted else ctx.success)(summary)
        ctx.summary(summary)


def _patterns(raw: str) -> list[str]:
    return [part.strip() for part in raw.replace(";", ",").split(",")
            if part.strip()]


def _matches(name: str, patterns: list[str]) -> bool:
    lowered = name.lower()
    return any(fnmatch.fnmatch(lowered, pattern.lower()) for pattern in patterns)


def _collision(result) -> str:
    if result.physics_fallback:
        return "render mesh"
    if result.has_physics:
        return "physics glTF"
    return "none"


def _mass(result) -> str:
    return f"{result.mass:.1f}" if result.mass else ""


def _kind(path: Path) -> str:
    name = path.name.lower()
    if name.endswith(".qc"):
        return "qc"
    if "_anim_" in name:
        return "animation"
    if name.endswith("_physics.smd"):
        return "collision"
    return "mesh"


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0
