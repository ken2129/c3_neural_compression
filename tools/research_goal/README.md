# goal-only CPU 実行経路（旧・内容承認方式）

新しく準備する goal 実行では、[Codex direct PR 経路](DIRECT_PR.md)を推奨します。
既定 OFF の別 opt-in で、Codex が実装・CPU 実験・テスト・報告・下書き PR 作成を担当します。
専用の GitHub actor／認証と GitHub 側の保護設定を別途承認・準備する必要があり、実機実行は未検証です。
以下は PR #71 系の private 候補・再起動・内容承認・manager 公開を残す旧経路の手順です。
既存 deployment を自動変更しません。

この経路は canary の固定 Python コマンドを使用しません。Codex が承認済みの目的・完了条件・入力・書込範囲・予算を受け取り、複数のコマンド、実装、検証、修正を選びます。GPU、依存追加、外部サービスへの操作は目的範囲に含めません。公開は別の内容承認後に下書き PR で行います。

**現状: ローカルのモデルなしテストのみ。新 Docker image の build・実環境の起動・モデル実行は未確認です。** 既存 canary の実証をこの経路の実証と読み替えません。この手順を代理実行する場合は、環境変更・資格情報の永続設定を含む必要な本人承認を先に取得してください。

## 1. 本人が設定と認証の移送を確認する

レビューした新 runtime commit の clean checkout を用意します。旧 canary の container/volume/履歴は変更・流用しません。入力の commit は runtime commit と別に指定できます。

`python3 -B tools/research_goal/prepare.py --template > goal-profile.json` で、無効状態の設定を作成します。本人が以下を確認してから編集します。

- `deployment_id`: template が毎回新規生成する公開 32 桁 lowercase hex ID。`goal-plan.json` にも同じ値をコピーします。秘密の container ID や IP は plan に含めません。
- `enabled: true` は新しい自由 CPU コマンド経路の明示有効化です。
- `input_source_sha`: 承認する公開入力が存在する commit。
- `runtime_source_sha`: 実装をレビューした現在の checkout commit。
- `model`: 明示的な固定モデル ID。`-latest` は受け付けません。
- `resource_limits`: operator 上限。例は wall 900 秒、CPU **各プロセス** 600 秒、aggregate memory 1 GiB、64 PID、work 256 MiB、出力 1 MiB。aggregate CPU は 1 CPU quota です。モデル処理は wall 終了の 20 秒前まで、残りは終了処理・保護コピー用です。
- `risk_acknowledgements` の 5 項目すべてを理解した場合だけ `true` にします。
  - `arbitrary_cpu_commands`: 固定 canary を超える CPU コマンド選択
  - `worker_model_auth_readable`: worker が自身のモデル認証を読める
  - `unrestricted_bridge_egress`: bridge の外向き通信は制限されない
  - `writable_runtime_homes_and_ipc`: 承認された成果物 scope のほか、runtime home と private IPC に書き込める
  - `usage_without_hard_cost_cap`: 金額のハード上限はない

これらは残るリスクへの同意であり、プロンプトで防げることの証明ではありません。モデル認証が読めることと unrestricted egress の組合せを、一般的な悪意あるコードに安全な隔離だと考えないでください。

初版の実 supervisor が扱える aggregate 上限は 1 GiB・64 PID・256 MiB work です。初回 plan の aggregate 予算は作成する container の実割当と一致させます。wall/CPU は canary の 120 秒/10 秒に固定されません。より大きい割当には別の実装検証と本人承認が必要です。

本人が別途ログインして準備した専用の Codex `auth.json` と GitHub CLI `hosts.yml` を指定します。両方とも private regular file（mode 0600、hardlink なし）である必要があります。installer は ambient HOME、旧 container、既存 canary volume、環境変数から資格情報を探しません。指定した二つのファイルだけを新しい manager-owned persistent volume に移送します。資格情報を Issue/チャット/profile/plan に貼らないでください。

## 2. 同じ goal plan を用いて一度だけ準備・起動する

初版は準備した 1 研究 task 専用です。prepare が検証済み plan 全体の canonical digest を計算し、保護設定の `installed_plan_digest` に保存します。この値を入力 template に手書きすることはできません。goal・完了条件・入力・write_scope・予算を含め、異なる plan は claim 前および runtime で拒否します。別の目的には新しい本人承認と準備が必要です。承認した goal の範囲で Codex が複数のコマンドや実装方法を選ぶ自由は維持されます。同一 deployment で複数の承認 goal を継続受付する operator 範囲は未実装であり、goal ごとの container 準備を本番 loop の完成形とは位置付けません。

`goal-plan.json` は `codex-goal-v1` の plan オブジェクトそのものです。必須項目は `deployment_id`, `source_sha`, `publish_result_pr: true`, `model`, `sandbox_mode: "externalSandbox"`, `external_network: "enabled"`, `max_seconds`, `model_usage_approved: true`, `goal`, `completion_criteria`, `inputs`, `write_scope`, `resource_budget` です。

