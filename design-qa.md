# Mobile UI QA

- Viewport: 390 × 844 CSS pixels, DPR 1, Firefox Responsive Design Mode.
- Reference: live 3.2.9 dashboard before the responsive update.
- Result: live 3.3.1 dashboard after the update.
- Screenshots remain local because they contain installation-specific names and network details.

## Review

- All four sections are visible without horizontal navigation.
- Overview metrics fit in a two-column grid and retain their color hierarchy.
- Header actions and the fixed save action remain reachable without covering the active controls.
- People creation, device assignment, policy selection, and settings fields fit the phone width.
- Wide data tables become labeled cards on phones; desktop table rendering is unchanged.
- Overview, People & devices, Policies & rules, and Settings were opened against the live Home Assistant installation.
- No configuration values were changed during the visual check.

final result: passed
