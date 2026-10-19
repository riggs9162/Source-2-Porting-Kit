"""Shared material parameter and preference declarations."""
from __future__ import annotations
from s2kit.core.asset_defaults import BRANCH_LABELS, GLOW_MODES, METAL_DIFFUSE_SUPPRESSION, COLORED_METAL_RELIEF

from s2kit.core.params import BoolParam, ChoiceParam, FloatParam, IntParam, Param, TextParam, options

CLAMP_CHOICES = options(
    (0, "Off (native resolution)"), (128, "128"), (256, "256"), (512, "512"),
    (1024, "1024"), (2048, "2048"), (4096, "4096"), (8192, "8192"))

MODE_CHOICES = options(
    ("fake", "Fake PBR (stock shaders)"),
    ("exo", "Exo PBR (screenspace shader)"))

SHADER_CHOICES = options(
    ("VertexLitGeneric", "VertexLitGeneric (models)"),
    ("LightmappedGeneric", "LightmappedGeneric (brushes)"),
    ("UnlitGeneric", "UnlitGeneric (no lighting)"))

TINT_MODE_CHOICES = options(
    ("selective", "Selective - compensate coloured metals only"),
    ("blanket", "Blanket - compensate everywhere"),
    ("off", "Off - no compensation"))

NORMAL_GREEN_CHOICES = options(
    ("auto", "Flip, unless the filename says DirectX"),
    ("flip", "Always flip (Source 2 -> Source 1)"),
    ("keep", "Never flip (the map is already Source 1)"))

def conversion_params(*, group: str = "Conversion") -> list[Param]:
    """Mode, engine branch and shader - the choices that gate everything else."""
    return [
        ChoiceParam("mode", "Output", default="fake", group=group,
                    style="segmented", choices=MODE_CHOICES,
                    help="Fake PBR runs on any Source 1 build. Exo PBR is a "
                         "real PBR shader but has to be installed on the target."),
        ChoiceParam("branch", "Engine branch", default="gmod", group=group,
                    choices=options(*BRANCH_LABELS),
                    depends_on=("mode", "fake"),
                    help="Gates the parameters that older branches ignore "
                         "silently, like $phongalbedotint."),
        ChoiceParam("shader", "Shader", default="VertexLitGeneric", group=group,
                    choices=SHADER_CHOICES, depends_on=("mode", "fake")),
        TextParam("surfaceprop", "Surface property", group=group,
                  depends_on=("mode", "fake"), advanced=True,
                  hint="metal, concrete, flesh ...",
                  help="Written as $surfaceprop. Empty omits it."),
    ]

