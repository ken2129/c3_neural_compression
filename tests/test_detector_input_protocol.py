"""CPU fixtures using real D2 mapper, no weights or GPU inference."""
import copy
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import numpy as np
from PIL import Image
import torch
from detectron2.data import transforms
from detectron2.data.detection_utils import read_image
from c3_neural_compression.configs import coco2017_detector_input
from c3_neural_compression.evaluation import comparison as common
from c3_neural_compression.evaluation import detector_input_protocol as protocol
from c3_neural_compression.evaluation import comparison_runner as runner
from c3_neural_compression.utils.data_loading import COCO2017Subset, load_dataset


class DetectorInputTest(unittest.TestCase):
  def setUp(self):
    self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
    y,x=np.indices((41,67));rgb=np.stack([(x*17+y*5)%256,(x*3+y*19)%256,(x*11+y*7)%256],axis=-1).astype(np.uint8)
    Image.fromarray(rgb).save(self.root/'000000000007.png')
    common.write_json(self.root/'annotation.json',{'images':[{'id':7,'file_name':'000000000007.png','height':41,'width':67}]})
    common.write_json(self.root/'ids.json',[7]);self.manifest=common.make_manifest(self.root/'annotation.json',self.root/'ids.json',self.root)

  def official(self):
    bgr=read_image(str(self.root/'000000000007.png'),format='BGR');mapped=transforms.ResizeShortestEdge([800,800],1333).get_transform(bgr).apply_image(bgr)
    return mapped[...,::-1].copy()

  def test_mapper_and_training_tensor_match_official_values(self):
    expected=self.official();ds=COCO2017Subset(self.root,self.root/'annotation.json',self.root/'ids.json',input_protocol=protocol.DETECTOR_INPUT);row=ds[0]
    np.testing.assert_array_equal(np.asarray(row['array']),expected)
    self.assertEqual(row['mapped_rgb_uint8_sha256'],hashlib.sha256(expected.tobytes()).hexdigest())
    self.assertEqual((row['original_height'],row['original_width']),(41,67))
    loader=load_dataset('coco2017',str(self.root),annotation_file=str(self.root/'annotation.json'),subset_ids_file=str(self.root/'ids.json'),input_protocol=protocol.DETECTOR_INPUT)
    tensor=next(iter(loader))['array'];self.assertEqual(tensor.dtype,torch.float32)
    np.testing.assert_array_equal(tensor.numpy(),expected.astype('float32')/255)

  def test_legacy_loader_is_unchanged(self):
    row=COCO2017Subset(self.root,self.root/'annotation.json',self.root/'ids.json')[0]
    self.assertEqual(row['array'].size,(67,41));self.assertNotIn('codec_input_protocol',row)

  def test_aspect_ratios_and_dtype(self):
    for h,w in [(41,67),(100,1000),(1000,100),(800,800)]:
      mapped=protocol.map_rgb_uint8(np.zeros((h,w,3),np.uint8));self.assertEqual(mapped.shape[:2],protocol.shared_detector.expected_resized_shape(h,w))
    with self.assertRaisesRegex(ValueError,'uint8'):protocol.map_rgb_uint8(np.zeros((41,67,3),np.float32))

  def test_png_no_second_resize_or_requantization(self):
    expected=self.official();p=self.root/'reconstruction.png';Image.fromarray(expected).save(p)
    with mock.patch.object(protocol,'map_rgb_uint8',side_effect=AssertionError('Second resize')):rgb=protocol.load_reconstruction(p,(41,67))
    np.testing.assert_array_equal(rgb.permute(1,2,0).numpy(),expected.astype('float32')/255)
    Image.new('RGB',(67,41)).save(p)
    with self.assertRaisesRegex(ValueError,'already'):protocol.load_reconstruction(p,(41,67))

  def test_arithmetic_mean_not_pooled_or_padded_rate(self):
    m=copy.deepcopy(self.manifest);m['image_ids'].append(9);m['images'].append(dict(m['images'][0],image_id=9,original_hw=[100,1000],path=str(self.root/'other.png')));rows=[]
    for image,bits in zip(m['images'],[90000.,100000.]):
      hw=list(protocol.shared_detector.expected_resized_shape(*image['original_hw']));rows.append(dict(image_id=image['image_id'],status='ok',rate_definition='entropy_estimated',original_hw=image['original_hw'],detector_hw=hw,codec_hw=hw,codec_input_protocol=protocol.DETECTOR_INPUT,estimated_bits={'latents':bits*.8,'synthesis_network':bits*.1,'entropy_network':bits*.1,'total':bits}))
    result=protocol.rate_summary(m,rows,'c3_image',protocol.DETECTOR_INPUT)
    self.assertAlmostEqual(result['estimated_bpp'],sum(row['estimated_bits']['total']/np.prod(row['detector_hw']) for row in rows)/2)
    self.assertNotAlmostEqual(result['estimated_bpp'],190000/sum(np.prod(row['detector_hw']) for row in rows))
    self.assertAlmostEqual(result['original_pixel_pooled_bpp_auxiliary'],190000/(41*67+100*1000))
    self.assertEqual(protocol.rate_summary(m,rows,'c3_image',protocol.ORIGINAL),common.aggregate(m,rows,'c3_image'))
    for changed in [dict(rows[0],codec_hw=[1024,1536]),dict(rows[0],codec_input_protocol='original_rgb')]:
      with self.assertRaises(ValueError):protocol.rate_summary(m,[changed,rows[1]],'c3_image',protocol.DETECTOR_INPUT)
    for bad in [rows[:1],[dict(rows[0],status='failed'),rows[1]]]:
      with self.assertRaises(ValueError):protocol.rate_summary(m,bad,'c3_image',protocol.DETECTOR_INPUT)

  def test_loop_passes_original_coordinates_and_all_ids(self):
    hw=list(protocol.shared_detector.expected_resized_shape(41,67));row=dict(original_hw=[41,67],detector_hw=hw,codec_hw=hw,codec_input_protocol=protocol.DETECTOR_INPUT,estimated_bits={'latents':8.,'synthesis_network':1.,'entropy_network':1.,'total':10.})
    d=mock.Mock();d.predict.return_value=[];evaluate=mock.Mock(return_value={'fixture_only':True})
    runner.run_images(self.manifest,'c3_image',lambda _: (torch.zeros(3,*hw),row),d,self.root/'run',evaluate,input_protocol=protocol.DETECTOR_INPUT)
    self.assertEqual(d.predict.call_args.kwargs['original_height'],41);self.assertEqual(d.predict.call_args.kwargs['original_width'],67);self.assertEqual(evaluate.call_args.args[2],[7])
    self.assertEqual(common.read_json(self.root/'run/rate_summary.json')['dataset_bpp_definition'],protocol.PRIMARY_RATE)

  def test_discover_rejects_original_protocol_and_forged_mapped_input(self):
    from c3_neural_compression.evaluation.comparison_attempt import ComparisonAttempt
    def attempt(name, mapped_hash):
      root=self.root/name;ledger=ComparisonAttempt.start(root,[7]);datum=root/'datum_00000';datum.mkdir()
      mapped=self.official();Image.fromarray(mapped).save(datum/'reconstruction.png')
      metadata={'image_id':7,'file_name':'000000000007.png','original_file_sha256':self.manifest['images'][0]['sha256'],**protocol.mapped_metadata(mapped)}
      metadata['mapped_rgb_uint8_sha256']=mapped_hash
      common.write_json(datum/'metrics.json',{'attempt_id':ledger.attempt_id,'datum_index':0,'datum_metadata':metadata,'input_shape':list(mapped.shape),'experiment_signature':{'sha256':'fixture','fields':{'machine_weight':0,'codec_input_protocol':protocol.DETECTOR_INPUT}},'estimated_bits':{'latents':8.,'synthesis_network':1.,'entropy_network':1.,'total':10.}})
      common.write_json(datum/'config.json',{'fixture_only':True});ledger.record_output(0,7);ledger.complete();return root
    valid=attempt('valid',hashlib.sha256(self.official().tobytes()).hexdigest())
    common.discover_c3(self.manifest,[valid],'c3_image',input_protocol=protocol.DETECTOR_INPUT)
    with self.assertRaisesRegex(ValueError,'input protocol'):common.discover_c3(self.manifest,[valid],'c3_image')
    invalid=attempt('invalid','0'*64)
    with self.assertRaisesRegex(ValueError,'mapped input SHA256'):common.discover_c3(self.manifest,[invalid],'c3_image',input_protocol=protocol.DETECTOR_INPUT)

  def test_configuration_preserves_budget_seed_features(self):
    env={n:str(self.root) for n in ('C3_COCO_ROOT','C3_COCO_ANNOTATIONS','C3_SUBSET_IDS','C3_OUTPUT_ROOT')}
    with mock.patch.dict(os.environ,env):
      a=coco2017_detector_input.get_config('image_lambda0.01').experiment_kwargs.config;b=coco2017_detector_input.get_config('combined_lambda0.03').experiment_kwargs.config
    self.assertEqual(a.opt,b.opt);self.assertEqual(a.random_seed,20261004);self.assertEqual(a.opt.num_noise_steps,500);self.assertEqual(a.opt.max_num_ste_steps,50)
    self.assertEqual(a.loss.machine_weight,0);self.assertEqual(b.loss.machine_weight,.1);self.assertEqual(b.loss.machine.feature_layers,('0','1','2','3'));self.assertNotEqual(a.output_dir,b.output_dir)
    for bad in ['image_lambda0','combined_lambdanan','image_lambdainf','other_lambda0.1']:
      with self.assertRaises(ValueError):coco2017_detector_input.get_config(bad)


if __name__=='__main__':unittest.main()
