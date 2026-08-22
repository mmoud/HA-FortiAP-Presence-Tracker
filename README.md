# FortiAP Presence Tracker for Home Assistant

Home Assistant integration for controlling FortiGate firewall policies and tracking selected FortiAP Wi-Fi clients.

It provides:

- an optional verified switch for each configured firewall policy
- `device_tracker` and presence `binary_sensor` entities for selected Wi-Fi clients
- multi-device presence users for phones, watches, and tablets
- policy-centric ANY/ALL rules with priorities and optional Schedule helpers
- dry-run mode, expiring policy overrides, and per-policy decision sensors
- an optional sensor showing the number of associated Wi-Fi clients
- a read-only button for immediately refreshing policy and presence data
- full setup and configuration through the Home Assistant UI

## Requirements

- Home Assistant 2026.8 or newer
- FortiGate reachable from Home Assistant over HTTPS
- FortiOS REST API token
- FortiAPs managed by the configured FortiGate for Wi-Fi tracking

## Installation

1. In HACS, open **Integrations > Custom repositories**.
2. Add `https://github.com/mmoud/HA-FortiAP-Presence-Tracker` as an **Integration**.
3. Download **FortiAP Presence Tracker** and restart Home Assistant.
4. Open **Settings > Devices & services > Add integration**.
5. Search for **FortiAP Presence Tracker** and complete the form.

No YAML configuration is required.

The FortiGate device includes a **Refresh data** button. It requests an immediate read of every configured policy and the shared Wi-Fi client dataset; it does not change firewall configuration.

## FortiGate API account

Create a dedicated REST API administrator, for example `homeassistant-api`. Tracker-only installations need read access to Wireless Controller monitor data. Firewall-policy switches additionally require read/write access to firewall policies in the required VDOM.

FortiOS permission profiles are generally scoped to configuration areas, not to one policy ID. Use the integration's expected-policy-name check as an additional safeguard, and restrict the API administrator's trusted hosts to the Home Assistant IP where possible.

Keep the FortiGate management interface on a private network. Do not expose it to the internet.

## Policy switch

The setup form requires the FortiGate host, HTTPS port, VDOM, API token, and TLS verification setting. Firewall policy IDs are optional. Leave the field empty when the integration will only create presence trackers and people. Enter one ID or a comma-separated list such as `61, 72, 83` only when Home Assistant should create switches for those existing policies or use them in built-in parental-control rules.

Each supplied ID is the numeric ID of an existing FortiGate firewall policy. It is read before the entry is saved, its returned name is stored as an identity guard, and Home Assistant creates a separate switch for every validated policy. The integration changes only the policy's enabled/disabled status. Add, remove, or clear IDs later on **Policies & rules**; clearing them does not remove Wi-Fi trackers or people.

The switch uses these endpoints:

```text
GET /api/v2/cmdb/firewall/policy/<policy-id>?vdom=<vdom>
PUT /api/v2/cmdb/firewall/policy/<policy-id>?vdom=<vdom>
```

The PUT body changes only the policy status:

```json
{"data":{"status":"enable"}}
```

or:

```json
{"data":{"status":"disable"}}
```

Before a write, the integration reads the selected policy and checks its ID and stored name. After the write, it reads that policy again and updates its switch only after FortiGate reports the requested state. A failed or unreliable read makes that switch unavailable; it is never interpreted as OFF.

TLS certificate verification is enabled by default. Disable it only when the FortiGate uses a certificate that Home Assistant cannot validate. With verification disabled, the connection is encrypted but the FortiGate's identity is not verified.

## Wi-Fi presence tracking

Open **Settings > Devices & services > FortiAP Presence Tracker** and select **Configure**. Version 3 provides a full-width management page instead of placing normal administration in a sequence of small dialogs. The same page is also available from **FortiAP Presence** in the Home Assistant sidebar.

The page is organized into four wide views:

- **Overview** shows connection health, configuration counts, safety behavior, and every person/device assignment.
- **People & devices** puts people management first. Create, rename, assign, and remove people in place, then manage tracker state, SSID/FortiAP details, friendly names, SSID scopes, and client discovery on the same page. The discovery table scrolls independently so a large FortiGate catalog does not hide people controls.
- **Policies & rules** manages optional verified firewall policy IDs and presence rules together.
- **Settings** contains polling, away timing, enforcement, dry-run mode, override duration, retention, and sensors.

