"""CPU-only final audit of all measured conditions, source and pooled AP."""
from pathlib import Path
import json,hashlib,math,contextlib,io,time,datetime,shutil,sys
if len(sys.argv)!=2:raise SystemExit('Usage: provenance_audit.py PRIVATE_RESULT_DIRECTORY (with pinned runtime on PYTHONPATH; CPU only)')
O=Path(sys.argv[1]).resolve();R=O;J=O/'jobs';S=Path(__file__).resolve().parent;sys.path.insert(0,str(S))
from c3_neural_compression.evaluation import comparison as common,detector_input_protocol as protocol,shared_detector
from report import public_report

def read(p):return json.loads(Path(p).read_text())
def sha(p):return common.sha256(p)
def scalar_metrics_equal(a,b):
 assert set(a)==set(b)
 for key in a:
  assert a[key]==b[key],f'Official COCO replay differs: {key}'
def main():
 started=time.time();plan=read(O/'scope_plan_v1.json');freeze=read(O/'frozen_eight_conditions.json');controller=read(O/'scientific_continuation_status.json')
 assert controller['status']=='measurements_and_uncertainty_complete' and sha(O/'frozen_eight_conditions.json')==controller['freeze_sha256']
 assert freeze['plan_sha256']==sha(O/'scope_plan_v1.json')==read(O/'scope_approval.json')['plan_sha256']
 assert read(O/'scope_approval.json')['approved'] and freeze['heldout_AP_not_seen'] and freeze['selection_uses_AP'] is False
 assert read(O/'correction_decisions_before_launch.json')['decisions']==[]
 for path,h in plan['execution_source_sha256'].items():assert sha(path)==h
 for path,h in plan['asset_sha256'].items():assert sha(path)==h
 receipts=[read(p) for p in (O/'control').glob('*_receipt.json')];assert len(receipts)==42 and all(r['status']=='complete' and r['returncode']==0 for r in receipts)
 for r in receipts:
  job=read(J/(r['job_id']+'.json'));assert r['job_sha256']==sha(J/(r['job_id']+'.json'));assert r['gpu_process_seconds']<=job['max_gpu_seconds'];assert r['stop_reason'] is None
  if r['job_id'].startswith('heldout_'):assert r['started_unix']>freeze['frozen_unix']
 registry=read(plan['job_registry']);assert registry['scope_plan_sha256']==sha(O/'scope_plan_v1.json')
 for jobid,h in registry['job_sha256'].items():assert sha(J/(jobid+'.json'))==h
 actual_encodes=sum(read(J/(r['job_id']+'.json')).get('image_count',0) for r in receipts if read(J/(r['job_id']+'.json'))['mode']=='codec');assert actual_encodes==324
 gpu_seconds=sum(r['gpu_process_seconds'] for r in receipts);assert gpu_seconds<40*3600
 manifests={phase:read(plan[phase+'_manifest']) for phase in ['calibration','heldout']}
 for m in manifests.values():common.validate_assets(m);assert len(m['image_ids'])==20
 assert not set(manifests['calibration']['image_ids'])&set(manifests['heldout']['image_ids'])
 selection=read(O/'heldout_selection_v1/selection.json');assert not set(manifests['heldout']['image_ids'])&set(selection['excluded_image_ids'])
 assert sha(plan['heldout_manifest'])==freeze['heldout_manifest_sha256']
 expected_detector=None;phases={};source_rows=[];uncertainty_checks=[]
 for phase,m in manifests.items():
  points=[];evaldirs={}
  for c in freeze['conditions']:
   name=c['selected_job'] if phase=='calibration' else f"heldout_{c['method'][3:]}_{round(c['target_bpp']*1000):03d}"
   job=read(J/(name+'.json'));assert job['lambda']==c['lambda'] and job['image_count']==20
   root=Path(job['output_dir']);found,signature=common.discover_c3(m,[root],c['method'],input_protocol=protocol.DETECTOR_INPUT)
   cfg=read(root/'datum_00000/config.json');cal=read(J/(c['selected_job']+'.json'));calcfg=read(Path(cal['output_dir'])/'datum_00000/config.json')
   def scientific_cfg(raw):
    raw=json.loads(json.dumps(raw));raw.pop('output_dir');raw['checkpointing'].pop('directory');raw['dataset'].pop('root_dir');raw['dataset'].pop('subset_ids_file');return raw
   assert scientific_cfg(cfg)==scientific_cfg(calcfg)
   assert cfg['loss']['rd_weight']==c['lambda'] and cfg['random_seed']==20261004 and cfg['opt']['num_noise_steps']==500 and cfg['opt']['max_num_ste_steps']==50
   assert signature['fields']['code_commit']==plan['runtime_commit']
   if phase=='calibration':
    assert sha(root/'datum_00000/config.json')==c['source']['config_sha256'];assert sha(root/'comparison_attempt.json')==c['source']['attempt_ledger_sha256']
   for index,image_id in enumerate(m['image_ids']):
    target=Path(job['evidence_dir'])/f'datum_{index:05d}';record=read(target/'final_artifacts_verified.json');trained=read(target/'trained_params_receipt.json')
    assert sha(target/'quantized_params.pkl')==record['sha256'] and sha(target/'trained_params.pkl')==record['trained_params_sha256']==trained['sha256']
    assert sha(root/f'datum_{index:05d}/reconstruction.png')==record['PNG_sha256'] and sha(root/f'datum_{index:05d}/metrics.json')==record['metrics_sha256']
    assert read(target/'input_identity.json')['image_id']==image_id
   ev=J/(name+'_eval')/'evaluation';assert read(ev/'status.json')['status']=='complete' and read(ev/'manifest.json')==m
   det=read(ev/'detector.json')
   if expected_detector is None:expected_detector=det
   else:assert det==expected_detector
   erows=read(ev/'per_image.json');summary=protocol.rate_summary(m,erows,c['method'],protocol.DETECTOR_INPUT);assert summary==read(ev/'rate_summary.json')
   for row in erows:
    item=found[row['image_id']];assert row['reconstruction_sha256']==sha(item['path']) and row['estimated_bits']==item['metrics']['estimated_bits']
   predictions=read(ev/'predictions.json');metrics=read(ev/'metrics.json')
   with contextlib.redirect_stdout(io.StringIO()):replayed=shared_detector.evaluate_coco(m['annotation_file'],predictions,m['image_ids'])
   scalar_metrics_equal(metrics,replayed)
   points.append({'method':c['method'],'target_bpp':c['target_bpp'],'status':'complete','image_count':20,'official_pooled_COCO':True,'mean_estimated_bpp':summary['estimated_bpp'],'metrics':metrics,'lambda':c['lambda'],'private_source':str(ev)})
   evaldirs[c['method']+'_'+str(round(c['target_bpp']*1000)).zfill(3)]=ev
   source_rows.append({'phase':phase,'job':name,'images':20,'attempt_hash':sha(root/'comparison_attempt.json'),'prediction_hash':sha(ev/'predictions.json'),'official_AP_replay_exact':True,'final_params_PNG_and_metrics_hashes_exact':True,'rate_recomputed_exact':True,'frozen_scientific_config_exact':True})
  base=J/(phase+'_baseline')/'evaluation';assert read(base/'status.json')['status']=='complete' and read(base/'manifest.json')==m and read(base/'detector.json')==expected_detector
  baseline_metrics=read(base/'metrics.json');predictions=read(base/'predictions.json')
  with contextlib.redirect_stdout(io.StringIO()):replayed=shared_detector.evaluate_coco(m['annotation_file'],predictions,m['image_ids'])
  scalar_metrics_equal(baseline_metrics,replayed)
  phases[phase]={'image_ids':m['image_ids'],'points':points,'baseline':{'status':'complete','image_count':20,'official_pooled_COCO':True,'metrics':baseline_metrics}}
  evaldirs['baseline']=base;unc=read(O/(phase+'_uncertainty.json'));assert len(unc['records'])==180 and unc['N']==20
  expected={(n,i) for n in evaldirs for i in m['image_ids']};seen=set()
  for record in unc['records']:
   key=record['condition'],record['omitted_image_id'];assert key in expected and key not in seen;seen.add(key)
   subset=[i for i in m['image_ids'] if i!=key[1]];pred=[p for p in read(evaldirs[key[0]]/'predictions.json') if p['image_id']!=key[1]]
   with contextlib.redirect_stdout(io.StringIO()):replay=shared_detector.evaluate_coco(m['annotation_file'],pred,subset)
   scalar_metrics_equal(record['metrics'],replay)
  assert seen==expected
  uncertainty_checks.append({'phase':phase,'official_delete_one_replays_exact':180,'category_support_range':unc['category_support_range']})
  print(phase,'all8 condition provenance, baseline and180 uncertainty replays passed',flush=True)
 finish=max(datetime.datetime.fromisoformat(r['finished_at']).timestamp() for r in receipts);start=min(r['started_unix'] for r in receipts)
 bundle={'schema_version':1,'targets':plan['targets'],'phases':phases,'costs':{'gpu_seconds':gpu_seconds,'wall_seconds':finish-start}}
 write=common.write_json;write(O/'verified_measurement_bundle_private.json',bundle);write(O/'verified_public_numerical_summary.json',public_report(bundle))
 size=sum(p.stat().st_size for p in O.rglob('*') if p.is_file());assert size<8*1024**3 and shutil.disk_usage(R).free>3*1024**3
 result={'status':'passed','audited_unix':time.time(),'CPU_audit_seconds':time.time()-started,'scope_plan_hash':sha(O/'scope_plan_v1.json'),'freeze_hash':sha(O/'frozen_eight_conditions.json'),
 'codec_encodes':324,'calibration_encodes':160,'heldout_encodes':160,'reproducibility_encodes':4,'complete_jobs':42,'failed_jobs':0,'official_full_set_AP_replays':18,'official_delete_one_AP_replays':360,'rows':source_rows,'uncertainty_checks':uncertainty_checks,'same_detector_metadata_all_conditions_and_baselines':True,'heldout_started_after_freeze':True,'frozen_lambda_config_exact':True,'mean_rate_recomputed':True,'no_correction_or_heldout_retuning':True,'GPU_seconds':gpu_seconds,'wall_seconds':finish-start,'current_result_bytes':size,'disk_free_bytes':shutil.disk_usage(R).free,'publication_complete':False,'goal_complete':False}
 write(O/'final_provenance_audit.json',result);print(json.dumps({k:v for k,v in result.items() if k not in ['rows']},indent=2))
if __name__=='__main__':main()
