"""Render audited private Issue49 results; exports public numerical fields only.

Usage: python final_report.py PRIVATE_RESULT_DIRECTORY OUTPUT_DIRECTORY
Requires final_provenance_audit.json status passed and the verified bundle,
frozen conditions, and both paired uncertainty files in PRIVATE_RESULT_DIRECTORY.
The independent provenance audit must be run before this presentation helper.
"""
from pathlib import Path
import json,sys,csv
if len(sys.argv)!=3:
 raise SystemExit(__doc__)
O=Path(sys.argv[1]);asset=Path(sys.argv[2])
from report import public_report,render
assert json.loads((O/'final_provenance_audit.json').read_text())['status']=='passed'
bundle=json.loads((O/'verified_measurement_bundle_private.json').read_text());public=public_report(bundle);freeze=json.loads((O/'frozen_eight_conditions.json').read_text());audit=json.loads((O/'final_provenance_audit.json').read_text())
public.update(source_commit='0c3a60c703bd41956b6558d527470350bcc7dd5d',optimization={'noise_steps':500,'max_STE_steps':50,'seed':20261004,'image_weight':1,'feature_weight_C3prime':.1,'FPN_layers':['0','1','2','3'],'layer_weights':[.25]*4,'teacher':'same fixed torchvision COCO_V1 feature teacher as prior protocol; shared evaluation detector is separate','generation_XLA_exclude_nondeterministic_ops':True,'generation_torch_cudnn_deterministic':True},frozen_conditions=[{k:c[k] for k in ['method','target_bpp','lambda','calibration_mean_bpp']} for c in freeze['conditions']],audit_summary={'completed_jobs':42,'codec_encodes':324,'full_set_official_AP_replays_exact':18,'delete_one_official_AP_replays_exact':360,'failed_jobs':0,'heldout_started_after_freeze':True,'new_GPU_experiments_during_final_audit':0},selection={'prior_used_images_excluded':624,'eligible_population':4376,'subset_N':20,'selection_seed':20261015,'rule':'SHA256(seed:ID) ascending, no category/AP filtering','unused_claim_scope':'No use in inspected experiment records; unavailable/unrecorded history not certified'},uncertainty={})
paired=[];uncrows=[]
for phase in ['calibration','heldout']:
 u=json.loads((O/(phase+'_uncertainty.json')).read_text());public['uncertainty'][phase]={'method':u['method'],'interpretation':u['interpretation'],'AP_ranges_percent':u['AP_ranges_percent'],'category_support_range':u['category_support_range']}
 for name,limits in u['AP_ranges_percent'].items():uncrows.append({'phase':phase,'condition':name,'delete_one_AP_min_percent':limits[0],'delete_one_AP_max_percent':limits[1],'min_supported_categories':u['category_support_range'][0],'max_supported_categories':u['category_support_range'][1]})
 for pair in public['phases'][phase]['paired_comparisons']:
  label=str(round(pair['target_bpp']*1000)).zfill(3);a={r['omitted_image_id']:r['metrics']['box_ap'] for r in u['records'] if r['condition']=='c3_image_'+label};b={r['omitted_image_id']:r['metrics']['box_ap'] for r in u['records'] if r['condition']=='c3_combined_'+label};assert set(a)==set(b) and len(a)==20;diffs=[100*(b[i]-a[i]) for i in a]
  pair.update(delete_one_AP_difference_min=min(diffs),delete_one_AP_difference_max=max(diffs));paired.append(dict(phase=phase,**pair))
public['conclusion']='Mixed independent-subset evidence. Positive AP differences at three target pairs and negative at0.225; all heldout target errors<=10%, but method-rate gaps exceed10% at0.10/0.15. No universal equal-rate superiority or statistical significance claim. Calibration C3prime is nonmonotonic; heldout selected C3prime points are monotonic in this sample.'
render(public,asset)
for name,rows in [('paired_comparisons.csv',paired),('uncertainty.csv',uncrows),('frozen_conditions.csv',public['frozen_conditions'])]:
 with (asset/name).open('w',newline='') as f:w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
fig,axes=plt.subplots(1,2,figsize=(10,4),sharey=True)
for ax,phase in zip(axes,['calibration','heldout']):
 rows=[r for r in paired if r['phase']==phase]
 for i,r in enumerate(rows):
  ax.vlines(i,r['delete_one_AP_difference_min'],r['delete_one_AP_difference_max'],color='#495d74',lw=2)
  ax.plot(i,r['AP_difference_points'],'o',color='#234d76',markerfacecolor='#234d76' if r['rate_pair_passed'] else 'white',ms=7)
 ax.axhline(0,color='0.4',lw=1,ls='--');ax.set_xticks(range(4),[str(r['target_bpp']) for r in rows]);ax.set_title(phase.capitalize());ax.set_xlabel('Target bpp (rates are not identical)');ax.grid(axis='y',alpha=.2)
axes[0].set_ylabel('AP difference: C3 prime - C3 (points)');fig.suptitle('Paired delete-one sensitivity ranges; not confidence intervals',fontsize=11);fig.tight_layout();fig.savefig(asset/'paired_sensitivity.png',dpi=180);fig.savefig(asset/'paired_sensitivity.pdf');plt.close(fig)

# Repository CSV artifacts use LF; numerical fields are unchanged.
for path in asset.glob("*.csv"):
 path.write_text(path.read_text())
