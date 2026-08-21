# FortiGate for Home Assistant

Home Assistant integration for controlling FortiGate firewall policies and tracking selected FortiAP Wi-Fi clients.

It provides:

- one verified switch for each configured firewall policy
- `device_tracker` and presence `binary_sensor` entities for selected Wi-Fi clients
- an optional sensor showing the number of associated Wi-Fi clients
- full setup and configuration through the Home Assistant UI

## Requirements

- Home Assistant 2026.8 or newer
- FortiGate reachable from Home Assistant over HTTPS
- FortiOS REST API token
- FortiAPs managed by the configured FortiGate for Wi-Fi tracking

## Installation

1. In HACS, open **Integrations > Custom repositories**.
2. Add `https://github.com/mmoud/ha-fortigate` as an **Integration**.
3. Download **FortiGate** and restart Home Assistant.
4. Open **Settings > Devices & services > Add integration**.
5. Search for **FortiGate** and complete the form.

No YAML configuration is required.

## FortiGate API account

Create a dedicated REST API administrator, for example `homeassistant-api`. Give it read/write access to firewall policies in the required VDOM. Wi-Fi tracking also needs read access to Wireless Controller monitor data.

FortiOS permission profiles are generally scoped to configuration areas, not to one policy ID. Use the integration's expected-policy-name check as an additional safeguard, and restrict the API administrator's trusted hosts to the Home Assistant IP where possible.

Keep the FortiGate management interface on a private network. Do not expose it to the internet.

## Policy switch

The setup form requires the FortiGate host, HTTPS port, VDOM, policy IDs, API token, and TLS verification setting. Enter one policy ID or a comma-separated list such as `61, 72, 83`.

Each ID is read before the entry is saved. The returned policy name is stored as an identity guard, and Home Assistant creates a separate switch for every validated policy. Adding or removing IDs later is done with the integration's **Reconfigure** action.

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

Open **Settings > Devices & services > FortiGate**, find the configured entry, and select **Configure**. The options menu has two clear actions:

- **Add or manage Wi-Fi trackers** discovers connected and recently seen clients, keeps existing offline trackers in the list, and accepts a MAC address manually when a device is not listed.
- **Polling and sensors** controls policy polling, Wi-Fi polling, the away grace period, and the optional client-count sensor.

On the tracker screen, enable Wi-Fi presence tracking, select one or more devices, and save. Only newly selected devices ask for a friendly name; existing names are preserved. Client selection combines the current FortiAP association list with available FortiGate device-detection and DHCP information. Each selected MAC creates two entities:

- `device_tracker.<device_name>` with `home`, `not_home`, or unavailable state
- `binary_sensor.<device_name>_presence`, which is ON at home, OFF when away, and unavailable during a FortiGate/API failure

Both entities use the same coordinator result and away grace period. The binary sensor does not add another FortiGate request.

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

## Example automations

Use the entity IDs assigned by your Home Assistant installation.

```yaml
alias: Enable policy when phone arrives
triggers:
  - trigger: state
    entity_id: binary_sensor.example_phone_presence
    to: "on"
actions:
  - action: switch.turn_on
    target:
      entity_id: switch.fortigate_policy
```

```yaml
alias: Disable policy when phone leaves
triggers:
  - trigger: state
    entity_id: binary_sensor.example_phone_presence
    to: "off"
actions:
  - action: switch.turn_off
    target:
      entity_id: switch.fortigate_policy
```

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
