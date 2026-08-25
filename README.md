<p align="center">
  <img src="brand/icon.png" alt="FortiAP Presence Tracker" width="160">
</p>

# FortiAP Presence Tracker for Home Assistant

A local Home Assistant integration for selected FortiAP Wi-Fi clients and verified FortiGate firewall-policy switches.

The two features are independent. Use presence only, policy switches only, or connect them with normal Home Assistant automations.

## Features

- selected-client discovery without creating entities for every network device
- `device_tracker` and presence `binary_sensor` entities
- multi-device people for a phone, watch, tablet, or other tracked devices
- conservative away timeout and correct FortiAP roaming behavior
- persistent first seen, last seen, connection, and network metadata
- optional new-device event, unknown-device sensor, and client-count sensor
- one verified switch per configured FortiGate policy
- importable presence-to-policy automation blueprint
- full configuration through a responsive Home Assistant panel
- centralized polling with one bulk client request per refresh

## Requirements

- Home Assistant 2026.8 or newer
- FortiGate reachable from Home Assistant over HTTPS
- a dedicated FortiOS REST API administrator
- FortiAPs managed by that FortiGate when presence tracking is used

## Installation

1. In HACS, open **Integrations > Custom repositories**.
2. Add `https://github.com/mmoud/HA-FortiAP-Presence-Tracker` as an **Integration**.
3. Download **FortiAP Presence Tracker** and restart Home Assistant.
4. Open **Settings > Devices & services > Add integration**.
5. Search for **FortiAP Presence Tracker** and complete the form.

No YAML is required. After setup, use **FortiAP Presence** in the sidebar for day-to-day configuration.

## Operating modes

### Presence only

Leave the policy list empty. Enable network tracking, select devices under **Devices**, and optionally combine them under **People**. Firewall-policy write permission is not needed.

### Policy switches only

Disable network tracking and add one or more existing policy IDs under **Policy switches**. Home Assistant creates an independent switch for every verified policy. FortiAP access is not required.

### Presence and policy control

Configure both features, then create Home Assistant automations that use a tracker as the trigger and a policy switch as the action. This keeps schedules, conditions, traces, notifications, and manual exceptions in Home Assistant's standard automation system.

The **Automations** tab provides a one-click blueprint import and links to Home Assistant's automation editor. The blueprint supports a person or device presence entity, multiple policy switches, independent at-home and away actions, and an optional Schedule helper. Importing it does not create or activate an automation; Home Assistant shows a normal form where you choose the entities and save each automation you want.

See [Policy control with Home Assistant automations](docs/parental-control.md) for the blueprint workflow and manual GUI alternatives. No YAML editing is required.

## FortiGate API account

Create a dedicated REST API administrator such as `homeassistant-api`.

- Presence tracking needs read access to Wireless Controller monitor data.
- Policy switches need read/write access to firewall policies in the configured VDOM.
- A combined installation needs both.

FortiOS permission profiles normally cover a configuration area rather than one policy ID. Restrict the administrator's trusted hosts to the Home Assistant address, use the least-permissive profile that works on the installed FortiOS release, and keep the management interface private. Do not expose it to the Internet.

## Policy switches

Policy IDs are optional and may be added or removed later. Each ID must identify an existing IPv4 firewall policy in the configured VDOM.

The integration uses:

```text
GET /api/v2/cmdb/firewall/policy/<policy-id>?vdom=<vdom>
PUT /api/v2/cmdb/firewall/policy/<policy-id>?vdom=<vdom>
```

The PUT body contains only `{"status":"enable"}` or `{"status":"disable"}`. Before writing, the integration reads the policy and confirms its ID and saved name. It then reads the policy again after the write. The switch changes only after FortiGate reports the requested state. A failed read makes the switch unavailable; it is never interpreted as OFF.

Policy switches can be used manually, on dashboards, in Home Assistant automations, or through HomeKit Bridge. The integration itself does not automatically connect presence to policy state.

## Network presence

Client discovery uses the configured VDOM:

```text
GET /api/v2/monitor/wifi/client?vdom=<vdom>
```

FortiOS response layouts vary. The parser accepts known nesting and field-name variations and ignores missing optional fields or malformed individual records. It normalizes MAC addresses as the stable identity; IP address, hostname, SSID, and access point may change without creating another Home Assistant device.

A selected client is:

- `home` immediately when its MAC appears anywhere in the valid FortiAP client result
- still `home` while missing within the configured away timeout
- `not_home` only after continued absence from successful API results
- unavailable when the FortiGate request or response cannot be trusted

Roaming between FortiAPs updates connection information without changing presence. A successful empty client list is valid absence; a timeout, authentication error, TLS error, or invalid response is not.

Each selected device can include a tracker, presence sensor, IP address, connection type, SSID, access point, first seen, last seen, and connected-since information. High-churn signal and duration sensors are disabled by default. All entities share one coordinator result, so selecting more devices does not create more FortiGate calls.

Under **People**, several devices can be assigned to one aggregate person. Any device at home makes the person home. The person becomes away only after every assigned device is definitively away. An unknown member prevents a false away result.

Apple devices may use private MAC addresses. Track the address FortiGate reports for the intended SSID; Apple's **Fixed Private Wi-Fi Address** mode provides a stable per-network identity without disabling the feature globally.

## Device history and new devices

First seen, last seen, connected since, the last useful metadata, and new-device announcement state are stored in Home Assistant storage. The initial successful poll establishes a silent baseline. Later, a newly observed MAC can fire `fortigate_new_network_device` once. Restarts do not repeat the event.

The optional unknown-device sensor counts associated clients that have not been selected as trackers. Recently discovered clients are retained for a bounded, configurable period; selected trackers are retained while offline.

## TLS

Certificate verification is enabled by default and is recommended. Install a certificate trusted by Home Assistant whenever possible. Disabling verification still encrypts traffic but no longer verifies that Home Assistant is talking to the intended FortiGate.

## HomeKit

Each policy entity is a normal Home Assistant switch and can be included through the HomeKit Bridge UI. No custom iOS application is required.

## Troubleshooting

Use the integration's **Enable debug logging** menu, reproduce the problem, then download diagnostics. Tokens and authorization headers are excluded. Client identifiers are redacted in diagnostics.

- **401/403:** replace the token or correct the API administrator permissions.
- **404:** check the VDOM, policy ID, and FortiOS support for the Wi-Fi monitor endpoint.
- **Policy unavailable:** verify the saved policy still has the same ID and name.
- **TLS failure:** install the appropriate CA certificate; disable verification only for an isolated test.
- **No clients:** confirm the FortiGate manages the FortiAPs and the administrator can read Wireless Controller monitor data.
- **Unexpected duplicate Apple device:** select the MAC used by the intended SSID and use a stable private-address mode.

The FortiGate device also provides **Refresh data**, which performs an immediate read of enabled features without changing firewall configuration.

## Removal

Remove the config entry from **Settings > Devices & services**, remove the integration from HACS, and restart Home Assistant.
