# C3の公開数値collector

現在の自動化構成は [実験コンテナ内でCodexを動かす経路](../research_resident/EXECUTION.md) を参照。
旧SSH接続・固定GPUレシピを先に実行する案は、この経路へ置き換えた。

`collect_c3.py CODEC_OUTPUT_ROOT PUBLIC_JSON` は、完了したC3比較attemptをCPUで照合して、
推定bpp、量子化後PSNR、計時等の選択した数値だけを抽出する任意の補助ツール。
実験の起動・承認・コンテナ選択は担当しない。元のmetrics、画像ID、内部パス、重み、
画像、詳細hashを公開JSONへコピーしない。1画像・同λの値を同rate比較やAP評価とは扱わない。

PR #109の既存条件AをCUDA非表示で読み、1画像の630-byte公開JSONを抽出した確認記録は
collectorに対する検証であり、新しい常駐経路・実Codex・実GPUのE2E成功ではない。
