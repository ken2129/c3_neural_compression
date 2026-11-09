# 公開入力3ファイルの provenance E2E（Issue #89）

[実行Issue #89](https://github.com/ken2129/c3_neural_compression/issues/89) の承認範囲で、CPUのみ・Python 3.11.17 の標準ライブラリのみを用いた1回の実行。研究結果の追加、AP/bppの再計算、モデル実行、依存追加は行っていない。

## 入力と結果

固定source SHA: `a3c285fb191f95bdf3ecea54408c53adb177b5c1`（承認計画から固定）。[manifest.json](manifest.json) に3入力の相対path・byte数・SHA256を保存した。入力はテキストとして解析せずbytesとして読み取った。合計33,500 bytes（JSON 13,631、CSV 4,716、Markdown 15,153）。時刻・絶対pathを含めない決定的JSONを生成する。

CLIは生成に加えて `--check` で現在の入力とmanifestの全内容を比較し、不一致・読み取り失敗を終了コード1で返す。指定公開入力は読み取りのみで、欠損・改変のテストには指定生成ディレクトリ内の一時fixtureを使い、終了後に削除した。

## 再現コマンド

リポジトリrootで実行する（`-B` はbytecode生成を抑止）。生成JSONは標準出力に出る。保存済みmanifestを上書きせず比較するには2番目のコマンドを使う。

```sh
python -B docs/research/generated/issue50-input-provenance-e2e/provenance.py
python -B docs/research/generated/issue50-input-provenance-e2e/provenance.py --check docs/research/generated/issue50-input-provenance-e2e/manifest.json
python -B -m unittest discover -s docs/research/generated/issue50-input-provenance-e2e -p 'test_*.py' -v
```

別のcheckoutには `--root /path/to/checkout` を指定できる。source SHAは固定メタデータであり、CLIはcheckoutのgit SHAそのものを検査しない。再現時は上記固定source SHAの入力を用意する必要がある。

## 実施した検証と負例

[unittestログ](test-results.txt): **7件実行、7件成功、失敗0、エラー0**。既知の `abc` のbyte数とSHA256、CLIの決定性、正常manifest照合、欠損fixture、同じ長さで内容を変えたfixture、source SHA不一致、処理前後のfixture入力不変性を検証した。欠損は終了コード1とエラー、内容変更は終了コード1と `provenance mismatch` を検出しており、これらは意図した負例の成功である。

実入力の `--check` は `OK: 3 inputs match manifest`、終了コード0。CLIの再出力と保存manifestのbyte単位比較も一致した。[検証集計](verification.json) に結果を保存した。追加のモデルrunや研究実験はない。

## 制限・未確認事項

環境に `rg` と `git` がなく、初期の検索・git確認コマンドは失敗した。以後Python標準ライブラリでファイルを確認した。ローカルにgitメタデータはなく、source SHAと入力内容のGitHub上での対応関係を独立には照合していない。manifestは提供された公開入力bytesの記録であり、改ざん耐性のある署名や科学的妥当性の保証ではない。

通常PRの公開は提供worker helperで実施する。E2E全体の合格に必要な `WORKER_FINISHED` のcompleted結果保存、dotへのPRイベント通知、後続巡回での二重dispatch不在はworkerでは未検証であり、本レポートはそれらの成功を主張しない。管理台帳の修復や追加PRは行わない。
