"""Checks scientific selection rules without fitting models or reading AP."""
import math
import unittest
from rate_selection import correction, select_candidate, tolerance_flags


class RateSelectionTests(unittest.TestCase):
    def test_AP_cannot_change_selection_or_correction(self):
        first=[{'lambda':.01,'mean_bpp':.1,'image_count':20,'AP':99},
               {'lambda':.001,'mean_bpp':.3,'image_count':20,'AP':1}]
        second=[dict(r,AP=100-r['AP']) for r in first]
        self.assertEqual(select_candidate(.15,first),select_candidate(.15,second))
        self.assertEqual(correction(.15,.005,first),correction(.15,.005,second))

    def test_interpolation_and_update_ceiling(self):
        points=[{'lambda':.01,'mean_bpp':.1,'image_count':20},{'lambda':.001,'mean_bpp':.4,'image_count':20}]
        result=correction(.2,.005,points)
        self.assertAlmostEqual(result['lambda'],math.sqrt(.01*.001))
        self.assertFalse(result['factor_cap_applied'])
        high=correction(1e50,.005,points)
        self.assertAlmostEqual(high['lambda'],.0025)
        self.assertTrue(high['factor_cap_applied'])

    def test_target_and_between_method_checks_are_separate(self):
        result=tolerance_flags(.15,.14,.16)
        self.assertTrue(result['normal_target_passed']);self.assertTrue(result['combined_target_passed'])
        self.assertFalse(result['between_method_passed'])
        self.assertAlmostEqual(result['between_method_relative_difference'],.02/.14)

    def test_cannot_substitute_one_image_for_fixed20(self):
        with self.assertRaisesRegex(ValueError,'fixed20'):
            correction(.2,.005,[{'lambda':.01,'mean_bpp':.1,'image_count':1},{'lambda':.001,'mean_bpp':.4,'image_count':20}])

    def test_equal_rate_pair_stops(self):
        with self.assertRaisesRegex(ValueError,'distinct pair'):
            correction(.2,.005,[{'lambda':.01,'mean_bpp':.1,'image_count':20},{'lambda':.001,'mean_bpp':.1,'image_count':20}])

    def test_ties_keep_first_attempt(self):
        self.assertEqual(select_candidate(.15,[{'lambda':.01,'mean_bpp':.15},{'lambda':.02,'mean_bpp':.15}])['candidate_index'],0)


if __name__=='__main__':unittest.main()