def fake_pbr_params(*, group: str = "Fake PBR") -> list[Param]:
    """The knobs that decide how the PBR energy is faked."""
    return [
        FloatParam("ao_strength", "AO strength", default=0.7, min=0.0, max=2.0,
                   step=0.05, decimals=2, group=group,
                   depends_on=("mode", "fake"),
                   help="How hard ambient occlusion is baked into the albedo."),
        FloatParam("ao_bokeh_strength", "Soft AO layer", default=0.35, min=0.0,
                   max=1.0, step=0.05, decimals=2, group=group,
                   depends_on=("mode", "fake"),
                   help="Blends a circular bokeh blur of AO into the albedo."),
        FloatParam("emissive_ao_relief", "Emissive AO relief", default=1.0,
                   min=0.0, max=1.0, step=0.05, decimals=2, group=group,
                   depends_on=("mode", "fake"),
                   help="How much AO to take back where the glow mask marks the "
                        "surface. A glowing panel makes its own light, so "
                        "occluding it only dims the glow. Needs both an AO map "
                        "and a self-illum mask to do anything."),
        FloatParam("gloss_gamma", "Gloss gamma", default=2.0, min=0.5, max=4.0,
                   step=0.1, decimals=2, group=group,
                   depends_on=("mode", "fake"),
                   help="Curve for roughness to Phong exponent. Higher tightens "
                        "highlights."),
        FloatParam("phong_strength", "Phong strength", default=0.75, min=0.0,
                   max=1.0, step=0.05, decimals=2, group=group,
                   depends_on=("mode", "fake"),
                   help="Scales the Phong mask and exponent. 0 disables Phong. "
                        "A faint roughness-shaped rim follows the same mask."),
        ChoiceParam("phong_tint_mode", "Phong tint compensation",
                    default="selective", group=group,
                    choices=TINT_MODE_CHOICES, depends_on=("mode", "fake"),
                    help="$phongalbedotint makes the engine multiply Phong by "
                         "the albedo; this divides it back out."),
        FloatParam("metal_diffuse_suppression", "Metal diffuse suppression",
                   default=METAL_DIFFUSE_SUPPRESSION, min=0.0, max=1.0, step=0.01, decimals=2,
                   group=group, depends_on=("mode", "fake"), advanced=True,
                   help="How much darker metallic areas get in the albedo. "
                        "Source 1 has no metallic energy split, so this fakes it."),
        FloatParam("colored_metal_relief", "Coloured metal relief", default=COLORED_METAL_RELIEF,
                   min=0.0, max=1.0, step=0.05, decimals=2, group=group,
                   depends_on=("mode", "fake"), advanced=True,
                   help="Eases the suppression on saturated metals so gold and "
                        "copper keep their colour."),
        FloatParam("envmask_gamma", "Envmap mask gamma", default=1.5, min=0.5,
                   max=4.0, step=0.1, decimals=2, group=group,
                   depends_on=("mode", "fake"), advanced=True),
        TextParam("envmap", "Cubemap", default="env_cubemap", group=group,
                  depends_on=("mode", "fake"), advanced=True,
                  help="Source 1 reflection probe. Fake PBR uses the metallic "
                       "mask and roughness to control its contribution."),
        BoolParam("emit_normal", "Ship a normal map", default=True, group=group,
                  depends_on=("mode", "fake"),
                  help="Off writes no $bumpmap or $phong either - Phong shades "
                       "against the bumpmap's normal. That "
                       "is what lets a prop_static keep the per-vertex or "
                       "lightmapped lighting VRAD baked for it; every branch "
                       "before CS:GO lights a normal-mapped static prop from a "
                       "single point instead. $envmap is unaffected."),
        BoolParam("bake_normal_detail", "Bake normal detail into base texture",
                  default=False, group=group, depends_on=("mode", "fake"),
                  help="Uses the source normal to bake fixed surface shading "
                       "when Ship a normal map is off. Requires Write VTF "
                       "textures. The shading cannot follow moving lights and "
                       "may reveal rotated UV islands."),
        FloatParam("normal_detail_strength", "Baked detail strength", default=0.25,
                   min=0.0, max=1.0, step=0.05, decimals=2, group=group,
                   depends_on=("mode", "fake"),
                   help="Strength of the optional normal detail bake. 0 leaves "
                        "the base texture unchanged; 0.25 gives subtle relief. "
                        "Only applies when Ship a normal map is off."),
        ChoiceParam("normal_green", "Normal green channel", default="auto",
                    group=group, choices=NORMAL_GREEN_CHOICES,
                    depends_on=("mode", "fake"),
                    help="Source 2 authors normals green-up and Source 1 wants "
                         "them green-down, so they are flipped by default. Auto "
                         "keeps a map whose filename already says _dx."),
        ChoiceParam("glow_mode", "Glow technique", default="emissiveblend",
                    group=group, choices=options(*GLOW_MODES),
                    depends_on=("mode", "fake"),
                    help="Used when the material carries a self-illumination mask."),
    ]

def exo_pbr_params(*, group: str = "Exo PBR") -> list[Param]:
    return [
        FloatParam("emission_scale", "$emissionscale", default=0.0, min=0.0,
                   max=10.0, step=0.1, decimals=2, group=group,
                   depends_on=("mode", "exo"),
                   help="0 auto-enables at 1.0 when an emissive map is present."),
        FloatParam("parallax_scale", "$parallaxscale", default=0.0, min=0.0,
                   max=1.0, step=0.01, decimals=3, group=group,
                   depends_on=("mode", "exo"),
                   help="0 auto-enables at 0.03 when a height map is present."),
        BoolParam("alpha_blend", "$alphablend", default=False, group=group,
                  depends_on=("mode", "exo"),
                  help="Forced on anyway when the source is translucent."),
    ]

