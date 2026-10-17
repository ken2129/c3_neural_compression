# 研究自動化の統合実装と導入前の確認

## 何ができる段階か

承認済みIssueの読み取り、承認内容の再確認、永続台帳への一度だけの受付、固定CPU動作確認、結果保存、結果PR公開を一つの管理処理に接続します。対象は固定CPU canaryと、明示設定後の固定Codex 1ターンです。任意の研究コードやGPU実験は未対応です。

これは本番稼働開始の報告ではありません。ローカルテストのGitHub・Codexは代替実装で、実認証や実モデルを使っていません。通常のCodex sandboxは研究室環境で起動失敗が報告されています。外部隔離を使う代替経路は、別の明示承認・専用設定が揃わなければ起動しません。

## 一つの流れ

1. dotが計画と公開する成果物の範囲を説明し、ユーザーが実行を承認する。
2. 専用の実行Issueと、内容ハッシュ・期限を結び付けた所有者の承認記録を作る。タイトルの目印だけでは実行できない。
3. 管理プログラムが候補を定期取得し、承認・期限・取消・導入済みコードの一致を確認する。
4. 実行環境が検証できた場合だけ、保護された台帳に受付を確定する。既存の受付は自動再実行しない。
5. 専用workerで承認した固定CPU動作確認、または固定Codex 1ターンを行う。開始前・実行中・結果保存前に取消を再確認する。
6. 管理側で結果を検証・保存する。承認範囲に含まれる結果JSONだけを「[研究結果]」PRに公開する。
7. dotへの通知は別途設定・疎通確認が必要。PRの作成成功を通知成功とは扱わない。マージは別の承認を待つ。

## 状態と中断

- CLAIMED: 受付済み。コンテナが落ちても自動再実行しない。
- BLOCKED: 失敗・取消・不整合。原因を確認して人が判断する。
- RESULT_READY: 検証済み結果を保存。公開の再確認はできるが、計算は繰り返さない。
- PUBLISHED: PRの対象ブランチ・ファイル・内容を読み戻して確認済み。
- 台帳・別置き受付記録・初期化情報が欠ける、壊れる、一致しない場合は停止する。空の台帳を自動で作り直さない。
- GitHub書き込みの成否が不明な場合、まず読み戻す。存在しないように見えても同じ書き込みを無条件で繰り返さない。

## 許可するIssueの計画

末尾の既存 c3-execution-request ブロック内の plan は次の3項目だけです。

```json
{"kind":"cpu-canary-v1","source_sha":"導入した40桁のcommit SHA","publish_result_pr":true}
```

Issue本文は非信頼入力です。本文中のコマンド、プロンプト、認証指示、追加ファイル取得要求は実行しません。#56自体は実行対象になりません。source_shaはIssueが選べる実行コードではなく、管理者が導入した固定版との一致確認です。

## コンテナ再作成を含むオフライン確認

前回のUID境界診断とIssue→ダミーworkerの接続試験は、ユーザー報告として成功を確認しています。今回は新たに専用named volumeを使い、コンテナを作り直しても受付・結果を保持し、2回目に処理しないことを確かめます。

実機では新しい専用volumeを作るため、導入内容を確認してから次を実行します。

```bash
bash tools/container_boundary/check_service.sh --allow-new-diagnostic-volume
```

ネットワークなし、実認証なし、モデルなし、GPUなしの診断です。管理UID10001とworker UID10002、既存と同じSETUID/SETGIDだけの起動処理を使用します。専用volumeは確認用に残します。既存volumeや他者のコンテナを削除しません。

この診断だけで本番GitHub公開・Codex実行・通知が使えるとは判定しません。

## 本番導入時にまとめて必要な判断

