# Hotspot editor

Open **Materials > Hotspot editor** in Source Backport. This is a native C#
replacement for the V2/V3 hotspot workflow; it does not invoke Python or a browser.

## Editing

- **Open material** resolves a VMT's `$basetexture` and existing `%rectanglemap`
  (also accepts `$rectanglemap`). It searches the material's content root, nearby
  textures, and sibling Garry's Mod add-ons. **Open texture** finds a nearby VMT
  that references it. Neither action modifies the material.
- **Open .rect / JSON** imports either rectangle dialect or an editable document.
  Set the original texture dimensions in **Sheet** before importing a standalone
  `.rect`; those files do not contain texture dimensions.
- Drag empty space to draw, drag a rectangle to move, and drag its eight handles
  to resize. Hold Alt or enable **Draw over regions** to draw overlapping regions.
  Shift-click toggles selection; Shift-drag selects enclosed regions. Overlap is
  intentional and valid. Smallest regions win hit-testing inside overlaps.
- Edit exact X/Y/width/height and Source 2 insets in **Rectangles**, then use
  **Apply coordinates**. Flags apply to all selected regions. Invalid geometry is
  reported and cannot be exported.
- **Sheet** contains grid and trim layouts, automatic region detection, dimension
  scaling, output choices, and the approximate fit preview. Layout and detection
  changes are undoable. Detection finds alpha islands or flat interior seams.
  **Fix oversized regions** repairs exact 2x/4x/8x/16x coordinate mismatches left
  after a texture was downscaled, without silently changing imported geometry.
- **View / help** configures grid lines, dots, crosses, ink, opacity, and major
  intervals. The grid adapts to zoom and follows the application's appearance.

| Key / gesture | Action |
| --- | --- |
| Wheel | Zoom around cursor |
| Right / middle drag | Pan |
| F / middle double-click | Fit view |
| G | Show/hide grid |
| Arrows / Shift + arrows | Move selected regions by 1px / one grid cell |
| Ctrl+A / Esc | Select all / clear selection or cancel drag |
| Ctrl+D / Delete | Duplicate / remove selected regions |
| Ctrl+Z / Ctrl+Y / Ctrl+Shift+Z | Undo / redo |
| Ctrl+S | Save |

Canvas shortcuts leave numeric/text fields alone. Moving snaps the displacement,
preserving an existing off-grid offset. Arrow nudges intentionally ignore snapping.

## Saving and recovery

**Save** and **Save as** write a `.rect`. Existing files are replaced atomically
with their previous contents retained in `.bak`. Optional **Link VMT when saving**
updates the root material reference with a materials-relative, extensionless path.
Linking requires a Hammer++ output within that material's `materials` folder.
A Source 2 export cannot overwrite the rectangle map referenced by a VMT.

Switching pages retains the document and 64 geometry undo states, but releases the
decoded image. The add-on also writes an automatic recovery draft under
`.state/addons/hotspot/hotspot-draft.json` after completed edits. Restarting restores
that document and its output paths; undo history is session-only. The recovery
draft does not replace saving a `.rect`. Opening another sheet explicitly replaces
the recovery draft and asks before discarding unsaved edits.

## Formats and limits

| Format | Coordinates | Properties |
| --- | --- | --- |
| Hammer++ / Strata `.rect` | Original texture pixels | rotate, reflect, alt; Hammer++ ignores these flags |
| Source 2 text KV3 `.rect` | 0..32768 fixed-point independently on each axis | allowRotation, allowTiling, inset |
| Editable `.json` | Original texture pixels | All editor properties and format-loss warnings |

Conversion reports incompatible properties. Named Source 2 sets are flattened;
set rotation/tiling defaults are applied to their rectangles. Unknown KV3 fields
produce explicit loss warnings. Source 2 coordinates are rounded to integer pixels
on import, so very fine subpixel boundaries can quantize. The fit preview uses the
V3 aspect/area heuristic; it is not Hammer's unpublished randomized algorithm.

Previews support PNG, JPEG, BMP, GIF, TIFF, true-color/grayscale TGA (raw or RLE),
and PC VTF 7.0-7.5 with DXT1/3/5 or common 8/16/24/32-bit pixel formats. VTF opens
one frame, face, and depth slice and prefers a mip at or below 2048 pixels. Only
one preview bitmap, at most 2048 pixels on its longest side, is retained per page.
Detection runs on that bounded preview and maps results to original coordinates.
Image loads and detection run off the UI thread; dragging only repaints overlays.

Maximum original image area is 64 million pixels; maximum document dimension is
65535 and maximum rectangle count is 4096. Input text is limited to 4 MiB. Other
VTF variants and compiled Source 2 textures should be exported to PNG first using
an external tool such as Source 2 Viewer. This editor does not modify VTF
image/resource data.

## CLI and MCP

Every operation below is also discovered as a typed MCP tool. Use absolute paths.
The same format, validation, texture, and detection code backs the desktop.
These supporting operations do not appear as separate sidebar tools.

```powershell
.\v4\Backport.cmd describe hotspot/inspect
.\v4\Backport.cmd run hotspot/inspect --input 'C:\work\panel.rect' --width 2048 --height 1024
.\v4\Backport.cmd run hotspot/generate --width 2048 --height 1024 --layout trim-124
.\v4\Backport.cmd run hotspot/detect --texture 'C:\work\panel.vtf' --minimum 16 --snap 8
.\v4\Backport.cmd run hotspot/convert --input 'C:\work\sheet.json' --format hammerpp
.\v4\Backport.cmd run hotspot/link-material --material 'C:\mod\materials\demo\panel.vmt' --rect 'C:\mod\materials\demo\panel.rect'
```

Inspection, generation, and detection produce `sheet.json` plus a validation report
in the job output directory. Generation/detection also write a Hammer++ `.rect`.
Agents edit `sheet.json` and call `hotspot/convert` to validate and export. Conversion
defaults to the job folder; an explicit `--output` path is backed up before replacing.
Detection also accepts a VMT as `--texture` and resolves its base texture automatically.
The input cannot be overwritten. Material linking is a separate explicit operation
and validates the rectangle map against the resolved base texture first.
Inspection reports a `suggestedScaleDivisor` for oversized pixel sheets; conversion
applies that repair only with `--repair_scale true`.

```json
{
  "version": 1,
  "width": 1024,
  "height": 1024,
  "rectangles": [
    { "x": 0, "y": 0, "width": 512, "height": 256, "rotate": true }
  ]
}
```

Rectangle indices in inspection/fit reports are zero-based. The desktop displays
one-based labels. Empty sheets and duplicates are warnings; out-of-bounds, zero-area,
and invalid-inset rectangles prevent export. Unknown JSON keys are rejected.

References: [Hotspot texturing](https://developer.valvesoftware.com/wiki/Hotspot_texturing),
[Valve VTF layout](https://github.com/ValveSoftware/source-sdk-2013/blob/master/src/public/vtf/vtf.h).
The hotspot wiki was unavailable (HTTP 403) during implementation; format behavior
was checked against the repository's V2/V3 implementation and installed Alyx files.