def output_params(*, group: str = "Output", batch: bool = True) -> list[Param]:
    """What gets written, and at what size."""
    params: list[Param] = [
        BoolParam("generate_vtf", "Write VTF textures", default=True, group=group),
        BoolParam("generate_vmt", "Write the VMT", default=True, group=group),
        BoolParam("generate_mipmaps", "Generate mipmaps", default=True,
                  group=group),
        ChoiceParam("clamp_size", "Clamp resolution", default=0, group=group,
                    choices=CLAMP_CHOICES,
                    help="Caps the working resolution before any maths runs, "
                         "which is the cheapest way to survive a folder of 4K "
                         "materials."),
        BoolParam("synthesize_missing_maps", "Synthesise a missing normal map",
                  default=False, group=group,
                  help="Fills a missing normal with a flat one instead of "
                       "failing. Roughness and metallic are never invented."),
    ]
    if batch:
        params.append(
            BoolParam("skip_existing", "Skip materials already converted",
                      default=False, group=group,
                      help="Uses the VMT as the marker, so hand edits survive "
                           "a re-run."))
    return params

def naming_params(*, group: str = "Naming") -> list[Param]:
    """Material path and the prefix/suffix applied to every output name."""
    return [
        TextParam("material_path", "Material path", default="models/ports",
                  group=group,
                  help="The path inside materials/ that the VMT's texture "
                       "references are written against."),
        TextParam("prefix", "Name prefix", group=group,
                  help="A prefix containing slashes also overrides the material "
                       "path, so 'models/me/pack/' moves both the files and "
                       "their references."),
        TextParam("suffix", "Name suffix", group=group),
    ]

def preferences(*, parallel: bool = True) -> list[Param]:
    """The per-tool preference block every material tool shares."""
    params: list[Param] = []
    if parallel:
        params.append(
            IntParam("workers", "Materials converted in parallel", default=4,
                     min=1, max=32, group="Performance",
                     help="Each worker holds a full set of decoded maps, so a "
                          "folder of 4K textures wants a low number."))
    params += [
        BoolParam("per_output_clamps", "Cap each output separately",
                  default=False, group="Resolution",
                  help="A Phong mask or envmap mask rarely needs the albedo's "
                       "resolution. Capping them individually also lowers the "
                       "working resolution, so it saves time as well as disk."),
        ChoiceParam("clamp_color", "Base colour cap", default=0,
                    choices=CLAMP_CHOICES, group="Resolution",
                    depends_on=("per_output_clamps", True)),
        ChoiceParam("clamp_normal", "Normal cap", default=0,
                    choices=CLAMP_CHOICES, group="Resolution",
                    depends_on=("per_output_clamps", True)),
        ChoiceParam("clamp_phong", "Phong cap", default=0,
                    choices=CLAMP_CHOICES, group="Resolution",
                    depends_on=("per_output_clamps", True)),
        ChoiceParam("clamp_envmask", "Envmap mask cap", default=0,
                    choices=CLAMP_CHOICES, group="Resolution",
                    depends_on=("per_output_clamps", True)),
        ChoiceParam("clamp_selfillum", "Self-illum cap", default=0,
                    choices=CLAMP_CHOICES, group="Resolution",
                    depends_on=("per_output_clamps", True)),
        BoolParam("preview_results", "Add converted textures to Preview",
                  default=True, group="Report"),
        IntParam("preview_limit", "Preview at most", default=8, min=0, max=100,
                 suffix="materials", group="Report"),
        BoolParam("log_each_material", "Log every material in full", default=False,
                  group="Report",
                  help="Off logs one line per material; on logs each bake and "
                       "encode step."),
    ]
    return params
