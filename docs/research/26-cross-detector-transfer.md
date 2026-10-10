# 別の検出器への特徴損失の効果の転移

更新日: 2026-10-10 JST  
関連: [Issue #26](https://github.com/ken2129/c3_neural_compression/issues/26)

## 目的

通常C3に対するcombinedのAP改善が、最適化に使っていない別backboneの検出器にもどの程度残るかを調べる。
特定の特徴損失用検出器にだけ適した復元になっていないかを切り分ける。

## 計画

### 決定済み（2026-10-10）

- 追加の評価専用検出器BはYOLOv8mとする。
- 同一の元画像、通常C3復元、combined復元を固定して各検出器で評価する。Bに合わせた再最適化や復元画像の作り直しは行わない。
- C3の特徴損失用torchvision Faster R-CNN R50-FPN COCO_V1と、#20/#23共通評価用Detectron2 R50-FPN 3xを区別する。この2つを同一checkpointとは扱わない。
- DINO-SwinTは将来の候補に留め、今回の選択には含めない。

- #20と同じ固定20画像、C3のrate–distortion係数λ=.0003/.001/.003の3レート（特徴損失係数ではない）を使う。
- 通常C3／combinedの各20画像×3レート、計120復元と元画像20枚を対象にする。同じλでも方式間のbppは一致するとは限らない。
- YOLOv8mの公開COCO checkpoint `yolov8m.pt`を使い、評価中は固定する。
- 主比較はA（特徴損失用torchvision Faster R-CNN R50-FPN COCO_V1）とB（YOLOv8m）。共通Detectron2評価器は既存比較の参照として区別する。
- 同一の保存済み復元をnative精度のまま用い、追加の非可逆変換をしない。再符号化・B向け最適化はしない。

### 実行前の未確認事項

- YOLOv8m checkpointの取得・SHA-256、依存version、前処理、COCO class対応、postprocessing、評価設定。
- 同一復元120枚の所在と完全性、必要な追加回収範囲、利用許可資源、時間／容量予算。

これは計画文書の承認であり、GPU実験開始は承認されていない。実行前に未確認事項を解消し、開始許可を確認する。

## 結果（未実施）

- 選択したYOLOv8mの公開重みは存在するが、今回の評価環境への取得・load・推論は未確認。
- cross-detector AP、改善量、転移率の実測結果はまだない。
- #20の最小パッケージは評価JSON中心で、入力画像、C3最適化artifact/checkpoint、float32復元を含まない。[最新の回収範囲](https://github.com/ken2129/c3_neural_compression/issues/20#issuecomment-6095005535)を参照。
- 既存の同一復元RGBの所在と完全性は未確認。元データ側に残っているかを調べ、必要な最小ファイルを特定する。再評価可能な画像を取得済みとは扱わない。

## 解釈・評価方法

主指標は共通COCOevalによる集合全体のbox AP@[.50:.95]とする。bppはentropy-estimated bitsを用い、`sum(estimated bits) / sum(original H×W)`で集計する。actual bitstream bppと混ぜず、画像別AP・bppの単純平均を主指標にしない。
各検出器の内部で、近いbppにおける「combined − 通常C3」のAP差、および元画像からのAP低下を比較する。
検出器間のAP絶対値の差自体は転移の根拠にしない。
画像ID、注釈、復元hash・精度、クラス対応、前処理、座標系、共通COCOevalを固定・記録する。
通常C3とcombinedの共通bpp範囲で比較し、補間方法を明記する。共通範囲がなければ同等bppの結論を保留し、範囲外へ外挿しない。
YOLOはimgsz=640、conf=.001、NMS iou=.7を出発点とし、固定versionで設定名と挙動を検証する。letterboxの逆変換、80クラスからCOCO category IDsへの対応、max detections、precision、augmentation、batch sizeも記録する。
追加のJPEG再圧縮や黙った8-bit丸めはしない。PNGで保存済みの場合も元runでの量子化とdtypeを照合する。元画像には圧縮方式としてのbppを割り当てない。
この20画像の計画は予備確認であり、一つのBへの転移だけであらゆる検出器への汎化を主張しない。差が消える・逆転する結果も残す。

## 判断・次の作業

1. 既存復元の所在・hash・画像集合・rateを確認し、評価用の固定入力を準備できるか判断する。
2. 利用可能な復元、必要な追加回収、評価器設定と計算予算を整理して実行条件を確定する。
3. A・Bのcheckpoint hash、ライブラリversion、各検出器固有の前処理・後処理を固定する。YOLOのnative前処理を確認し、Aへ同じresizeを無理に適用しない。
4. 実験開始が承認されたら固定subsetの1画像・1レートで、元画像／通常C3／combinedをsmoke評価する。RGB値、hash、letterbox、元座標へのbbox逆変換、COCO category対応、予測JSONを検証する。
5. smoke成功後に20画像・全3レートへ進み、同一入力による検出器別AP差を測る。空検出は除外せず、失敗や欠損で20画像が揃わなければ未完了とする。
6. 結果を見て、今回選んだYOLOv8mにも改善が残るかを判断する。

## 出典・変更履歴

- [Issue #26](https://github.com/ken2129/c3_neural_compression/issues/26): 当初の目的・仮説・評価上の注意。最新の実行チェックリストと条件を参照。
- 2026-10-10 JSTの研究方針相談: YOLOv8mと固定20画像×3レートの計画を選択。計画文書の更新とGPU実験開始の許可を分離した。
- [#20最小パッケージの検証限界](https://github.com/ken2129/c3_neural_compression/issues/20#issuecomment-6095005535)
