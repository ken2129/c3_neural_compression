# 通常C3・combined・TransTICの比較

更新日: 2026-10-10 JST  
関連: [Issue #20](https://github.com/ken2129/c3_neural_compression/issues/20)、[draft PR #22](https://github.com/ken2129/c3_neural_compression/pull/22)

## 目的

現在のC3＋特徴損失が、通常C3と外部方式TransTICに対して、近い推定bppでどの程度のCOCO box APを得られるか調べる。
新しい仕組みを加える前に、現在の方式の位置づけと、次に調べる価値のある条件を判断する。

## 計画（決定済み）

- 固定COCO val2017校正20画像。通常C3／combined各3レートとTransTIC det1–4の計10条件。100画像の本評価は対象外。
- code: `issue20/cpu-comparison-preparation`、commit `01d1fb3fde6d75f42ba7c6affe9014293be37710`。実験branchと固定commitは記録の編集に伴って変更しない。
- C3: λ=.0003/.001/.003、各500 noise＋最大50 STE、seed=20261004。通常C3とcombinedは同じ最適化予算。
- combined: 特徴損失係数.1、固定torchvision Faster R-CNN R50-FPN COCO_V1、特徴層0–3、各重み.25、特徴距離L1。共通評価用Detectron2とは異なる。
- TransTIC: 公開det1–4、再学習なし。固定source commitは下記技術手順を参照。
- 共通評価: Detectron2 Faster R-CNN R50-FPN 3x、`model_final_280758.pkl`。TransTIC論文と厳密に同一のcheckpointかは未確認。
- bpp = `sum(estimated bits) / sum(original H×W)`。画像別bppの単純平均やactual bitstreamの値と混ぜない。
- 主指標は同一20画像集合全体のCOCO box AP@[.50:.95]。画像別APの平均を使わない。
- 入力hash、注釈、前処理、元画像／detector／codec入力サイズ、検出器、出力schemaを照合する。

詳細は固定commitの[比較手順](https://github.com/ken2129/c3_neural_compression/blob/01d1fb3fde6d75f42ba7c6affe9014293be37710/docs/issue20_comparison.md)と[CPU検証記録](https://github.com/ken2129/c3_neural_compression/blob/01d1fb3fde6d75f42ba7c6affe9014293be37710/docs/issue20_validation.md)を参照。初期smokeと後の校正実行条件を区別する。

## 結果（観測・確認範囲）

### 2026-10-09の引き継ぎ時点

[引き継ぎ記録](https://github.com/ken2129/c3_neural_compression/issues/20#issuecomment-6082923972)では、次の7条件についてstatus=complete、image IDs 20件、per_image 20件を確認している。

- 通常C3: λ=.0003/.001/.003
- combined: λ=.0003/.001
- TransTIC: det1/det2

combined λ=.003は中断、TransTIC det3/det4は未実施だった。移行先での完全artifact validateと全10条件の比較集計は、この時点の確認には含まれない。

### 2026-10-10の最小引き継ぎパッケージ

[最新の回収方針](https://github.com/ken2129/c3_neural_compression/issues/20#issuecomment-6095005535)では、最小ZIP 351,381 bytesとSHA256SUMSの転送・照合完了が07:12 UTCに記録された。先行する大容量転送は停止され、未完成の一式は使用しない。

最小版には固定manifest/IDs、plan、設定・補助スクリプト、環境version、完了7条件の評価JSONが含まれる。公開重み、入力画像、C3最適化artifact/checkpoint、float32復元、大量ログは含まれない。
したがって最小版だけでは、元artifactの完全validate・復元・再評価はできない。

これは転送に関する記録であり、現在の実験プロセスの生存確認ではない。最新runの進捗と全10条件の集計結果は、実行側の記録で別途確認する。本Markdown作成では評価JSON本体の数値を再集計していない。

## 解釈

- 7条件の完了記録だけでは全レートの比較を結論づけられない。
- 短い校正予算の結果は収束済みの性能比較ではない。1画像smokeは接続確認であり、性能の根拠にしない。
- 方式間の特徴損失用検出器、損失、前処理、出力精度の差を残す。INR一般の優位性や、特徴損失だけの効果を外部方式との比較から主張しない。
- 校正結果から少数点のBD-rateやSOTAを主張しない。

## 判断・次の作業

決定済み:
- 完了7条件は元評価JSONを保持し、manifest/20画像ID/検出器/必要メトリクスの一致を確認して再利用する。完全artifact検証済みとは報告しない。
- 未完了combined λ=.003は固定20画像すべてを新runで実行する。部分attemptの継ぎ合わせ、APを見たseed・再試行選択はしない。
- 残り3条件と既存7条件の整合を確認し、全10条件が揃ってから集計する。
- 検証契約上追加artifactが必要なら必要ファイルを特定する。全体回収・全条件再実行を自動で追加しない。

結果が揃った後に、重なるbpp範囲、改善するレート帯、時間条件を確認して次の比較条件を判断する。勝つことを完了条件にしない。

## 出典

- [校正20画像の固定条件と実行開始（2026-10-07）](https://github.com/ken2129/c3_neural_compression/issues/20#issuecomment-6030930218)
- [中断の観測（2026-10-09）](https://github.com/ken2129/c3_neural_compression/issues/20#issuecomment-6082860213)
- [引き継ぎ条件・run参照・hash（2026-10-09）](https://github.com/ken2129/c3_neural_compression/issues/20#issuecomment-6082923972)
- [最小パッケージへの変更と検証限界（2026-10-10）](https://github.com/ken2129/c3_neural_compression/issues/20#issuecomment-6095005535)
