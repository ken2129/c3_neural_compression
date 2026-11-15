# 実験コンテナ経路のE2Eと起動監視の修正（未完走）

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
私有診断を追加しない。**この診断修正はまだ稼働imageへ配置していない。**

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
**容量監視の修正も、まだ稼働imageへ配置していない。**

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
