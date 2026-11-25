# Design direction

## Technical field guide

Control Freak should read like a precise service tool built for a television, not a web dashboard enlarged to 720p and not an imitation arcade menu.

### Visual system

- **Palette:** deep blue-black shell, layered slate panels, warm off-white text, amber for active focus and section location, teal for healthy or enabled state, red only for actionable faults.
- **Typography:** sturdy humanist sans, large enough for couch use; uppercase micro-labels provide orientation, while titles and descriptions remain sentence case.
- **Density:** four settings rows per 720p viewport. Descriptions appear once in the row and once as focused contextual help only when the extra space improves comprehension.
- **Signature move:** the settings index behaves like a field-guide thumb index. Numbered sections remain visible while the right side exposes the selected setting as a short field note.

### Interaction rules

- Focus is always visible as a two-pixel amber outline.
- Moving right enters the settings rows; moving left returns to the section index.
- Disabled values explain why they are unavailable.
- Potentially disruptive actions use a confirmation layer with explicit consequences.
- Color reinforces labels and never carries meaning alone.
- Stable is the default update channel; nightly is explicit.

### Avoid

- neon arcade nostalgia
- simulated terminals, CRT effects, or skeuomorphic control panels
- card grids used as navigation
- tiny dashboard typography
- icon-only controls
- continuous animation
