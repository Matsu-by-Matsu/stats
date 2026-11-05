# 世界統計ダッシュボード

公開URL: https://matsu-by-matsu.github.io/stats/

## フォルダの見方(これだけ)
| 場所 | 役割 | 触る? |
|---|---|---|
| `index.html` | サイト本体(画面・グラフ・静的な統計データ) | 修正する時だけ |
| `data/market.json` | 金利・株価・為替・商品の日次終値(自動更新) | 触らない |
| `data/news.json` | ニュース(媒体名・URL付き、自動更新) | 触らない |
| `data/flow.json` | マネーフロー画面用の日次全期間(S&P500・NASDAQ・ドル円) | 触らない |
| `data/status.json` | 最後の取得結果と失敗の記録 | 見るだけ |
| `scripts/update_data.py` | 上の3ファイルを作るプログラム(AI不使用) | 取得元を変える時だけ |
| `.github/workflows/update-data.yml` | 毎日2回(朝7時・夕17:30 JST)に自動実行する設定 | 時刻を変える時だけ |

## 自動更新のしくみ
GitHub Actions が毎日、`scripts/update_data.py` を実行 → `data/` を更新して自動コミット → サイトが読み込む。
AI・APIキー・課金は使いません。取れなかった指標は「前回の値を保持」し、推定値は入れません。
失敗は `data/status.json` と、サイト下部の「データ状態」に表示されます。

## 手動で今すぐ更新したい時
GitHub の Actions タブ → `update-data` → `Run workflow`。

## データの出どころ
- 米国金利・株価・為替・商品: Yahoo Finance 日次終値
- 日本10年国債: 財務省 国債金利情報(公式CSV)
- ニュース: Google ニュース RSS(各記事の媒体名と元記事リンクを表示。日経は見出しのみ・本文は有料のため非表示)
- TIBOR・政策金利・各種年次統計: `index.html` 内の静的データ(日次更新ではない。画面に注記あり)
