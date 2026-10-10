# Adapt-ICMHを加えた比較

更新日: 2026-10-10 JST  
関連: [Issue #23](https://github.com/ken2129/c3_neural_compression/issues/23)、[C3／TransTICの研究記録](20-c3-transtic-comparison.md)

## 目的

通常C3／combined／TransTIC／Adapt-ICMHを共通条件で比較し、現在の方式の位置づけがTransTIC一つへの比較に依存しないか確認する。

## 計画（決定済み）

2026-10-10の実施方針:
- #20と同じ主比較環境・同じユーザー起動Codexセッションを使う。Adapt-ICMH依存環境は分離し、既存C3環境を変更しない。
- 公開Detection重みlevel1–4を評価する。再学習しない。
- 最初に1モデル・1画像で取得、strict load、RGB復元、有限な推定bits、共通評価への接続を順に確認する。成功後、固定20画像×4公開levelへ進む。
- 今回は校正・予備比較。100画像の本評価は含めない。
- #20のmanifest、入力hash・注釈、共通Detectron2評価器、bpp/AP定義、C3 budgetとseedを継承する。
- 前処理差を明記する。Adapt-ICMHの256倍数化はpadding、TransTICはresizeであり、同一操作とは扱わない。
- 公開checkpointのhash、公式source commit、依存version、実行資源、時間測定範囲を記録する。
- 時間比較の測定中はGPU実験を重ねない。異なる実行環境の時間を同一条件として混ぜない。

## 結果（観測・未確認）

[Issue #23](https://github.com/ken2129/c3_neural_compression/issues/23)の2026-10-10更新時点:
- 公開Detection重み4ファイルのasset存在は確認済み。
- 以前のクラウド側の取得はtimeoutとなり、重み本体の取得、strict load、推論、共通評価成功は未確認。
- 実施方針の記録は、環境構築・実験開始・完全validateの完了報告ではない。
- このMarkdown追加で新しい実験や重み取得は行っていない。

#20最小パッケージの制約も引き継ぐ。7条件の評価JSONの再利用と、元artifactの完全validateは区別する。後者に必要なartifactは最小版に含まれない。

## 解釈

公開モデルがあることは、主比較環境で評価可能であることを保証しない。
[著者の説明](https://github.com/qingshi9974/ECCV2024-AdpatICMH/issues/3#issuecomment-2735226569)では公開重みは再学習版で論文Figure 4と一致しないため、論文値を今回の測定値として使わない。
方式全体の比較として扱い、INR一般の優位性は主張しない。

## 判断・次の作業

- 取得・load・復元・bits・共通評価の各gateを順に記録する。
- 失敗時は対象、エラー、再開条件を残し、成功するまで4level×20画像へ拡大しない。
- 成功後は4方式の共通bpp範囲、AP、圧縮時間と条件差をまとめる。
- 同じ20画像集合全体からAPを計算し、失敗画像を除外した部分集合を全体結果にしない。
- SA-ICM等の追加方式は今回の範囲に含めない。

## 出典

- [Issue #23: 実施方針・検証gate](https://github.com/ken2129/c3_neural_compression/issues/23)
- [公式実装](https://github.com/qingshi9974/ECCV2024-AdpatICMH)
- [公開重みv1.0](https://github.com/qingshi9974/ECCV2024-AdpatICMH/releases/tag/v1.0)
- [論文](https://arxiv.org/abs/2407.09853)
