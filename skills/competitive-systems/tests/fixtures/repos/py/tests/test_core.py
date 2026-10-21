import unittest

from pkg.core import frobnicate


class CoreTest(unittest.TestCase):
    def test_frobnicate_dedups_and_doubles(self):
        self.assertEqual(frobnicate([3, 1, 3, 2]), [2, 4, 6])

    def test_empty(self):
        self.assertEqual(frobnicate([]), [])
