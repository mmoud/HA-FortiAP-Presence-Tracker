# Dashboard UI QA

- Viewport: 390 × 844 CSS pixels, DPR 1, Firefox Responsive Design Mode.
- Reference: live 3.3.1 dashboard before this change.
- Result: live 3.4.2 dashboard after deployment and restart.
- Screenshots remain local because they contain installation-specific names and network details.

## Review

- Home Assistant and Refresh are distinct purple and cyan actions; Track/Add, Remove, Disable policy, and Save changes use consistent green, red, orange, and blue treatments.
- People and Devices are separate tabs. Settings spans the final mobile navigation row without an uneven empty cell.
- New and existing name fields have visible labels, a two-pixel blue boundary, and a tinted editable surface.
- Tracked devices and discovered clients render as labeled mobile cards with reachable Track and Remove actions.
- Configured policies show their verified state and a direct enable/disable action. The action changes only `status` and updates the UI only after readback succeeds.
- A rejected live policy action displayed an error and correctly retained the previously verified state.
- Header navigation, all five tabs, the fixed save action, discovery, and policy controls remain reachable at phone width without horizontal page overflow.
- Before/after comparisons used the same theme, data, viewport, and browser.

final result: passed