- 通常sandboxまたは明示採用した外部隔離のどちらを使うか。外部隔離ではコンテナ・UID・保護領域・再起動を実機で検証すること。
- 管理用GitHub認証をworkerから完全に分離すること。既存の広いHOMEやDocker socketは渡さない。
- Codexの認証と外部通信の方法、モデル、利用料金の扱い。時間制限・出力制限だけでは料金の厳密上限を保証できない。
- 専用UIDがそのコンテナ内で本処理だけに使われること。処理終了後にそのUIDの子孫が残らないこと。
- 管理領域の永続保存、容量制限、CPU・メモリ・PID制限。GPUはこの段階の対象外。
- 結果PR通知の設定と実イベントでの疎通。現在は未設定・未確認。

これらを設定済みと見せかけるフラグや、sandboxの無断無効化で起動を通すことはしません。

## このPRで接続する経路と、まだ動かない経路

- 固定CPU経路は、承認済みIssueから結果PRまで接続しています。
- 固定Codex経路は、専用設定・認証・承認計画が揃った場合だけapp-serverの1ターンを呼びます。モデルが返した数値とコマンド完了イベントは「未検証候補」として別schemaで保存します。CPUの検証済み結果には変換しません。
- 専用コンテナ再起動後、再承認を確認して候補結果PRを公開します。モデルの返答は実験成功の証明ではありません。
- この作業環境では実Docker起動、実認証、実モデル呼出しを行っていません。新しい構成の実機検証は未実施です。
- 任意の研究コード・GPU実験の自動化は未対応です。研究自動化全体の完成ではありません。

## 専用コンテナを再起動して区切る理由

