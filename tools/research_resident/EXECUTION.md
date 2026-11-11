# Issueで指定した実験コンテナ内でCodexを動かす

この経路は **leoなどLinuxの1台のDocker Engine** を対象にする。管理用コンテナが
Issue受付・承認・ON/OFF・実行台帳・コンテナの準備と監督を担当し、指定された
実験用コンテナでCodexが通常のシェル・ファイル操作を使って研究する。
GPU実験を先に固定レシピで実行してから解析用Codexを起動する旧案は置き換えた。

**コードは未配置。実Codex・実GPU・実GitHubを通す新経路のE2Eは未実施。**
以前のCPU E2EとPR #109の手動GPU pilotは、新経路の成功証明ではない。
旧controllerは停止・OFFを維持する。

```text
同じマシン
  管理用コンテナ
    Actions受付 → 承認・ON/OFF → 登録先照合 → 一度だけclaim
        |                                    |
        | 固定したローカルDocker操作          | 状態・結果・回収確認
        v                                    ^
  Issueで指定した実験用コンテナ（再利用可能）
    Codex起動 → 調査・修正 → 実験を実行して待機 → 解析 → PR作成
```

実験用の別runnerコンテナ、マシン間通信、SSH鍵、TLSサーバー、独自の実験操作MCPは
追加しない。GitHub Actions runnerは従来どおり管理用コンテナ内の受付プロセスである。
Codexを呼ぶのは実験側の一つのapp-server/thread/turnだけで、実験待ちを理由に
解析用Codexを再起動しない。modelと共通プロンプトは従来の共通runtime設定・コードから
解決して渡す。実験側の手編集用モデル設定ファイルは増やさない。

## Issueと実行先

既存の正式な `codex-direct-pr-v1` planに、次のフィールドを追加する。

```json
{"execution_container": "c3-work"}
```

これは登録済みの参照名である。Issueから任意のDocker ID、Docker引数、ソケット、
ホストパス、接続先、権限設定を受け取らない。owner承認はコンテナの指定を含む
Issue内容と、解決後のモデル・資源予算を拘束する。実行先変更には再承認が必要。
タイトルだけでは起動せず、従来の承認・期限・取消・再配送の検査を維持する。

`execution_container` がないCPU依頼は従来の管理コンテナ内CPU workerを使う。
旧 `gpu_experiment` / `gpu_targets` / SSHレシピ設定は受け付けない。

## 設定は同じruntimeファイルに置く

保護された `/control/goal-runtime.json` に `execution_containers` と
`docker_control_approved: true` を追加する。以下は未設定値を含む説明用断片で、
実行承認やそのまま使える配置設定ではない。

```json
{
  "docker_control_approved": true,
  "execution_containers": {
    "c3-work": {
      "name": "uchishiba-c3-work",
      "container_id": "OPERATOR_VERIFIED_FULL_CONTAINER_ID",
      "create_image": null,
      "python": "/usr/local/bin/python",
      "codex": "/opt/codex/bin/codex",
      "worker_uid": 10002,
      "workspace": "/research/c3",
      "cpu_cores": 2,
      "resource_limits": {
        "max_seconds": 1800,
        "cpu_seconds_per_process": 1200,
        "memory_bytes": 8589934592,
        "max_processes": 128,
        "max_work_bytes": 1073741824,
        "max_output_bytes": 1048576
      },
      "gpu_uuids": ["OPERATOR_APPROVED_GPU_UUID"],
      "max_wait_seconds": 300,
      "disk_reserve_bytes": 10737418240,
      "sampled_limits_accepted": true,
      "environment": {"JAX_PLATFORMS": "cuda,cpu"}
    }
  }
}
```

- 既存コンテナでは名前と64桁の実IDを両方照合する。同名の別コンテナへ自動追従しない。
- 新規作成では `container_id: null` と、Docker Engineに準備済みの
  `create_image: "sha256:<immutable-image-id>"` を登録する。既存の同名コンテナは、
  導入ID・参照名の管理ラベル・image IDが一致するものだけ再利用する。
- 新規作成時のCPU・RAM・PID・capability・namespace設定はハーネスが組み立てる。
  新規コンテナのGPU割当は初期版では1枚まで。Issueから任意のDockerfile build、
  image pull、host bind、privileged、host PID/network、任意デバイスを要求できない。
- コンテナは毎仕事作り直さない。コンテナ内の排他ロックで同時実行を拒否し、
  現行の1件ずつの受付キューで後続仕事を待機させる。別管理系との自動調停はしない。
- 研究workspaceと既存成果物は維持する。承認された公開inputsは固定sourceのbytesと
  照合し、未配置ファイルだけを追加する。不一致を上書きして実行しない。

上位の `resource_limits` は従来の管理/CPU配置の割当を維持し、実験側の上限は
指定先の `resource_limits` から解決する。Issueの要求は登録上限以下でなければならない。
job全体の `max_seconds` はGPU待ち・同じCodexでの実験/解析/PR・回収を含む。
内部で20秒の回収余地を確保し、実験経路は最低30秒を要求する。

## 初回準備と権限

実験コンテナには、既存Linuxユーザー、system Python（pidfd対応）、Codex **0.160.0** と
研究の依存環境を準備しておく。Codex実行ファイルはroot所有・非書込可能であることを
調べ、別の一時homeでバージョン確認する。バージョン確認用homeとモデル認証用homeは
共有しない。Codexや科学ライブラリの自動インストール・アップグレードは行わない。
新規作成用imageも、この準備が済んだものを使う。
supervisorはsystem Pythonを`-I -S`で起動し、研究用venvのsite設定をrootで読み込まない。
コンテナ内supervisorにはUID切替・所有権設定・自分の子孫回収に必要なcapabilityが必要で、
CodexへUIDを落とした後は追加groupを持たせず、昇格を禁止する。

