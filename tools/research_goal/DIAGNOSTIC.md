# 承認前の診断設計とモデルなし検証

これは診断コードの仕様です。通常の `prepare.py` / Compose / Dockerfile は変更せず、
診断 image の build・container 起動・認証移送・モデル呼出しはこの文書だけでは許可されません。
Issue83 の本番障害を再現できたとは扱いません。

## 追加観測と実行承認の分離

修正前の診断は `DiagnosticController._approval_snapshot()` で ShadowRead を先に呼び、
その例外で合成承認の確認を実施せずに実行を止めていました。本番にはこの追加 callback はありません。
観測内の API 例外だけでなくローカルの日時検証失敗も、同じ contract 記録になります。

修正版は次の順序です。

1. 管理 UID の起動・新規台帳初期化を共通 launcher で確認する。
2. `observe_once(ShadowRead().observe)` を独立した `approval_read_budget(5)` 内で1回だけ呼ぶ。
   通常の例外・予算切れは `DIAGNOSTIC_OBSERVATION_FAILED` の固定 reason として記録する。
   秘密、例外本文、応答本文は記録しない。ログ出力失敗も承認判定に使わない。
3. 観測の予算 context を解放してから新しい合成受付・合成承認を作成する。
4. 本番と同じ `GoalController` を直接使う。`_approval_snapshot` を上書きしない。
   以後の受付・再確認・IPC・BLOCKED・保存経路には ShadowRead が一切入らない。

単に callback 内で例外を捕まえる方式では、観測が共有5秒予算を消費して正当な承認を
期限超過にするため不十分です。観測はモデル実行中に周期実行しません。
観測が報告する過去 Issue83 の expiry は実行承認ではありません。
プロセス制御シグナル、PID1 hard watchdog、UID/reserve 検査、cleanup 不明時の拒否は維持します。

## 診断固有の差分

| 項目 | 診断 | 通常サービス |
| --- | --- | --- |
| entrypoint / purpose | `diagnostic_driver.py` / `dedicated-goal-diagnostic` | `goal_launcher.py` / `dedicated-goal-controller` |
| 再起動 | `restart=no`、1回のみ | `unless-stopped`、世代変更後の照合あり |
| 受付・承認の入力 | 明示した合成 Issue 999999999 と合成 owner receipt | ManagerGitHub / GitHubOwnerComments の実データ |
| 実行対象 | 公開 fixture を読み、専用 scope の小さい JSON 1件を作る診断 | installed plan に固定した研究 goal |
| 追加 GitHub 観測 | 管理起動時に1回のみ、承認 callback 外 | なし |
| モデルと時間 | gpt-6.1-sol / 0.160.0、wall 上限180秒、通信は20秒前終了、hard はwall+4秒 | 承認済み protected profile / plan |
| protocol ログ | 共通 transport の subclass で固定 RPC 名と回数のみ記録。重複起動 RPC を拒否 | 共通 transport のまま |
| 公開元 base / 公開 | fixture の固定 source、公開 factory は拒否 | 実 main の読取、別内容承認後に公開 |
| 保存後 | `RESULT_AWAITING_RESTART` の private 保存で停止。after_epoch はなし | restart 照合後に内容確認待ちへ進む |
| startup 異常 | 固定診断を出して終了。起動全体も230秒で停止 | protected 設定異常では固定診断で待機 |

診断 deployment validator は本番 validator から entrypoint・purpose・restart の期待値だけを変えたものです。
実測 inspect は書き換えません。差分の AST 一致と、不正な権限・mount 等の拒否をテストします。
通常 installer / Dockerfile は診断 entrypoint を選択しません。専用配置には別の明示承認が必要です。

## 共通経路と今回の検証結果

- 承認: 同じ `GoalController.process/_approval_snapshot`、`inspect_once`、frozen receipt と plan の照合、
  `_RunApprovalRecheck` を使用する。診断用の承認拒否条件への置換はない。
- IPC: 同じ `GoalPipeRuntime` と frame 検証。承認拒否は負の reply になり、管理台帳は BLOCKED。
- 停止: 同じ `require_recheck_reply` → transport の `_checked_recheck` → `GoalUIDSupervisor`。
  モデルなしの process fixture に対して停止・cleanup・固定診断を確認する。
