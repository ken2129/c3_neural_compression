"""Fixed YOLOv8m transfer pilot on existing C3 reconstructions; no codec fitting."""
import argparse, hashlib, inspect, json, os, time
from pathlib import Path
import numpy as np
from PIL import Image,ImageOps

def read(p):return json.loads(Path(p).read_text())
def write(p,d):
 p=Path(p);p.parent.mkdir(parents=True,exist_ok=True);t=p.with_suffix('.tmp');t.write_text(json.dumps(d,indent=2,allow_nan=False));t.replace(p)
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def original_boxes(xyxy,mapped_hw,original_hw):
 boxes=np.array(xyxy,dtype=np.float64,copy=True).reshape(-1,4)
 boxes[:,[0,2]]*=original_hw[1]/mapped_hw[1];boxes[:,[1,3]]*=original_hw[0]/mapped_hw[0]
 return boxes

def prepare(root,source):
 from c3_neural_compression.evaluation import comparison as common,detector_input_protocol as protocol
 from ultralytics import __version__
 from ultralytics.data.augment import LetterBox
 from ultralytics.utils import ops
 assert __version__=='8.3.0'
 manifest=read(source/'heldout_selection_v1/frozen_manifest.json');common.validate_assets(manifest)
 images={x['image_id']:x for x in manifest['images']}; groups=[]
 groups.append({'method':'JPEG_baseline','target':None,'bpp':None,'inputs':[{'image_id':x['image_id'],'path':x['path'],'sha256':x['sha256'],'original_hw':x['original_hw'],'mapped_hw':list(protocol.shared_detector.expected_resized_shape(*x['original_hw'])),'baseline':True} for x in manifest['images']]})
 for method in ['image','combined']:
  for rate in [100,150,225,300]:
   job=source/f'jobs/heldout_{method}_{rate}';evaldir=source/f'jobs/heldout_{method}_{rate}_eval/evaluation'
   assert read(evaldir/'status.json')['status']=='complete'
   found,_=common.discover_c3(manifest,[str(job/'results')],f'c3_{method}',input_protocol=protocol.DETECTOR_INPUT)
   s=read(evaldir/'rate_summary.json');rows=[]
   for i in manifest['image_ids']:
    x=images[i];p=found[i]['path'];row=next(v for v in s['images'] if v['image_id']==i)
    assert sha(p)==row['reconstruction_sha256']
    rgb=np.array(Image.open(p));assert rgb.dtype==np.uint8 and list(rgb.shape)==row['detector_hw']+[3]
    rows.append({'image_id':i,'path':str(p),'sha256':sha(p),'original_hw':x['original_hw'],'mapped_hw':list(rgb.shape[:2]),'baseline':False})
   recomputed=sum(v['estimated_bits']['total']/(v['detector_hw'][0]*v['detector_hw'][1]) for v in s['images'])/20
   assert abs(recomputed-s['estimated_bpp'])<1e-12
   groups.append({'method':f'c3_{method}','target':rate/1000,'bpp':s['estimated_bpp'],'d2_metrics':str(evaldir/'metrics.json'),'inputs':rows})
 weight=root/'assets/yolov8m.pt';assert sha(weight)==read(root/'assets/download_receipt.json')['sha256']
 cfg={'ultralytics':'8.3.0','checkpoint_sha256':sha(weight),'imgsz':640,'batch':1,'rect':False,'auto':False,'scaleup':True,'center':True,'padding_value':114,'precision':'FP32','conf':0.001,'iou':0.7,'multi_label':True,'agnostic':False,'max_det':300,'COCO_maxDets':[1,10,100],'augment':False,'inference_device':0,'torch_deterministic_algorithms':True,'original_rate_unchanged':True,'preprocessor_source_sha256':hashlib.sha256(inspect.getsource(LetterBox).encode()).hexdigest(),'nms_source_sha256':hashlib.sha256(inspect.getsource(ops.non_max_suppression).encode()).hexdigest(),'smoke_max_GPU_seconds':360,'smoke_max_predictions':6,'new_experiment_bytes_limit':1073741824,'minimum_free_bytes':3221225472,'annotation_file':manifest['annotation_file'],'image_ids':manifest['image_ids'],'groups':groups}
 write(root/'frozen_inputs_and_config.json',cfg)
 print('Verified180 inputs,8 existing rate points; config frozen before YOLO AP',flush=True)
 return cfg

