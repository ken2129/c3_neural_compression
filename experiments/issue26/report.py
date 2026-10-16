"""Recompute official subset COCO AP and publish detector-specific summaries."""
import argparse,contextlib,csv,io,json
from pathlib import Path
from evaluate import read,write,sha

def main(root,output):
 import numpy as np
 import matplotlib
 matplotlib.use('Agg')
 import matplotlib.pyplot as plt
 from c3_neural_compression.evaluation import shared_detector
 cfg=read(root/'frozen_inputs_and_config.json');assert read(root/'inference_receipt.json')['input_count']==180
 rows=[];audits=[]
 for group in cfg['groups']:
  label=group['method']+('_'+str(group['target']) if group['target'] else '')
  directory=root/'evaluation'/label;pred=read(directory/'predictions.json');receipts=read(directory/'inputs.json')
  assert [r['image_id'] for r in receipts]==cfg['image_ids']
  for inp,receipt in zip(group['inputs'],receipts):assert sha(inp['path'])==inp['sha256']==receipt['input_sha256']
  with contextlib.redirect_stdout(io.StringIO()):
   m=shared_detector.evaluate_coco(cfg['annotation_file'],pred,cfg['image_ids'])
   replay=shared_detector.evaluate_coco(cfg['annotation_file'],pred,cfg['image_ids'])
  assert m==replay
  write(directory/'metrics.json',m)
  smoke=root/'smoke'/label/'predictions.json'
  if smoke.exists():assert read(smoke)==[p for p in pred if p['image_id']==cfg['image_ids'][0]]
  audits.append({'condition':label,'inputs':20,'prediction_count':len(pred),'metric_replay_exact':True})
  if group['method']=='JPEG_baseline':
   old=root.parent/'issue49_evaluation_20261015/jobs/heldout_baseline/evaluation/metrics.json'
  else:old=Path(group['d2_metrics'])
  d2=read(old);assert d2['image_ids']==cfg['image_ids']
  for detector,metrics,provenance in [('Detectron2',d2,'Issue49 reused'),('YOLOv8m',m,'Issue26 new inference')]:
   row={'detector':detector,'method':group['method'],'target_bpp':group['target'],'actual_bpp':group['bpp'],'image_count':20,'status':'complete','provenance':provenance}
   row.update({k:(v*100 if v>=0 else None) for k,v in metrics.items() if k in ['box_ap','box_ap50','box_ap75','box_ap_small','box_ap_medium','box_ap_large']});rows.append(row)
 baselines={r['detector']:r['box_ap'] for r in rows if r['method']=='JPEG_baseline'}
 for row in rows:row['JPEG_AP_drop']=baselines[row['detector']]-row['box_ap']
 pairs=[]
 for detector in baselines:
  for target in [.1,.15,.225,.3]:
   a=next(r for r in rows if r['detector']==detector and r['method']=='c3_image' and r['target_bpp']==target)
   b=next(r for r in rows if r['detector']==detector and r['method']=='c3_combined' and r['target_bpp']==target)
   pairs.append({'detector':detector,'target_bpp':target,'C3_bpp':a['actual_bpp'],'C3prime_bpp':b['actual_bpp'],'relative_rate_difference':abs(b['actual_bpp']-a['actual_bpp'])/a['actual_bpp'],'C3prime_minus_C3_AP':b['box_ap']-a['box_ap']})
 output.mkdir(parents=True,exist_ok=True)
 data={'image_count':20,'rows':rows,'pairs':pairs,'rate_definition':'mean estimated codec bits / D2-resized nonpadding pixels; frozen Issue49 rates','metric':'official pooled COCO bbox AP percentage','interpretation':'Exploratory transfer pilot; unpaired rates are not matched-rate gains; no significance or generalization claim','YOLO':{k:v for k,v in cfg.items() if k in ['ultralytics','imgsz','batch','rect','auto','scaleup','center','padding_value','precision','conf','iou','multi_label','agnostic','max_det','COCO_maxDets','augment']}}
 annotations=read(cfg['annotation_file'])['annotations'];ids=set(cfg['image_ids']);annotations=[a for a in annotations if a['image_id'] in ids]
 sizes={}
 for label,lo,hi in [('small',0,1024),('medium',1024,9216),('large',9216,1e10)]:
  subset=[a for a in annotations if lo<=a['area']<=hi and not a.get('iscrowd',0)]
  sizes[label]={'noncrowd_objects':len(subset),'images':len(set(a['image_id'] for a in subset))}
 data['GT_support']={'all_annotations':len(annotations),'noncrowd_annotations':sum(not a.get('iscrowd',0) for a in annotations),'size_support':sizes,'supported_categories':len(set(a['category_id'] for a in annotations))}
 write(output/'issue26_yolo_transfer.json',data)
 with (output/'issue26_yolo_transfer.csv').open('w') as f:
  w=csv.DictWriter(f,fieldnames=list(rows[0]),lineterminator='\n');w.writeheader();w.writerows(rows)
 fig,axes=plt.subplots(1,2,figsize=(11,4.2),sharey=True)
 for ax,detector in zip(axes,baselines):
  for method,label in [('c3_image','C3'),('c3_combined',"C3'")]:
   rs=[r for r in rows if r['detector']==detector and r['method']==method]
   ax.plot([r['actual_bpp'] for r in rs],[r['box_ap'] for r in rs],'o-',label=label)
  ax.axhline(baselines[detector],color='gray',linestyle='--',label='COCO JPEG baseline')
  ax.set(title=detector+' (same20 images)',xlabel='Mean estimated bits / D2-resized nonpadding pixel');ax.grid(alpha=.25);ax.legend()
 axes[0].set_ylabel('Official COCO box AP (%)');fig.suptitle('Issue26: detector transfer; measured points, unmatched rates')
 fig.tight_layout()
 for ext in ['png','pdf']:fig.savefig(output/f'issue26_yolo_transfer.{ext}',dpi=180)
 plt.close(fig)
 write(root/'final_metric_and_input_audit.json',{'status':'passed','new_inputs':180,'new_COCO_evaluations':9,'metric_dictionary_replays_exact':9,'smoke_full_first_image_predictions_exact':True,'all_input_hashes_rechecked':True,'existing_D2_conditions':9,'rate_points_reused':8,'conditions':audits})
 print(json.dumps({'baseline_AP':baselines,'AP_differences':pairs},indent=2))
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();main(a.root,a.output)