- 保存: 同じ launcher の `freeze_snapshot` / result finalization と `ServiceState.save_result` を使用する。
  新テストでは共通 result readback と SQLite 保存・再openを検証し、正常 wire 応答は実 pipe で処理する。
  quarantine の atomic freeze/readback と保存失敗 ACK は既存回帰テストで検証する。

モデルなしで、観測の contract 例外・観測自身の5秒予算切れ・観測ログ出力失敗の後も、
正当な合成承認が検証され RESULT_AWAITING_RESTART に保存されることを確認します。
観測が成功しても、承認無効・取消・期限切れ・確認不能は受付前に拒否し、
claim 後なら負の IPC と BLOCKED で停止して二重実行しないことを確認します。
模擬の epoch 更新後は共通経路で AWAITING_CONTENT_REVIEW になり、公開しません。

これらのテストでの UID/process/deployment と app-server 応答、成果物 bytes、epoch は fixture です。
台帳・frame 検証・bounded stdio parser・private result readback は実コードで処理します。
観測が実行可否を上書きしないことと、共通経路のコード上の接続は確認できます。
実物 Codex の tool 実行・成果物生成・quarantine 保存、実 API とモデルの同時負荷、
本番 owner intake、実 restart 境界、Issue83 の過去原因を実証したとは扱いません。

## 次に必要な試験案（未承認・未実施）

この修正を含む固定 commit で、専用 image/container/volume を各1つだけ新規に作る。
専用 GitHub / ChatGPT 認証のみを同一ホスト内で新管理領域へ設定し、UID10001:10001・0600、親0700を検証する。
旧サービス・旧診断・Issue83・既存台帳は変更・流用しない。認証を host archive やログへ出さない。

1CPU / 1GiB / 64PID、work256MiB / output1MiB、readonly root、SETUID/SETGIDのみ、NNP、
bridge / docker-default、restart=no を維持する。外向き通信は遮断されず、worker は自身のモデル認証を読める。
金額の hard cap はない。GPU/socket/広い HOME mount、権限追加は使わない。

管理 UID10001 → worker UID10002 → 実物 Codex 0.160.0 / gpt-6.1-sol の起動1回・turn/start1回だけを実施する。
model通信160秒 / runtime180秒 / runtime hard184秒、buildから全体10分を上限にする。
追加観測は開始前の独立した1回のみ。合成承認は実 GitHub の承認と明確に区別する。
公開 dummy fixture の読取と `diagnostic-output/connection.json` 1件の生成、quarantine/result の
正確な内容・digest・SQLite 保存、worker0 を確認する。計算研究の成功とは呼ばない。

成功判定は実物の成果物保存までとし、after_epoch / 公開 / 本番障害再現は対象外。
失敗や成否不明なら2回目のモデル呼出しをしない。新 container を停止したまま証拠の専用 volume/image とともに保持する。
新 Issue・コメント・PR 作成、本番更新、merge はしない。モデルなし検証の通過だけではこの試験の成功を保証しない。

## PR71 の関連レビュー対応

- cb5fbd5: goal transport に埋もれた再確認の固定 reason/origin を管理ログまで伝える。
- 3f8a1cd: `GOAL_FINALIZATION_FAILED/persist` の ACK を許容し、cleanup 開始後の child exit code を原因として記録しない。
  API の CLI/status/errno/PID counter も固定分類で記録する。
- 今回: ShadowRead の診断固有 veto と共有承認予算への干渉を除き、診断コードとその差分をレビュー対象へ収録した。

追加の「copy_records へ -a を足す」指摘については変更していない。
当該ホストの Docker Client/Server は28.3.2で、通常サービスと旧診断の `Config.User=0:0`、
変更されていない goal-runtime.json は両方とも10001:10001・0600の通常ファイルと読み取り確認した。
stdin tar の転送と、readback の UID/GID/mode/bytes 検証を維持する。
Docker 28.3.2 の [extract 実装](https://github.com/moby/moby/blob/v28.3.2/daemon/archive_unix.go#L124-L135) は
copyUIDGID 時に [Config.User による ChownOpts](https://github.com/moby/moby/blob/v28.3.2/daemon/archive_tarcopyoptions_unix.go#L16-L28) を選ぶ。
数値 UID/GID を入れた stdin tar とローカルファイルコピーを混同して -a を追加しない。
このホストの実測を他の Docker 版や非root Config.User の一般保証とは扱わず、各配置の readback が不一致なら停止する。
