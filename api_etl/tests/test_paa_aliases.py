from __future__ import annotations

import unittest

from api_etl.paa_aliases import (
    are_paa_equivalent,
    get_location_codes_for_paa_scope,
    get_paa_alias_candidates,
    get_paa_scope,
)


class PaaAliasesTests(unittest.TestCase):
    def test_pemba_location_names_match_pemba_scope(self):
        self.assertEqual(get_paa_scope(name="Kaskazini Pemba"), "PEMBA")
        self.assertEqual(get_paa_scope(name="Kusini Pemba"), "PEMBA")
        self.assertTrue(are_paa_equivalent("Kaskazini Pemba", "PEMBA"))
        self.assertTrue(are_paa_equivalent("Kusini Pemba", "PEMBA"))

    def test_unguja_location_names_match_unguja_scope(self):
        self.assertEqual(get_paa_scope(name="Kaskazini Unguja"), "UNGUJA")
        self.assertEqual(get_paa_scope(name="Kusini Unguja"), "UNGUJA")
        self.assertEqual(get_paa_scope(name="Mjini Magharibi"), "UNGUJA")
        self.assertTrue(are_paa_equivalent("Kaskazini Unguja", "UNGUJA"))
        self.assertTrue(are_paa_equivalent("Kusini Unguja", "UNGUJA"))
        self.assertTrue(are_paa_equivalent("Mjini Magharibi", "UNGUJA"))

    def test_zanzibar_codes_resolve_to_paa_scope(self):
        self.assertEqual(get_paa_scope(code="54"), "PEMBA")
        self.assertEqual(get_paa_scope(code="55"), "PEMBA")
        self.assertEqual(get_paa_scope(code="51"), "UNGUJA")
        self.assertEqual(get_paa_scope(code="52"), "UNGUJA")
        self.assertEqual(get_paa_scope(code="53"), "UNGUJA")

    def test_zanzibar_child_location_codes_resolve_by_parent_prefix(self):
        self.assertEqual(get_paa_scope(code="5402"), "PEMBA")
        self.assertEqual(get_paa_scope(code="5501"), "PEMBA")
        self.assertEqual(get_paa_scope(code="5101"), "UNGUJA")
        self.assertEqual(get_paa_scope(code="5201"), "UNGUJA")
        self.assertEqual(get_paa_scope(code="5301"), "UNGUJA")

    def test_location_codes_for_scope(self):
        self.assertEqual(get_location_codes_for_paa_scope("PEMBA"), ["54", "55"])
        self.assertEqual(get_location_codes_for_paa_scope("UNGUJA"), ["51", "52", "53"])

    def test_alias_candidates_include_scope_and_siblings(self):
        candidates = get_paa_alias_candidates("Kaskazini Pemba")
        self.assertIn("pemba", candidates)
        self.assertIn("kusini pemba", candidates)


if __name__ == "__main__":
    unittest.main()
