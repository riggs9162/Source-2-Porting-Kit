# V4 ideas

## Optional separate static model output

Recorded from Riggs on 2026-10-28. This is a future model-porting feature request,
not an implemented V4 operation.

When a model has no physics or a single rigid physics body, optionally compile
an additional, completely separate model with `_static` appended to its name.
Exclude models with multiple articulated physics bodies, such as ragdolls.

Keep the regular model output available. Give the regular and static versions
independent texture-resolution parameters and separate material paths so their
lighting settings cannot overwrite each other.

The static version must use Source 1 static-prop lighting rules: compile with
`$staticprop`, bake normal-map detail into the base texture, and omit runtime
`$bumpmap`, `$normalmap`, and `$phong` from every material and skin. Preserve
cubemap reflections, reflection masks, authored metalness darkening, opacity,
tint, and emission where supported.

Keep the additional static output completely optional. Validate both model
outputs and their material dependencies independently.
