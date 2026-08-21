"""Dependency-free tests for multi-policy configuration and migration."""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

PACKAGE = "fortigate_policy_config_tests"
PACKAGE_PATH = Path(__file__).parents[1] / "custom_components" / "fortigate_policy"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(PACKAGE_PATH)]
sys.modules[PACKAGE] = package

const = __import__(f"{PACKAGE}.const", fromlist=["*"])
policy_config = __import__(f"{PACKAGE}.policy_config", fromlist=["*"])

PolicyDefinition = policy_config.PolicyDefinition
configured_policies = policy_config.configured_policies
migrate_v1_data = policy_config.migrate_v1_data
parse_policy_ids = policy_config.parse_policy_ids
serialize_policies = policy_config.serialize_policies


class TestPolicyConfiguration(unittest.TestCase):
    def test_policy_id_parser_normalizes_and_deduplicates(self) -> None:
        self.assertEqual(("61", "72", "83"), parse_policy_ids("61, 72,61, 83"))
        for invalid in ("", "61,", "61,abc", "61;72"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_policy_ids(invalid)

    def test_current_policy_list_round_trip(self) -> None:
        expected = (
            PolicyDefinition("61", "Family access"),
            PolicyDefinition("72", "Guest access"),
        )
        serialized = serialize_policies(expected)

        self.assertEqual(
            expected, configured_policies({const.CONF_POLICIES: serialized})
        )

    def test_legacy_single_policy_format_is_supported(self) -> None:
        self.assertEqual(
            (PolicyDefinition("61", "Family access"),),
            configured_policies(
                {
                    const.CONF_POLICY_ID: "61",
                    const.CONF_POLICY_NAME: "Family access",
                }
            ),
        )

    def test_version_one_migration_preserves_primary_switch_identity(self) -> None:
        original = {
            const.CONF_POLICY_ID: "61",
            const.CONF_POLICY_NAME: "Family access",
            "host": "fortigate.example.test",
        }

        migrated = migrate_v1_data(original)

        self.assertEqual("61", migrated[const.CONF_LEGACY_PRIMARY_POLICY_ID])
        self.assertEqual(
            [
                {
                    const.CONF_POLICY_ID: "61",
                    const.CONF_POLICY_NAME: "Family access",
                }
            ],
            migrated[const.CONF_POLICIES],
        )
        self.assertEqual("fortigate.example.test", migrated["host"])


if __name__ == "__main__":
    unittest.main()
