"""Coordinate and label contract checks independent of model performance."""
import unittest
import numpy as np
import torch
from ultralytics.utils import ops
from ultralytics.data.converter import coco80_to_coco91_class
from c3_neural_compression.evaluation.shared_detector import COCO_CATEGORY_IDS
from evaluate import original_boxes
class CoordinateTest(unittest.TestCase):
 def test_original_boxes_survive_letterbox_for_both_orientations(self):
  for mapped,original in [((800,1296),(395,640)),((1296,800),(640,395))]:
   with self.subTest(mapped=mapped):
    expected=np.array([[20.,30.,300.,350.]])
    gain=640/max(mapped);pad=[round((640-mapped[1]*gain)/2-.1),round((640-mapped[0]*gain)/2-.1)]
    encoded=expected.copy();encoded[:,[0,2]]*=mapped[1]/original[1];encoded[:,[1,3]]*=mapped[0]/original[0];encoded*=gain;encoded[:,[0,2]]+=pad[0];encoded[:,[1,3]]+=pad[1]
    restored=ops.scale_boxes((640,640),torch.tensor(encoded),mapped).numpy()
    np.testing.assert_allclose(original_boxes(restored,mapped,original),expected,atol=1e-8)
 def test_COCO_category_mapping(self):self.assertEqual(tuple(coco80_to_coco91_class()),tuple(COCO_CATEGORY_IDS))
 def test_empty_predictions(self):self.assertEqual(original_boxes([], (800,1296),(395,640)).shape,(0,4))
if __name__=='__main__':unittest.main()
