# Codex sandbox 専用コンテナ：診断用の下書き

これは研究実験や計画生成を開始する設定ではありません。ホスト上で別コンテナを作り、Codex 0.160.0 の sandbox 内で `/usr/bin/true` が成功するかだけを確認します。既存の実験コンテナは変更しません。

## 今わかっていること

- ホストの user namespace 作成は成功しています。
- 既存コンテナの Bubblewrap 起動は namespace 作成で失敗しています。
- seccomp、AppArmor、その他の実行条件のどれが原因かは未確定です。
- Dockerfile の中身と Docker 起動時のセキュリティ設定は別です。イメージを作るだけでは外側の制限は変わりません。

## ファイル

- `Dockerfile`: 既存実験イメージを流用せず、公式 Node/Debian ベースから構築。非rootユーザーを登録し Codex 0.160.0 を固定。
- `compose.yaml`: Docker 標準制限の比較用。通信なし、GPUなし、ホストマウントなし、capability 全削除、資源制限。
- `compose.seccomp-candidate.yaml`: OpenAI の Bubblewrap 対応 seccomp を使う候補。
- `diagnostic.sh`: バージョン・制限情報と sandbox 試験結果を出力。モデルは起動しない。
- `sources.json`: 公式ソース固定commitと取得したプロファイルのSHA-256、npmの確認済みバージョン・integrity。

## 確認済みのホスト条件

- Docker 28.3.2
- Docker Compose 2.38.2
- AppArmor parser 3.0.4

上流AppArmor候補は ABI 4.0 と `userns` 構文を含むため、今回のホストへ読み込ませません。AppArmorプロファイルや適用用overrideはこのPRに含めません。AppArmorの更新や無効化も行いません。
今回の段階は「標準設定で試す → 明示承認後に seccomp 候補だけを適用し、docker-default AppArmor を維持して試す」までです。docker-default の mount 制限などで失敗が残る可能性があります。その場合は出力を報告して止め、AppArmor 3対応の個別プロファイルは別途レビュー・承認します。

## 実行前の条件

1. 公式ベース `node:22-bookworm-slim` の manifest digest を Docker Registry の読み取りで確認し、Dockerfileに固定しました（値は sources.json）。APTパッケージは未固定なので、ビルド後のイメージIDを記録します（APTパッケージ一覧の保存は未実装です）。
2. ビルド時には公式配布元への通信・ダウンロードが発生します。ここではビルドしていません。
3. 専用seccompの適用は新規コンテナのセキュリティ設定の変更です。対象・内容を確認し明示的に承認されるまでは実行しません。この手順にAppArmorの登録・変更は含みません。
4. 公式の汎用プロファイルを固定取得しましたが、これが研究用の厳密な最小権限設定だと認定したものではありません。差分レビューと実機試験が必要です。

## 承認後の診断手順

保護チェックとログ保存を含むスクリプトから実行します。このディレクトリで、承認された範囲に合わせて次を使います。

```sh
bash run_checks.sh --allow-seccomp-candidate
```

標準設定を先に試し、既知のnamespace拒否だけが出た場合に専用seccomp候補を1回試します。AppArmorは標準のままなので、ここで失敗してもseccomp候補が無効だったとは断定できません。個別のComposeコマンドだけで実行すると既存資源・空き容量などの確認を飛ばすため、このスクリプトを使ってください。

結果が `sandbox_true_exit=0` なら sandbox 起動の第一段階だけ成功です。実験データの隔離、書込み拒否、プロセス回収、モデル接続、料金上限、GitHub連携は別途検証が必要です。失敗したら出力を保存して止め、privileged・SYS_ADMIN・unconfined・sandbox無効化に切り替えません。

## 公式根拠

固定commit: `eaf1ffada1bf72fc6179c31d366ed05ce8a949d3`

- https://github.com/openai/codex-security/blob/eaf1ffada1bf72fc6179c31d366ed05ce8a949d3/compose.runner.yaml
- https://github.com/openai/codex-security/blob/eaf1ffada1bf72fc6179c31d366ed05ce8a949d3/docker/codex-security-seccomp.json
- https://github.com/openai/codex-security/blob/eaf1ffada1bf72fc6179c31d366ed05ce8a949d3/docker/codex-security.apparmor
- https://docs.docker.com/engine/security/seccomp/