管理側はroot所有の読み取り専用runtimeから、公開のsupervisorコードと固定inputsだけを
実験コンテナの `/opt/c3-execution/<runtime-sha>-<input-manifest-digest>/` へ配置する。versionごとに配置し、
途中のinstallや不一致を勝手に上書きしない。モデル認証・manager認証をimageやこの
公開bootstrapへ含めない。

管理側のroot brokerだけが `/run/c3-docker.sock` を使用する。Codexは実験側の
非root UID・空の追加group・no-new-privilegesで起動し、Dockerソケット、管理volume、
owner承認用認証を受け取らない。登録先にhost bind、Docker socket、host namespace、
危険な追加capabilityがあれば拒否する。Codexが指定外コンテナを選ぶAPIも設けない。

この構成は**管理用コンテナへホストDockerの強い操作権限を渡す変更**を含む。
配置時は `--approve-local-docker-control` とprofileの明示フラグを両方必要とする。
`compose.execution.yaml` のsocket mountはその場合だけ追加する。ホストにファイルや
常駐プログラムを配置しないが、Docker権限の追加まで「影響なし」とは扱わない。
コードをmergeしただけではsocketをmountせず、イメージ作成・認証移送・起動もしない。

同じコンテナでCodexのshell/file toolsを使うため、各実験コマンドをallowlistへ
一つずつ登録しない。科学コードは目的・scope・予算内で修正して実行できる。
その代わり、再利用先は信頼された研究環境として準備する。同一UIDが読める既存の
SSH鍵・Webhook等を自動で隠す仕組みではない。研究専用UIDと、必要なデータだけを
読めるworkspaceを用意し、コンテナ内に管理権限を残さない。
`write_scope` と公開helperの検査は、再利用コンテナ内の任意コードを隔離する
ファイルシステムsandboxの証明ではない。

## 実行・回収・認証

1. 管理側が現行Issueとowner承認、参照名、資源、公開scopeを検証する。
2. 固定したローカルDocker操作で指定先を照合・準備し、実験側supervisorの準備を確認する。
   この段階ではモデル認証を渡さず、Codex本番turnも開始しない。
3. 管理側が永続台帳で一度だけclaimしてから、実行bindingと必要なworker認証を渡す。
   実験側にも排他的なclaimを保存する。未確定claimがあるコンテナは次の仕事を拒否する。
4. 許可GPUの空きを3回観測してローカルlock後に再確認し、1枚を選択する。
   GPUなしの登録先も使える。GPU競合を検出したら自分の仕事を停止する。
5. 実験側でCodexを1回起動し、通常のツールで調査・修正・実験・待機・解析・PR作成する。
   管理側は承認を再確認し、短いleaseを更新する。取消・期限・接続断で停止する。
   実行開始後のOFFは次の仕事を止め、現在の仕事は既存の承認検査を続けて完了させる。
6. Linux subreaper、pidfd、ECHILDで、そのjobの子孫だけの停止を確認する。
   既存コンテナのPID1や別の研究・通知プロセスは停止しない。
7. workerの認証コピーを削除し、私有の実行記録とCodex thread/turnを保存して結果を返す。
   Codexが認証を更新した場合は、同じaccountの更新だけを私有channelで管理側へ戻し、
   原本の競合を確認して保存する。認証更新frameをPR報告や結果台帳へ流さない。
   不明な回収・未保存の認証更新では次の仕事を始めず、私有情報を残してblockする。

実験側に渡す認証はモデル用と明示承認されたPR用だけで、actorを毎回照合する。
現在のようにownerのPR認証を使う選択では、権限によっては承認コメントの作成能力まで
残る。`owner_identity_risk_accepted` を残し、これを承認権限の完全分離と呼ばない。

新規コンテナのCPU/RAM/PIDはDockerの上限でも制限する。再利用先では既存の割当を
無断変更せず、RSS・thread数・workspace増分・一時home・最低空き・時間を監視する。
`sampled_limits_accepted: true` が必須で、hard limitと同一視しない。既存コンテナの
GPU露出を `CUDA_VISIBLE_DEVICES` だけで安全に縮小できるとは主張しない。
初期の競合検査は単GPU・単compute-process向けで、DDP等は別の契約が必要である。

## 検証の範囲

契約・登録先/権限検査・正式承認fixtureと実SQLite claim・通常pipeでの配送を試験する。
モデル応答のfixtureでも、実際に別UIDのプロセスがファイルを修正し、CPUコマンドを
失敗→修正→再実行して同じthreadで待機する。再利用、認証の私有返却、EOF/取消、
escaped childの回収と無関係なプロセスの生存も確認する。

2026-11-11のLinux隔離CPU試験では、全1,195件を通常実行と `-O` の両方で
実行し、各1,187成功・8skip・失敗なし。skipのうち6件はUID切替・子孫回収用の
capabilityを持つ別の使い捨てCPUコンテナで実行し、通常・`-O` とも6件成功した。
残る2件は既存の非特権fork制限・DAC拒否観測の環境条件によるskipである。
試験用コンテナにGPU、ネットワーク、Docker socket、host bindは渡していない。

Docker操作はargv/inspectのfixture、モデル・GPU・GitHub公開もfixtureである。
新管理イメージbuild、実Dockerでの配置、実Codex、実GPU、実結果PRはまだ検証していない。
これらを1件の小さな実E2Eで確認してから、運用可能という判断をする。