`inputs` は入力 commit 内の regular file の相対パスだけです。installer はその Git blob を読み、immutable image 内に内容 hash とともに封入します。`write_scope` は新規の専用成果物ディレクトリとし、入力と重ねません。例えば公開 Issue 50 の JSON/CSV を読み、集計用コードと JSON/Markdown を `docs/research/generated/issue50-goal-review` 以下に生成する目的にできます。コマンドや実装言語を plan で固定する必要はありません。

本人が新 image・新 volume・bridge・資格情報の永続利用を承認した後、次の一つの installer を実行します。

```sh
python3 -B tools/research_goal/prepare.py \
  --approve-new-goal-deployment \
  --project c3-goal-review-001 \
  --profile goal-profile.json --plan goal-plan.json \
  --codex-auth-file /PRIVATE/PATH/auth.json \
  --github-hosts-file /PRIVATE/PATH/hosts.yml
```

このコマンドは committed runtime と指定した入力だけから一時 build context を作り、新しい container と `goal-control` volume を作成し、host inspect/volume consumers/UID・mode・転送 bytes を確認して起動します。同名 container/volume が存在すれば拒否します。また同じ `deployment_id` を持つ container（停止済みを含む）・volume・image が同じ Docker engine に存在すれば拒否します。新しい installation は新しい template から ID を生成し、同じ volume の restart では ID を変えません。別ホストへの移送や全記録削除後にも、operator が過去の ID を意図的に再利用してはいけません。既存 deployment への fallback、削除、volume 初期化し直しはありません。失敗した新 deployment も自動削除しません。

`enabled: false` または同意不足の profile は Docker 操作前に拒否します。起動後に profile が無効化・欠落・破損した場合、launcher は子プロセスを起動せず `GOAL_BLOCKED_STARTUP` で待機します。本人が設定を修復してから明示的に restart するまで、再試行しません。

起動に成功しても、その時点では goal 実行の owner 承認はありません。準備に使った plan と同じ plan を、新しい実行用 Issue の `c3-execution-request` ブロックへ入れ、既存の owner receipt 手順で内容 hash・plan digest・期限を承認します。タイトルだけで実行されません。同じ入力 commit でも、旧 deployment の Issue は `deployment_id` 不一致で claim 前に拒否されます。参照: `docs/research_issue_intake.md` と `docs/research_owner_approval.md`。

## 3. 進行・候補・公開を確認する

installer が表示する container ID を使って `docker logs --tail 30 CONTAINER_ID` で確認します。ログは固定診断と status のみで、モデルの任意文・成果物本文・資格情報は転記しません。

1. owner 承認を再検証し、source/model/resources/UID/namespace/入力 hash/書込 scope を preflight します。
2. durable な単回 claim の後に実行します。期限切れ・変更・取消・不明な応答は fail closed。途中失敗でも同じ claim を再実行しません。
3. 成果物の immutable bytes を manager-owned quarantine に atomic rename・fsync し、path/hash/binding を検証します。これはコピー元全体の静止や研究成功の証明ではありません。
4. 候補は `RESULT_AWAITING_RESTART` で private に保存します。PID1 終了・新しい namespace の確認後にのみ `AWAITING_CONTENT_REVIEW` になります。
5. 本人が private 候補の exact files・path・mode・source/base と digest を確認します。状態 DB と quarantine は新 volume の `/control/state`・`/control/quarantine` 内です。owner の端末へ `docker cp CONTAINER_ID:/control/quarantine ./goal-review` で取り出して確認できます。JSON の `content_base64` は内容であり、生成コードを manager/root として実行して確認しないでください。
6. 同じ Issue に、未編集の本人コメントで `c3-goal-publication-approval-v1` を作成します。必須 binding は `repository`, `issue`, `request_digest`, `candidate_digest`, `issue_content_sha256`, `source_sha`, `base_sha` と `expires_at`, `decision: "approve-publication"` です。実行承認を内容承認へ流用しません。公開取消は同じ binding の `decision: "cancel-publication"` です。
7. fresh な内容承認の後だけ exact file diff を単一 commit として draft PR にします。private summary は PR 本文に転記しません。merge は行いません。外部 write の成否不明時は journal を保持し、同じ write を推測で再送しません。

新規 goal の実機起動を行うまでは、「朝すぐ本番運用できると実証済み」とは扱わないでください。最初は公開入力だけの小さい CPU 集計で、この一連の経路を本人承認の下で確認します。

通知接続は未設定で、実イベントの dot 宛て疎通も未確認です。ログや PR 作成の成功を通知成功とは扱いません。

### 実行失敗の診断

