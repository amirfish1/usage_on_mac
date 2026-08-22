# Restore Expanded CCC View

## Goal

Keep CCC as the preferred usage-data source while rendering the same detailed
xbar dropdown that is used by the direct Chrome/cache path.

## Design

The plugin will normalize a fresh CCC response into the existing rendering
variables instead of returning through `render_from_ccc`. CCC remains the
authority for Claude percentages, reset times, pace details, Codex usage, and
the source timestamp. The shared detailed renderer continues to add local
active-session context, update information, and menu actions.

CCC does not currently expose Anthropic extra-usage billing data, so that
optional section will be omitted on the CCC path. The plugin will not perform a
second Chrome fetch solely to populate it. If CCC is missing, stale, or in the
middle of an incomplete refresh, the existing Chrome/cache fallback remains
unchanged.

## Verification

Add a subprocess regression test with a local CCC fixture. It must prove that
the CCC path includes the detailed pace breakdown and footer actions and no
longer includes the compact `via CCC` marker. Run the complete unittest suite,
compile the Python sources, and inspect a live plugin invocation.
