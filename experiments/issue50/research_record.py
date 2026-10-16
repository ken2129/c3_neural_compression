"""Render the final research narrative using audited public aggregate values."""
import argparse
import json
from pathlib import Path


def render(worktree):
    worktree=Path(worktree);assets=worktree/'docs/research/assets'
    data=json.loads((assets/'issue50_four_method.json').read_text());points=data['points']
    labels={'c3':'C3','c3_prime':'C3′','transtic':'TransTIC','adapt_icmh':'Adapt-ICMH'}
    text='''# Issue50: 同じ未使用20画像でのC3／C3′／TransTIC／Adapt-ICMH比較

対象は[Issue50](https://github.com/ken2129/c3_neural_compression/issues/50)。
[Issue49結果PR51](https://github.com/ken2129/c3_neural_compression/pull/51)の固定評価20画像、
通常C3／C3′各4点とCOCO配布JPEG baselineを再利用し、外部2方式各4点・計160推論を追加した。
外部全8条件、float主比較／uint8補助比較、全点のpaired delete-one評価が完了した。
#49の設定・画像・結果・最適化には変更を加えていない。PR51は別PRであり、このPRへ取り込まない。

## 図と全点

![4方式の主比較とuint8補助比較](assets/issue50_four_method.png)

[PNG](assets/issue50_four_method.png)・[PDF](assets/issue50_four_method.pdf)・
[CSV](assets/issue50_four_method.csv)・[JSON](assets/issue50_four_method.json)。
主比較16点、外部uint8補助8点、JPEG baselineを全て残した。
線は観測点の対応を示すだけで、補間・外挿したAPではない。縦線は同じ1画像を除いた
19画像集合の公式AP範囲で、信頼区間ではない。

**TransTICはcodec前後に公式bilinear resizeを行う。Adapt-ICMHはpadding→crop、C3は共通入力寸法のまま。**
この方式固有の再サンプリング差と、主比較の外部float32／C3 uint8という精度差が残る。
共通検出器はどの方式にも追加resizeしない。図の右側は同じ外部復元をuint8にした補助比較で、
codec推論もbitsも追加していない。主・補助の有利な方を事後選択していない。

AP/AP50/AP75/サイズ別APは公式COCO集合bbox評価の%であり、画像別APの平均ではない。
C3の品質欄は校正時の目標bpp、外部方式は公開品質番号で、同rateを意味しない。

| 方式 | 品質/目標 | 実測bpp | AP | AP50 | AP75 | small | medium | large | delete-one AP範囲 |
|:---|---:|---:|---:|---:|---:|---:|---:|---:|:---|
'''
    for m in labels:
        for p in sorted((p for p in points if p['method']==m and p['precision']==('uint8' if m in ('c3','c3_prime') else 'float32')),key=lambda p:p['quality']):
            q=f"{p['quality']/1000:.3f}" if m in ('c3','c3_prime') else str(p['quality'])
            text+=f"| {labels[m]} | {q} | {p['bpp']:.6f} | {p['box_ap']:.3f} | {p['box_ap50']:.3f} | {p['box_ap75']:.3f} | {p['box_ap_small']:.3f} | {p['box_ap_medium']:.3f} | {p['box_ap_large']:.3f} | {p['delete_one_AP_min']:.3f}–{p['delete_one_AP_max']:.3f} |\n"
    b=data['JPEG_baseline']
    text+=f"| COCO JPEG baseline | — | — | {b['box_ap']:.3f} | {b['box_ap50']:.3f} | {b['box_ap75']:.3f} | {b['box_ap_small']:.3f} | {b['box_ap_medium']:.3f} | {b['box_ap_large']:.3f} | CSV参照 |\n"
    text+='''
JPEG baselineは配布JPEGであり圧縮前のlossless原画ではない。今回のentropy-estimated rate軸上の
codec動作点を持たないため、図ではAP水平線、CSVではbpp空欄として区別した。

## 復元精度の感度

外部APを見る前に、主比較をclamp後float32、補助比較を
`torch.round(clamp(rgb,0,1)*255)`（ties-to-even）のuint8 HWC RGB lossless PNGと固定した。
PNG decode後はfloat32で255除算する。全復元についてNumPy独立roundとの一致、PNG往復、
float32保存往復、寸法、有限性、hashを検証した。C3の未保存float復元は推測していない。

| 方式 | 品質 | float AP | uint8 AP | uint8−float (pp) | paired delete-one差の範囲 (pp) |
|:---|---:|---:|---:|---:|:---|
'''
    for m in ('transtic','adapt_icmh'):
        for q in range(1,5):
            f=next(p for p in points if p['method']==m and p['quality']==q and p['precision']=='float32')
            u=next(p for p in points if p['method']==m and p['quality']==q and p['precision']=='uint8')
            lo,hi=data['paired_delete_one_uint8_minus_float_AP_pp_ranges'][f'{m}_{q}']
            text+=f"| {labels[m]} | {q} | {f['box_ap']:.3f} | {u['box_ap']:.3f} | {u['box_ap']-f['box_ap']:+.3f} | {lo:+.3f}–{hi:+.3f} |\n"
    text+='''
roundの影響は品質・方式により正負が変わる。同じ復元でもAPが変わるため、主比較の精度非対称を
無視しない。差の範囲も信頼区間や優越性検定ではない。

## rate範囲と限定的な解釈

主rateは各画像の推定bitsを**codec alignment前のD2 resize済み非padding画素数**で割った値の
算術平均。外部はy+z全likelihood bits、C3はlatentと画像ごとのnetwork bitsを含む。
外部の共有事前学習重みは画像ごとに課金しない仮定であり、実bitstream・コンテナoverheadの測定ではない。
元画素分母の旧点は混ぜず、元bitsから全点を再集計した。

| 方式 | 最低実測bpp | 最高実測bpp |
|:---|---:|---:|
'''
    for m,(lo,hi) in data['rate_ranges'].items():text+=f'| {labels[m]} | {lo:.6f} | {hi:.6f} |\n'
    overlap=data['four_way_rate_overlap']
    text+=f"\n4方式の実測rate端点範囲の共通部分は{overlap[0]:.6f}–{overlap[1]:.6f} bpp。" if overlap else '\n4方式の実測rate端点範囲に共通部分はない。'
    text+='''端点間を連続測定した意味ではない。
範囲外では全方式が揃わず、範囲内にも離散4点しかない。全6組の端点範囲重なりはJSONに記録した。
外部公開4品質はC3の目標4rateへ調整していないため、同rate到達は未達である。
AP差を同rate改善と扱わず、AP補間・外挿やBD-rateを作らない。

主比較では外部方式の観測APは全点でC3/C3′の観測範囲と重なるか上回るが、rateとalignmentが異なる。
Adapt品質2のfloat APはJPEG baselineより高い一方、uint8化で値が下がる。
これはこの20画像・固定検出器の観測で、lossless原画を超える一般的改善や論文再現を意味しない。
TransTIC/Adaptとも公開品質番号に対してAPは単調ではない。全品質を残し、APで選び直していない。
#49のC3′−C3がrateにより混合結果だったことも、この追加測定で変更しない。

## 入力・出典・公式alignmentの監査

#49の固定評価20画像manifestと画像bytesをそのまま使用し、校正20画像との非重複を確認した。
全8 C3条件・計160件の保存済み入力ハッシュと、外部codec直前のD2 uint8入力をbitwise照合した。
短辺800・長辺上限1333の共通入力後に方式固有alignmentを適用する。

TransTICは[固定公式source](https://github.com/NYCU-MAPL/TransTIC/tree/1132f62cbddc0122aa19f620453c3b0c4de49b2c)の
`Alignment(divisor=256, mode='resize')`、codec前後bilinear・`align_corners=False`を維持した。
当初Issueのpadding→crop条件との矛盾は新規TransTIC推論前に検出し、測定を保留した。
2026-10-16のユーザー明示承認とIssue本文更新により公式resizeを例外採用した。
公式align/resumeと独立bilinear実装が全80件で一致した。結果を見てpadding経路へ変更していない。
Adapt-ICMHは[固定公式source](https://github.com/qingshi9974/ECCV2024-AdpatICMH/tree/cf77f2eab798c9e949d2efd46db1adf3bbbc5cc1)の
zero padding→cropを独立実装と照合した。共通検出器は元画像座標へboxを戻す。

外部8重みのhashと公式sourceの固定commit/未変更を実行前に照合した。
共通検出器の重み・設定・環境・padding・前処理metadataは#49と完全一致する。
元実行runtimeは固定source commit `0c3a60c703bd41956b6558d527470350bcc7dd5d`で、
未マージ実行基盤PR22/42に依存する。mainだけで実行可能とは扱わない。
[実装と再集計手順](../../experiments/issue50/README.md)に各段階を記録した。
画像ID、内部絶対パス、モデル識別hash、環境詳細、実画像・重み・全予測は公開に含めない。

## 不確実性・費用・完了範囲

全25評価経路（主16点・外部補助8点・JPEG）について同じ1画像を除く20通りの19画像集合で
公式COCO APを再計算した（計500評価）。全体で支持される30カテゴリが削除画像により26–30へ
変わる。paired delete-oneは記述的感度であり、信頼区間や統計的優越性ではない。
外部両方式の全集合AP16件と再利用C3/JPEGの9件も元予測から再計算して完全一致した。
全点のrate・復元・条件対応を私有正本から監査し、公開CSV/JSON/PNG/PDFを再生成した。

'''
    costs=data['costs'];text+=f"codec推論は累計{costs['codec_attempts']}件（各方式80、各品質20）、失敗・再試行なし。\n起動・モデルload・codec・検出・終了を含む累計GPU時間は{costs['gpu_seconds_including_startup_and_exit']:.3f}秒（{costs['gpu_seconds_including_startup_and_exit']/3600:.6f} GPUh）。\n集計時の私有成果物は{costs['new_artifact_bytes_at_report']:,} bytes（{costs['new_artifact_bytes_at_report']/1024**3:.3f} GiB）。\n"
    text+='''承認済み160推論・2GPUh・新規4GiB・最低空き3GiBを維持した。
各方式の固定先頭画像pilotは条件・hashを検証して本測定へ再利用した。
GPU実使用者を起動直前に確認し、tmux実行・時間/容量guardを使い、他ジョブに干渉していない。
COCO AP再集計とjackknife監査はGPUを非表示にしたCPU処理である。新Box先や追加規模への拡張はない。
開始・進捗通知の配送証拠は私有領域に保持した。最終公開状態は結果PRと完了監査を参照する。

本比較は過去の20画像結果も見て企画した追加比較で、新しい独立な確認実験ではない。
外部事前学習データとの非重複は未確認で、主張しない。公開Adapt-ICMH重みは論文Figure 4そのものの
checkpointではない。全5,000画像の論文APとの差し引き、論文再現、一般的優越性は主張しない。
次に画像数を増やすなら、支持カテゴリが4カテゴリ変わる感度と小標本での非単調APを理由に
別の固定計画で評価する。rate追加なら端点共通範囲での近接した実測rateを事前計画し、
今回は追加画像・品質・seed・最適化へ拡張しない。ユーザーがレビュー・mergeを判断し、勝手にmerge/closeしない。
'''
    (worktree/'docs/research/2026-10-16_issue50.md').write_text(text)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--worktree',required=True);render(p.parse_args().worktree)
