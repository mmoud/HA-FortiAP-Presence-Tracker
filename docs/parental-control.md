# Policy control with Home Assistant automations

FortiAP Presence Tracker deliberately keeps presence detection and firewall control separate:

- device and person trackers report verified presence
- policy switches report and change the actual FortiGate policy state
- Home Assistant automations decide when one should control the other

This makes schedules, multiple people, manual exceptions, notifications, and other Home Assistant conditions available without duplicating an automation engine inside the integration.

## Before creating an automation

1. Open **FortiAP Presence** from the Home Assistant sidebar.
2. Under **Devices**, track the required phone, watch, or tablet.
3. Under **People**, optionally combine several devices into one person. A person stays home while any assigned device is home.
4. Under **Policy switches**, add the numeric ID of an existing FortiGate policy and select **Save changes**.
5. Confirm the tracker and policy switch work independently from **Settings > Devices & services > Entities**.

Use a dedicated, non-critical policy while testing. The integration changes only its `status`; it does not alter addresses, services, action, NAT, schedule, interfaces, logging, or order.

## Recommended: use the included blueprint

1. Open **FortiAP Presence > Automations**.
2. Select **Import blueprint**, preview it in Home Assistant, then select **Import**. This installs a reusable template; it does not activate any policy control.
3. Select **Create automation** for **FortiAP presence policy control**.
4. Choose one person or device presence entity.
5. Choose one or more FortiGate policy switches.
6. Choose whether those policies should be enabled or disabled while home and while away.
7. Optionally select a Schedule helper. Leave it empty for all-day operation.
8. Name, save, and enable the automation.

Create another automation from the same blueprint for each person or policy group that needs different behavior. The entity selectors show only compatible FortiAP presence entities and FortiGate policy switches.

The blueprint reacts only to exact `home`, `not_home`, `on`, or `off` transitions. `unknown` and `unavailable` do not run an action. The integration's away timeout is applied before the presence entity becomes away.

The manual examples below are useful when you need conditions or actions beyond the blueprint.

## Enable a policy when someone arrives

1. Open **Settings > Automations & scenes**.
2. Select **Create automation**, then **Create new automation**.
3. Select **Add trigger**.
4. Choose **Entity**, then **State**.
5. Select the person's aggregate device tracker, or a single device tracker.
6. Set **To** to `home`. Leave **From** empty.
7. Select **Add action**.
8. Choose **Switch: Turn on** and select the FortiGate policy switch.
9. Give the automation a clear name, such as **Enable family access on arrival**, then save it.

Using an exact `home` destination prevents an unavailable tracker from being treated as an arrival.

## Disable a policy after everyone leaves

1. Create another automation from **Settings > Automations & scenes**.
2. Add an **Entity > State** trigger for the aggregate person tracker.
3. Set **To** to `not_home`. Leave **From** empty.
4. Add **Switch: Turn off** and select the policy switch.
5. Name it **Disable family access after departure** and save it.

The integration's away timeout has already completed before the tracker changes to `not_home`. If FortiGate cannot be queried, the tracker becomes unavailable and this exact-state trigger does not run.

## Control several policies

In the automation editor, either select several FortiGate switches in the same switch action or add one action per policy. Separate actions are easier to inspect in an automation trace when one policy rejects a change.

For example, an arrival automation can turn on **School access** and **Messaging**, while the departure automation turns both off. Each switch still performs its own policy preflight, status-only update, and read-back verification.

## Use several people

Create one trigger per person when any arrival should run the action. To act only when everybody is away:

1. Use one person's `not_home` transition as the trigger.
2. Add an **Entity state** condition for every other person.
3. Require each condition to be `not_home`.

If the same policy has several automations, document their purpose in their names and avoid opposite actions for the same event.

## Add a schedule

1. Create a Schedule helper under **Settings > Devices & services > Helpers**.
2. Open the presence automation.
3. Select **Add condition > Entity state**.
4. Select the Schedule helper and require it to be **On**.

Home Assistant will then change the policy only when both the presence trigger and schedule condition match. If the policy must also be reconciled when the schedule itself changes, create a second automation triggered by that Schedule helper.

## Restriction policies

First decide what an enabled FortiGate policy means:

- For an access policy, arrival normally turns it on and departure turns it off.
- For a restriction policy, arrival may turn it off and departure may turn it on.

Choose the switch action that matches the policy's tested behavior; the integration does not infer intent from the policy name.

## Manual control and HomeKit

Without an internal rule engine, a manual switch change remains in place until a Home Assistant automation explicitly changes it. The same verified switch can be placed on a dashboard or exposed through HomeKit Bridge.

## Test from the UI

1. Open the automation and select **Run actions** with a non-critical test policy.
2. Confirm the policy state in FortiGate and on the Home Assistant switch.
3. Use **Traces** on the automation page to confirm the trigger, conditions, and action.
4. Test a real arrival and departure after the configured away timeout.
5. Temporarily make FortiGate unavailable. The tracker should become unavailable, not `not_home`, and the departure automation must not run.
6. Restore connectivity and confirm tracker and switch state recover from fresh FortiGate reads.

Wi-Fi presence is an automation signal, not an identity or security boundary. Apple devices should use a stable address such as **Fixed Private Wi-Fi Address** for the tracked SSID.
