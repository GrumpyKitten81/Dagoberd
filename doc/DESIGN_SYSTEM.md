# Shared design system

Dagoberd uses a light theme with warm surfaces, petrol accents and semantic status colors. The GUI, profile reports and HTML analyses share the same system fonts and components. No fonts, frameworks or other resources are downloaded at runtime.

## Resource ownership

`src/dagoberd/ui/styles/` is the single source of presentation foundations:

- `tokens.css`: colors, typography, spacing, radii, shadows and motion. Existing color-variable names remain the semantic API; saved/conflict/failed states alias their corresponding status surfaces.
- `components.css`: controls, focus states, cards, dialogs, tables, chips and badges.
- `report.css`: standalone report layout, responsive rules and print treatment.

The GUI loads tokens and components before its view styles. These resources participate in the frontend content hash and are served under `/assets/<digest>/design/`. Changing a shared resource invalidates the asset namespace.

`dagoberd.ui.report_styles()` embeds all three resources in newly generated HTML reports. The resource loader does not import GUI or ledger services. Reports remain usable offline; already generated files are not rewritten.

## Components

| Element or class | Responsibility |
| --- | --- |
| Native button, input, select, textarea | Control typography, border, background, disabled state and focus |
| `button.primary` | Main action with petrol background |
| `.ui-card` | Shared surface, border, card radius and subtle shadow |
| `.ui-dialog` | Dialog surface, viewport bounds, backdrop and action footer |
| `.ui-table` | Table typography, headers, separators, hover and numeric alignment |
| `.ui-chip` | Compact filter/tag presentation |
| `.ui-badge` | Status presentation; `warning`, `positive`, `negative` provide semantic variants |

Retain feature classes alongside these foundations, for example `class="ui-card panel"` or `class="ui-table transactions compact-grid"`. Feature styles own arrangement and behavior: grid columns, sticky positioning, overflow, widths, chart geometry and compact table density. Do not duplicate component surfaces or introduce literal colors and font stacks in view styles.

Use the spacing scale (4/8/12/16/24/32/48px), control/card/dialog radii (6/10/14px) and typography tokens. Smaller local offsets are permitted for dense cells and icon alignment. Zero radii on docked panels, pill shapes, structural table borders and geometry-specific sizes are deliberate layout exceptions. Status text and accessible labels remain necessary; color alone is insufficient.

## Extending and verifying

Reuse an existing semantic token before adding another. Put reusable presentation in components; keep report-only layout in `report.css`. Avoid broad selectors that override the compact grid's dimensions or dialog behavior. Transitions use the shared duration, which becomes zero for reduced motion.

Check all GUI tabs at 320, 800 and 1280px in English, German and Spanish. Check populated, empty, selected, disabled and error states, keyboard focus and narrow dialogs. Open both HTML report types offline, exercise profile filtering and details, and verify print media. Print treatment removes decoration without changing filters or opening closed sections. Analysis tables keep values on one line on screen inside horizontal scroll containers; print media allows wrapping.

The palette contrast tests cover normal text, semantic statuses, primary actions, focus and control boundaries. Browser tests compare shared presentation across GUI and report components and reject external report resource requests. Installed-artifact tests verify resource availability and GUI serving independently of the checkout.
