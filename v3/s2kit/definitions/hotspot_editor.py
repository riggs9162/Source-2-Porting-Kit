"""Shared hotspot editor schema and metadata."""
from __future__ import annotations

from s2kit.core.registry import tool
from s2kit.core.tool_definition import ToolDefinition
from s2kit.core.params import BoolParam, ChoiceParam, IntParam, options

GRID_SIZES = (1, 2, 4, 8, 16, 32, 64, 128, 256)


@tool(
    id="hotspot-editor",
    title="Hotspot Editor",
    category="Materials",
    icon="HIGHLIGHT_ALT",
    description="Draw the rectangles Hammer++ and Strata use to fit a trim "
                "sheet to a face.",
    keywords=("hotspot", "rect", "rectanglemap", "trim", "trimsheet", "hammer",
              "hammer++", "strata", "fit", "atlas", "subrect", "panel"),
    order=40,
)
class HotspotEditor(ToolDefinition):
    """Shared declaration for HotspotEditor."""
    HEADLESS = False
    UI_CLASS = "s2kit.tools.hotspot_editor:HotspotEditor"
    RUN_LABEL = "Save .rect"
    RUN_ICON = "SAVE"
    PARAMS = []
    PREFERENCES = [
        ChoiceParam("default_grid", "Default grid", default=16,
                    group="Editing",
                    choices=options(*[(size, f"{size} px") for size in GRID_SIZES])),
        BoolParam("snap", "Snap to grid", default=True, group="Editing"),
        BoolParam("show_grid", "Show the grid", default=True, group="Editing"),
        BoolParam("show_labels", "Number the rectangles", default=True,
                  group="Editing"),
        BoolParam("hover_feedback", "Highlight and read out under the cursor",
                  default=True, group="Editing",
                  help="Costs one event per pixel of mouse travel. Turn it off "
                       "if the canvas feels heavy."),
        ChoiceParam("grid_style", "Grid style", default="lines", group="Grid",
                    choices=options(
                        ("lines", "Lines"),
                        ("dots", "Dots at the intersections"),
                        ("crosses", "Small crosses")),
                    help="Dots and crosses mark the same positions as lines "
                         "without laying a mesh over the art."),
        ChoiceParam("grid_color", "Grid colour", default="white", group="Grid",
                    choices=options(
                        ("white", "White"), ("black", "Black"),
                        ("cyan", "Cyan"), ("magenta", "Magenta"),
                        ("yellow", "Yellow"), ("green", "Green"),
                        ("orange", "Orange"), ("red", "Red")),
                    help="Black is the one that reads on a bright sheet."),
        IntParam("grid_opacity", "Grid strength", default=6, min=1, max=100,
                 suffix="%", group="Grid",
                 help="How visible the fine grid is. The emphasised lines "
                      "scale with this."),
        ChoiceParam("grid_major", "Emphasise every", default=8, group="Grid",
                    choices=options(
                        (0, "Never - one flat grid"),
                        (2, "2 cells"), (4, "4 cells"),
                        (8, "8 cells"), (16, "16 cells")),
                    help="Draws a stronger line at this interval, so you can "
                         "count cells without measuring."),
        ChoiceParam("target", "Save as", default="hammerpp", group="Output",
                    choices=options(
                        ("hammerpp", "Hammer++ / Strata (.rect)"),
                        ("source2", "Source 2 KV3 (.rect)"))),
        BoolParam("write_flags", "Write rotate / reflect / alt", default=True,
                  group="Output",
                  help="Strata honours these; Hammer++ ignores them. Off keeps "
                       "the file plain."),
        BoolParam("update_vmt", "Wire up the VMT's %rectanglemap", default=True,
                  group="Output",
                  help="Adds or updates the line in the matching .vmt beside "
                       "the texture."),
        IntParam("detect_min_size", "Smallest detected region", default=16,
                 min=2, max=512, suffix="px", group="Detection"),
    ]
    HELP = """
### Hotspot Editor

Hotspot texturing means authoring one texture full of trim and panel regions,
saying where those regions are, and letting the editor pick the right one per
face. In Hammer++ or Strata you apply the material, press **Fit**, and it snaps
the face's UVs onto whichever rectangle best matches its size and aspect. One
material, one draw call, no manual aligning.

#### Getting rectangles onto the sheet

| | |
| --- | --- |
| **Draw** | Drag on empty space. |
| **Move** | Drag a rectangle. |
| **Resize** | Drag one of its eight handles. |
| **Pan / zoom** | Right-drag, and the wheel. Double-click fits the view. |
| **Layouts** | A grid or trim-sheet split, in one click. |
| **Detect** | Finds the regions automatically - see below. |

Everything snaps to the grid unless you turn snapping off. Moving snaps the
*distance*, not the position, so a rectangle that started off-grid keeps its
offset instead of jumping.

#### Keyboard

| | |
| --- | --- |
| **Delete** / **Backspace** | Delete the selection. |
| **Arrows** | Nudge by one pixel. **Shift** nudges by one grid cell. |
| **Ctrl+A** / **Esc** | Select all, or select nothing. |
| **Ctrl+D** | Duplicate. |
| **Ctrl+Z** / **Ctrl+Y** | Undo, redo. **Ctrl+Shift+Z** also redoes. |
| **Ctrl+S** | Save the `.rect`. |
| **F** / **G** | Fit the view; toggle the grid. |

Nudging deliberately ignores snapping - it is what you reach for when snap has
left a rectangle a pixel out. These keys stand down while you are typing in the
Fit fields.

#### The grid

Cell size and snapping are on the toolbar; how the grid *looks* is under
**Settings**, and changes land on the texture as you make them.

| | |
| --- | --- |
| **Style** | Lines, dots at the intersections, or small crosses. Dots and crosses mark the same positions without laying a mesh over the art. |
| **Colour** | Eight inks. White suits most Source textures; black is the one that reads on a blown-out sheet. |
| **Strength** | How visible the fine grid is, 1-100%. The emphasised lines scale with it. |
| **Emphasise every** | A stronger line every N cells, so you can count cells without measuring. |

Whatever the cell size, the grid is only drawn while the lines are far enough
apart to mean something - zoom out far enough and it doubles the interval, then
stops. So a 4 px grid is safe to leave on.

#### Detect

Two strategies, picked from what the texture carries:

* **Alpha islands** - if the sheet has real transparency, every connected opaque
  region becomes a rectangle.
* **Seams** - otherwise it cuts along rows and columns that are one flat colour,
  which is what the gutters between panels are. The cut recurses, so a sheet
  that is bands-then-cells comes out right rather than as one coarse grid.

#### Fit preview

Type a face size and the preview names the rectangle Hammer would most likely
choose, with its runners-up. Fit is deliberately random among close matches -
pressing it again re-rolls - so the ranking matters more than the winner.

Use it to answer the question a trim sheet exists to answer: *does this sheet
cover the face shapes my map actually has?* If nothing scores well for a long
thin wall, the sheet needs a long thin rectangle.

#### Saving

Writes the `.rect` beside the texture and, unless you turn it off, adds
`%rectanglemap` to the matching VMT. The reference is materials-relative and
carries no extension, which is the form Hammer expects.

Overlapping rectangles are fine and are not reported as a problem - authoring
several candidates over the same art is how you give Fit something to choose
between. Valve's own sheets overlap heavily.

#### The two `.rect` formats

Source 2 and Hammer++ both use `.rect` and are otherwise unrelated: Source 2
stores KV3 with coordinates as a fraction of the texture, Hammer++ stores plain
text in pixels. **Open** reads either, so a Half-Life: Alyx sheet's rectangles
can be imported and converted instead of redrawn.
"""
