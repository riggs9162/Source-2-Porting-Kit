"""
Source 1 static collision models (`$collisionmodel`).

Everything studiomdl does to a collision SMD, reproduced in Python so the kit
can predict the compile instead of guessing at it: how the mesh is split into
convex pieces, what volume and surface area those pieces have, what `$mass`
the engine would pick, and which of the compiler's warnings a given mesh is
about to trip.

The numbers here are not folklore. They were derived by pairing Valve's own
HL2 model sources (`modelsrc`, 1593 static `$collisionmodel` blocks) against
the shipped compiled `.phy` files (1587 pairs), then confirmed against
studiomdl's `collisionmodel.cpp`:

* **Convex decomposition.** studiomdl splits the collision SMD into connected
  components and convex-hulls each one; `$concave` is what allows more than
  one. Reproducing that split and hulling each component reproduces the
  compiled `"volume"` field to a median relative error of 0.0000% over the 244
  `$automass` props that could be checked end to end.
* **The mass law.** `$automass` is `volume * density`, except for materials
  that carry a `thickness` in `surfaceproperties.txt` - those are hollow
  shells and use `surface_area * thickness * density` instead. Verified on the
  same corpus: 118/122 solid-material props and 104/122 shell-material props
  match to within 2%. Getting the shell branch wrong is not subtle; a hollow
  metal prop comes out ~30x too heavy, because `metal` is a 0.1 inch shell.
* **The 1.0 kg floor.** `CollisionModel_Write` clamps every solid to a minimum
  of 1.0 kg *after* distributing `$mass`. A model whose pieces fall under that
  floor weighs more in game than its `$mass` says, and nothing warns you.
* **Compiler defaults.** Omitting `$inertia`/`$damping`/`$rotdamping` yields
  1.0/0.0/0.0, which is what 1587 of Valve's 1593 static props ship with.
  Writing invented values here is how a ported prop ends up rolling and
  spinning like nothing else in the game, so the block omits them by default.

`density` is kg/m^3 and `thickness` is inches, both straight out of
`scripts/surfaceproperties.txt` + `surfaceproperties_hl2.txt`.

Framework-free: no Flet, no Qt. Ragdolls and jointed props (`$collisionjoints`)
are a separate problem and are not handled here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import trimesh

#: 1 cubic inch in cubic metres - Source works in inches, `$mass` is kilograms.
CUBIC_INCH_IN_CUBIC_METRES = 1.6387064e-5

#: studiomdl clamps every compiled solid to this floor (`collisionmodel.cpp`,
#: `if ( pPhys->m_mass < 1.0 ) pPhys->m_mass = 1.0;`). Asking for less is not
#: an error, it is silently ignored.
MIN_SOLID_MASS_KG = 1.0

#: A convex piece thinner than this makes studiomdl emit "Model has
#: 2-dimensional geometry" - and, worse, give up on decomposing the model at
#: all; see :func:`thicken_thin_pieces`.
MIN_PIECE_THICKNESS_IN = 0.5

#: What a thin piece is inflated to. A hair over the limit rather than exactly
#: on it: measured against studiomdl, a piece at exactly 0.5 is still rejected
#: - the SMD carries six decimals and the hull it rebuilds lands just under -
#: while 0.55 passes.
THICKEN_TARGET_IN = 0.55

#: How many times one piece may be inflated. Pushing a piece out along its
#: thinnest direction fixes that direction and no other, so a sliver thin on
#: two axes is still two-dimensional after one pass - which is how the combine
#: printer shipped eight pieces down to 0.148in thick after a repair that
#: reported thickening 157 of them. Each pass fixes one axis and can never
#: undo an earlier one (inflation only ever grows the piece), so three is
#: enough for three dimensions and the fourth is slack.
MAX_THICKEN_PASSES = 4

#: studiomdl's default `$maxconvexpieces`. Above it the compiler prints
#: "COSTLY COLLISION MODEL!!!!" - which is a real cost, not a formality.
DEFAULT_MAX_CONVEX_PIECES = 40

#: How much slack to leave when declaring `$maxconvexpieces`, as a fraction of
#: the measured piece count plus a flat margin.
#:
#: `$maxconvexpieces` is a hard budget, not a hint. When studiomdl's own part
#: count exceeds it, the compiler abandons the decomposition and hulls the
#: **entire model into one piece** - the same catastrophic, near-silent result
#: a two-dimensional piece produces. Measured on the printer's collision: 120
#: real parts compile as 120 solids under `$maxconvexpieces 120` and as a
#: single 3KB hull under 119. Our count can differ from the compiler's by a few
#: either way (weld tolerance, and pieces it counts that have no volume for us
#: to measure), so the number written carries headroom. Declaring too many
#: costs nothing at all; declaring too few costs the model its collision.
CONVEX_BUDGET_HEADROOM = 0.25
CONVEX_BUDGET_MARGIN = 8

#: Above this, vphysics is being asked to do something silly; clamp instead of
#: emitting a nonsense `$mass`.
MAX_MASS_KG = 100_000.0

#: `(density kg/m^3, thickness inches)` per Source 1 surfaceprop, resolved
#: through the `base` inheritance chain in Half-Life 2's
#: `scripts/surfaceproperties.txt` and `surfaceproperties_hl2.txt`.
#: A non-zero thickness means the material is a hollow shell rather than a
#: solid volume - see `automass`.
SURFACEPROPS: Dict[str, Tuple[float, float]] = {
    "alienflesh":              (900, 0),
    "antlion":                 (900, 0),
    "antlionsand":             (1600, 0),
    "armorflesh":              (900, 0),
    "bloodyflesh":             (900, 0),
    "boulder":                 (2400, 0),
    "brakingrubbertire":       (1600, 0),
    "brick":                   (2400, 0),
    "canister":                (2700, 0.1),
    "cardboard":               (500, 0.25),
    "carpet":                  (500, 0.1),
    "ceiling_tile":            (500, 0.25),
    "chain":                   (1600, 0.5),
    "chainlink":               (1600, 0.5),
    "combine_glass":           (2700, 0.5),
    "combine_metal":           (2700, 0),
    "computer":                (2700, 0.1),
    "concrete":                (2400, 0),
    "concrete_block":          (2400, 0),
    "crowbar":                 (2700, 0.1),
    "default":                 (2000, 0),
    "default_silent":          (2000, 0),
    "dirt":                    (1600, 0),
    "flesh":                   (900, 0),
    "floating_metal_barrel":   (500, 0.1),
    "floatingstandable":       (800, 0),
    "foliage":                 (700, 0),
    "glass":                   (2700, 0.5),
    "glassbottle":             (2700, 0.5),
    "grass":                   (1600, 0),
    "gravel":                  (2400, 0),
    "grenade":                 (2700, 0.1),
    "gunship":                 (2700, 0.1),
    "ice":                     (917, 0),
    "item":                    (600, 0.25),
    "jeeptire":                (1600, 0),
    "ladder":                  (2700, 0.1),
    "metal":                   (2700, 0.1),
    "metal_barrel":            (2700, 0.1),
    "metal_bouncy":            (10000, 0),
    "metal_box":               (2700, 0.1),
    "metal_seafloorcar":       (2700, 0.1),
    "metalgrate":              (1600, 0.5),
    "metalpanel":              (2700, 0.1),
    "metalvehicle":            (2700, 0.1),
    "metalvent":               (2700, 0.04),
    "mud":                     (1600, 0),
    "no_decal":                (900, 0),
    "paintcan":                (2700, 0.1),
    "paper":                   (500, 0.25),
    "papercup":                (500, 0.25),
    "plaster":                 (1600, 0),
    "plastic":                 (500, 0.25),
    "plastic_barrel":          (500, 0.25),
    "plastic_barrel_buoyant":  (150, 0.25),
    "plastic_box":             (500, 0.25),
    "player":                  (1000, 0),
    "player_control_clip":     (2000, 0),
    "popcan":                  (2700, 0.1),
    "porcelain":               (2400, 0),
    "pottery":                 (2700, 0.5),
    "quicksand":               (600, 0),
    "rock":                    (2400, 0),
    "roller":                  (2700, 0.1),
    "rubber":                  (1600, 0),
    "rubbertire":              (1600, 0),
    "sand":                    (1600, 0),
    "slidingrubbertire":       (1600, 0),
    "slidingrubbertire_front": (1600, 0),
    "slidingrubbertire_rear":  (1600, 0),
    "slime":                   (2000, 0),
    "slipperymetal":           (2700, 0.1),
    "slipperyslime":           (1600, 0),
    "snow":                    (800, 0),
    "solidmetal":              (2700, 0),
    "strider":                 (2700, 0.1),
    "tile":                    (2700, 0.5),
    "wade":                    (1000, 0),
    "water":                   (1000, 0),
    "watermelon":              (900, 0),
    "weapon":                  (2700, 0.1),
    "wood":                    (700, 0),
    "wood_box":                (700, 0),
    "wood_crate":              (700, 0),
    "wood_furniture":          (700, 0),
    "wood_lowdensity":         (300, 0),
    "wood_panel":              (700, 1),
    "wood_plank":              (700, 0),
    "wood_solid":              (700, 0),
    "woodladder":              (700, 0),
    "zombieflesh":             (900, 0),
}

#: Source 2 surfaceprop names that do not exist in Source 1, mapped to the
#: closest stock Source 1 material. Source 2 keeps a much finer material list
#: (`world.metal_combine`, `prop.cardboard`, ...); anything not listed falls
#: back to the last dot-separated segment, then to `default`.
SOURCE2_SURFACEPROP_ALIASES: Dict[str, str] = {
    "metal_combine": "combine_metal",
    "metal_thin": "metalpanel",
    "metal_thick": "solidmetal",
    "metal_hollow": "metalvent",
    "metal_grate": "metalgrate",
    "metal_panel": "metalpanel",
    "metal_barrel": "metal_barrel",
    "wood_thin": "wood_panel",
    "wood_thick": "wood_solid",
    "wood_plank": "wood_plank",
    "cardboard_box": "cardboard",
    "plaster_wall": "plaster",
    "stone": "rock",
    "stucco": "plaster",
    "asphalt": "concrete",
    "ceramic": "porcelain",
    "rubber_tire": "rubbertire",
    "foliage_leaves": "foliage",
    "flesh_human": "flesh",
    "glass_bottle": "glassbottle",
    "glass_window": "glass",
}


def resolve_surfaceprop(name: Optional[str]) -> str:
    """Best Source 1 surfaceprop for a raw (possibly Source 2) material name.

    Source 2 names are dotted and namespaced (`world.metal_combine`,
    `prop.cardboard`); Source 1 wants a flat name that exists in
    `surfaceproperties.txt`. Tries the whole name, then the alias table, then
    the last dotted segment, and finally gives up on `default` - which is what
    the engine would fall back to anyway, just without the warning.
    """
    if not name:
        return "default"
    raw = name.strip().strip('"').lower().replace("\\", "/")
    if raw in SURFACEPROPS:
        return raw
    if raw in SOURCE2_SURFACEPROP_ALIASES:
        return SOURCE2_SURFACEPROP_ALIASES[raw]
    tail = raw.rsplit(".", 1)[-1]
    if tail in SURFACEPROPS:
        return tail
    if tail in SOURCE2_SURFACEPROP_ALIASES:
        return SOURCE2_SURFACEPROP_ALIASES[tail]
    return "default"


def surfaceprop_density(name: Optional[str]) -> Tuple[float, float]:
    """`(density kg/m^3, thickness inches)` for a surfaceprop, via
    :func:`resolve_surfaceprop`."""
    return SURFACEPROPS.get(resolve_surfaceprop(name), SURFACEPROPS["default"])


@dataclass
class ConvexPiece:
    """One convex element of a collision model - what studiomdl turns a single
    connected component of the collision SMD into."""

    volume: float
    area: float
    #: Bounding-box extents in inches, smallest first.
    extents: Tuple[float, float, float]

    @property
    def min_thickness(self) -> float:
        return float(min(self.extents))

    @property
    def is_2d(self) -> bool:
        """True when studiomdl would call this piece 2-dimensional."""
        return self.min_thickness < MIN_PIECE_THICKNESS_IN


@dataclass
class CollisionModel:
    """A collision mesh measured the way studiomdl measures it."""

    pieces: List[ConvexPiece] = field(default_factory=list)

    @property
    def piece_count(self) -> int:
        return len(self.pieces)

    @property
    def volume(self) -> float:
        """Summed convex volume in cubic inches - the compiled `"volume"`."""
        return float(sum(p.volume for p in self.pieces))

    @property
    def area(self) -> float:
        """Summed convex surface area in square inches, for the shell mass law."""
        return float(sum(p.area for p in self.pieces))

    @property
    def thin_pieces(self) -> List[int]:
        """Indices of pieces studiomdl would flag as 2-dimensional."""
        return [i for i, p in enumerate(self.pieces) if p.is_2d]


def weld_vertices(mesh: trimesh.Trimesh, distance: float = 0.0) -> None:
    """Merge coincident vertices in place, tolerating ``distance`` of slop.

    trimesh 4 dropped `merge_vertices(radius=...)` in favour of a decimal-place
    quantisation, and passing the old keyword raises `TypeError` - which, inside
    the usual `try/except`, reads as "welding happened" while nothing did. The
    distance is converted to the number of decimal places that expresses it.
    """
    kwargs = {}
    if distance > 0:
        kwargs["digits_vertex"] = max(0, int(round(-math.log10(distance))))
    try:
        mesh.merge_vertices(**kwargs)
    except TypeError:
        mesh.merge_vertices()


def _vertex_groups(faces: np.ndarray, vertex_count: int) -> np.ndarray:
    """Connected-component label per vertex, over the graph the faces induce.

    Union-find over the deduplicated triangle edges. Deduplicating first is
    what keeps it quick - a 700,000-vertex render mesh resolves in about a
    second, against the convex hulls that follow costing far more.
    """
    parent = np.arange(max(vertex_count, 1), dtype=np.int64)

    def find(node: int) -> int:
        root = node
        while parent[root] != root:
            root = parent[root]
        while parent[node] != root:
            parent[node], node = root, parent[node]
        return root

    edges = np.unique(np.sort(np.vstack([faces[:, [0, 1]], faces[:, [1, 2]],
                                         faces[:, [2, 0]]]), axis=1), axis=0)
    for left, right in edges:
        root_left, root_right = find(int(left)), find(int(right))
        if root_left != root_right:
            parent[root_left] = root_right
    return np.array([find(i) for i in range(vertex_count)], dtype=np.int64)


def split_elements(mesh: trimesh.Trimesh) -> List[trimesh.Trimesh]:
    """The connected elements of a collision mesh, the way studiomdl groups them.

    Two triangles belong to the same element when they share a *vertex*, which
    is the rule `collisionmodel.cpp` applies and **not** the rule
    `Trimesh.split` applies. trimesh walks face adjacency - faces sharing an
    *edge* - and an edge used by more than two faces drops out of that
    adjacency entirely. Source 2 collision is full of interpenetrating boxes
    that do exactly that, so the mesh comes back shattered: the combine
    printer's 2,902-triangle collision splits into 1,156 fragments under
    trimesh and 121 elements under this function, against the 118 studiomdl
    builds. Everything downstream inherits the error - the fragments measure as
    80 convex pieces instead of 118, and `thicken_thin_pieces` inflates
    fragments while the real pieces stay two-dimensional.

    Holes are left alone, which `Trimesh.split` does not do either: it repairs
    every component it returns, so the open reveal around a doorway - four
    strips forming a tube - comes back capped into a solid box. On one Alyx
    doorframe that invented 44 faces and a 19,391 in^3 slab across the doorway,
    which then survived the convex decomposition untouched because a box is
    already convex.
    """
    faces = np.asarray(mesh.faces)
    if faces is None or not len(faces):
        return []

    labels = _vertex_groups(faces, len(mesh.vertices))
    face_labels = labels[faces[:, 0]]
    order = np.argsort(face_labels, kind="stable")
    groups = np.split(order,
                      np.flatnonzero(np.diff(face_labels[order])) + 1)
    if len(groups) <= 1:
        return [mesh]
    return list(mesh.submesh(groups, only_watertight=False, repair=False,
                             append=False))


def convex_pieces(mesh: trimesh.Trimesh,
                  *, weld_distance: float = 0.0) -> CollisionModel:
    """Measure ``mesh`` as studiomdl's convex decomposition would see it.

    studiomdl treats each *connected component* of the collision SMD as one
    convex element and hulls it, which is why collision meshes are authored as
    separate closed shapes that may interpenetrate. Welding first is what makes
    the components connected at all - an SMD (like VRF's physics glTF) with
    three unshared vertices per triangle splits into one component per
    triangle otherwise.

    Degenerate components (fewer than 4 vertices, or a hull with no volume) are
    dropped, matching studiomdl discarding them rather than emitting a solid.
    """
    model = CollisionModel()
    verts = np.asarray(mesh.vertices, dtype=np.float64)
    faces = np.asarray(mesh.faces)
    if verts.size == 0 or faces is None or len(faces) == 0:
        return model

    work = trimesh.Trimesh(vertices=verts, faces=faces, process=False)
    try:
        weld_vertices(work, weld_distance)
    except Exception:
        pass

    try:
        components = split_elements(work)
    except Exception:
        components = []
    if not components:
        components = [work]

    for comp in components:
        if comp is None or len(comp.vertices) < 4:
            continue
        try:
            # A flat component divides by a zero volume inside trimesh's mass
            # properties; the volume guard below is the answer, the warning it
            # prints on the way is noise in every batch log.
            with np.errstate(invalid="ignore", divide="ignore"):
                hull = comp.convex_hull
                volume = (abs(float(hull.volume))
                          if hull is not None and len(hull.faces) else 0.0)
        except Exception:      # qhull refuses coplanar / collinear input
            continue
        if hull is None or len(hull.faces) == 0 or volume <= 1e-9:
            continue
        bounds = np.asarray(hull.bounds, dtype=np.float64)
        extents = np.abs(bounds[1] - bounds[0])
        model.pieces.append(ConvexPiece(
            volume=volume,
            area=abs(float(hull.area)),
            extents=(float(extents[0]), float(extents[1]), float(extents[2])),
        ))
    return model


# ----------------------------------------------------------------------
# Concave collision
# ----------------------------------------------------------------------

#: How far past the cut plane each half of a split is taken, in inches. The two
#: halves have to overlap rather than meet: pieces that share a face share its
#: vertices, studiomdl welds those, and the two hulls fuse back into the one
#: hull the split existed to avoid. Interpenetration is how Valve's own
#: collision meshes are authored, and vphysics does not care.
SPLIT_OVERLAP_IN = 0.05

#: A cut has to buy at least this fraction of the mesh's starting hull volume
#: to be worth a piece. Below it the decomposition is chasing detail vphysics
#: will never notice, out of a budget of 40. Measured over the Alyx combine
#: hallway set, 1% is where the curve flattens: it spends the whole default
#: budget and gives back 63-80% of the volume a single hull swallowed, while
#: 2% stops at half that and 0.5% only overruns the budget.
MIN_SPLIT_GAIN = 0.01

#: Cut positions tried per axis, spread across the piece's extent.
SPLIT_SAMPLES = 11


#: A patch count above this means a curved or finely tessellated shell, where a
#: slab per facet would cost more pieces than the whole model is allowed. Frames
#: - a doorway reveal, a window surround, an open box - come in far under it.
MAX_SHELL_PATCHES = 12

#: An open surface whose convex hull holds this many times the volume of the
#: shell it would make is enclosing space the source never had.
HOLLOW_HULL_RATIO = 3.0


@dataclass
class ShellSlabs:
    """What :func:`slab_open_shells` replaced."""

    mesh: Optional[trimesh.Trimesh] = None
    replaced: int = 0
    slabs: int = 0

    @property
    def changed(self) -> bool:
        return self.replaced > 0


def coplanar_patches(component: trimesh.Trimesh,
                     limit: int = 0) -> List[trimesh.Trimesh]:
    """The component's faces grouped into connected coplanar patches.

    `limit` gives up early and returns nothing once the component is known to
    have more distinct planes than that. Grouping is the expensive half - a
    submesh and a split per plane - and a curved shell has one plane per facet,
    so a caller that would reject the answer should never pay for it.
    """
    normals = np.asarray(component.face_normals, dtype=np.float64)
    centres = np.asarray(component.triangles_center, dtype=np.float64)
    if not len(normals):
        return []
    key = np.column_stack([np.round(normals, 2),
                           np.round((normals * centres).sum(1), 1)])
    _unique, plane_of = np.unique(key, axis=0, return_inverse=True)
    if limit and int(plane_of.max()) + 1 > limit:
        return []

    patches: List[trimesh.Trimesh] = []
    for plane in range(int(plane_of.max()) + 1):
        selected = np.flatnonzero(plane_of == plane)
        if not len(selected):
            continue
        patch = component.submesh([selected], only_watertight=False,
                                  repair=False, append=True)
        patches.extend(split_elements(patch))
    return patches


def _slab_from_patch(patch: trimesh.Trimesh,
                     thickness: float) -> Optional[trimesh.Trimesh]:
    """A flat patch given a thickness, as the solid studiomdl will build."""
    normals = np.asarray(patch.face_normals, dtype=np.float64)
    if not len(normals):
        return None
    direction = normals[0]
    length = float(np.linalg.norm(direction))
    if length < 0.5:
        return None
    direction = direction / length
    points = np.asarray(patch.vertices, dtype=np.float64)
    return _hull_of(np.vstack([points + direction * thickness / 2.0,
                               points - direction * thickness / 2.0])[None])


def slab_open_shells(mesh: trimesh.Trimesh, *, weld_distance: float = 1e-4,
                     thickness: float = THICKEN_TARGET_IN,
                     max_patches: int = MAX_SHELL_PATCHES,
                     hollow_ratio: float = HOLLOW_HULL_RATIO) -> ShellSlabs:
    """Rebuild hollow open surfaces as one slab per flat face.

    A Source 2 `m_meshes` shape is a *surface*, and some of what it describes is
    a hole: the reveal around a doorway is four strips forming an open tube.
    Hulling that tube fills the doorway in, and no amount of convex
    decomposition undoes it - cutting a frame in half leaves two halves whose
    hulls still cover the opening between them, so the volume the cut saves is
    zero and a greedy search correctly refuses to make it. The hole only opens
    after the fourth cut, which no pairwise measure can see coming.

    Following the surface instead sidesteps the search entirely: each flat face
    of the frame becomes its own thin solid, and the opening between them is
    what is left over. Only open, hollow, and *few-faced* components qualify -
    a curved shell of five hundred facets would want five hundred slabs, and
    the whole model is allowed forty.
    """
    result = ShellSlabs(mesh=mesh)
    if mesh is None or len(getattr(mesh, "faces", ())) == 0:
        return result

    work = trimesh.Trimesh(vertices=np.asarray(mesh.vertices, dtype=np.float64),
                           faces=np.asarray(mesh.faces), process=False)
    try:
        weld_vertices(work, weld_distance)
        components = split_elements(work) or [work]
    except Exception:
        return result

    parts: List[trimesh.Trimesh] = []
    for component in components:
        tris = np.asarray(component.vertices,
                          dtype=np.float64)[np.asarray(component.faces)]
        hull = _hull_of(tris)
        shell_volume = abs(float(component.area)) * thickness / 2.0
        # A closed component is a solid and means what it says; an open one
        # whose hull is no bigger than its own shell is already tight.
        if (component.is_watertight or hull is None or shell_volume <= 0
                or abs(float(hull.volume)) <= hollow_ratio * shell_volume):
            parts.append(component)
            continue

        patches = coplanar_patches(component, limit=max_patches)
        if not patches or len(patches) > max_patches:
            parts.append(component)
            continue

        slabs = [slab for slab in (_slab_from_patch(patch, thickness)
                                   for patch in patches) if slab is not None]
        if not slabs:
            parts.append(component)
            continue
        parts.extend(slabs)
        result.replaced += 1
        result.slabs += len(slabs)

    if result.changed and parts:
        result.mesh = trimesh.util.concatenate(parts)
    return result


@dataclass
class ThinPieceRepair:
    """What :func:`thicken_thin_pieces` had to do to a collision mesh."""

    mesh: Optional[trimesh.Trimesh] = None
    thickened: int = 0
    dropped: int = 0
    #: Thinnest piece found, before anything was done about it.
    thinnest: float = 0.0

    @property
    def changed(self) -> bool:
        return bool(self.thickened or self.dropped)


def _width_along(points: np.ndarray, direction: np.ndarray) -> float:
    projected = points @ direction
    return float(projected.max() - projected.min())


def _thinnest_direction(component: trimesh.Trimesh
                        ) -> Tuple[Optional[np.ndarray], float]:
    """The direction this piece is thinnest in, and how thin it is there.

    Face normals are the candidates: the minimum width of a convex solid is
    reached along one of them or along an edge-pair direction, and for the
    slabs and sheets this exists to catch it is always a face normal. Read off
    the component rather than its hull, because a piece with no volume has no
    hull to read - which is exactly the piece that needs finding.
    """
    points = np.asarray(component.vertices, dtype=np.float64)
    if len(points) < 3:
        return None, 0.0
    try:
        normals = np.asarray(component.face_normals, dtype=np.float64)
    except Exception:
        return None, 0.0
    normals = normals[np.isfinite(normals).all(axis=1)]
    if not len(normals):
        return None, 0.0

    best_direction, best_width = None, float("inf")
    for direction in np.unique(np.round(normals, 5), axis=0):
        length = float(np.linalg.norm(direction))
        if length < 0.5:
            continue
        direction = direction / length
        width = _width_along(points, direction)
        if width < best_width:
            best_direction, best_width = direction, width
    if best_direction is None:
        return None, 0.0
    return best_direction, best_width


def _thinnest_direction_of_points(points: np.ndarray
                                  ) -> Tuple[Optional[np.ndarray], float]:
    """As :func:`_thinnest_direction`, for a point set that has volume.

    Reads the candidate directions off the point set's own convex hull, which
    is the solid studiomdl will build from it - the piece being measured
    between inflation passes no longer has faces of its own to read.
    """
    hull = _hull_of_points(points)
    if hull is None:
        return None, 0.0
    return _thinnest_direction(hull)


def thicken_thin_pieces(mesh: trimesh.Trimesh, *, weld_distance: float = 1e-4,
                        min_thickness: float = THICKEN_TARGET_IN
                        ) -> ThinPieceRepair:
    """Inflate collision pieces too thin for studiomdl to make a solid of.

    **One piece under half an inch thick takes the whole model with it.**
    studiomdl hulls each connected element of the collision SMD, and an element
    it cannot turn into a solid makes it abandon that and hull the entire mesh
    into a single piece - a doorway filled in, a window bricked up - while
    saying nothing but `WARNING: Model has 2-dimensional geometry`. Measured on
    `combine_hall_wall_window_wrapped_a`: nine real pieces compile to 566,525
    in^3 of collision, and one 0.0008 in^3 sheet alongside them takes it to
    1,098,614 - a single hull around everything.

    Source 2 collision meshes are full of these, because a Source 2 `m_meshes`
    shape is a *surface*: a zero-thickness quad is a real barrier there, and a
    pane of glass can legitimately be one. So they are thickened rather than
    dropped - `$remove2d` is studiomdl's own answer and it deletes them, which
    costs the grate, the sign and the pane the collision they were there for.
    Thickening keeps it, at the price of half an inch nobody can see.

    The inflation is a Minkowski sum with a segment along the thin axis: every
    vertex is duplicated to either side, which extends the piece in that one
    direction and nowhere else, and keeps it convex.
    """
    result = ThinPieceRepair(mesh=mesh)
    if mesh is None or len(getattr(mesh, "faces", ())) == 0:
        return result

    work = trimesh.Trimesh(vertices=np.asarray(mesh.vertices, dtype=np.float64),
                           faces=np.asarray(mesh.faces), process=False)
    try:
        weld_vertices(work, weld_distance)
        components = split_elements(work) or [work]
    except Exception:
        return result

    parts: List[trimesh.Trimesh] = []
    thinnest = float("inf")
    for component in components:
        direction, width = _thinnest_direction(component)
        if direction is not None:
            thinnest = min(thinnest, width)
        if direction is None:
            result.dropped += 1
            continue
        if width >= min_thickness:
            parts.append(component)
            continue

        points = np.asarray(component.vertices, dtype=np.float64)
        for _ in range(MAX_THICKEN_PASSES):
            push = 0.5 * (min_thickness - width)
            points = np.vstack([points + direction * push,
                                points - direction * push])
            direction, width = _thinnest_direction_of_points(points)
            if direction is None or width >= min_thickness:
                break

        slab = _hull_of_points(points)
        if slab is None:
            result.dropped += 1
            continue
        parts.append(slab)
        result.thickened += 1

    result.thinnest = 0.0 if thinnest == float("inf") else thinnest
    if result.changed and parts:
        result.mesh = trimesh.util.concatenate(parts)
    elif not parts:
        # Nothing survived: leave the mesh alone rather than hand studiomdl an
        # empty collision model, which is worse than the hull it compiles now.
        result.thickened = result.dropped = 0
    return result


@dataclass
class Decomposition:
    """What :func:`decompose_concave` did to a collision mesh."""

    mesh: Optional[trimesh.Trimesh] = None
    pieces_before: int = 0
    pieces_after: int = 0
    volume_before: float = 0.0
    volume_after: float = 0.0
    #: Pieces still concave enough to be worth cutting when the budget ran out.
    unsplit: int = 0

    @property
    def changed(self) -> bool:
        return self.pieces_after > self.pieces_before

    @property
    def recovered(self) -> float:
        """Fraction of the hulled-over volume the cuts gave back."""
        if self.volume_before <= 0:
            return 0.0
        return 1.0 - self.volume_after / self.volume_before


def clip_triangles(tris: np.ndarray, axis: int, position: float,
                   keep_below: bool) -> np.ndarray:
    """Clip a triangle soup against one axis-aligned plane.

    Sutherland-Hodgman over the three cases that survive: a triangle wholly
    inside passes through, one vertex inside becomes a smaller triangle, and
    two inside become a quad cut in two. It asks nothing of the topology, which
    is the point - a physics mesh cut this way is rarely closed, and every
    alternative in trimesh wants a watertight mesh or shapely.
    """
    if len(tris) == 0:
        return tris
    distance = tris[:, :, axis] - position
    if not keep_below:
        distance = -distance
    inside = distance <= 0.0
    count = inside.sum(axis=1)

    parts: List[np.ndarray] = [tris[count == 3]]

    def reordered(rows: np.ndarray, first: np.ndarray):
        """The selected triangles and their distances, rotated to start at `first`."""
        order = (first[:, None] + np.arange(3)[None, :]) % 3
        return (np.take_along_axis(tris[rows], order[:, :, None].repeat(3, 2), 1),
                np.take_along_axis(distance[rows], order, 1))

    def crossing(a, b, near, far):
        return a + (near / (near - far))[:, None] * (b - a)

    single = np.flatnonzero(count == 1)
    if len(single):
        tri, span = reordered(single, np.argmax(inside[single], axis=1))
        keep, lost_a, lost_b = tri[:, 0], tri[:, 1], tri[:, 2]
        parts.append(np.stack([keep,
                               crossing(keep, lost_a, span[:, 0], span[:, 1]),
                               crossing(keep, lost_b, span[:, 0], span[:, 2])],
                              axis=1))

    double = np.flatnonzero(count == 2)
    if len(double):
        tri, span = reordered(double, np.argmin(inside[double], axis=1))
        lost, keep_a, keep_b = tri[:, 0], tri[:, 1], tri[:, 2]
        cut_a = crossing(lost, keep_a, span[:, 0], span[:, 1])
        cut_b = crossing(lost, keep_b, span[:, 0], span[:, 2])
        parts.append(np.stack([cut_a, keep_a, keep_b], axis=1))
        parts.append(np.stack([cut_a, keep_b, cut_b], axis=1))

    parts = [part for part in parts if len(part)]
    if not parts:
        return np.zeros((0, 3, 3), dtype=tris.dtype)
    return np.concatenate(parts, axis=0)


def _hull_of_points(points: np.ndarray) -> Optional[trimesh.Trimesh]:
    """The solid studiomdl would build around these points, or None if flat."""
    if points is None or len(points) == 0:
        return None
    points = np.unique(np.asarray(points, dtype=np.float64).reshape(-1, 3),
                       axis=0)
    if len(points) < 4:
        return None
    hull, volume = None, 0.0
    try:
        # A degenerate hull divides by a zero volume deep inside trimesh's mass
        # properties; the guard below catches the result, the warning is noise.
        with np.errstate(invalid="ignore", divide="ignore"):
            hull = trimesh.Trimesh(vertices=points, process=False).convex_hull
            if hull is not None and len(hull.faces):
                volume = abs(float(hull.volume))
    except Exception:      # qhull refuses coplanar / collinear input
        return None
    if hull is None or len(hull.faces) == 0 or volume <= 1e-9:
        return None
    return hull


def _hull_of(tris: np.ndarray) -> Optional[trimesh.Trimesh]:
    """The solid studiomdl would build for this piece, or None if there is none."""
    if len(tris) == 0:
        return None
    return _hull_of_points(np.asarray(tris).reshape(-1, 3))


def _best_cut(tris: np.ndarray, overlap: float
              ) -> Optional[Tuple[float, np.ndarray, np.ndarray]]:
    """The axis-aligned cut that covers this piece in the least volume.

    Volume is the whole objective: a hull that swallows a doorway costs exactly
    the doorway, so the cut that shrinks the summed hulls the most is the one
    that gave the most of the opening back.
    """
    best: Optional[Tuple[float, np.ndarray, np.ndarray]] = None
    points = tris.reshape(-1, 3)
    low, high = points.min(axis=0), points.max(axis=0)
    for axis in range(3):
        span = float(high[axis] - low[axis])
        if span < 4.0 * overlap:
            continue
        for fraction in np.linspace(0.1, 0.9, SPLIT_SAMPLES):
            position = float(low[axis]) + span * float(fraction)
            below = clip_triangles(tris, axis, position + overlap, True)
            above = clip_triangles(tris, axis, position - overlap, False)
            hull_below, hull_above = _hull_of(below), _hull_of(above)
            if hull_below is None or hull_above is None:
                continue
            total = abs(float(hull_below.volume)) + abs(float(hull_above.volume))
            if best is None or total < best[0]:
                best = (total, below, above)
    return best


# eq=False so `list.remove` matches by identity: the default __eq__ compares
# the numpy arrays and raises on a shape mismatch.
@dataclass(eq=False)
class _Piece:
    """One candidate piece, plus the cut it is holding out for."""

    tris: np.ndarray
    component: int
    volume: float
    cut: Optional[Tuple[float, np.ndarray, np.ndarray]] = None
    searched: bool = False

    def gain(self, overlap: float) -> float:
        """Volume the best cut would give back. Searched once, then cached."""
        if not self.searched:
            self.cut = _best_cut(self.tris, overlap)
            self.searched = True
        return (self.volume - self.cut[0]) if self.cut else 0.0


def decompose_concave(mesh: trimesh.Trimesh, *,
                      max_pieces: int = DEFAULT_MAX_CONVEX_PIECES,
                      weld_distance: float = 1e-4,
                      overlap: float = SPLIT_OVERLAP_IN,
                      min_gain: float = MIN_SPLIT_GAIN,
                      cancel: Optional[Any] = None) -> Decomposition:
    """Cut concave components up until studiomdl can represent them.

    Source 2 collision is often a single `m_meshes` triangle soup - arbitrary
    concave geometry, which Rubikon takes as-is for anything that does not
    move. Source 1 has no such shape. studiomdl hulls each connected component
    of the collision SMD, so a doorframe compiles as a solid slab with the
    doorway filled in, and nothing in the compile says so.

    The concave components are therefore cut into pieces whose hulls do fit,
    greedily and axis-aligned: whichever piece can give back the most volume is
    cut next, until the cuts stop paying for themselves or the piece budget
    runs out. What comes back for a cut component is convex hulls - the exact
    solids the compiler will build - while a component nobody had to cut keeps
    its own triangles, so a collision mesh that was already fine passes through
    untouched.
    """
    result = Decomposition(mesh=mesh)
    if mesh is None or len(getattr(mesh, "faces", ())) == 0:
        return result

    work = trimesh.Trimesh(vertices=np.asarray(mesh.vertices, dtype=np.float64),
                           faces=np.asarray(mesh.faces), process=False)
    try:
        weld_vertices(work, weld_distance)
        components = split_elements(work) or [work]
    except Exception:
        components = [work]

    pieces: List[_Piece] = []
    for index, component in enumerate(components):
        tris = np.asarray(component.vertices,
                          dtype=np.float64)[np.asarray(component.faces)]
        hull = _hull_of(tris)
        if hull is None:
            continue
        pieces.append(_Piece(tris=tris, component=index,
                             volume=abs(float(hull.volume))))

    result.pieces_before = result.pieces_after = len(pieces)
    result.volume_before = result.volume_after = sum(p.volume for p in pieces)
    if not pieces:
        return result

    floor = min_gain * result.volume_before
    while len(pieces) < max_pieces:
        if cancel is not None and cancel.is_set():
            break
        best = max(pieces, key=lambda piece: piece.gain(overlap))
        if best.cut is None or best.gain(overlap) <= floor:
            break
        _total, below, above = best.cut
        pieces.remove(best)
        for tris in (below, above):
            hull = _hull_of(tris)
            if hull is None:
                continue
            pieces.append(_Piece(tris=tris, component=best.component,
                                 volume=abs(float(hull.volume))))

    result.pieces_after = len(pieces)
    result.volume_after = sum(piece.volume for piece in pieces)
    result.unsplit = sum(1 for piece in pieces if piece.gain(overlap) > floor)

    if result.pieces_after <= result.pieces_before:
        return result

    per_component: Dict[int, int] = {}
    for piece in pieces:
        per_component[piece.component] = per_component.get(piece.component, 0) + 1
    was_cut = {index for index, count in per_component.items() if count > 1}

    parts: List[trimesh.Trimesh] = [component
                                    for index, component in enumerate(components)
                                    if index not in was_cut]
    for piece in pieces:
        if piece.component in was_cut:
            hull = _hull_of(piece.tris)
            if hull is not None:
                parts.append(hull)
    if parts:
        result.mesh = trimesh.util.concatenate(parts)
    return result


def automass(model: CollisionModel, surfaceprop: Optional[str]) -> float:
    """`$mass` in kilograms, by studiomdl's `$automass` rule.

    Solid materials use volume; materials with a `thickness` in
    `surfaceproperties.txt` are hollow shells and use surface area times that
    thickness. Result is floored at :data:`MIN_SOLID_MASS_KG`, because
    studiomdl would apply that floor anyway.
    """
    density, thickness = surfaceprop_density(surfaceprop)
    cubic_inches = model.area * thickness if thickness > 0 else model.volume
    kilograms = cubic_inches * density * CUBIC_INCH_IN_CUBIC_METRES
    return float(min(max(kilograms, MIN_SOLID_MASS_KG), MAX_MASS_KG))


def convex_piece_budget(piece_count: int) -> int:
    """The `$maxconvexpieces` to declare for a mesh measured at `piece_count`.

    Never below studiomdl's own default of 40, and above the measured count by
    :data:`CONVEX_BUDGET_HEADROOM` plus :data:`CONVEX_BUDGET_MARGIN` - because
    a budget the compiler's part count overruns collapses the whole collision
    model into a single hull, and a generous one costs nothing.
    """
    if piece_count <= 0:
        return DEFAULT_MAX_CONVEX_PIECES
    padded = (int(math.ceil(piece_count * (1.0 + CONVEX_BUDGET_HEADROOM)))
              + CONVEX_BUDGET_MARGIN)
    return max(DEFAULT_MAX_CONVEX_PIECES, padded)


def check_collision_model(model: CollisionModel, mass: float,
                          *, max_convex_pieces: int = DEFAULT_MAX_CONVEX_PIECES
                          ) -> List[str]:
    """Warnings studiomdl (or the shipped model) would produce, in advance.

    Returns human-readable strings; an empty list means the mesh compiles
    clean. These are the failures that otherwise only show up as a prop that
    falls through the world or weighs nothing.
    """
    warnings: List[str] = []

    if not model.pieces:
        warnings.append(
            "collision mesh has no usable convex pieces - studiomdl will "
            "produce no collision at all")
        return warnings

    if model.piece_count > max_convex_pieces:
        warnings.append(
            f"{model.piece_count} convex pieces exceeds the default "
            f"$maxconvexpieces {max_convex_pieces} - the QC raises the budget "
            f"to {convex_piece_budget(model.piece_count)} to cover them, since "
            "a budget the compiler overruns makes it hull the whole model into "
            "one piece, but 'COSTLY COLLISION MODEL' is a real cost and the "
            "prop is expensive to simulate")

    thin = model.thin_pieces
    if thin:
        thinnest = min(model.pieces[i].min_thickness for i in thin)
        warnings.append(
            f"{len(thin)} of {model.piece_count} convex piece(s) are thinner "
            f"than {MIN_PIECE_THICKNESS_IN}in (thinnest {thinnest:.3f}in) - "
            "studiomdl reports 'Model has 2-dimensional geometry' and the "
            "collision is unreliable")

    if mass <= MIN_SOLID_MASS_KG:
        warnings.append(
            f"$mass {mass:.3f} is at or below studiomdl's {MIN_SOLID_MASS_KG}kg "
            "per-solid floor - the compiled prop will weigh "
            f"{MIN_SOLID_MASS_KG}kg regardless")

    return warnings


def collisionmodel_block(
    smd_name: str,
    *,
    mass: Optional[float] = None,
    concave: bool = True,
    piece_count: int = 1,
    max_convex_pieces: Optional[int] = None,
    remove2d: bool = False,
    inertia: Optional[float] = None,
    damping: Optional[float] = None,
    rotdamping: Optional[float] = None,
    indent: str = "    ",
) -> List[str]:
    """The `$collisionmodel { ... }` QC lines for a static prop.

    Shaped like Valve's own: of 1593 static `$collisionmodel` blocks in the HL2
    sources, 1477 carry `$concave`, 1345 a `$mass`, and only a handful set
    anything else - so `$inertia`/`$damping`/`$rotdamping` are omitted unless
    explicitly asked for, leaving studiomdl's 1.0/0.0/0.0.

    `$concave` is only written when the mesh actually has more than one convex
    piece, and `$maxconvexpieces` only when the piece count needs it - Valve
    never raises it, and raising it blindly just hides the compiler's cost
    warning.
    """
    lines = [f'$collisionmodel "{smd_name}" {{']
    if remove2d:
        lines.append(f"{indent}$remove2d")
    if concave and piece_count > 1:
        lines.append(f"{indent}$concave")
    if max_convex_pieces is not None and piece_count > DEFAULT_MAX_CONVEX_PIECES:
        lines.append(f"{indent}$maxconvexpieces {max_convex_pieces}")
    if mass is not None:
        lines.append(f"{indent}$mass {mass:.2f}")
    if inertia is not None:
        lines.append(f"{indent}$inertia {inertia:.2f}")
    if damping is not None:
        lines.append(f"{indent}$damping {damping:.2f}")
    if rotdamping is not None:
        lines.append(f"{indent}$rotdamping {rotdamping:.2f}")
    lines.append("}")
    return lines
