# FortiAP Presence Tracker for Home Assistant

Home Assistant integration for controlling FortiGate firewall policies and tracking selected FortiAP Wi-Fi clients.

It provides:

- an optional verified switch for each configured firewall policy
- `device_tracker` and presence `binary_sensor` entities for selected Wi-Fi clients
- multi-device user profiles with independent home and away policy rules
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
2. Add `https://github.com/mmoud/ha-fortigate` as an **Integration**.
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

The setup form requires the FortiGate host, HTTPS port, VDOM, API token, and TLS verification setting. Firewall policy IDs are optional: leave the field empty for Wi-Fi tracking only, or enter one ID or a comma-separated list such as `61, 72, 83`.

Each supplied ID is read before the entry is saved. The returned policy name is stored as an identity guard, and Home Assistant creates a separate switch for every validated policy. Add, remove, or clear IDs later with **Configure > Firewall policies**.

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

Open **Settings > Devices & services > FortiAP Presence Tracker**, find the configured entry, and select **Configure**. The options menu includes:

- **Firewall policies** adds or removes optional firewall-policy switches.
- **Add or manage Wi-Fi trackers** discovers connected and recently seen clients, keeps existing offline trackers in the list, and accepts a MAC address manually when a device is not listed.
- **Users and policy rules** combines one or more trackers into a user and assigns independent home and away policy states.
- **Remove Wi-Fi trackers** deletes selected tracker entities, presence sensors, and their Home Assistant device entries without changing anything on FortiGate.
- **Polling and sensors** controls policy polling, Wi-Fi polling, the away grace period, and the optional client-count sensor.

Disabling Wi-Fi tracking is reversible and retains the selected devices. Use **Remove Wi-Fi trackers** when the entities should be deleted from Home Assistant.

Use **Firewall policies** in the same Configure menu to add or remove policy switches with a comma-separated list of policy IDs. The integration verifies every policy before saving the change.

On the tracker screen, enable Wi-Fi presence tracking, select one or more devices, and save. Only newly selected devices ask for a friendly name; existing names are preserved. Client selection combines the current FortiAP association list with available FortiGate device-detection and DHCP information. Each selected MAC creates two entities:

- `device_tracker.<device_name>` with `home`, `not_home`, or unavailable state
- `binary_sensor.<device_name>_presence`, which is ON at home, OFF when away, and unavailable during a FortiGate/API failure

Both entities use the same coordinator result and away grace period. The binary sensor does not add another FortiGate request.

Under **Users and policy rules**, assign a phone, watch, tablet, or other selected trackers to one user. Home Assistant creates an aggregate `device_tracker.<user>` and `binary_sensor.<user>_presence`. The user is home as soon as any assigned device is home. The user becomes away only after every assigned device is definitively away and has completed its own grace period. If no device is home and any member state is unknown, the user is unavailable rather than away.

Presence polling uses:

```text
GET /api/v2/monitor/wifi/client?vdom=<vdom>
```

FortiOS response fields vary between releases. The integration normalizes known MAC, IP, hostname, SSID, FortiAP, radio, band, channel, VLAN, username, and association-time fields. Missing optional fields are ignored.

The MAC address is the tracker's stable identity. Colon-separated, hyphenated, and compact MAC formats are normalized to the same value. Renaming a tracker does not change its unique ID.

### Presence rules

- A selected MAC seen on any managed FortiAP is `home` immediately.
- Roaming between access points does not change presence.
- A missing client remains `home` until the away grace period expires.
- A successful empty response starts the normal away timer.
- A timeout, authentication error, TLS error, invalid response, or FortiGate outage makes trackers unavailable. It does not mark them `not_home`.
- Selected clients remain configured while offline.

The default polling interval is 30 seconds and the default away grace period is 180 seconds.

Apple devices may use a private MAC address. Track the address shown by FortiGate for the required SSID. Apple's Fixed private address mode provides a stable address without disabling Private Wi-Fi Address globally.

## Parental control

The integration can apply policy states directly from presence without separate Home Assistant automations:

1. Configure the firewall policies and Wi-Fi trackers first.
2. Open **Configure > Users and policy rules**.
3. Add a user and select all of that user's tracked devices.
4. Choose policies to enable or disable while the user is home and away.
5. Repeat for each person who needs a different policy profile.

For example, a user with an iPhone and Apple Watch remains home while either device is connected. Another user can independently disable policies 1 and 2 while away. Policy fields are optional, so aggregate user trackers can also be used only in Home Assistant automations.

Rules are evaluated from aggregate current state rather than only on transitions. This makes them recover correctly after a restart or a manual policy change. When users' rules disagree about a shared policy, disable wins. Unknown aggregate presence or an unavailable Wi-Fi API leaves the policy unchanged. Every change still uses the normal policy preflight, status-only write, and read-back verification.

The presence entities and policy switches remain available for normal Home Assistant automations when more complex conditions are required.

See [Parental control with FortiGate](docs/parental-control.md) for setup, single-device and multi-device examples, policy direction, failure behavior, and testing.

## HomeKit

The policy entity is a standard Home Assistant switch and can be included in HomeKit Bridge from its UI configuration. No iOS application or Focus Mode setup is required.

## Troubleshooting

Enable debug logging temporarily from the integration's **Enable debug logging** menu. Download the resulting diagnostics after reproducing the problem. Tokens and client identities are excluded from integration diagnostics.

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
