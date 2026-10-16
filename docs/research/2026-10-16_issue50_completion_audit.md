# Issue50 完了条件への対応

[研究記録・全点](2026-10-16_issue50.md)と[実装手順](../../experiments/issue50/README.md)。
更新後Issue50の24チェック項目を私有の要件別監査に対応付けた。

| Issueの項目 | 証拠と判定 |
|:---|:---|
| 1–2 固定20画像・#49再利用 | manifest/画像bytes一致、校正との非重複、全8 C3完了attempt、保存済み入力160件の一致。C3/JPEGの全集合AP9件を再計算して完全一致。再学習・最適化・λ変更なし。 |
| 3 全外部条件 | TransTIC/Adapt各4品質×20、合計160推論・160成功。失敗・再試行なし。APで品質を選別しない。 |
| 4–6 alignment・入力・検出器 | 共通D2 uint8入力をbitwise照合。ユーザー承認済みTransTIC公式resize、Adaptのzero padding/cropを独立実装と照合。共通検出器metadata完全一致、追加resizeなし、元座標box。 |
| 7–8 rateと公式metric | 元y+z bits/C3 latent+network bitsからalignment前画素数分母のmean bppを再計算。公式集合AP/AP50/AP75/サイズAPを全点掲載。共有重み非課金・entropy-estimatedを明示。 |
| 9–10 pilot・予算 | 固定先頭画像を再利用。起動/load/終了込みGPU累計188.234秒、160/160推論。今回成果物約1.82GiB、物理空き約17GiBで4GiB/free3GiB以内。CPU監査はGPU非表示。 |
| 11–14 実行・通知・保存・停止guard | 起動直前のGPU使用確認、tmux、時間/容量guard、非有限/入力/hash/寸法チェック、既存出力時の再実行拒否。開始/進捗のHTTP成功receiptを保持。完了通知と配信状態は最終私有receiptに記録。#49を変更せず別領域で保存。新Box先は使わない。 |
| 15–16 全点図表・rate重なり | PNG/PDF/CSV/JSONに主16点、外部uint8補助8点、JPEG baseline。rate端点共通範囲0.113950–0.277056、全6組の範囲重なりを記録。同rate未達を明記しAP補間・外挿を行わない。 |
| 17 paired delete-one | 全25経路×20削除集合＝500公式再評価。全外部監査320件と#49既存180件の辞書に完全一致。同じ19画像を使用し、支持カテゴリ30→26–30を記録。信頼区間と呼ばない。 |
| 18–20 解釈と次の計画 | 追加比較・小標本・事前学習重複未確認・公開Adapt重みの相違を明記。論文AP差し引き/再現/BD-rate/一般的優越性なし。追加画像/rateは別計画。 |
| 21–23 成果物・公開範囲 | 全外部条件の対応・復元・bits・AP監査合格。公開数値25 CSV行を元bits/metricと照合。CSV/PNG再生成完全一致、PDFは作成日時以外一致、JSONは保存量snapshot以外一致。実図を目視確認。私有ID/絶対パス/モデルhash/画像/重み/全予測を公開から除外。 |
| 24 結果PRと依存 | main向けの独立結果PR。C3結果はPR51を参照し、固定runtime依存PR22/42を明記。公開後にhead treeと全blobを読み戻してローカル成果物との同一性を検証する。merge/Issue closeは行わない。 |

外部APを見る前にround/tensor→PNGを凍結し、主は外部float32/C3 uint8、補助は全uint8。
全160復元でfloat保存往復と独立round/PNG往復の全画素一致を検証した。
uint8化のAP差は−0.992～+0.940点で、影響の符号が一定ではない。主・補助を選び直していない。

科学測定・ローカル成果物の監査は合格。PR作成だけで完了とはせず、公開head tree/全blobの
読み戻し検証と最終状態確認を別の私有監査に保持する。ユーザーのレビュー・merge判断を尊重する。
