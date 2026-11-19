import unittest

from tools.settlement import group_by_source


class SettlementPortTest(unittest.TestCase):
    def test_group_by_settlement_source_without_inventing_report_format(self):
        first = {"id": "1", "source_account_id": "settlement-a"}
        second = {"id": "2", "source_account_id": "settlement-b"}
        third = {"id": "3", "source_account_id": "settlement-a"}
        self.assertEqual(
            group_by_source([first, second, third]),
            {"settlement-a": [first, third], "settlement-b": [second]},
        )
        with self.assertRaises(ValueError):
            group_by_source([{"id": "missing-source"}])