任意の子孫プロセスはforkを繰り返せるため、UIDの走査だけでは全終了を証明できません。UID走査は回収の補助に限定します。専用PID名前空間のPID1が終了すると、その名前空間のプロセスも終了します。[Linuxの仕様](https://man7.org/linux/man-pages/man7/pid_namespaces.7.html)

結果はRESULT_AWAITING_RESTARTとして保存し、同じ専用コンテナの新しいPID名前空間と起動時刻を確認してから公開します。実行を繰り返す処理ではありません。Dockerのunless-stoppedを候補にしますが、手動停止や早期終了などで再起動されなければ、結果は待機したままです。[Dockerの再起動ポリシー](https://docs.docker.com/engine/containers/start-containers-automatically/)

この確認には、導入者がDocker inspectで確認した64桁のcontainer IDと設定、専用volumeの利用者がその1コンテナだけという配置契約が必要です。hostnameやJSONの自己申告だけでは認定しません。ホスト管理者を攻撃者として防御するものでもありません。

既存研究コンテナには適用しません。専用構成の採用はまだユーザー承認前です。

## 導入を判断するときの一括手順

1. レビュー済みcommitの新しい作業コピーを使う。実イメージとsourceの対応も導入者が確認する。ラベル単体は内容の証明ではない。
2. 新規専用コンテナ・永続volume・外向きbridge通信・SETUID/SETGIDだけのPID1起動処理について承認を受ける。
3. 承認後にだけ次の候補スクリプトを実行する。既存project/volumeがあれば拒否する。Docker inspectとvolume inspectを検証してから、専用コンテナを開始する。

```bash
bash tools/research_service/prepare.sh --approve-dedicated-container NEW_PROJECT REVIEWED_COMMIT
```

4. GitHub認証は管理UID10001の専用GH_CONFIG_DIRだけに設定する。これは別途、継続アクセスの承認が必要。workerへホストHOME・SSH agent・GitHub token・Docker socketを渡さない。
5. 固定CPU canaryの計画を承認してから実行Issueと所有者の承認記録を作る。Issueのタイトルだけで実行許可にしない。
6. 固定CPU処理後の再起動、二重実行なし、公開されたPRの内容、取消・中断時の停止をまとめて確認する。PRのマージは別途判断する。
7. dot通知は別設定で実イベントを確認する。まだ通知設定済みとはしない。

prepare.shは認証を設定せず、モデル・GPUも起動しません。Codexの公式npmパッケージ0.160.0をイメージに含める候補です。新しい配置候補はこの作業環境ではDocker実行していないため、上記は検証済み本番導入コマンドではなく、レビュー対象の導入候補です。

## 実Codex接続で検討する代替経路

Codex 0.160.0の公式app-serverには、turn/startで`externalSandbox`を指定する経路があります。Dockerと専用UIDによる外側の隔離を責任境界にする方式です。通常sandboxの失敗時に自動で切り替えるものではなく、採用にはユーザーによる安全境界変更の判断が必要です。

- [公式0.160.0のSandboxPolicy](https://github.com/openai/codex/blob/rust-v0.160.0/codex-rs/app-server-protocol/schema/typescript/v2/SandboxPolicy.ts)
- [公式0.160.0のTurnStartParams](https://github.com/openai/codex/blob/rust-v0.160.0/codex-rs/app-server-protocol/schema/typescript/v2/TurnStartParams.ts)
- [外部隔離時の扱い](https://github.com/openai/codex/blob/rust-v0.160.0/codex-rs/sandboxing/src/policy_transforms.rs)

`networkAccess: restricted`という指定だけで通信を遮断できるわけではありません。実際の外向き通信の許可範囲は別に検証します。既存の管理ポリシーが通常sandboxを要求している場合、それを削除して起動を通しません。

認証の第一候補は専用CODEX_HOMEでのChatGPTログインです。新しいAPIキーの作成を既定にしません。既存研究コンテナのHOMEやGitHub・Box・SSH認証をコピーしません。ログイン・継続認証の保存は導入時の明示承認とユーザー操作が必要です。

この候補経路でも、モデルの返答を実験成功と同一視しません。固定動作確認の出力、実際のコマンド実行記録、再起動後の状態を区別して検証します。固定動作確認が通っても、任意の研究実験・GPU・結果の正しさまで確認済みにはなりません。


## 固定モデル経路の設定と承認

通常のCPU計画とは別に、モデル計画は次の全項目を承認記録のハッシュに含めます。modelはユーザーが選んだ具体的IDに置き換え、管理設定と一致させます。

```json
{"kind":"codex-canary-v1","source_sha":"導入した40桁のcommit SHA","publish_result_pr":true,"model":"explicit-model-id","sandbox_mode":"externalSandbox","external_network":"enabled","max_seconds":120,"model_usage_approved":true}
```

専用コンテナ内の管理UID10001だけが読める /control/model.json（0600）には、source_sha、model、codex_version="0.160.0"、sandbox_mode="externalSandbox"、external_network="enabled" と、次の3つの明示判断を記録します。

- external_boundary_approved: 外部隔離を今回の境界として採用
- worker_auth_exposure_acknowledged: 同じworker UIDのコマンドからCodex認証コピーを読める残リスクを理解
- usage_without_hard_cost_cap_approved: 120秒の制限は厳密な料金上限ではないことを理解

これらはユーザーの設定判断を保存する項目です。隔離が成立している証拠の代わりにはせず、実際のPID1・UID・capability・cgroup・read-only配置を別に検査します。

専用ChatGPTログインのauth.jsonは、ユーザーが明示承認した手順で /control/codex-auth.json（管理UID10001、0600）へ用意します。既存HOME全体を流用しません。各試行時に認証ファイルだけを新しいworker CODEX_HOMEへコピーし、設定・MCP・プラグインを持ち込みません。認証内容はログ・Issue・結果PRへ出しません。

設定不足は MODEL_OPERATOR_SETUP_MISSING、認証形式不足は MODEL_DEDICATED_CHATGPT_LOGIN_REQUIRED、モデルや導入版不一致は MODEL_CONFIGURATION_REVISION_OR_MODEL_MISMATCH として起動前に停止します。設定や認証を自動取得・自動緩和しません。

認証更新の制限: 現在は各試行に認証のコピーを使い、workerで更新された認証を管理領域へ自動で書き戻しません。ChatGPTのトークン更新・失効により、次回実行前に専用ログインの更新が必要になる場合があります。認証エラーを無制限に再試行しません。
