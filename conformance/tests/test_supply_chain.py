import unittest
from tools.check_supply_chain import check


class SupplyChainTest(unittest.TestCase):
    def test_pins_nonroot_execution_and_key_mount_separation(self):
        lock = check()
        self.assertEqual(len(lock["images"]), 7)
        self.assertEqual(len(lock["actions"]), 6)
