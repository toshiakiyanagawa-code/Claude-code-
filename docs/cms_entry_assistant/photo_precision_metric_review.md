**1. 計測手法 critique**  
photographer match は「同じ iStock 作家まで到達したか」の高精度な診断には使えますが、編集者が選ぶ写真への近さそのものではありません。別作家の同等写真を false negative にします。  
body alt jaccard は lexical smoke test 程度です。日本語の同義語・上位概念・言い換えに弱く、「腰/背中/痛み/押さえる」の近さを拾えません。

**2. 0/16 photographer match の意味**  
0/16 だけで「精度壊滅」とは言えません。まず actual photographer が候補プール内に存在したか、つまり ranker の問題か retrieval ceiling の問題かを分けるべきです。  
random top-5 の期待値は slot ごとに `1 - C(N-c,5)/C(N,5) ≒ 5c/N`。`N=100, c=1` なら 16件で期待 0.8 hit、0 hit も普通に起きます。  
必ず「同じ候補プール内で top-5 をランダム抽出」した baseline と、`c>0` の upper bound を併記してください。

**3. 同じ画像なのに alt が違う問題**  
token jaccard は補助指標に落とし、主指標は semantic similarity に寄せるべきです。`text-embedding-3-small` で actual alt と candidate alt/title/keywords の cosine を取り、少数の人手ラベルで閾値調整するのが現実的です。  
可能なら最強は画像同士の CLIP/vision embedding 比較です。actual CDN 画像と候補サムネイルを比べれば、alt の言い換え問題をかなり回避できます。

**4. 精度向上のレバー**  
一番効きそうなのは、slot ごとに「抽象テーマ」ではなく「具体的に写るべき物・人物・場所・動作」と「stock で取れるか」を出す source-aware visual brief を作ることです。  
中国例は iStock stock search で無理に埋めるほど悪化します。`editorial_needed` を立てて別ソースへ回す、または iStock 候補を abstain/demote する方が効果が大きいです。

**5. サンプル数**  
20記事はゼロ近傍の異常検知には十分ですが、topic 別評価には少なすぎます。特に `中国 n=4` は判断材料になりません。  
50記事は v4 の smoke baseline としては妥当です。ただし本格的な閾値調整や topic/source 別比較には、少なくとも 100-200 slot/pair 程度ほしいです。

**次に取り組むべき 1 ステップ**  
`precision_analyzer.py` に「候補プール内 random top-5 baseline」と「retrieval ceiling: actual photographer が候補プールに存在したか」を追加してください。これで 0/16 が ranker の失敗なのか、検索到達不能なのかを最初に切り分けられます。
