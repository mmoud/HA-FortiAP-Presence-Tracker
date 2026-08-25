"""Native FortiGate quarantine parsing and mutation safety tests."""

from __future__ import annotations

import unittest

from custom_components.fortigate_policy.quarantine import (
    parse_quarantine_state,
    quarantine_target_name,
    updated_quarantine_targets,
)

MAC = "aa:bb:cc:dd:ee:ff"


class TestQuarantineModel(unittest.TestCase):
    def test_state_normalizes_formats_and_requires_drop_enable(self) -> None:
        state = parse_quarantine_state(
            {
                "results": {
                    "quarantine": "enable",
                    "targets": [
                        {
                            "entry": "manual",
                            "macs": [
                                {"mac": "AA-BB-CC-DD-EE-FF", "drop": "enable"},
                                {"mac": "11:22:33:44:55:66", "drop": "disable"},
                                {"mac": "malformed", "drop": "enable"},
                            ],
                        }
                    ],
                }
            }
        )
        self.assertTrue(state.enabled)
        self.assertEqual(frozenset({MAC}), state.quarantined_macs)
        self.assertEqual(1, state.target_count)

    def test_add_is_deterministic_and_idempotent(self) -> None:
        original = {"quarantine": "enable", "targets": []}
        targets, changed = updated_quarantine_targets(
            original, "AABBCCDDEEFF", True, "Example phone"
        )
        self.assertTrue(changed)
        self.assertEqual("HA_AABBCCDDEEFF", targets[0]["entry"])
        self.assertEqual("enable", targets[0]["macs"][0]["drop"])

        targets_again, changed_again = updated_quarantine_targets(
            {"targets": targets}, MAC, True, "Renamed phone"
        )
        self.assertFalse(changed_again)
        self.assertEqual(targets, targets_again)

    def test_release_removes_only_selected_mac_from_shared_target(self) -> None:
        original = {
            "targets": [
                {
                    "entry": "administrator-target",
                    "description": "Keep this target",
                    "macs": [
                        {"mac": "AA:BB:CC:DD:EE:FF", "drop": "enable"},
                        {"mac": "11:22:33:44:55:66", "drop": "enable"},
                    ],
                },
                {
                    "entry": "another-target",
                    "macs": [{"mac": "22:33:44:55:66:77", "drop": "enable"}],
                },
            ]
        }
        targets, changed = updated_quarantine_targets(original, MAC, False, "")
        self.assertTrue(changed)
        self.assertEqual("administrator-target", targets[0]["entry"])
        self.assertEqual(
            ["11:22:33:44:55:66"],
            [item["mac"] for item in targets[0]["macs"]],
        )
        self.assertEqual(original["targets"][1], targets[1])
        self.assertEqual(2, len(original["targets"][0]["macs"]))

    def test_release_owned_empty_target_but_preserves_manual_empty_target(self) -> None:
        targets, changed = updated_quarantine_targets(
            {
                "targets": [
                    {
                        "entry": quarantine_target_name(MAC),
                        "macs": [{"mac": MAC, "drop": "enable"}],
                    },
                    {
                        "entry": "manual-empty-after-release",
                        "macs": [{"mac": MAC, "drop": "enable"}],
                    },
                ]
            },
            MAC,
            False,
            "",
        )
        self.assertTrue(changed)
        self.assertEqual(
            [{"entry": "manual-empty-after-release", "macs": []}], targets
        )

    def test_release_absent_is_idempotent(self) -> None:
        original = {
            "targets": [
                {
                    "entry": "manual",
                    "macs": [{"mac": "11:22:33:44:55:66", "drop": "enable"}],
                }
            ]
        }
        targets, changed = updated_quarantine_targets(original, MAC, False, "")
        self.assertFalse(changed)
        self.assertEqual(original["targets"], targets)

    def test_invalid_shapes_are_rejected_instead_of_clearing_configuration(self) -> None:
        with self.assertRaises(ValueError):
            parse_quarantine_state({"results": {"targets": {}}})
        with self.assertRaises(ValueError):
            updated_quarantine_targets(
                {"targets": [{"entry": "bad", "macs": {}}]}, MAC, False, ""
            )


if __name__ == "__main__":
    unittest.main()
