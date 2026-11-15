# 実験コンテナ経路のE2Eと起動・公開照合の修正

[Issue111](https://github.com/ken2129/c3_neural_compression/issues/111)で、新しい管理・実験コンテナ経路を1回だけ実行した。
実GitHubのIssueイベント、Actions、正式承認、一度だけclaimまでは進み、実験側workerは
`AppServerBlocked` で失敗した。**GPUの数値結果・結果PRは得られていない。**
管理は `BLOCKED` を保存してOFFへ戻し、claimをリセットしたり実験を再試行したりしていない。
その後、本人が同条件の再検証を明示承認したため、別の[Issue112](https://github.com/ken2129/c3_neural_compression/issues/112)で1回実行した。
Issue112も `AppServerBlocked` で停止した。各Issueは1回だけで、両方の失敗記録とclaimを保持している。

## 配置と依頼

| 対象 | 実際に用いたもの |
| --- | --- |
| 配置runtime | 公開commit `8dcbd9731eef44a6cf200201d4e119a513889a39`（PR #108、未merge） |
| 管理image | `sha256:4fb8b12a73f4b022b09859ae7fc58132fc80d19eba637dadf2c9ea4cea9efa92` |
| 実験image | `sha256:46977109e255f872e221d02d6a1a8b6a63c7a8ec74fd4b66fa2c66cf34d42bb8` |
| 実験先 | 登録名 `c3-work`。再利用用コンテナで、host bind・Docker socketなし |
| Codex / 設定モデル | 0.160.0 / `gpt-6.1-sol`。実モデル応答の成功を示すものではない |
| runner | 2.338.1、登録ID22、onlineを確認 |
| 上限 | 1件、GPU1枚、600秒、2CPU、4GiB、128PID。依存追加なし |
| 入力 / 出力 | main `6fd11016d64f88721eb08caff2bb8b0988f3d847` のLICENSE/README、`docs/research/generated/native-container-e2e/` |

目的はCodexが小さなJAX行列計算をGPUで実行して待機し、CPU基準値と照合し、同じセッションで
解析・結果PRまで進むことだった。C3の圧縮性能測定ではない。

## 外側で観測した流れ

1. 管理イメージと実験イメージをbuild。実験側のUID・Codex・JAX環境と、実管理brokerからの
   Docker照合・bootstrap・prepare/readyを確認した。ここまでは認証・model turnを渡していない。
2. 引き継いだ管理用・PR用GitHub認証がともにHTTP401で拒否された。本人がGitHub device loginを
   完了した後、本人ID・repository・レビュー済みworkflowを照合し、専用の私有設定を更新した。
3. GitHub runnerを旧登録と置換せず新規登録し、onlineと管理READY/OFFを確認した。
   受付の初期設定を有効にするためのrestartで一時的なrunner session競合が生じたが、その後解消した。
4. Issue作成時の最初の配送は受付OFFにより失敗した。
   [最初の配送run](https://github.com/ken2129/c3_neural_compression/actions/runs/43688108941)の失敗jobを再送した。
   その時点で管理実行台帳0件・実験側claim0件・認証未配置を確認しており、GPU/モデル実験の再試行ではない。
5. [本文編集run](https://github.com/ken2129/c3_neural_compression/actions/runs/43688458470)と
   [承認コメントrun](https://github.com/ken2129/c3_neural_compression/actions/runs/43688528670)を含む実配送が届いた。
   管理は[未編集の正式記録](https://github.com/ken2129/c3_neural_compression/issues/111#issuecomment-6890852726)と本文hashを照合した。
6. 実行を一時ONにし、Issue111の `CLAIMED` を確認した後にOFFへ戻した。
   ONから約10秒の外側観測で `BLOCKED` へ遷移した。記録された実行はこの1件だけ。
7. 実験側の私有記録は `worker_failure / EXECUTION_FAILED / AppServerBlocked`。
   `cleanup_verified: true`、workerの排他lock解放、一時worker/model認証homeの削除を確認した。
   管理側もECHILD・一時認証回収の確認を記録し、既存コンテナ・他者プロセスを停止していない。

Actionsの成功は通知の配送であり、GPU実験やPR作成の成功ではない。
OFFで待機する後続の同Issue通知は、追加の研究実行として扱わない。

## 失敗の診断と限界

配置版は例外classだけを私有記録へ残し、app-serverの固定理由・段階を保存していなかった。
したがって、**実turnがどこまで進んだか、今回の例外の唯一の原因が何かは、この失敗記録から断定できない。**

認証なしの別診断では、同じCodexのinitialize/thread-startを確認し、`turn/start` を必ず拒否して終了した。
この診断にモデル呼出し・GPU計算はない。保存済みChatGPT認証は10月23日更新、access tokenは11月2日で
期限切れだった。モデルを呼ばないaccount-readによる更新確認でも有効なaccountは得られなかった。
これは認証を更新する必要がある根拠だが、今回の通信例外の唯一の原因を確定したものではない。

その後、本人がChatGPT device loginを完了し、同じ記録上のaccount identityを照合して専用認証を更新した。
実験コンテナ内のCodexでも、モデルを呼ばないaccount-readでaccountを認識できることを確認した。
一時的な認証コピーと診断プロセスは回収済みである。

新しい修正は既存の安全な診断exporterを使い、固定理由・protocol段階・elapsedだけを
実験側の私有記録へ保存する。raw protocol、stderr、例外本文、認証は保存せず、配送結果・公開用結果にも
私有診断を追加しない。この診断修正は、後述の `d1c30c40` として稼働imageへ配置した。

## 再認証後の1回と、モデルを使わない原因再現

Issue112は本人が改めて承認したGPU1枚・600秒の検証で、配置コードは同じ `8dcbd973`。
[owner記録](https://github.com/ken2129/c3_neural_compression/issues/112#issuecomment-6891258474)と
[実配送run](https://github.com/ken2129/c3_neural_compression/actions/runs/43691279312)を経て1回だけclaimした。
外側ではONから約12秒で `BLOCKED` を観測し、OFFへ戻した。GPUの数値結果・結果PRは得られなかった。
再認証だけでは解消しないことが分かった。両方のworkerで回収・認証home削除・排他lock解放を確認した。

次に、モデルも認証も使わず、`turn/start` を必ず拒否する診断へ実workerと同じ容量走査を加えた。
Codex0.160.0は起動時に私有homeへ4つの正常なsymlinkを作り、旧 `tree_bytes` がこれを
`RESOURCE_LIMIT` として拒否することを再現した。通信側の固定診断は
`APP_SERVER_RECHECK_FAILED / initialize_wait`。容量走査なしの同じ初期化は通っていた。

修正では、私有runtime homeだけはリンク自身の `lstat` サイズを数え、リンク先をたどらない。
研究workspaceのリンクとruntime内の特殊ファイルは従来どおり拒否する。
修正関数を診断プロセス内だけに読み込んだ同じ実Codex初期化・thread-startは通過した。
稼働ファイルやclaimは書き換えず、model turn・GPU計算も行っていない。
これは起動を妨げる実装バグの再現・修正であり、実モデル・GPU経路の完走証明ではない。
容量監視の修正も、後述の `d1c30c40` として稼働imageへ配置した。

## 修正のCPU回帰試験

- Linuxの使い捨て1CPU/1GiB/64PIDコンテナ。GPU・ネットワーク・Docker socket・host bindなし。
- 全1,198件を通常・`-O`で確認し、各1,189成功・9skip・失敗なし。
- skipのうち7件は、UID切替・自分の子孫回収用capabilityを持つ別のCPUコンテナで通常・`-O`とも成功。
  残る2件は既存の非特権fork・DAC観測の環境条件。
- 追加した試験は、不正なmodel-free応答とstderrに置いたdummy private markerが私有診断にも配送結果にも
  含まれず、固定の通信失敗codeだけが私有記録に残り、子孫・認証が回収されることを確認する。
- fixtureも実Codexと同じarg0リンクを作る。リンク先のprivate/large/directoryをたどらず、
  切れたリンクもリンク自身だけを数え、workspaceリンク・FIFOを引き続き拒否する試験を追加した。

初回の停止後は実行OFFへ戻した。ChatGPT再認証後の再検証は別の実行記録で行い、初回と混ぜない。
初回claimを消して再実行したり、この記録を完走成功・結果PRの代わりに扱ったりしない。

## 修正版への更新

2026-11-15に、PR108の公開runtime `d1c30c40a3e8b069ade70495d55a0edf00a70fa0` を
管理image `sha256:041709a6069f9a6cc8727eb8e8131e45b0a8afa03ec47bf8c9ea4da897e5aa46` として配置した。
管理コンテナは `8d5af9d78c76…` に置き換わったが、同じ専用control/runner volumeを使用し、
認証・runner登録ID22・Issue111/112のBLOCKED記録とclaimを保持した。
停止前後のSQLite・claimはbyte一致を確認し、実行OFF・manager READYで起動した。
新しい管理IDに合わせた配置manifestと初期化済みmarkerの更新は、信頼する運用操作として行った。
台帳の初期化・claimのリセット・古いIssueの再実行は行っていない。
旧imageも撤回用に保持し、ホスト常駐ファイル・他者コンテナは変更していない。

本人が同条件の1回を改めて承認したため、新しい[Issue113](https://github.com/ken2129/c3_neural_compression/issues/113)を準備した。
[未編集owner記録](https://github.com/ken2129/c3_neural_compression/issues/113#issuecomment-6891684033)は
新しい依頼のhashと期限に結び付けた。

## Issue113: 実GPUから結果PRまで

修正後のruntime `d1c30c40` で1回だけ実行し、[結果PR114](https://github.com/ken2129/c3_neural_compression/pull/114)を得た。
管理コンテナ更新後のrunner session競合は約2分で解消したが、Actions配送はその後もしばらくqueuedだった。
最初の配送待ち監視は、OFF・台帳2件だけ・実験側claim2件だけ・認証homeなしのまま終了した。
状態コメントを追加した後、既存の[openedイベントの実配送](https://github.com/ken2129/c3_neural_compression/actions/runs/43694502241)を受け取った。
通知の配送待ちは実験やモデルの再試行ではなく、Issue113のclaim・GPU計算は各1回だけである。

claim後は直ちにOFFへ戻し、実行中の仕事が続行することも確認した。
実験側の私有完了記録は同一thread/turnの組を1つ記録し、モデル `gpt-6.1-sol`、
`completed`、経過185.38秒、PR114を返した。管理台帳は `WORKER_FINISHED`。
回収は `cleanup_verified: true`、ECHILD、排他lock解放、一時worker/model認証homeの削除を確認した。
管理・実験コンテナと無関係なコンテナは継続しており、実行はOFFである。

公開物は承認scope内の `check.py`、`result.json`、`report.md` の3件だけ。
PRの親は承認されたmain `6fd11016d64f88721eb08caff2bb8b0988f3d847`、
headは `69141419e507e740c055673e8eef29f74339f61e`。通常PRで、mergeしていない。
外側からGitHubの差分・完全tree・blob SHA/modeを照合し、公開blob自体も読んだ。
スクリプトの構文、GPU限定・同期・NumPy参照比較の実装と、JSONの数値条件・レポート整合を確認した。
外側でGPU実験を再実行したものではない。

| 観測 | 値 |
| --- | --- |
| GPU | GTX1080 1枚、backend `gpu`、出力もGPU配置 |
| 計算 | seed113、float32の32×32行列積、NumPy float64参照 |
| 最大絶対誤差 / 許容値 | `5.270694112269325e-07` / `1e-4` |
| 計算時間 | `0.5754531295970082` 秒。初回compileから同期まで。転送とCPU参照を除く |
| 全体 | 185.38秒。Issue配送の待ち時間は含まない |

この結果は小規模な実行経路の確認であり、C3学習・圧縮性能・長時間運用・他の形状やマシンは未検証。
初回と再認証後の失敗、runner再接続・配送待ち、人手による認証とON/OFFはこの記録に保持する。

## 結果PRの公開後照合で見つかった改行差

実験側CodexはPR公開後のヘルパー照合が失敗したことを申告し、既存PRを読み取りだけで独立確認してから完了した。
外側でもGitHub Git database APIのcommit本文が、送信した本文の末尾LF1文字だけを除いて返ることを確認した。
PR本文・親commit・3ファイルのblob/modeには差がなかった。
したがって、**結果PRまで到達したが、公開ヘルパーの無介入での最終照合はこの実行では通っていない。**

修正はcommit本文の完全一致に加え、末尾LF1文字だけが除かれた応答も受け入れる。
PR本文、他の文字、tree、親、actor等の検査は維持し、余計なLF・空白・CR・本文変更は拒否する。
既存intentからの再呼出しは読み取りだけで、失敗時にPRやbranchを作り直さない。
fixtureも実GitHubと同じ末尾LFの省略を既定にした。
全1,200件を通常・`-O`で確認し、各1,191成功・9skip・失敗なし（23.179秒 / 28.154秒）。
skipの7件は直前の起動監視修正版で実UID用capabilityのあるCPUコンテナでも成功しており、残る2件は既存環境条件。
この追加修正の配置と、既存PR114に対する読み取り照合は別途記録する。追加GPU実験は行わない。
