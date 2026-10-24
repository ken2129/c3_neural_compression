# 特徴圧縮の基礎とC3への応用可能性

公開資料版

調査基準日 2026年10月24日

本版は出版論文、公開学位論文、公式書誌、公式実装に基づく文献調査である。文献で確認された結果と、本レポートの未検証の研究提案を区別して記載する。

## このレポートの結論

C3を機械認識へ応用する際は、通常の復元RGBを認識器に入力する方法だけでなく、特徴を復号して認識器の後半へ渡す方法、圧縮latentから直接タスクを解く方法、複数タスクに共通する特徴を符号化する方法を分けて考える必要がある。本レポートは、そのうち**認識までの経路でRGB再構成を必須としない研究**を中心に、基礎から近年の発展まで整理する。RGB画像を出力する既存研究は、境界を示す比較対象として扱う。

最初の研究候補として説明しやすいのは、固定認識器の中間特徴をC3型の画像別表現で再構成し、同じ後段ネットワークに接続する構成である。ただし、これは元のC3に損失を一つ追加するだけではない。出力チャネル数、特徴の分布、分割点を横断する全テンソル、画像ごとに送る重み、実符号量を含む設計変更になる。一方、C3の内部latentにadapterを付ける構成は、元のcodecを残しやすいが、画像間でlatentの意味が揃う保証がなく、共有codecを前提とする既存研究のbridgeをそのまま移植できない。

新規性の議論では、Li・ZhangのJETCAS 2024とNeural-Centric Video Processing Pipeline for Unified Multi-Task Inferenceが特に重要になる。前者はCOOL-CHIC/EQLINR系の画像別最適化に機械専用latent経路と意味応答の損失を組み合わせている。したがって、「INRを機械タスク向けに最適化する」「RGB復元を経由しない機械経路を作る」といった広い主張は、それだけでは新規性にならない。研究として問うべきなのは、どの特徴を、どのモデル共有条件で、どの費用まで含めて符号化し、どのタスク・未知モデルに有効かである。

以下の提案は文献に基づく設計候補であり、C3での性能を実験済みとするものでも、実験の実行を決定したものでもない。公開論文の結果、公式書誌のみ確認できた関連研究、今回の提案を区別して記載する。

## 1 特徴圧縮を理解するための地図

### 1.1 画像ではなく何を送るのか

通常の画像通信では、送信側が画像を圧縮し、受信側がRGB画像を復元してから認識する。機械の目的が物体検出や領域分割だけなら、人が見るための全ての画素情報を忠実に復元する必要はないかもしれない。そこで、認識器が画像から取り出した特徴、あるいは圧縮器の内部表現を送る発想が生まれる。

「特徴」は一種類ではない。CNNの中間層なら高さ×幅×チャネルのテンソル、Transformerならトークン×埋め込み次元の行列、画像検索なら一枚を表すベクトル、動画なら時間方向を含む特徴列になる。これらは必要な構造も後段処理も異なる。例えば、分類直前の小さなベクトルを圧縮できても、検出器の位置情報を含む多尺度特徴を同じ方法で圧縮できるとは限らない。

また、特徴が低次元であることと、圧縮後のbit数が小さいことは同じではない。少数のfloat32要素でも大きな通信量になり、反対に多くの要素があっても予測しやすい量子化記号ならentropy codingで小さくなる。論文では、要素数、量子化bit幅、推定entropy、実bitstream長のどれを測っているかを読む必要がある。

### 1.2 四つの経路と二つの独立した設計軸

特徴圧縮全体を「二方式だけ」と分類するのは不十分である。まず次の四経路を区別すると、論文の違いを見失いにくい。

| 経路 | 受信側の処理 | 主に送るもの | 今回の位置づけ |
|---|---|---|---|
| RGB復元を介するcascade | 復元RGB → 認識backbone → task head | 画像codecのbitstream | 比較対象。feature lossを使っても経路はRGB型 |
| 認識器分割型 | 復号特徴 → backboneの残り／task head | 認識器の中間特徴を表すbitstream | 特徴圧縮の基本形 |
| 画像codecのlatent直結型 | latent → bridge／adapter／task decoder | 画像codecの量子化latentと必要な補助情報 | 元の画像decoderを機械経路では使わない |
| INRによる特徴表現型 | 伝送された表現・小型ネットワーク → 特徴 → task head | latent、画像／動画固有のネットワーク重み等 | C3への転用を考える際の直接的な候補 |