Changes remain local to the page until **Save changes** is selected. The backend then validates the complete configuration, re-reads every configured policy using its saved name guard, updates the config entry atomically, and reloads the integration. Invalid input does not partially modify the integration.

The management page is bundled with the HACS integration; no separate frontend repository, card, add-on, or YAML resource is required. It is restricted to Home Assistant administrators. The API token and authorization header are never sent to the panel.

Disabling Wi-Fi tracking is reversible and retains the selected devices. Remove a row from **People & devices** and save when its tracker and presence entities should be deleted from Home Assistant.

Use **Policies & rules** to add or remove policy IDs. The integration verifies every policy and its existing name guard before saving the change.

On the tracker screen, enable Wi-Fi presence tracking, select one or more devices, and save. Only newly selected devices ask for a friendly name; existing names are preserved. Client selection combines the current FortiAP association list with available FortiGate device-detection and DHCP information. Each selected MAC creates two entities:

- `device_tracker.<device_name>` with `home`, `not_home`, or unavailable state
- `binary_sensor.<device_name>_presence`, which is ON at home, OFF when away, and unavailable during a FortiGate/API failure

Both entities use the same coordinator result and away grace period. The binary sensor does not add another FortiGate request.

On **People & devices**, assign a phone, watch, tablet, or other selected trackers to one person. Home Assistant creates an aggregate `device_tracker.<user>` and `binary_sensor.<user>_presence`. The person is home as soon as any assigned device is home. The person becomes away only after every assigned device is definitively away and has completed its own grace period. If no device is home and any member state is unknown, the person is unavailable rather than away.

The **People and devices** landing page lists all people and their assignments without opening an edit form. If every tracked device is already assigned, guided parental-control setup asks which existing person the new rule should follow instead of requiring another tracker.

Each user has its own away grace period. This can extend the base device grace for phones or watches that sleep aggressively. A device can belong to only one user, preventing accidental duplicate presence profiles.

Presence polling uses:

```text
GET /api/v2/monitor/wifi/client?vdom=<vdom>
```

FortiOS response fields vary between releases. The integration normalizes known MAC, IP, hostname, SSID, FortiAP, radio, band, channel, VLAN, username, and association-time fields. Missing optional fields are ignored.

The MAC address is the tracker's stable identity. Colon-separated, hyphenated, and compact MAC formats are normalized to the same value. Renaming a tracker does not change its unique ID.

By default, an association on any FortiGate-managed SSID means home. To scope a device, enter one or more comma-separated SSIDs in its **Allowed SSIDs** field on **People & devices**. A device seen on another SSID is treated as absent and follows the normal away grace period. SSID matching is case-sensitive. API failures still make the tracker unavailable; they are never treated as an SSID mismatch.

### Presence rules

- A selected MAC seen on any managed FortiAP is `home` immediately.
- Roaming between access points does not change presence.
- A missing client remains `home` until the away grace period expires.
- A successful empty response starts the normal away timer.
- A timeout, authentication error, TLS error, invalid response, or FortiGate outage makes trackers unavailable. It does not mark them `not_home`.
- Selected clients remain configured while offline.

The default polling interval is 30 seconds and the default away grace period is 180 seconds.

Recently discovered clients are retained for a configurable number of days and then pruned from the bounded discovery list unless they are selected trackers. The native device selector is searchable and lists the last observation time when it is known.

Apple devices may use a private MAC address. Track the address shown by FortiGate for the required SSID. Apple's Fixed private address mode provides a stable address without disabling Private Wi-Fi Address globally.

## Parental control

The integration can apply policy states directly from presence without separate Home Assistant automations. Add the person's devices and assignments on **People & devices**, then configure the verified policies and behavior on **Policies & rules**. Review the complete page before saving.

For more complex installations, use **People & devices** to maintain people and **Policies & rules** to create rules involving multiple people, priorities, or schedules.

For example, a user with an iPhone and Apple Watch remains home while either device is connected. Another user can independently disable policies 1 and 2 while away. Policy fields are optional, so aggregate user trackers can also be used only in Home Assistant automations.

