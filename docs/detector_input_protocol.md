# 圧縮前resizeと画像別平均rateの評価規約

C3へ `detector_input_v1` を適用する手順。実装は基盤PR #22と追加PR #42を経て2026-10-16にmainへ統合済み。旧入力の比較例は[比較手順](compression_comparison.md)、実行と承認の扱いは[研究ポリシー](experiment_policy.md)を参照する。新しい実験の画像・資源・予算は対応Issueで固定する。

## 入力・復元・rate

- 元画像bytesと元COCO annotations、画像ID・寸法・hashを私有manifestに固定する。
- D2 `ResizeShortestEdge([800,800],1333)`をuint8 RGBへ適用してから圧縮。テンソル変換はfloat32で255除算し、公式mapperの画素値と一致させる。C3のcodec入力に追加paddingはない。
- C3のround／clip済みuint8 PNGをmapper寸法のまま評価する。共通Detectron2側で追加resizeを行わず、検出boxを元画像座標へ戻す。
- 読み込むPNGはprotocol・mapped入力hash・寸法・実験signature・完了attemptとの対応を確認し、元画像入力のartifactと混在させない。
- 主rateは画像ごとの全推定bits／resize済み非padding画素数の算術平均。latentと画像ごとの量子化synthesis／entropy network bitsを含める。実bitstream・header／container込みの伝送サイズではない。
- 元画素分母のpooled rateは補助量として保持できる。集計軸を変えただけでは、旧入力での結果が新入力の測定になるわけではない。
- C3′の基本条件は画像MSE1、feature L1 .1、FPN0–3各.25、500 noise＋最大50 STE、seed20261004と画像ID折込みRNG。採用値・checkpoint選択・数値計算設定は実験ごとに凍結し、APで再選択しない。

## 実行入口

専用環境、入力・重み・manifest、空の出力領域を準備し、sourceを固定する。以下のlambdaは書式例であり、新しい測定の校正済み条件ではない。

```sh
export C3_COCO_ROOT=/path/to/images
export C3_COCO_ANNOTATIONS=/path/to/instances_val2017.json
export C3_SUBSET_IDS=/path/to/frozen_ids.json
export C3_OUTPUT_ROOT=/path/to/new/output
python -m c3_neural_compression.experiments.image \
  --config=c3_neural_compression/configs/coco2017_detector_input.py:combined_lambda0.01
```

`comparison_runner`のC3評価configには、元画像manifest、`condition: c3_combined`または`c3_image`、`input_protocol: detector_input_v1`、固定検出器とSHA256、`c3_output_roots`を指定する。C3-onlyの集計は `detector_input_protocol.collect_runs`。旧protocolと新protocol、異なる主rateや検出器metadataの混在を拒否する。

このconfigだけで後続実験の全監督・保存・決定論的設定が自動適用されるわけではない。[4rate比較の実装](../experiments/issue49/README.md)には500＋50を保持した保存・監督・集計手順を記録している。GPU・時間・容量・停止条件は新しい実行計画で指定する。

## 外部方式・別検出器との比較

TransTICの公式codec alignmentは256倍数へのresizeと復元後のbilinear resume。Adapt-ICMHはzero paddingとcrop。共通mapperの後に方式固有の操作を行い、共通検出器側の追加resizeとは区別する。外部float32とC3 PNGの主比較、全uint8の補助比較を分ける。外部の共有事前学習重みは画像ごとに課金しない仮定を明記する。

YOLO等へ再評価する場合は、その評価器用letterbox等を全入力へ共通に適用し、元座標までの逆変換とクラス対応を検証する。YOLO入力画素数へcodec bpp分母を変えない。

- [4rate校正・非重複20画像評価](research/2026-10-16_1041_JST.md)
- [4方式の共通20画像比較](research/2026-10-16_four_method_comparison.md)
- [YOLOv8mへの転移評価](research/2026-10-16_detector_transfer.md)

## 当初のCPU検証・旧点のrate監査（2026-10-14）

以下はprotocol追加当時の証拠であり、現在のGPU未実行という意味ではない。

[CPU検証JSON](issue41/cpu_protocol_validation.json)は実20画像の公式BGR loaderとのuint8 mapper一致を記録。元画素総数5,659,540、resize後総数17,724,800。当時151 CPUテストで、画素・float変換、旧loader、PNG寸法、二重resize拒否、元座標、画像別平均／pooled／padding分母、欠損・失敗・hash・protocol・stale集計・検出器binary差を検証した。2026-10-16の基盤統合時には選択した関連CPUテスト127件を再実行した。両者は対象の異なるテスト集合である。

[旧点のrate監査CSV](issue41/existing_rate_audit.csv)と[出典JSON](issue41/existing_rate_audit.json)は、外部8点と旧C3の4点を同じ20画像・検出器metadataで再集計した当時の記録。旧C3は元画像を圧縮していたため、resize済み画素数での再表示は補助値であり新入力の実験ではない。論文検出器との重み同一性や公開Adapt重みの論文曲線再現を、この監査から主張しない。

当初の1画像smoke・追加校正案は後続の測定で置き換えられている。現在の状態は各結果記録と対応するGitHub Issue／PRを参照し、古い予算・候補・承認待ちを新しい実験へ持ち越さない。