seccomp候補は namespace/mount関連の複数syscallを許可します。`unshare` 一つの許可だけで直るという前提は置きません。プロファイルは公式ソースそのままを診断候補として保存しています。Docker-in-Docker、Dockerソケットの共有、ホストデータや認証情報のマウントは含みません。

## この作業の影響と上限

- ビルド：公式Node/Debianイメージ、Debianパッケージ、npmのCodexとプラットフォーム依存物をダウンロードし、ローカルDockerイメージ・ビルドキャッシュを作成します。ビルド自体のダウンロード容量・時間は未確定です。モデル/API課金は発生する手順ではありません。イメージ公開や認証設定はありません。
- 診断実行：新しい一時コンテナ1つ。CPU上限0.5、メモリ512MiB・swap追加なし、PID上限64、tmpfs計128MiB、shm16MiB。sandbox試験は10秒、強制終了猶予2秒。制限はこのコンテナ内のみで、Dockerビルドの資源上限ではありません。
- 保存：終了後コンテナは `--rm` で削除されます。イメージ・キャッシュは残ります。既存イメージやデータを削除するpruneはしません。
- セキュリティ候補：namespace/mountを含むsyscallの許可範囲を広げます。cap-drop ALL、no-new-privileges、非root、通信なし、docker-default AppArmorは維持します。ホストのsysctlや他のコンテナには適用しません。
- GitHubのPRで内容を確認し、固定commitを独立した作業ディレクトリに取得して使います。curlで取得してそのまま実行する手順は使いません。

注意：sources.json のnpm integrityは照合用の記録です。Dockerfileはバージョン指定でnpm installし、記録したintegrityを追加検査する処理までは組み込んでいません。またCodexの起動用補助ファイルに備え、tmpfsはexec可能です。tmpfsが実行不可だとは主張しません。

Dockerクライアント設定から認証付きプロキシURLが自動継承されるのを避けるため、HTTP_PROXY/HTTPS_PROXY/FTP_PROXY/ALL_PROXY/NO_PROXYと小文字版を、build argsと実行時environmentで空文字に固定しています。ホストのDocker設定・環境変数の中身は出力しません。プロキシ必須の環境でビルドが失敗しても、認証情報を追加して回避せず停止します。

Composeは必ず明記した `-f` と専用project名で実行し、自動的な compose.override.yaml 読込みを避けます。`--env-file /dev/null` を指定し、実行前のconfig表示で、意図しないmount・権限・通信・追加serviceがないことを確認してください。同名projectが既にあれば再利用せず、この診断スクリプトを終了します。既存コンテナは停止・変更しません。

## ホスト実行用のまとめスクリプト

承認された診断を行う場合、このディレクトリで `bash run_checks.sh --allow-seccomp-candidate` を実行できます。Dockerデータ領域の空き容量を表示し、2GiB未満なら中止する目安の保護があります。これはビルド使用量の上限や十分性の保証ではありません。既存の同名プロジェクトがあれば診断スクリプト自身を終了します。既存コンテナは停止・変更しません。標準設定の結果を記録し、既知のnamespace拒否（終了コード1と診断マーカー付き）で失敗した場合だけ、明示した候補許可フラグに従い専用seccompで再試験します。タイムアウト・OOM・起動失敗・その他のエラーでは再試験しません。各終了コードとログの保存場所を表示します。AppArmor変更、再試験の繰返し、他コンテナの停止、pruneは行いません。

`--allow-seccomp-candidate` を省略すると標準設定の試験だけで終了します。再試験や設定適用は読み戻したログと承認範囲を確認して行ってください。

## 手元で確認した範囲

`python tools/sandbox_diagnostic/test_run_checks.py` は偽のDockerコマンドを使う制御フロー試験です。9件で、標準成功、明示許可付きnamespace失敗（2表記）、許可なし、タイムアウト、マーカー欠落、一覧取得失敗、リモート接続指定を確認します。加えて、base と override の security_opt が重複せず権限昇格禁止を継承することを確認します。Docker自体は起動しません。Compose実装による結合検証はこの環境では実施できていません。ホストではビルド前に config を検証し、失敗すればそこで中止します。シェル構文・YAML/JSON・取得ファイルハッシュも確認しています。これらは実機のsandbox成功を意味しません。