`stage: turn` は goal runner 呼出し全体を指し、`initialize` より前にも設定されます。
`turn/start` 到達は、この stage から判断せず、固定診断の
`app_server.phase` / `protocol.phase`、`method`、`initialized`、
`thread_started`、`turn_accepted` を確認してください。

- `GOAL_APPROVAL_RECHECK_FAILED` は管理側の承認再確認拒否です。
  `app_server.recheck_reason/recheck_origin` または `approval_failure` に固定分類を残します。
- 通信切断、期限、形式不一致は `app_server.reason` で区別します。
  `GOAL_SERVER_ERROR` は goal protocol のサーバーエラー応答の拒否です。
  いずれも例外本文、リモートの任意メッセージ、stderr、モデル出力は記録しません。
- `observed_child_returncode` は停止処理より前に `poll()` で実測できた場合だけ存在します。
  欠落を exit 0 や、強制停止による終了コードに置き換えてはいけません。
- root の `GOAL_RUNTIME_BLOCKED` / `GOAL_RUNTIME_FAILED` と管理側の
  `GOAL_MANAGER_BLOCKED` に閉じた診断を伝えます。管理側が先に再確認を拒否して終了する場合は
  `GOAL_MANAGER_RECHECK_REJECTED` に固定 reason/origin を記録します。
  診断の有無は BLOCKED 保存や namespace 終了の代わりにはなりません。
- GitHub 読取の失敗地点では `MANAGER_GITHUB_READ_FAILED` に固定 reason と、
  取得できた CLI 終了コード・HTTP status・OS errno を残します。
  `cgroup_pids_current/max/denied_total` は専用 allocation 全体の実測値です。
  denied の累積値だけで当該照会や Codex が PID 上限に達したとは断定しません。
  取得できなかった数値は省略し、応答本文・stderr・コマンド引数は出しません。
  分類や診断出力の失敗で再確認を成功扱いしたり、自動 retry したりしません。

Issue83 の 13ab 実機ログは `GOAL_TRANSPORT_FAILURE / turn` だけで、元の分類は保存されていません。
13ab のモデルなし実 pipe 再現では、管理側 `source_timeout` と不正 JSON の両方が同じ外側診断になりました。
したがって、過去の原因をネットワーク障害や承認期限切れと確定しません。
fixture による正常応答・結果保存と、実物 Codex/実モデルの接続・成果物保存は区別します。

2026-10-18 の本人承認済み専用診断では、cb5fbd5 の runtime と実物 Codex 0.160.0、
gpt-6.1-sol を使い、manager UID10001 から worker UID10002 の
`initialize`・`thread/start`・`turn/start` 各1回と `turn/started` を観測しました。
受付/承認は明示した合成 fixture、配置検証は診断用 entrypoint/purpose と restart=no を期待する
別 validator です。実測 inspect の書換えや、UID/capability/resources/reserve の検査省略はしていません。
過去 Issue83 の期限切れ承認を実行許可には使いませんでしたが、当時の診断 driver は
追加の `ShadowRead.observe()` を承認 snapshot の前に直列接続していました。
4回の観測成功後にこの追加観測が例外を出し、合成承認の確認へ到達せず、
stream 中の再確認が contract で拒否され、`GOAL_APPROVAL_RECHECK_FAILED` /
`APP_SERVER_RECHECK_FAILED` / `recheck_origin=manager` で停止しました。
台帳 BLOCKED、result/snapshot なし、container は停止、再起動0回です。
停止後のモデルなし4照会は成功しましたが、失敗時の CLI/status/errno/PID counter は
残っておらず、API形式、プロセス資源、観測内のローカル検証のどれかを確定できません。
モデルなし再現でも、REST例外・GraphQL例外・観測対象の不正な日時が同じ診断になりました。
この診断は実物接続の到達証拠であり、成果物保存成功、Issue83の過去原因、
本番 owner intake、再起動境界や公開の成功証拠ではありません。
新しいモデル試験や本番更新は、この不足を明示して別途承認を受けるまで実施しません。

診断用コードとモデルなし検証を `diagnostic_driver.py` / `diagnostic_observation.py` /
`diagnostic_boundary.py` に保存しました。修正版は追加観測を承認 callback から完全に外し、
独立した5秒の読取予算で管理起動時に1回だけ実施します。通常の観測例外は固定分類だけ記録し、
予算の context を解放後に新しい合成承認を作り、本番そのままの `GoalController` を呼びます。
観測失敗を承認と見なしたり、本番の拒否を成功へ変えたりしません。
診断固有の差分、fixture と実測の区別、未実施の次試験は [DIAGNOSTIC.md](DIAGNOSTIC.md) を参照してください。

旧経路の PR #78 の手動復旧と PR #80 の自動失敗結果は、新 goal 経路の成功実績ではありません。