Rules are evaluated from aggregate current state rather than only on transitions. This makes them recover correctly after a restart or a manual policy change. When users' rules disagree about a shared policy, disable wins. Unknown aggregate presence or an unavailable Wi-Fi API leaves the policy unchanged. Every change still uses the normal policy preflight, status-only write, and read-back verification.

### Policy automation rules

For installations with several users, use **Policies & rules**. A rule contains:

- one or more presence users
- **Any user** or **All users** matching
- required state: home or away
- one or more target policies
- enable or disable action
- priority from 0 to 100
- an optional Home Assistant `schedule` entity

The integration shows a complete preview and requires confirmation before saving. A schedule that is OFF makes the rule inactive. An unavailable schedule or an unknown required presence blocks the affected policy rather than guessing.

Only the highest-priority matching rules control a policy. If rules at the same winning priority request opposite states, disable wins and the conflict is exposed by the decision sensor. Existing user-attached rules continue to work at priority 0.

**Configure > Advanced settings > Polling, safety, and sensors** also provides:

- **Enable automatic policy enforcement**: global maintenance pause. Decisions remain visible, but no automatic policy writes occur.
- **Dry run**: evaluate presence rules without changing FortiGate. Manual override selections remain deliberate commands and are still applied.
- **Default manual override duration**: how long a forced or paused policy remains overridden; `0` means until changed or Home Assistant restarts.

Every rule-managed policy receives:

- `sensor.fortigate_policy_<id>_automation_decision`, showing the calculated action, reason, conflict, dry-run state, last application, and error
- `select.fortigate_policy_<id>_automation_override` with Automatic, Force enabled, Force disabled, and Paused

Overrides are intentionally kept in memory. A Home Assistant restart returns control to Automatic, preventing an old forced state from being silently restored. Verified changes appear in Activity and fire a `fortigate_policy_decision` event containing only the policy ID, verified state, and safe decision reason. Repeated enforcement failures create a Home Assistant Repair issue.

For a one-off duration, call the **FortiAP Presence Tracker: Set policy automation override** action from Home Assistant's action UI. Supply the config entry ID, policy ID, mode, and duration in minutes. This does not require YAML.

The presence entities and policy switches remain available for normal Home Assistant automations when more complex conditions are required.

See [Parental control with FortiGate](docs/parental-control.md) for setup, single-device and multi-device examples, policy direction, failure behavior, and testing.

## HomeKit

The policy entity is a standard Home Assistant switch and can be included in HomeKit Bridge from its UI configuration. No iOS application or Focus Mode setup is required.

## Troubleshooting

Enable debug logging temporarily from the integration's **Enable debug logging** menu. Download the resulting diagnostics after reproducing the problem. Tokens and client identities are excluded from integration diagnostics.

Diagnostics identify `fortiap_association` as the presence source and report whether per-tracker SSID filters are in use. FortiOS version is read from the Wi-Fi response when available; otherwise the integration tries `/api/v2/monitor/system/status` once as optional diagnostic enrichment. Failure of that optional request never affects presence.

Common checks:

- **401/403:** replace the API token or correct the administrator permissions.
- **404:** check the VDOM, policy ID, and whether the Wi-Fi monitor endpoint is available in the installed FortiOS release.
- **Policy unavailable:** confirm the policy ID and expected policy name match exactly.
- **TLS failure:** install a trusted certificate on the FortiGate or, for an isolated test only, disable certificate verification.
- **No clients listed:** confirm the FortiGate manages the FortiAPs and the API administrator can read Wireless Controller monitor data. You can still enter the device MAC manually on **Add or manage Wi-Fi trackers**.
- **Duplicate phone names:** choose the MAC used on the intended SSID; private Wi-Fi addresses can create multiple records.

Test Wi-Fi API access from a trusted host without putting the token in the URL:

```sh
curl --fail --silent --show-error \
  --header "Authorization: Bearer $FORTIGATE_API_TOKEN" \
  --header "Accept: application/json" \
  "https://FORTIGATE_HOST:FORTIGATE_PORT/api/v2/monitor/wifi/client?vdom=FORTIGATE_VDOM"
```

## Removal

Remove the config entry from **Settings > Devices & services**, then remove the integration from HACS and restart Home Assistant.
