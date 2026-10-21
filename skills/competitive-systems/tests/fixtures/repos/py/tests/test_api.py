import unittest

from pkg import api


class ApiTest(unittest.TestCase):
    def test_first_page(self):
        self.assertEqual(api.paginate([5, 1, 3], 0), [2, 6])

    def test_last_page(self):
        self.assertEqual(api.paginate([5, 1, 3], 1), [10])
