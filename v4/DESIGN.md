# Source Backport visual rules

## Identity

Name: **Source Backport**. Purpose: **Source 2 → Source 1 asset porting**.
Always show the direction in the desktop identity or title; the name alone must
not carry the explanation. V4 describes the generation rather than the product.
The mark is two offset engine-number blocks joined by a return arrow. Its source
is `AppIdentity.DrawMark`; the executable icon is a small generated copy.

## Direction

Three structures were considered: a compact workbench with a persistent tool
rail, a drafting interface led by a map canvas and inspector, and a studio layout
with top-level mode tabs. The workbench was selected because tools and add-ons are
the current navigation unit. It supports a future viewer or soundscape workspace
without making every utility look like a map editor.

The previous V4 layout used a full-width wordmark, a plain tree, and a startup-log
table. This version moves identity into the tool rail, uses clearer navigation
groups, places workspace commands in a narrow toolbar, and starts on actionable
tasks, project access, and recent results. Logs remain available with jobs.

## Palette and type

Graphite uses warm charcoal, muted stone text, and copper accents. Blueprint uses
deep slate and cyan. Paper uses pale stone, dark ink, and burnt orange. Accent
colors are personalizable. Selection uses a quiet tint; primary actions use a
solid accent with a computed contrasting foreground.

Use a system UI font and a monospace face only for paths, technical identifiers,
and compact counters. Hierarchy comes from type size, weight, alignment, and
surface separation. Borders are thin and structural. Keep corners square.

## Interaction

Use flat task rows, conventional forms, and tables for real tabular data. Keep
the asset list dense and readable. Preserve native keyboard focus and accessible
control names. Primary buttons indicate the next meaningful action. Appearance
changes update controls in place and persist locally.

Avoid decorative gradients, glass, large background imagery, oversized headings,
floating cards, and animation. The interface remains a native .NET desktop app.
Page factories, stream-based asset reads, and disposal rules still govern resource
use; appearance features do not create hidden preview sessions.

## Validation

Inspect the rendered workshop, personalization form, generated tool forms, and
asset browser. Check dark and light palettes, custom accents, larger text, compact
spacing, keyboard navigation, and persistence across launch. Local checks cover
palette contrast, normalization, settings round trips, and repeated style passes.