第二と第四は排他的ではない。認識器を分割し、その中間特徴をINRで符号化することもできる。第三でも、adapterが既存認識器の中間特徴を再現する場合と、特徴再現を省いて最終タスクを直接解く場合がある。さらに、human用のRGB復元とmachine用の特徴経路を同じbitstreamの基本層・拡張層として設計する方法もある。[Torfason et al.](https://arxiv.org/abs/1803.06131)、[Choi and Bajić](https://arxiv.org/abs/2107.08373)

別に考えるべき軸は、**何を共有学習し、何を入力ごとに最適化するか**である。多数の画像で学習したencoder・decoderを全画像で共有する方式と、C3のように入力ごとにlatentや小型ネットワークを最適化する方式では、受信側のモデル、学習時間、伝送する重みが違う。「事前学習なし」という表現も、codecを共有学習しないだけなのか、教師認識器まで含めて事前学習を使わないのかを区別する必要がある。

### 1.3 用語と損失の読み方

- **Backbone**：画像から汎用的な特徴を取り出す部分。**Head／tail**：その特徴から分類・検出・分割などを行う後段。論文によってtailはbackboneの残りも含む。
- **Split point**：送信側と受信側を分ける位置。深く分けると通信量が減っても、送信側の計算量が増え得る。
- **Latent**：codecの内部表現。認識器が出す特徴と同じ意味・shapeであるとは限らない。
- **Quantization**：連続値を有限の記号に丸める処理。学習では加法noiseやstraight-through推定で近似することが多い。
- **Entropy model**：各記号の出現確率を予測し、短い符号を割り当てるためのモデル。hyperpriorやcontext modelにも伝送・計算の費用がある。
- **Rate**：伝送量。学習中の−log₂pによる推定値と、実際に生成されたファイルのbit数は区別する。
- **Distortion**：何をどれだけ失ったか。画素MSE、特徴MSE、教師出力とのKL、タスク損失は別の目的である。
- **Distillation**：教師モデルの特徴や予測を生徒に近づける学習。正解ラベルを使わなくてもよいが、教師の事前学習や推論費用が不要になるわけではない。

特徴圧縮の典型的な目的は「rate＋λ×特徴の誤差」、あるいは「rate＋λ×タスク損失」である。前者は既存の固定後段へ戻しやすい一方、全チャネルのMSEを同じ重みで減らすことが認識に最適とは限らない。後者はタスクに合わせて情報を捨てられるが、特定のheadやラベル体系に適応し、別のタスクには使いにくくなることがある。この違いが、後述するC3の研究候補を分ける。

## 2 初学者向けの読む順番

最初から全論文を順に読む必要はない。まず次の7件を読むと、基本構造、目的関数、画像別最適化との違い、評価上の落とし穴をつなげて理解できる。

1. **Choi and Bajić 2018**。特徴をタイル化して従来codecで送るという具体例から入る。Fig. 2–3を見て、RGB画像を送らずにどこで推論を再開するかを理解する。[本文](https://arxiv.org/html/1802.03931)
2. **Torfason et al. 2018**。認識器の中間特徴ではなく、画像codecのlatentから認識する別系統を知る。Fig. 2と§3–6を読み、「without decoding」がRGB合成を省く意味であることを押さえる。[本文](https://arxiv.org/pdf/1803.06131)
3. **Singh et al. 2020**。圧縮しやすさを−log₂pで表し、タスク損失と同時に最適化する基本形を読む。§2.1–2.2、Eq. 6、Appendix Fig. 2が中心。[本文](https://arxiv.org/pdf/2007.11797)
4. **Entropic Student 2022**。固定teacherの特徴を軽量encoder・decoderで再現し、rateも最適化する方法を読む。Fig. 1–3とEq. 2で、共有学習と教師蒸留を理解する。[本文](https://arxiv.org/pdf/2108.11898)
5. **Omni-ICM 2022**。既存特徴をそのまま圧縮するだけでなく、複数タスクに役立つ圧縮可能な特徴を作る研究へ進む。Fig. 1–3、Eq. 9–12を見る。[本文](https://www.ecva.net/papers/eccv_2022/papers_ECCV/papers/136970500.pdf)
6. **Li and Zhang 2024**。C3に近いCOOL-CHIC系列で、画像別fitと機械専用latent経路を扱う先行研究を読む。§III、pp.201–204、Eqs.1–5が重要。新規性を考える前に確認したい。[論文ページ](https://ieeexplore.ieee.org/document/10495030)
7. **SC2 Benchmark 2023**。最後に、bitrateだけでなく送信側計算量も揃える評価設計を読む。Fig. 1、§2.3、§3を、自分の比較実験のチェックリストとして使う。[本文](https://openreview.net/pdf?id=p28wv4G65d)

この後、C3による特徴表現を検討するならNVP、画像codecのlatentを使いたいならLiuらのPCS 2022、動画ならVNVC、基盤モデル特徴ならLaMoFCへ分岐するとよい。STDLCは関連する新しい出版文献として挙げるが、本版では具体的な構成を確定していない。Cohenらの2021年論文は、提案法を複雑にする前に読むべき、再学習不要の低複雑度baselineである。

## 3 基礎研究を設計の違いから読む

### 3.1 中間特徴をそのまま送る初期の代表例

**Deep Feature Compression for Collaborative Object Detection**は、YOLO9000系列を途中で分割し、特徴を8・10・12-bitに量子化して、チャネルをタイル状に並べ、HEVC RExtで符号化する。受信側はタイルを特徴テンソルに戻し、検出器の残りを実行する。画像に見える二次元配置を使うが、それはcodecへ入力するための配置であり、元の自然画像RGBを復元しているわけではない。

既存重みを使う評価に加えて、圧縮・復号を学習中に挟むcompression-augmented trainingも検討している。したがって「固定認識器でのcodec評価」と「認識器も圧縮に適応した評価」を混ぜないことが重要になる。VOC2007・2012の計16,551枚で学習し、別のVOC2007 test 4,952枚で検出mAPを評価する。rateは平均Kbits/imageであり、逆量子化用min/maxの32-bit float二つ、計8 bytesも数える。この小さな記載は、特徴圧縮でもshape・scale等のside informationを無視できないことを示す。

C3へつなげるなら、まず同じ固定特徴と同じtailを使い、HEVC部分をC3型特徴表現に替える比較が自然である。ただし、分割点が深くなるほど通信に有利でも、端末でYOLOの多くを計算するため、通信量だけでは優劣を決められない。研究固有の公式codeは今回の確認では特定できていない。[Choi and Bajić, ICIP 2018, Fig. 1–3, §3–4](https://arxiv.org/html/1802.03931)

### 3.2 タスクに必要で圧縮しやすい特徴を学習する

**End-to-end Learning of Compressible Features**は、認識networkにbottleneckを設け、量子化特徴の確率モデルとtask networkを共同学習する。損失はtask lossと推定符号長の和であり、単にチャネル数を減らすのではなく、実際に短い符号になりやすい表現を学ぶ。rate側にはuniform noise、task側にはroundingとstraight-through gradientを用い、学習中に扱いやすい連続近似と推論時の量子化をつなぐ。

CIFAR、ImageNet、YouTube-8Mなどを扱うが、代表的な実験は最終分類層直前のactivation圧縮である。同程度のImageNet分類性能で、lossless特徴容量の約12.2%という結果を報告する。この数値は、画像JPEGの12.2%という意味ではない。baselineはfloat32特徴をzlib圧縮した容量であり、PCA baselineの共有basis容量は除かれる。画像単位の符号化経路はAppendix Fig. 2に示される。

C3への最も大切な示唆は、「rate＋task」を明示的に最適化する考え方である。一方、各テスト画像の最適化に正解ラベルを使えば、一般的な運用では利用できない情報を与えることになる。画像別fitで応用するなら、ラベル付き学習時のtask lossと、ラベルなしテスト画像で利用できる教師特徴・教師予測との一致を分ける必要がある。[Singh et al., ICIP 2020, §2, Eq. 6](https://arxiv.org/pdf/2007.11797)

### 3.3 再学習しない圧縮は外せない比較相手

**Lightweight Compression of Intermediate Neural Network Features for Collaborative Intelligence**は、固定されたDNNの中間activationを、clipping、量子化、binarization、簡略CABACで符号化する。DNNを再学習せず、activation統計から範囲や量子化を設計するため、新しい学習codecを提案するときの有用な対照になる。

ResNet-50／ImageNetとYOLOv3／COCOで、32-bit activationを概ね0.6–0.8 bit/elementへ圧縮し、精度低下を1%未満に抑えた条件が報告される。ここでの分母は特徴要素数であり、元画像のpixel数ではない。headerも分類12 bytes、検出24 bytesとして含める。特徴が大きければ、0.6 bit/elementでも画像圧縮より多くのbytesを送る可能性がある。

C3型の画像別fitは時間がかかるため、このような単純方式に対して、同精度で何bytes減るかだけでなく、何秒余分にかかるかを示す必要がある。「高度なINRだから強い」という前提を置かず、まず同一特徴で単純量子化＋entropy codingを越えるかが出発点になる。[Cohen, Choi and Bajić, OJCAS 2021, §IV, Algorithm 1, Fig. 8](https://arxiv.org/pdf/2105.07102)

### 3.4 Entropic Studentは特徴再構成型の設計参考になる

**Supervised Compression for Resource-Constrained Edge Computing Systems**では、軽量encoderが画像からlatentを作り、そのlatentを圧縮・伝送し、decoderがteacherの中間特徴を再現する。後段はteacher由来のtask networkである。ここでは「元の大きなbackbone前半を端末で走らせて特徴を得る」構成そのものも見直し、軽量studentに置き換える。

第一段階では、teacherの中間特徴との二乗誤差とentropy rateを使い、studentと確率モデルを学習する。第二段階ではencoderを固定し、decoder・task tailをfine-tuneする。分類ではhard labelとteacherのsoft targetを使う。したがって、全条件が「元の認識器を一切変更しない」わけではない。ResNet-50分類、RetinaNet検出、DeepLabv3分割を扱い、Raspberry Pi 4やJetson TX2を使った遅延も評価する。

C3への参考点は、出力対象をRGBから教師特徴へ変え、その特徴をtask interfaceにすることだ。ただし、Entropic Studentのencoderはdatasetで共有学習する一方、C3は入力別に最適化する。この学習単位の違いを保ったまま、「rate＋教師特徴再現」「固定tail」「tail適応あり」を別条件にするのがよい。公式実装があり、単純な再実装よりも設定・checkpoint・容量計測を確認しやすい。[Matsubara et al., WACV 2022, Fig. 1–3, Eq. 2, §3.3](https://arxiv.org/pdf/2108.11898)、[公式code](https://github.com/yoshitomo-matsubara/supervised-compression)

### 3.5 Omni ICMは特徴そのものの学習を扱う

**Image Coding for Machines with Omnipotent Feature Learning**は、既存の一つの認識器の特徴を忠実に保つのではなく、複数taskに有用で圧縮しやすい表現を学習する。backbone headとinformation-filtering moduleが特徴を作り、feature codecがそれを送る。受信側は各taskのtailを実行する。

学習は、①対比学習とentropy制約による特徴学習、②他の部分を固定したfeature codec学習、③その特徴を使ったdownstream tailのfine-tune、という段階に分かれる。codecの損失には、直接の特徴MSEに加えて、復号前後の特徴をtailへ通した複数深度の誤差も含まれる。これは、低層の誤差が小さくても、後段で意味が崩れる可能性に対処する設計として読める。

VOC・COCOの検出、COCOのinstance segmentation・姿勢推定、Cityscapesのsemantic／panoptic segmentationを扱う。rateは実bitstream file sizeを元画像pixel数で割る。複数taskで評価していても、未知taskを一切学習せずに解ける保証ではなく、task tailの適応を含む点が重要である。

C3の特徴再構成型に取り入れるなら、単一層MSEに加え、固定tailの後段特徴や教師出力を保つ損失を比較する案が考えられる。しかし、その損失自体は既存の発想である。差分は画像別最適化との組合せや転移・rate・計算費用を含む検証に置く必要がある。[Feng et al., ECCV 2022, Fig. 1–3, Eq. 9–12, §4.3](https://www.ecva.net/papers/eccv_2022/papers_ECCV/papers/136970500.pdf)

### 3.6 RGB復元を省くことと特徴復元を省くことは違う

Torfasonらの**Towards Image Understanding from Deep Compression without Decoding**は、画像codecの約W/8×H/8×Cのlatentから分類・segmentationを行う。画像再構成用codecを先に学習し、固定codecで認識器を学習する条件と、codec・分類networkを共同fine-tuneする条件を比較する。「without decoding」はentropy decodingまで不要という意味ではなく、RGB synthesisを経由しないという意味である。評価はImageNetとPASCAL VOCで、0.0983、0.330、0.635 bppという動作点を扱うが、headerを含む実bitstream会計の細部は今回の確認では確定していない。[Torfason et al., ICLR 2018, Fig. 2, §3–6](https://arxiv.org/pdf/1803.06131)

一方、Nazirらの**Distributed Semantic Segmentation with Efficient Joint Source and Task Decoding**は、送信latentからjoint decoderでsegmentationを直接出力する。RGBだけでなく、分割点の元特徴をいったん厳密に再現する段階も必須にしない。ResNet-50／DeepLabv3を基礎に、hyperprior、量子化latent・hyperlatentのarithmetic coding、ASPPベースのdecoderを使う。COCOとCityscapesでrate–mIoUを評価し、cloud側のparameter数を従来feature-coding SOTAの9.8%・11.59%にしたという報告がある。この割合はbitrate削減率ではない。

この系統をC3に使うなら、「固定tailが要求する特徴を復元するcodec」から「C3表現を読めるtask decoder」へ研究問題が変わる。task decoderの学習・配布・汎用性まで費用と条件に含める必要がある。また、本文の初期学習説明からimage encoderの全条件での固定までは確認できないため、固定と推測して比較してはいけない。[Nazir et al., ECCV 2024](https://www.ecva.net/papers/eccv_2024/papers_ECCV/papers/12134.pdf)

## 4 主要文献の比較地図

表は優劣の順位ではなく、何を送るか、どこを学習するか、C3へ何を借りられるかを比較するための地図である。「未確認」は不存在を意味しない。各論文の削減率はデータ、baseline、タスク、モデルの固定条件が違うため、横断的な数値ランキングにはしていない。

### 4.1 中間特徴と複数タスクの圧縮

| 論文 | 年と位置づけ | 機械側の入力と学習 | 読む理由と注意点 |
|---|---|---|---|
| [Deep Feature Compression for Collaborative Object Detection](https://arxiv.org/abs/1802.03931) | ICIP 2018 | YOLO中間特徴を量子化・HEVC符号化 | 分割推論の基本。固定重みと圧縮適応学習を分ける |
| [End-to-end Learning of Compressible Features](https://arxiv.org/abs/2007.11797) | ICIP 2020 | 分類特徴と確率モデルをtask＋rateで学習 | 次元数ではなくentropyを減らす。主に分類直前特徴 |
| [Lightweight Compression of Intermediate Neural Network Features](https://arxiv.org/abs/2105.07102) | OJCAS 2021 | 固定DNN特徴のclipping・量子化・CABAC | 低複雑度baseline。bits/elementとimage bppを区別 |
| [Supervised Compression for Resource-Constrained Edge Computing Systems](https://arxiv.org/abs/2108.11898) | WACV 2022 | 軽量studentがteacher特徴を復元 | 蒸留＋rate。第二段階はtail適応を含む |
| [Omni-ICM](https://www.ecva.net/papers/eccv_2022/papers_ECCV/papers/136970500.pdf) | ECCV 2022 | 対比学習で汎用特徴を作り、feature codecを学習 | 複数task向け。未知taskへの無学習適用とは別 |
| [SC2 Benchmark](https://openreview.net/pdf?id=p28wv4G65d) | TMLR 2023 | input／feature圧縮の統一比較 | codec一方式ではなく評価基盤。edge負担も比較 |
| [Joint Source and Task Decoding](https://www.ecva.net/papers/eccv_2024/papers_ECCV/papers/12134.pdf) | ECCV 2024 | latentからsegmentationを直接出力 | 元特徴の完全再現を不要にするtask専用decoder |
| [Compressive Feature Selection](https://arxiv.org/abs/2405.09077) | ICME workshop 2024 | 既存特徴をMI・norm等で選択 | 学習codec以外の情報削減。MIが全taskで最良ではない |
| [DMOFC](https://arxiv.org/abs/2405.04044) | PCS 2024 | ReID／顔認証ベクトルを識別損失で圧縮 | 空間特徴とは異なる。容量実験を実entropy符号化と混同しない |
| [LaMoFC](https://arxiv.org/abs/2412.04307) | ICCV 2025 | DINOv2等の特徴をVTM／Hyperpriorで符号化 | 大型モデル特徴の評価基盤。小規模選択subsetに注意 |
| [ATDC](https://openaccess.thecvf.com/content/CVPR2026/papers/Huang_Discovering_Adaptive_Task_Dependencies_for_Efficient_Multi-Task_Representation_Compression_CVPR_2026_paper.pdf) | CVPR 2026 | task間依存を画像ごとに選び、条件付き残差符号化 | 複数taskの冗長性を減らす。依存graphの費用も確認 |

### 4.2 画像codecのlatentから認識する研究

| 論文 | 年と位置づけ | 機械側の経路 | C3へ借りるもの |
|---|---|---|---|
| [Towards Image Understanding from Deep Compression without Decoding](https://arxiv.org/abs/1803.06131) | ICLR 2018 | 画像latent → cResNet等 | 同一streamのRGB経路と直接経路の比較 |
| [Learning in Compressed Domain for Faster Machine Vision Tasks](https://ieeexplore.ieee.org/document/9675369/) | VCIP 2021 | entropy順に選んだlatent → task網 | 単純channel選択の先行例。詳細はabstract確認範囲 |
| [Improving Multiple Machine Vision Tasks in the Compressed Domain](https://waseda.elsevierpure.com/en/publications/improving-multiple-machine-vision-tasks-in-the-compressed-domain/) | ICPR 2022 | gate選択latent → transform → task網 | 主task共同学習と後続taskの追加を分離 |
| [Semantic Segmentation in Learned Compressed Domain](https://arxiv.org/abs/2209.01355) | PCS 2022 | 固定hyperprior latent → gate → bridge → DeepLabv3 | 固定codec・teacher、trainable bridgeの明瞭な構成 |
| [Scalable Image Coding for Humans and Machines](https://arxiv.org/abs/2107.08373) | TIP 2022 | base latent → LST → 認識器後半 | 機械baseとhuman拡張層の独立bitstream化 |
| [Learned Disentangled Latent Representations](https://arxiv.org/abs/2301.04183) | DCC 2023 | 特徴＋縮小RGBでbaseを作り、YOLO特徴へ復元 | semantic baseの学習とhuman品質のtrade-off |
| [Unified Architecture Adaptation for Compressed Domain Semantic Inference](https://pmc.ncbi.nlm.nih.gov/articles/PMC10403241/) | TCSVT 2023 | 固定codec latent → subpixel adaptation → task網 | shapeの整合、RGB teacherからのfeature transfer |
| [Reconstruction-Free Image Compression for Machine Vision via Knowledge Transfer](https://doi.org/10.1145/3678471) | TOMM 2024 | 固定codec latent → mapping → modified backbone | logitと中間featureの両方を蒸留 |
| [POLC](https://arxiv.org/abs/2507.01608) | ICME 2025 | 知覚指向latent → 小型adapter → vision後半 | adapter-only学習を支えるlatent設計 |
| [Efficient Object Detection on JPEG AI Pre-Reconstruction Latents](https://ieeexplore.ieee.org/document/11630155) | ICIP 2026 | JPEG AI latent → bridge → pruned YOLOv12n | 合成省略の計算利益と精度低下を併記する例 |

### 4.3 INRと動画の近接研究

| 論文 | 年 | 経路と位置づけ | 直接型かどうか |
|---|---|---|---|
| [Human–Machine Collaborative Image Compression Method Based on Implicit Neural Representations](https://doi.org/10.1109/JETCAS.2024.3386639) | JETCAS 2024 | COOL-CHIC/EQLINR系のfirst latent → SEN → 認識器入力 | human RGB復元は不要。ただし中間層への注入ではなく3-channel入力 |
| [VNVC](https://arxiv.org/abs/2306.10681) | TPAMI 2024 | 部分復号した動画特徴 → task network | 直接型。共有符号器を固定後にtask網を学習 |
| [FLAVC](https://openaccess.thecvf.com/content/CVPR2025/papers/Zhang_FLAVC_Learned_Video_Compression_with_Feature_Level_Attention_CVPR_2025_paper.pdf) | CVPR 2025 | 特徴attentionを用いたRGB映像符号化 | 内部特徴の利用だけでは機械直接型とはしない |
| [STDLC](https://www.cmsworkshops.com/ICASSP2026/view_paper.php?PaperNum=9756) | ICASSP 2026 | 機械向け映像符号化の関連文献。具体的経路は未確認 | 公式プログラムで題名・著者・発表を確認。直接型の確定例とはしない |
| [NVP](https://openaccess.thecvf.com/content/CVPR2026/papers/Lee_Neural-Centric_Video_Processing_Pipeline_for_Unified_Multi-Task_Inference_CVPR_2026_paper.pdf) | CVPR 2026 | NeRV/HNeRV中間特徴 → adapter → 固定backbone内部 | 直接型。C3のadapter構想に非常に近い |
| [Objects Disentangled Implicit Neural Representation for Image Coding](https://doi.org/10.1145/3746278.3759393) | McGE 2025 | 物体／背景latent → RGB → 固定認識器 | cascade側の比較対象。task lossは用いない |

## 5 画像codecのlatentを使うときの重要な設計

### 5.1 Bridgeは形を合わせるだけの部品ではない

Torfason以降の多くの研究は、codec latentと認識器の特徴のshape・分布の違いをadapterで吸収する。Duanらは、認識器前半のdownsamplingを減らし、入力にsubpixel upsamplingを置くことで、RGB teacherと後続特徴のshapeを整合させる。codecは固定し、task networkを学習する。teacher特徴とのMSEが有効な条件を示すが、ImageNet200のablation結果をImageNet1Kの本結果と混同してはいけない。また、計算量比較はentropy decodingを含まない。[Duan, Ma and Zhu, TCSVT 2023, Fig. 2–3, §IV–VI](https://pmc.ncbi.nlm.nih.gov/articles/PMC10403241/)

Tuらは固定CompressAI codecのlatentからmapping blockを経てmodified backboneへ接続し、分類の正解label、teacher/studentのlogit KL、C5 feature L2を併用する。instance segmentationではFPNの多尺度特徴を転移する。単なる入力チャネル変換ではなく、既存認識器が必要とする意味表現へ近づける学習である。RGB baselineをscratchから学習するか、pretrained重みで初期化するかで結論が変わる例もあり、公平な初期化条件が重要になる。[Tu et al., TOMM 2024, §3.2–3.3, Tables 3–4](https://doi.org/10.1145/3678471)

POLCは、rate・MSEだけでなくLPIPS・GANで知覚指向のcodecを学習した後、codecを固定して小型adapterを学ぶ。linear projection、pixel shuffle、ResBlockでvision stemを迂回し、task lossとstage feature distillationを使う。adapterだけのfine-tuneが有効という結果は魅力的だが、そこにはcodecの事前学習が先に存在する。式にrate項が書かれていても、codecを固定したadapter-only段階では、その項によって符号量を変える自由度はない。「universal」も、一つのadapter重みが全taskへ無学習で使える意味ではない。[POLC, ICME 2025, Fig. 2–3, Eq. 4–7](https://arxiv.org/html/2507.01608)、[公式code](https://github.com/NJUVISION/POLC)

C3でこの発想を使う場合、特に難しいのは、共有encoderの出すlatentではないことである。ある画像のgridが担う役割と別画像のgridの役割は、各画像で一緒にfitされたsynthesis重みに依存する。空間サイズを揃えたというだけでは、共有adapterが同じ意味として読めるとは限らない。

### 5.2 RGB合成を省いても通信量が減るとは限らない

固定された画像codecのbitstreamをそのまま使い、RGB synthesisだけをbridgeに置き換えるなら、主な利益は受信側計算の削減である。bitstreamを変更していない限り、画像通信量が減ったとは言えない。逆に、受信側計算を増やしてでもtaskに不要な情報を捨てれば、通信量を減らせることがある。この二つは別の軸で評価する。

Choi・Bajićの**Scalable Image Coding for Humans and Machines**は、baseとenhancementを分け、machineはbaseからlatent-space transformを通じてYOLO等の中間特徴を得る。humanは追加のenhancementも使ってRGBを復元する。TIP版では別bitstream化によりmachine側でenhancementのentropy decodingを不要にする。単にlatent channelをmaskしただけで、他のchannelを復号するためのcontext依存が残るなら、同じ性質は得られない。[Choi and Bajić, TIP 2022, Fig. 4–6, §III-B–F](https://arxiv.org/html/2107.08373)

Özyılkanらは、base encoderへYOLOv3の初期特徴と縮小RGBを入れ、compactなtask baseとhuman enhancementを学習する。これはsemantic baseの作り方の参考になる一方、送信側で教師特徴を計算する費用を持つ。さらに、compact baseを優先するとenhancementがsuboptimalになるというtrade-offを本文で認めている。機械・人間の両方で常に得をするとは限らない。[Özyılkan et al., DCC 2023, Fig. 2–3, §3–5](https://arxiv.org/html/2301.04183)

JPEG AIのpre-reconstruction latentからYOLOv12nへ接続するICIP 2026論文は、合成を省く効果と精度のtrade-offを明確に示す。bridge-only学習後にdetectorもfine-tuneするが、COCO val2017では各評価rateでRGB baselineより精度が低い。latent上でRGB augmentationを完全に再現できないことも要因として議論する。JPEG AIの大きいsynthesisを省いたGMACs削減を、そのまま小型decoderのC3へ期待するのは適切ではない。[ICIP 2026, §3–4](https://doi.org/10.1109/icip61757.2026.11630155)

## 6 早稲田大学 甲藤研究室に関連する研究

### 6.1 張淳以外ではJinming Liuの系列が特に近い

今回の目的に直接役立つのは、**Jinming Liu・Heming Sun・Jiro Kattoによる2021–2022年のcompressed-domain inference系列**である。早稲田大学の機関リポジトリは、Liuの2022年学位論文『Learning in Compressed Domain for Machine Vision Tasks』と、指導教員Jiro Kattoを確認できる資料である。現在の所属や在籍を推測するのではなく、当時の研究上の関係として扱う。[学位論文の公式記録](https://waseda.repo.nii.ac.jp/records/76933)

VCIP 2021の**Learning in Compressed Domain for Faster Machine Vision Tasks**は、learned codecのlatentを直接task networkへ入れ、entropy順にchannelを選択する。公表abstractでは、同精度でMACsを約3分の1、推論時間を約5分の1減らし、channel選択によって最大6.8%のbitrate削減を報告する。計算・時間・bitrateは別の比較であり、一つの総合削減率にしてはいけない。この個別conference論文の本文は今回回収できていないため、詳細な固定条件は未確定である。[出版社記録](https://ieeexplore.ieee.org/document/9675369/)

ICPR 2022の**Improving Multiple Machine Vision Tasks in the Compressed Domain**は、学習gate、feature distillation、主taskと後続taskの学習へ進む。詳細は、著者自身が当該論文に基づく章としている学位論文の第3–4章を確認した。主taskと画像RDを共同学習した後、encoderを固定してsecondary taskを学ぶ。Cityscapesのsegmentation、DUTSのforeground検出、VOCの物体検出を扱う。これはconference本文を独立に全て監査したこととは区別する。[公式書誌](https://waseda.elsevierpure.com/en/publications/improving-multiple-machine-vision-tasks-in-the-compressed-domain/)、[学位論文全文](https://waseda.repo.nii.ac.jp/record/76933/files/Rikoshuron_202209_t5120fg47.pdf)

最初に読むなら、本文確認ができ、構成も明確なPCS 2022の**Semantic Segmentation in Learned Compressed Domain**がよい。CompressAIのhyperprior codecとpretrained teacherを固定し、latentのchannel gateと、DeepLabv3の初期二層を置き換えるtransform bridgeを学習する。Gumbel-softmaxによる画像別dynamic gateと、学習画像で選ばれやすいchannelを固定するstatic selectionを比較する。損失はsegmentation cross-entropyと、teacher中間特徴のMSEである。

この方式は、元画像を一度復元してから特徴を計算するものではない。human向けの復元branchは残せるが、machine経路はそれを必要としない。C3へ参考になるのは、まずcodecとteacherを固定し、接続部分だけを学ぶ実験を独立させる点と、複雑なgateを単純channel選択と比較する点である。ただし、共有hyperprior latentで成立したchannel選択を、画像ごとに意味が変わり得るC3 gridに適用できるかは別問題になる。

論文の最大削減率には、compressed-domain baseline比、pixel-domain baseline比、16-channel設定の時間削減が混在する。これらを一つの動作点として並べず、対応する設定を読んで使うべきである。C3研究ではまず、この論文の実験の分離とbridgeの構成を借りる方が安全である。[Liu, Sun and Katto, PCS 2022, 全文](https://arxiv.org/html/2209.01355v1)

### 6.2 張淳の公開研究ではFLAVCとSTDLCを区別する

対象は、公開の研究室・共同研究者業績および公式学会プログラムで照合した**張淳／Chun Zhang**である。FLAVCはCVPR 2025の映像符号化で、feature-level attention、重なりpatch、CNN–Transformer構成を使う。ただし主評価はRGB再構成のrate–distortionであり、名称にfeatureがあっても、機械認識への特徴直接利用が実証されたことにはならない。[FLAVC本文, Fig. 3, Fig. 5, Table 1](https://openaccess.thecvf.com/content/CVPR2025/papers/Zhang_FLAVC_Learned_Video_Compression_with_Feature_Level_Attention_CVPR_2025_paper.pdf)、[公式repository](https://github.com/Z-CV-code/FLAVC)

**STDLC: Video Coding for Machines with Spatial-Temporal Decoupled Latent Composition**は、ICASSP 2026の公式プログラムで、Chun Zhang、Heming Sun、Jiro Kattoによる論文として確認できる。公式プログラム上の当時の所属は、ZhangとKattoがWaseda University、SunがYokohama National Universityであり、2026年5月5日のImage and Video Codingセッションに掲載される。[公式プログラム](https://www.cmsworkshops.com/ICASSP2026/view_paper.php?PaperNum=9756)

本版で確認した公開一次資料からは、STDLCの具体的なlatent取り出し点、認識器への接続、RGB復元の要否、学習する部分と固定する部分、損失、数値結果までは確定できない。したがって、機械向け映像符号化の関連文献として紹介するにとどめ、FLAVCと同一構成、INR-based、あるいは実証済みの特徴直接型だとは分類しない。

### 6.3 共著研究と直接型未確認の論文

Liuhong Chenらの**Fast Object Detection in HEVC Intra Compressed Domain**は、HEVCのpartition depth、intra mode、residualからimage-likeな表現を作ってYOLOへ渡す。従来codecの完全復元を省くsystems研究として関連するが、学習特徴のentropy codingではない。著者所属では筆頭著者はFudanであり、Kattoとの共著だけで研究室学生とみなさない。BDD100KのQP22ではRGBより検出精度が下がる条件も報告され、速度と精度のtrade-offを見る例になる。[EUSIPCO 2021本文](https://eurasip.org/Proceedings/Eusipco/Eusipco2021/pdfs/0000756.pdf)

**A Multi-Grid Implicit Neural Representation for Multi-View Videos**は、時間・視点・両者のgridからRGB映像を合成するPCS 2025の共著研究である。grid factorizationやparameter量子化・Huffman codingはC3との接点になるが、機械特徴直接推論の実証ではない。[MV-MGINR本文](https://arxiv.org/html/2509.16706v1)

**CGICM: CLIP-Guided Semantic Frequency Adaptation in Image Compression for Machines**（VCIP 2025）と、**Towards Multi-Task Perception for Remote Sensing Imagery via Compression and Prompt Tuning**（GRSL 2025）も調査対象に含めた。前者はFFT・wavelet・CLIP/text guidanceを用いるadapterという公式abstractを確認し、後者は公式業績で出版を確認した。ただし、今回取得できた資料だけでは機械側がRGBを通るか確定できない。題名にICMやperceptionがあるだけで、特徴直接型一覧へ昇格させない。[CGICM公式記録](https://waseda.elsevierpure.com/en/publications/cgicm-clip-guided-semantic-frequency-adaptation-in-image-compress/)、[研究室業績一覧](https://serena.katto.comm.waseda.ac.jp/~katto/wp/)

## 7 C3と最も近い先行研究

### 7.1 C3は共有encoderが特徴を出すcodecではない

COINは画像ごとに座標からRGBを出すMLPをfitし、その重みを表現として送る。COIN++は共有base networkと入力別modulationを組み合わせる。一方、COOL-CHIC／C3は、異なる解像度のsingle-channel latent grids、小型synthesis network、entropy networkを使い、画像ごとに最適化する。INRという共通名があっても、何を共有し何を送るかは異なる。[COIN](https://arxiv.org/abs/2103.03123)、[COIN++](https://arxiv.org/abs/2201.12904)、[COOL-CHIC](https://openaccess.thecvf.com/content/ICCV2023/html/Ladune_COOL-CHIC_Coordinate-based_Low_Complexity_Hierarchical_Image_Codec_ICCV_2023_paper.html)

C3のlatent gridsは、通常の認識器が出すFPNの多尺度特徴とは異なる。空間解像度は既知でも、各gridが同じsemanticな役割を持つようにdatasetで学習されたものではない。synthesisと一緒に画像ごとにfitするため、latentだけを見て意味が読めるか、synthesis重みも条件として必要かが研究課題になる。[C3, Fig. 2, Appendix A.1](https://arxiv.org/html/2312.02753v1)

公式実装のsynthesisは、pixelwise MLPを1×1 convolutionで実装し、out_channelsは設定可能でdefaultが3、出力clipはdefaultで[0,1]である。したがって特徴出力への改変は実装上考えられるが、3を256などへ替えれば完成するわけではない。特徴の負値・値域、normalization、空間解像度、多チャネルresidual層の重み・演算量、量子化後の歪み、bitrate分母を同時に見直す必要がある。[synthesis実装](https://github.com/google-deepmind/c3_neural_compression/blob/main/model/synthesis.py#L91)

また、画像実験コードではforwardがlatent gridsも返し、通常lossは画像MSE＋latent rateである。重みの量子化step探索ではsynthesis／entropy重みのbit数も加える。このため「学習lossに入っていない費用」と「最終rateで数えていない費用」を同一視してはいけない。C3付録B.3は、論文のrate曲線が確率からの推定値であり、default非adaptive設定で実bitstreamとの近さを検証したと説明する。新しいfeature codecで同じ近似精度が成立するかは再確認が必要である。[画像実験実装](https://github.com/google-deepmind/c3_neural_compression/blob/main/experiments/image.py#L77)、[C3付録](https://arxiv.org/html/2312.02753v1)

### 7.2 LiとZhangのJETCAS 2024は直接の先行研究

**Human–Machine Collaborative Image Compression Method Based on Implicit Neural Representations**は、COOL-CHICとEQLINRを基盤に、入力画像へlatentと小型codecをfitする。人間向けは全latentからMLP・local enhancement moduleを通じて画像を復元する。機械向けは最初のfull-resolution latentだけをsemantic enhancement network、SENへ通して認識器へ入力する。機械側は通常のhuman RGB復元を通らない。

ただし、SENの出力は3-channelのimage-likeなembeddingであり、Swinの入力へ渡す。Swinの中間層へ、既存の認識特徴と同じテンソルを直接注入するsplit-computingではない。この点を丁寧に区別すると、元画像を再現しない「機械入力表現」と、固定backboneの中間特徴を復元する「特徴interface」の違いが見える。[Li and Zhang, JETCAS 2024, §III-A/C, pp.201–204, Figs.1/4](https://doi.org/10.1109/JETCAS.2024.3386639)

目的は、画像MSE＋latent rate＋意味応答の損失である。LCEという記号だが、式の実体はSEN側と元画像側のSwin応答にtemperature T=2のsoftmaxを適用したKLで、式の向きはSEN側から元画像側である。通常の正解labelへのcross-entropyとは異なる。具体的にどの層のsemantic featureかは明確でなく、「確実にlogitのみ」「内部特徴は使っていない」とまでは断定できない。[同論文, §III-B, Eqs.1–5, pp.202–203](https://ieeexplore.ieee.org/document/10495030)

画像別fitの後、context、MLP、LEM、SENの重みを量子化する。機械用payloadにもcontextとSEN重みが含まれるため、SENを無料の共有adapterとみなしてはいけない。宣言されたtotal rateはlatentとnetwork parameterを含む一方、学習目的で直接penaltyを課すrateはlatentのみである。Swinは分類datasetの90%で60 epochs fine-tuneするので、codecが画像別fitでも、pipeline全体がdataset学習不要という意味にはならない。

評価はFlowers・AwA2分類だけでなく、CrowdHumanのbpp–APも含む。したがって「分類しか扱っていない」「INRで検出rate–accuracyは未検討」という説明は誤りになる。一方、画像別fit中のSwin固定条件、検出専用loss、APの詳細定義には記述の不足がある。今回確認した範囲では、同じbitstreamの別認識器への転移実験も確認できなかった。この不足を研究設計の確認点にしつつ、不存在の証拠として強い新規性を主張しないことが重要である。[同論文, §IV, pp.204–207, Figs.5–11](https://ieeexplore.ieee.org/document/10495030)

### 7.3 NVPはINR内部から固定認識器へ接続する近接例

**Neural-Centric Video Processing Pipeline for Unified Multi-Task Inference**は、NeRV／HNeRVを途中まで復号し、中間特徴をresizeと軽量adapterでtask backboneの内部表現へ合わせる。RGB生成とbackbone前半を省き、CKAで接続位置の候補を絞る。task backboneを固定して、INR固定・adapter-only学習と、INRも更新するjoint学習を検討する。feature matchingにはMSE・cosine・Huberなどを用い、jointでは再構成・タスク・特徴の損失を組み合わせる。[NVP, CVPR 2026, §4, Fig. 3](https://openaccess.thecvf.com/content/CVPR2026/papers/Lee_Neural-Centric_Video_Processing_Pipeline_for_Unified_Multi-Task_Inference_CVPR_2026_paper.pdf)

これは、「INR内部特徴→adapter→固定認識器内部」という構想に非常に近い。C3へ転用する場合も、接続点をいくつか比較し、特徴の似ている位置を探すという考え方は有用である。ただし、対象はNeRV／HNeRVの動画表現であり、C3の極小synthesisと多尺度gridで同じ利益が出ることは示していない。動画を一度fitして多数回推論するwrite-once/read-manyの利用像も、画像を一度だけ送る用途とは異なる。

主要実験はclassification、detection、action recognition、captioningであり、abstractの言及だけからsegmentationも実証済みとはしない。また、本文・補足から、adapterの未見動画への共有・転移protocolや、量子化・entropy coding・adapter・metadataを含む総bit数の算定を完全には追跡できなかった。BPPが報告されることと、全費用込みの実bitstreamを再現確認できることは別である。

この論文は重要な構成上の先行研究であり、C3での新規性を考える前に読むべきである。一方、報告された高速化をC3の期待値としてそのまま使わず、利用頻度・計測範囲・追加モデル費用を揃えて検証する必要がある。[NVP補足](https://openaccess.thecvf.com/content/CVPR2026/supplemental/Lee_Neural-Centric_Video_Processing_CVPR_2026_supplemental.pdf)

### 7.4 動画ではVNVCが学習条件を理解しやすい

VNVCは、entropy-decoded latentそのものではなく、部分復号された高次元特徴をtask inputとし、必要ならRGBも生成する。特徴を次フレームの参照にも使う。先に映像codecをrate–image-distortionで学習し、その特徴ループを固定してtask networkを学習するため、**feature-directであることと、task lossでcodecまで最適化することは別**だと理解しやすい。

同じbitstreamの復号画像経路と特徴経路を比べ、行動認識・検出・追跡・分割を扱う。C3でもまず同一stream比較を行えば、特徴直結の利益をcodec変更による利益から切り離せる。ただし、VNVCの時間予測や動き表現は静止画C3にそのまま必要ない。タスク時間表でentropy decodingを除く条件と、別の符号化時間表で含める条件も分けて読む。[Sheng et al., TPAMI 2024, Figs.1–4](https://arxiv.org/html/2306.10681v2)、[公式code](https://github.com/xhsheng-ustc/TPAMI_2024_VNVC)

### 7.5 Objects Disentangledは強い境界比較になる

Objects Disentangledは、SAM2のmaskでobject／background latentを分け、領域ごとの画素歪みを重み付けし、C3に近いCOOL-CHIC系列を画像別にfitする。認識器はfine-tuneせず評価するが、受信側はRGB画像を再構成して認識するため、今回の特徴直接型とは分ける。task／detector feature lossは目的関数に入らない。

部分復号ではbackground latentを捨てるので、通信量削減は全画像の同等画質を保つ改善ではない。Table 1の検出−56.5%、segmentation−57.1%はCool-Chic v3基準のBD-rate削減であり、精度がその割合上がったという意味でもない。特徴直接型を提案する際にも、単に背景を捨てた効果や物体へのbit配分と区別するための比較相手として重要になる。[Lin et al., McGE 2025, §3–4, Table 1](https://doi.org/10.1145/3746278.3759393)

## 8 近年の研究から追加したい視点

### 8.1 特徴誤差が小さければ十分とは限らない

DMOFCはReID・顔認証の最終ベクトルを対象に、positiveをnegativeより近づける識別損失を使う。低容量では識別関係を保つことが有効でも、高容量では純粋な識別損失が特徴MSEに劣る条件がある。したがって「task-orientedならMSEは不要」とは言えない。さらに、16–256 bytesという制御容量の実験を、学習entropy coding込みの実bpp削減と読み替えてはいけない。C3では、特徴MSEとtask/output lossを併用・比較する動機にはなるが、空間特徴全般への普遍的な効果の根拠ではない。[DMOFC, PCS 2024](https://arxiv.org/html/2405.04044v1)

LaMoFCは、DINOv2等の大型モデル特徴を扱うdataset・test conditions・baselineを提供する。静止画ではclassification、segmentation、depthの特徴を、range truncation・packing・VTMまたはHyperpriorで符号化し、固定headへ戻す。現行PDFの静止画subsetはImageNet分類500、VOC2012分割100、NYU-depth80で、分類は元モデルが正解した画像を選ぶ。したがって、baselineが全ImageNetでの精度を意味するわけではない。token×channel行列を単なる自然画像として扱うことの限界を学びつつ、小規模な選択subsetでの結果を広範な汎化の証明にしない。[LaMoFC, ICCV 2025](https://arxiv.org/pdf/2412.04307)、[codeとdata](https://github.com/chansongoal/LaMoFC)

### 8.2 重要な特徴を選ぶだけでも比較になる

Alvar・BajićのCompressive Feature Selectionは、Cityscapesの複数taskに対して、mutual informationに基づくchannel選択を調べる。hard selectionではchannelを捨て、soft selectionでは重要channelと残りを異なる方法で扱う。再学習を伴う新しいentropy modelでなくても、送る情報の選び方に改善余地がある。

ただし、50%保持時にsegmentation・disparityではnorm rankingがMIを僅かに上回る条件もある。C3でchannelやgridを選ぶなら、提案した複雑な基準だけを比較せず、uniform、norm、entropyなど単純な選択を置くべきである。また、選ばれたchannelのidentityやmaskを受信側へどう伝えるかまで含める。[Alvar and Bajić, ICME workshop 2024](https://arxiv.org/html/2405.09077v1)

### 8.3 複数taskでは相互依存も圧縮対象になる

ATDCは、taskごとの特徴を独立に送る代わりに、画像ごとにtask間の予測可能性を推定し、疎な有向非巡回graphを作る。root特徴を符号化し、子特徴は**復号済み親特徴**から予測した残差を符号化する。encoderだけが知る未圧縮親特徴を使わない点が重要である。

Taskonomy Tinyでsegmentation、keypoints、texture edges、normals、depth、RGB再構成を扱う。これは単に同じtensorを複数headで共有する方式より踏み込んだ、task間冗長性の利用である。一方、親の復号待ちによる遅延やgraph signalingが加わる。新task追加時の固定条件を初期学習全体へ一般化したり、graphの費用を未確認のまま無料にしたりしない。C3では最初の単一task実験が成立した後に検討する拡張であり、初回から組み込む必要はない。[ATDC, CVPR 2026](https://openaccess.thecvf.com/content/CVPR2026/papers/Huang_Discovering_Adaptive_Task_Dependencies_for_Efficient_Multi-Task_Representation_Compression_CVPR_2026_paper.pdf)

## 9 C3で立てられる三つの研究候補

以下は文献を踏まえた未検証の提案である。まず一つのtaskと固定された認識器interfaceを選び、何を変えた効果なのか識別できる形にする。特徴型という要件だけでC3を継続することが自動的に正解になるわけではなく、C3の画像別適応が費用に見合うかを問いに含める。

### 候補A 固定認識器の特徴をC3型codecで圧縮する

**経路**：画像 → 固定frontend → 特徴F → C3型の画像別fit → bitstream → 復号特徴F̂ → 固定tail。

**問い**：同じ特徴interfaceと同じtailを保ったとき、画像別に適応するlatent・entropy・synthesis設計は、単純量子化や共有feature codecより、総bytesとtask精度の関係を改善するか。

初期段階は特徴MSE＋rateで成立性を見て、その後にchannel正規化した誤差、固定tail内の複数層誤差、教師出力蒸留を比較する。テスト画像のGT labelを最適化に使う必要はない。特徴出力はRGBの[0,1]制限を外すなど、値域に合った設計にする。高チャネルをそのまま出力すると重み数と演算量が増えるため、低rank表現やgroupingも候補になるが、それ自体の効果は別ablationにする。

**近い先行研究**はChoi／Cohenの固定特徴圧縮、Entropic Student、Omni-ICMである。INR一般の機械利用にはJETCAS・NVPがある。従って「特徴を圧縮する」「特徴MSEを使う」「INRを使う」だけは新規性にならない。C3由来の画像別最適化が、固定interfaceで有効であることを示すのが研究の中心になる。

**成功・失敗の見方**：特徴MSEだけでなく、実bitstream込みの同task精度でbytesが減るかを判定する。改善がdecoder重みの増加で消える、あるいはfit時間が用途に合わないなら、その条件では不利だという結論も有益である。三候補の中では、入力・出力の意味が最も定義しやすく、最初の実験として整理しやすい。

### 候補B C3表現を共有adapterで固定認識器へつなぐ

**経路**：通常C3の受信側latentまたはsynthesis中間activation → 共有adapter → 固定認識器の中間特徴 → 固定tail。

**問い**：独立にfitされた画像ごとのC3表現から、同じadapterが未見画像でも意味の揃った特徴を取り出せるか。元のRGB decoderを省く利益が、adapterの演算・共有学習費用を上回るか。

最初はcodecを変更しない同一bitstream比較を行う。adapterの学習画像と評価画像を分け、画像、rate、optimization seedが変わったときの劣化を見る。latent gridsだけを入力する場合と、synthesis中間activationやcodec parameterを条件にする場合も別方式として比較する。後者はより多くの情報や計算を使うため、同じadapterと一括りにしない。

共有adapterで整合しない場合、teacher-alignedな特徴目標をC3の画像別fitへ加える案が考えられる。この段階はcodec不変の実験とは別である。共有adapterをdatasetで学習すれば、元C3の「各画像だけでcodecをfitする」という条件も変わる。shared base＋画像別modulationというCOIN++の発想は参考になるが、別のhybrid codecとして扱う。

**近い先行研究**はLiu PCS 2022、Duan、Tu、POLC、VNVC、特にNVPである。「INRからadapterで既存認識器へ接続する」ことは既に先行例がある。差分候補は、C3の画像別多尺度表現について、共有adapterの未見画像・rate・seedへの汎化を明示的に検証し、全bit・全計算のtrade-offを示す点になる。

### 候補C 画像別adapterを量子化して一緒に送る

**経路**：C3受信側表現 → 画像別にfit・量子化した小型adapter → 固定認識器の中間特徴。

**問い**：画像別adapterで表現のずれに対応した際、追加重みを送っても、特徴直接経路の総bit数・task精度・推論費用に利益が残るか。

共有adapterより画像固有の表現に適応しやすいが、低rate・小画像ではadapter重みが支配的になり得る。量子化前のadapterで精度を測り、bit数だけ量子化後として計算してはいけない。受信側で実際に復元した重みを用いてtaskを評価し、重みの量子化step選択にもfeature/task distortionを反映する。task-only streamで不要なsynthesis重みを省く場合は、残るdecoder依存関係を確認した上で省く。

**近い先行研究**は画像別SENを送るJETCASと、INR内部adapterのNVPである。固定認識器の内部interface、adapter overhead、実符号化・量子化後評価を揃えることが差分候補であり、「画像ごとにadapterをfitする」だけで未踏とは言えない。

AとBは別の問題を解く。Aでは符号化する特徴の意味が固定され、Bでは既存C3表現から意味を読み出せるか自体が課題になる。CはBの整合問題を画像別適応で解く代わりにbitを支払う。まずAまたはBの一方を主軸に決め、Cを対照とする方が、全てを一つの複雑な提案へ混ぜるより結果を解釈しやすい。

## 10 実験の前に固定したい評価条件

### 10.1 Bitの会計

主指標は総伝送bytesと、元画像のpixel数を分母にしたbppにする。特徴codec内の比較にはbits/feature-element、BPFPなどを補助として使えるが、分母を変えた数値同士を比較しない。

総bitには、必要なlatent、entropy model重み、synthesis／feature decoder重み、画像別adapter、shape、normalization統計、量子化step、layer／channel選択情報、header等を含める。datasetで共有するteacher・tail・adapterは、事前配布済みという仮定とモデル容量を別に明示する。共有重みと画像別重みを混ぜず、何枚に償却したかも必要に応じて示す。

学習時の推定rate、量子化後の推定rate、entropy coderが出す実file sizeを区別する。C3原著の推定と実測の近さを、新しいfeature分布や大きいadapterへ無条件に引き継がない。machine-onlyで不要なbranchを外せるなら、それは別stream構成として報告し、full human streamとの違いを明記する。

### 10.2 精度と学習条件

- 同一画像、resize、crop、backbone、checkpoint、split point、task metricを揃える。
- 固定tail、tail fine-tune、codec共同学習を別結果として報告する。
- 共有adapterのdataset学習、テスト画像ごとのcodec fit、教師の推論を分ける。
- テスト画像のGT labelは通常運用では使わない。教師特徴・教師予測を使うなら、そのモデルと計算費用を記載する。
- 接続層・損失重み・量子化設定の選択はvalidationで行い、testの正解やtask scoreを見て調整しない。画像別fitで利用する元画像の教師特徴は、test正解labelとは区別する。
- feature MSEだけでなく検出AP、segmentation mIoUなど実task性能まで測る。APのIoU範囲や評価subsetを明記する。
- FPNやskip connectionを横切るsplitでは、後段が必要とする全tensorを特定する。単一tensorだけを数えて他の特徴を無償で受信したことにしない。
- 一つのtaskのhead変更、別backbone、別dataset、未知taskへの転移を区別する。「複数taskを個別に学習した」ことを「同一bitstreamの無適応汎化」と呼ばない。

### 10.3 時間と計算量

送信側はfrontend＋画像別fit＋量子化・entropy encodingを数える。受信側はentropy decoding＋grid展開／部分synthesis＋adapter＋残りの認識器を数える。RGB synthesisの削減量だけをpipeline全体の高速化としない。

C3は小型decoderが長所で、入力ごとの最適化は重い。共有codecのencoder parameter数だけでは、この非対称性を表せない。encode wall-clock、最適化step、GPU時間、decode latency、task inference latencyを分ける。動画の繰返し推論なら何回利用すると前処理費用を回収できるか、静止画一回利用ならその条件で有用かを考える。[SC2 Benchmarkの評価思想](https://openreview.net/pdf?id=p28wv4G65d)

### 10.4 最小限の比較セット

1. 非圧縮画像と非圧縮特徴の基準性能参照。
2. 通常C3のRGB復元 → 同じ認識器というcascade。
3. 同じ固定特徴に対する単純量子化＋entropy coding。
4. 可能ならCohen型または標準の共有feature codec。
5. 提案するC3型特徴codec、またはC3＋adapter。
6. pixel／feature MSE、正規化feature誤差、教師出力蒸留のablation。
7. 共有adapterと画像別adapter、量子化前後、追加重み込みの比較。

これは全てを一度に実装する指示ではない。最初の小規模な成立性確認で、どの方式に進む価値があるか判断するための設計案である。JETCAS・NVPは重要な比較対象だが、公開codeや仕様を揃えられない場合、再現したことにせず、何を忠実に再現できたかを明示する。

## 11 調査範囲と残った確認事項

本公開資料版は、2026年10月24日までに確認できた出版論文・公開学位論文・公式書誌・公式実装を対象とし、中間特徴圧縮、画像latent直接推論、human/machine scalable coding、INR、動画、甲藤研究室関連研究の各系統を横断した。CVF・ECVAの公開本文、arXiv、出版社／機関リポジトリ、公式repositoryを優先し、利用できる本文では構成・損失・固定条件・評価を照合した。27件を比較地図へまとめ、周辺のINR基礎・研究室共著・追加候補も記載した。27件には、構成を確定していない書誌確認段階のSTDLCも含む。これは主要系統を広く調べた整理であり、世界中の全論文を漏れなく含むという保証ではない。

論文の本文確認と、実装を再現して結果を検証することは別である。本調査では圧縮・認識実験を実行していない。公式codeを確認できたのはEntropic Student、SC2、POLC、VNVC、LaMoFC、C3などで、FLAVCもrepositoryは確認したが、完全な再現実装が利用できるとは断定しない。他の「code未確認」は、存在しないという意味ではない。

残る主な制約は以下の通りである。

- LiuのVCIP 2021は出版社abstract、ICPR 2022の詳細は学位論文を根拠とした。PCS 2022は原論文本文を確認した。
- STDLCは公式プログラムで題名・著者・発表を確認したが、本公開資料版では具体的な構成、RGB復元の要否、loss・freeze・数値を確定していない。CGICMとremote-sensing prompt研究も機械側経路を確定していない。
- C3付録の詳細確認はarXiv v1を含み、最終CVPR版との全差分監査は行っていない。
- JETCASは全文を確認したが、認識器固定条件や検出評価の詳細は本文で不足する。NVPは本文・補足を確認したが、総bit会計・adapter汎化protocolに未確定点が残る。
- NVPでは本文と補足でdataset本数や一部評価説明に相違があり、時間表には構成時間の合計とtotalが合わない箇所も確認された。そのため本レポートでは該当の高速化率を転記していない。研究の発想とは別に、数値を再利用する際の確認事項である。
- LaMoFCは旧HTML版と現行PDFでsample数・値・repository名に相違があるため、ここでは現行PDFとLaMoFC repositoryを優先した。
- 参照先は出版論文のDOI・出版社ページ、公式公開本文、公開学位論文、公式業績・プログラム、公式repositoryに限定した。書誌のみ確認できた研究は、その範囲を超えて構成や結果を推測していない。

TransTICは復号RGBをtask networkへ渡し、Adapt-ICMHもH×W×3の復元画像を認識器へ渡す構成である。task-awareな最適化を使っていても、今回の特徴直接型とは分ける。[TransTIC, Fig. 2, §3.1–3.2](https://arxiv.org/html/2306.05085v2)、[Adapt-ICMH, Fig. 3, §3.2](https://www.ecva.net/papers/eccv_2024/papers_ECCV/papers/06823.pdf)

同様に、FLAVC・MV-MGINRのように内部にfeatureやgridを持つRGB codecも、それだけで直接型としない。分類名よりも、**受信側のどこで何を復号し、認識器へ何を渡すか**を図で追うのが確実である。

## 12 最初に決めると研究が進むこと

まず、機械側に必要なのは「既存認識器の中間特徴を復元すること」なのか、「C3表現を使ってタスクを解くこと」なのかを決めたい。前者なら候補A、後者なら候補B・Cが主軸になる。これは損失の細部より先に決めるべき、問題設定の違いである。

その判断のためには、基礎のChoi 2018・Torfason 2018、設計参考のEntropic Student・Liu PCS 2022、C3に最も近いJETCAS・NVPを優先するとよい。そこまで読めば、特徴圧縮の基本、研究室の既存の流れ、C3を使う場合に避けられない先行研究がつながる。

現時点で最も明瞭な問いは、**画像別に適応するC3型表現を、どの特徴interface・整合・量子化設計で使えば、全伝送bitと全推論費用を抑えながら、固定認識器の性能を保てるか**である。単にRGBの代わりに特徴を出すことを目標にするより、何が既存研究で解かれ、C3特有の何が残るかをこの問いに沿って確かめる方が、研究の差分を説明しやすい。

## 内部補足資料

研究室内資料に基づく補足は公開本文から分離しています。[内部補足資料（Box・閲覧権限が必要）](https://waseda.app.box.com/file/2547200643904)。リンク先の既存アクセス権は変更していません。
