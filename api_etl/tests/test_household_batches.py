import unittest
from api_etl.batching import household_batches


class HouseholdBatchTests(unittest.TestCase):
    def test_interleaved_households_stay_together(self):
        rows = [{'group_code': key, 'member': index} for index, key in enumerate(['a','b','a','b','c'])]
        batches = list(household_batches(rows, 3))
        self.assertEqual([[r['group_code'] for r in b] for b in batches], [['a','a'],['b','b','c']])
        self.assertEqual(sorted(r['member'] for b in batches for r in b), list(range(5)))

    def test_oversized_household_and_missing_keys(self):
        rows = [{'json_ext':'{"group_code":"large"}'} for _ in range(4)] + [{},{}]
        self.assertEqual([len(b) for b in household_batches(rows, 2)], [4,2])

    def test_empty_and_single_batch_compatibility(self):
        self.assertEqual(list(household_batches([], 2)), [])
        self.assertEqual([len(b) for b in household_batches([{},{}], 1)], [2])