def inference(root,cfg,smoke):
 import shutil,torch,cv2
 from ultralytics import YOLO
 from ultralytics.data.augment import LetterBox
 from ultralytics.utils import ops
 from c3_neural_compression.evaluation import detector_input_protocol as protocol,shared_detector
 if shutil.disk_usage(root).free<cfg['minimum_free_bytes']+256*1024**2:raise RuntimeError('Disk reserve')
 assert torch.cuda.is_available() and torch.cuda.device_count()==1
 torch.set_num_threads(2);torch.manual_seed(20261016);torch.backends.cudnn.benchmark=False;torch.backends.cudnn.deterministic=True;torch.use_deterministic_algorithms(True)
 start=time.monotonic();model=YOLO(str(root/'assets/yolov8m.pt')).model.to('cuda:0').float().eval().requires_grad_(False)
 assert model.model[-1].nc==80
 letter=LetterBox(new_shape=(640,640),auto=False,scaleFill=False,scaleup=True,center=True,stride=32)
 selected=[cfg['groups'][0],cfg['groups'][1],cfg['groups'][5]] if smoke else cfg['groups']
 results=[];repeat_exact=True
 for group in selected:
  rows=group['inputs'][:1] if smoke else group['inputs']; predictions=[];receipts=[]
  for row in rows:
   assert sha(row['path'])==row['sha256']
   with Image.open(row['path']) as im:
    rgb=protocol.map_rgb_uint8(ImageOps.exif_transpose(im).convert('RGB')) if row['baseline'] else np.array(im)
   assert list(rgb.shape[:2])==row['mapped_hw']
   bgr=rgb[...,::-1].copy();padded=letter(image=bgr)
   t=torch.from_numpy(np.ascontiguousarray(padded[...,::-1].transpose(2,0,1))).to('cuda:0').float().div_(255).unsqueeze(0)
   assert list(t.shape)==[1,3,640,640]
   outputs=[]
   for _ in range(2 if smoke else 1):
    with torch.inference_mode():
     raw=model(t,augment=False);det=ops.non_max_suppression(raw,conf_thres=.001,iou_thres=.7,multi_label=True,agnostic=False,max_det=300)[0]
    assert torch.isfinite(det).all()
    outputs.append(det.cpu().numpy())
   if smoke:
    assert np.array_equal(*outputs),'Independent inference repeat mismatch'
   det=torch.from_numpy(outputs[0].copy());det[:,:4]=ops.scale_boxes((640,640),det[:,:4],tuple(row['mapped_hw']))
   boxes=original_boxes(det[:,:4].numpy(),row['mapped_hw'],row['original_hw'])
   for box,score,cls in zip(boxes,det[:,4].numpy(),det[:,5].numpy()):
    x1,y1,x2,y2=box.tolist();predictions.append({'image_id':row['image_id'],'category_id':shared_detector.COCO_CATEGORY_IDS[int(cls)],'bbox':[x1,y1,x2-x1,y2-y1],'score':float(score)})
   receipts.append({'image_id':row['image_id'],'input_sha256':row['sha256'],'prediction_count':len(det),'mapped_hw':row['mapped_hw'],'YOLO_hw':[640,640]})
   if time.monotonic()-start>cfg['smoke_max_GPU_seconds' if smoke else 'full_max_GPU_seconds']:raise RuntimeError('GPU budget')
   if shutil.disk_usage(root).free<cfg['minimum_free_bytes']:raise RuntimeError('Disk reserve')
  label=group['method']+('_'+str(group['target']) if group['target'] else '')
  out=root/('smoke' if smoke else 'evaluation')/label
  write(out/'predictions.json',predictions);write(out/'inputs.json',receipts)
  results.append({'condition':label,'input_count':len(rows),'prediction_count':len(predictions),'bpp':group['bpp']})
 torch.cuda.synchronize();elapsed=time.monotonic()-start
 write(root/('smoke_receipt.json' if smoke else 'inference_receipt.json'),{'status':'complete','conditions':results,'input_count':sum(x['input_count'] for x in results),'forward_count':6 if smoke else 180,'independent_repeat_exact':repeat_exact if smoke else None,'GPU_seconds_including_load':elapsed,'peak_GPU_bytes':torch.cuda.max_memory_allocated(),'additional_codec_encodes':0})
 print('Inference complete',elapsed,flush=True)

def main():
 p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--source',type=Path);p.add_argument('--stage',choices=['prepare','smoke','full'],required=True);a=p.parse_args()
 if a.stage=='prepare':prepare(a.root,a.source)
 else:inference(a.root,read(a.root/'frozen_inputs_and_config.json'),a.stage=='smoke')
if __name__=='__main__':main()
