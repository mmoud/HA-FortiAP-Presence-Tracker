# Parental control with FortiGate

This integration supplies:

- a presence binary sensor for each selected Wi-Fi client
- a verified switch for each selected FortiGate firewall policy
- optional per-tracker rules that map home and away state to policy state

The built-in rules cover normal per-device parental control. Home Assistant automations remain available for schedules, groups, or other conditions.

## Before starting

Use a dedicated FortiGate policy whose match criteria and position have already been tested. Decide what its enabled state means:

- an access policy grants access while enabled
- a restriction policy blocks or limits access while enabled

The integration only changes `status`. It does not change the policy action, addresses, services, schedule, NAT, interfaces, or ordering.

In Home Assistant, confirm these entities work independently before creating an automation:

- `binary_sensor.child_phone_presence`
- `switch.fortigate_policy_61`

Use the actual entity IDs from **Settings > Devices & services > Entities**.

## Built-in presence policy rules

Open **Settings > Devices & services > FortiAP Presence Tracker > Configure > Presence policy rules**. Select a tracker, then choose any combination of:

- policies to enable while home
- policies to disable while home
- policies to enable while away
- policies to disable while away

Save and repeat for other trackers. Each tracker keeps an independent profile and can control one policy or several. Leave all four lists empty to remove that tracker's profile.

Rules are state-based and are reconciled after valid Wi-Fi or policy updates. If several profiles affect the same policy, disable wins. If any relevant tracker is unknown, the integration does not change that policy. A failed Wi-Fi poll therefore cannot act like a departure.

The examples below use normal Home Assistant automations as an alternative when conditions beyond tracker state are needed.

## One device

This example makes an access policy follow one device. It enables the policy when the device connects and disables it after the device has been absent for the configured grace period.

### Create it in the Home Assistant UI

1. Open **Settings > Automations & scenes > Create automation > Create new automation**.
2. Add an **Entity state** trigger for the child's presence binary sensor, changing to **On**.
3. Add a **Switch: Turn on** action and select the FortiGate policy switch.
4. Save the automation as `Child Wi-Fi access - enable on arrival`.
5. Create a second automation with the same presence sensor changing to **Off** and a **Switch: Turn off** action.

For a restriction policy, use **Turn off** on arrival and **Turn on** on departure. No YAML configuration is required.

### YAML reference

```yaml
alias: Child Wi-Fi access - enable on arrival
triggers:
  - trigger: state
    entity_id: binary_sensor.child_phone_presence
    to: "on"
actions:
  - action: switch.turn_on
    target:
      entity_id: switch.fortigate_policy_61
mode: single
```

```yaml
alias: Child Wi-Fi access - disable after departure
triggers:
  - trigger: state
    entity_id: binary_sensor.child_phone_presence
    to: "off"
actions:
  - action: switch.turn_off
    target:
      entity_id: switch.fortigate_policy_61
mode: single
```

For a restriction policy, reverse the two switch actions.

The triggers only match `on` and `off`. They do not treat `unavailable` as away, so a FortiGate outage does not run the departure automation.

## Several devices sharing one policy

Create a binary-sensor Group helper when one policy should remain enabled while any selected device is home:

1. Open **Settings > Devices & services > Helpers**.
2. Select **Create helper > Group > Binary sensor group**.
3. Add the FortiGate presence binary sensors.
4. Leave **All entities** disabled.
5. Use the group entity in the two automations above.

The group is `on` when at least one member is on and `off` when all available members are off. If a FortiGate outage makes every member unavailable, the group is unavailable rather than off. This follows Home Assistant's documented [binary-sensor group behavior](https://www.home-assistant.io/integrations/group/#binary-sensor-light-and-switch-groups).

## Grace period

Departure delay belongs in the integration's **Away grace period** setting. The default is 180 seconds. A device that reappears before the timer expires remains present and does not trigger the departure automation.

Avoid adding another automation delay unless a second delay is intentional. Arrival remains immediate after the next successful FortiGate poll.

## Manual override

For a policy managed by a built-in presence rule, a manual switch change is reconciled back to the rule's current desired state after the next valid policy or Wi-Fi update.

A Home Assistant presence automation runs only when its binary sensor changes state. With that approach, a manual policy change remains until the next matching presence transition.

For a longer override, disable the relevant Home Assistant automation rather than changing the FortiGate policy definition.

## Failure behavior

- FortiGate unreachable, authentication failure, TLS failure, invalid JSON, or monitor API failure: presence becomes unavailable; no departure trigger fires.
- One successful poll without the client: the away grace timer starts; the device remains present.
- Client returns during the grace period: the timer clears without an away transition.
- Policy write rejected: the policy switch retains the state read back from FortiGate and the action fails in Home Assistant.
- Policy update accepted but not applied: the integration re-reads the policy and reports failure instead of showing the requested state.

## Test checklist

1. Verify the selected MAC is stable for the intended SSID.
2. Confirm the presence binary sensor turns on when the device connects.
3. Confirm it turns off only after the grace period.
4. Toggle the policy switch manually and verify the FortiGate policy status both ways.
5. Run each automation manually once.
6. Test arrival and departure with a non-critical policy.
7. Make FortiGate temporarily unreachable and confirm presence becomes unavailable without changing the policy.
8. Confirm the policy's rule order and fallback behavior before relying on it.

## Limits

Wi-Fi association is a useful automation signal, not an identity or security boundary. A device may avoid the controlled path by using cellular data, another SSID, another device, or a changed private MAC address. On Apple devices, use the Fixed Private Wi-Fi Address option for the tracked SSID when available.

FortiOS permission profiles commonly grant access to a configuration area rather than one policy ID. Restrict the REST administrator by trusted host, use least privilege, and keep the FortiGate management interface private.
