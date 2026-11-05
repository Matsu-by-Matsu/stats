#!/usr/bin/env python3
"""毎日1回、GitHub Actionsから実行される。AI・APIキー・課金は一切使わない。
出力: data/market.json (金利・株価・為替・商品の日次終値)
      data/news.json   (ニュース。媒体名・URL付き)
      data/status.json (取得成否の記録。サイト下部に表示)
取得できなかった指標は「前回の値を保持」し、推定値は絶対に入れない。
"""
import json, os, re, sys, time, urllib.request, urllib.parse, datetime as dt
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, 'data')
UA = {'User-Agent': 'Mozilla/5.0 (stats-dashboard daily updater)'}
NOW = dt.datetime.now(dt.timezone.utc)

# ---- 取得対象(index.htmlのLIVE_SYMBOLSと同じ) ----
SYMBOLS = {
  'stocks': {'nikkei': '^N225', 'topix': '^TOPX', 'reit_idx': '1343.T'},
  'rates': {'us_3m': '^IRX', 'us_5y': '^FVX', 'us_10y': '^TNX', 'us_30y': '^TYX'},
  'world_stocks': {'sp500': '^GSPC', 'nasdaq': '^IXIC', 'nikkei': '^N225', 'dax': '^GDAXI', 'ftse': '^FTSE',
                   'hangseng': '^HSI', 'dow': '^DJI', 'russell2000': '^RUT', 'eurostoxx50': '^STOXX50E',
                   'cac40': '^FCHI', 'shanghai': '000001.SS', 'sensex': '^BSESN'},
  'world_fx': {'usdjpy': 'JPY=X', 'eurusd': 'EURUSD=X', 'gbpusd': 'GBPUSD=X', 'usdcny': 'CNY=X', 'usdkrw': 'KRW=X',
               'usdinr': 'INR=X', 'audusd': 'AUDUSD=X', 'usdcad': 'CAD=X', 'usdchf': 'CHF=X', 'usdmxn': 'MXN=X',
               'usdbrl': 'BRL=X', 'usdtry': 'TRY=X'},
  'world_commodities': {'gold': 'GC=F', 'wti': 'CL=F', 'brent': 'BZ=F', 'silver': 'SI=F', 'copper': 'HG=F',
                        'natgas': 'NG=F', 'platinum': 'PL=F', 'corn': 'ZC=F', 'wheat': 'ZW=F', 'soybeans': 'ZS=F'},
}
MAX_JUMP = 0.35        # 前日比35%超の変化は異常値として採用しない
DAILY_DAYS = 400       # 直近400日は日次、それ以前は月末のみ(ファイルを軽くするため)

def get(url, timeout=30, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            last = e
            time.sleep(2 * (i + 1))
    raise last

def load(name, default):
    try:
        with open(os.path.join(DATA, name), encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default

def save(name, obj):
    with open(os.path.join(DATA, name), 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, separators=(',', ':'))

def compress(daily):
    """直近DAILY_DAYSは日次、それ以前は各月の最終営業日のみ残す。"""
    keys = sorted(daily)
    cutoff = (NOW - dt.timedelta(days=DAILY_DAYS)).strftime('%Y-%m-%d')
    out, last_of_month = {}, {}
    for k in keys:
        if k >= cutoff:
            out[k] = daily[k]
        else:
            last_of_month[k[:7]] = k
    for m, k in last_of_month.items():
        out[k] = daily[k]
    return dict(sorted(out.items()))

def validate(daily, label):
    """明らかな異常(0以下・非数・前日比35%超)を除く。除いた件数も返す。"""
    clean, bad, prev = {}, 0, None
    for k in sorted(daily):
        v = daily[k]
        if not isinstance(v, (int, float)) or v != v or v <= 0:
            if label.startswith('rates') and isinstance(v, (int, float)) and v == v and v >= 0:
                pass  # 金利は0%近傍もありうる
            else:
                bad += 1; continue
        if prev is not None and prev > 0 and abs(v / prev - 1) > MAX_JUMP and not label.startswith('rates'):
            bad += 1; continue
        clean[k] = v; prev = v
    return clean, bad

def fetch_yahoo(sym, rng='10y'):
    q = urllib.parse.quote(sym, safe='')
    url = f'https://query1.finance.yahoo.com/v8/finance/chart/{q}?range={rng}&interval=1d&events=history'
    j = json.loads(get(url))
    res = j['chart']['result'][0]
    ts = res['timestamp']; closes = res['indicators']['quote'][0]['close']
    tz = res['meta'].get('exchangeTimezoneName') or 'UTC'
    try:
        from zoneinfo import ZoneInfo
        z = ZoneInfo(tz)
    except Exception:
        z = dt.timezone.utc
    out = {}
    for t, c in zip(ts, closes):
        if c is None: continue
        d = dt.datetime.fromtimestamp(t, z).strftime('%Y-%m-%d')
        out[d] = round(float(c), 4)
    return out

def fetch_jgb10y():
    """財務省 国債金利情報(公式CSV)。10年物の日次利回り。"""
    urls = ['https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/historical/jgbcme_all.csv',
            'https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/jgbcm.csv']
    last = None
    for u in urls:
        try:
            text = get(u, timeout=60).decode('utf-8', 'ignore')
            lines = [l for l in text.splitlines() if l.strip()]
            hi = next(i for i, l in enumerate(lines) if re.match(r'\s*Date\s*,', l))
            cols = [c.strip() for c in lines[hi].split(',')]
            ci = cols.index('10Y')
            out = {}
            for l in lines[hi + 1:]:
                p = [c.strip() for c in l.split(',')]
                if len(p) <= ci: continue
                m = re.match(r'(\d{4})[/-](\d{1,2})[/-](\d{1,2})', p[0])
                if not m: continue
                try: v = float(p[ci])
                except ValueError: continue   # '-' などは欠損
                out['%s-%02d-%02d' % (m.group(1), int(m.group(2)), int(m.group(3)))] = v
            if len(out) > 100:
                return out
            last = RuntimeError('too few rows')
        except Exception as e:
            last = e
    raise last

def update_market():
    old = load('market.json', {'series': {}})
    series = old.get('series', {})
    errors = []
    def put(cat, key, symbol, daily, source):
        daily, bad = validate(daily, cat)
        if len(daily) < 20:
            raise RuntimeError('有効データ不足(%d件)' % len(daily))
        latest = max(daily)
        series.setdefault(cat, {})[key] = {
            'symbol': symbol, 'source': source, 'latest': latest, 'latestValue': daily[latest],
            'fetchedAt': NOW.strftime('%Y-%m-%dT%H:%MZ'), 'rejected': bad, 'daily': compress(daily)}
    for cat, m in SYMBOLS.items():
        for key, sym in m.items():
            try:
                put(cat, key, sym, fetch_yahoo(sym), 'Yahoo Finance')
            except Exception as e:
                errors.append({'id': f'{cat}.{key}', 'symbol': sym, 'error': str(e)[:160]})
            time.sleep(0.4)
    try:
        put('rates', 'jp_10y', 'JGB10Y', fetch_jgb10y(), '財務省 国債金利情報')
    except Exception as e:
        errors.append({'id': 'rates.jp_10y', 'symbol': 'MOF', 'error': str(e)[:160]})
    save('market.json', {'generatedAt': NOW.strftime('%Y-%m-%dT%H:%MZ'), 'series': series})
    return errors

# マネーフロー画面用: 2006年からの日次全期間(3系列のみ)
FLOW_SYMBOLS = ['^GSPC', '^IXIC', 'JPY=X']
FLOW_START = '2006-01-01'
def update_flow():
    old = load('flow.json', {'series': {}}).get('series', {})
    errors = []
    for sym in FLOW_SYMBOLS:
        try:
            d = {k: v for k, v in fetch_yahoo(sym, 'max').items() if k >= FLOW_START}
            d, bad = validate(d, 'flow')
            if len(d) < 1000:
                raise RuntimeError('有効データ不足(%d件)' % len(d))
            old[sym] = {k: round(v, 3) for k, v in d.items()}
        except Exception as e:
            errors.append({'id': 'flow.' + sym, 'symbol': sym, 'error': str(e)[:160]})
        time.sleep(0.4)
    save('flow.json', {'generatedAt': NOW.strftime('%Y-%m-%dT%H:%MZ'), 'series': old})
    return errors

# ---- ニュース ----
def gn(q, hl, gl, ceid):
    return 'https://news.google.com/rss/search?q=%s&hl=%s&gl=%s&ceid=%s' % (urllib.parse.quote(q), hl, gl, ceid)
JA = ('ja', 'JP', 'JP:ja'); EN = ('en-US', 'US', 'US:en'); AU = ('en-AU', 'AU', 'AU:en'); KO = ('ko', 'KR', 'KR:ko')
def F(q, loc, cat): return {'url': gn(q, *loc), 'category': cat}
REGIONS = {
 'jp': [F('site:nikkei.com when:2d', JA, '経済・金融'), F('site:nikkei.com 市場 when:2d', JA, 'マーケット'),
        F('site:nikkei.com 不動産 when:7d', JA, '不動産・REIT'), F('site:nikkei.com 政治 when:2d', JA, '政治・外交'),
        F('日本経済 when:1d', JA, '経済・金融'), F('不動産 地価 REIT when:3d', JA, '不動産・REIT'),
        F('日経平均 為替 when:1d', JA, 'マーケット'), F('日銀 金利 when:2d', JA, 'マーケット'),
        F('site:nhk.or.jp when:1d', JA, '政治・外交'), F('site:reuters.com 日本 when:2d', JA, '経済・金融'),
        F('site:bloomberg.co.jp when:2d', JA, 'マーケット'), F('エネルギー 電力 原油 when:2d', JA, 'エネルギー'),
        F('半導体 AI テクノロジー when:2d', JA, 'テック')],
 'us': [F('US economy markets when:1d', EN, '経済・金融'), F('Federal Reserve interest rate when:2d', EN, 'マーケット'),
        F('Wall Street stocks when:1d', EN, 'マーケット'), F('US politics policy when:1d', EN, '政治・外交'),
        F('US real estate mortgage when:3d', EN, '不動産・REIT'), F('oil energy when:2d', EN, 'エネルギー'),
        F('AI technology when:2d', EN, 'テック')],
 'cn': [F('China economy when:2d', EN, '経済・金融'), F('China policy Beijing when:2d', EN, '政治・外交'),
        F('China property market when:7d', EN, '不動産・REIT'), F('China stocks yuan when:2d', EN, 'マーケット')],
 'eu': [F('Europe economy ECB when:2d', EN, '経済・金融'), F('European Union policy when:2d', EN, '政治・外交'),
        F('euro DAX stocks when:2d', EN, 'マーケット'), F('Europe energy gas when:3d', EN, 'エネルギー')],
 'au': [F('Australia economy RBA when:2d', AU, '経済・金融'), F('Australia property housing when:7d', AU, '不動産・REIT'),
        F('Australia politics when:2d', AU, '政治・外交')],
 'kr': [F('한국 경제 when:2d', KO, '経済・金融'), F('코스피 원달러 when:2d', KO, 'マーケット'),
        F('한국 부동산 when:7d', KO, '不動産・REIT')],
 'th': [F('Thailand economy when:3d', EN, '経済・金融'), F('Thailand baht SET when:3d', EN, 'マーケット'),
        F('Thailand politics when:3d', EN, '政治・外交')],
 'global': [F('global economy markets when:1d', EN, 'マーケット'), F('global real estate investment when:3d', EN, '不動産・REIT'),
            F('central bank interest rate when:2d', EN, 'マーケット'), F('oil prices OPEC when:2d', EN, 'エネルギー'),
            F('geopolitics when:2d', EN, '政治・外交')],
}
PER_REGION = 60

def parse_feed(raw, cat):
    root = ET.fromstring(raw)
    out = []
    for it in root.iter('item'):
        title = (it.findtext('title') or '').strip()
        link = (it.findtext('link') or '').strip()
        src_el = it.find('source')
        source = (src_el.text or '').strip() if src_el is not None else ''
        if source and title.endswith(' - ' + source):
            title = title[:-(len(source) + 3)].strip()
        elif not source and ' - ' in title:
            title, source = title.rsplit(' - ', 1)
        try: t = parsedate_to_datetime(it.findtext('pubDate')).astimezone(dt.timezone.utc)
        except Exception: continue
        if not title or not link.startswith('http') or not source: continue
        out.append({'headline': title, 'source': source, 'url': link, 'category': cat,
                    'time': t.strftime('%Y-%m-%dT%H:%MZ')})
    return out

def update_news():
    old = load('news.json', {'regions': {}})
    regions, errors = {}, []
    for key, feeds in REGIONS.items():
        items, ok = [], 0
        for f in feeds:
            try:
                items += parse_feed(get(f['url'], timeout=30), f['category']); ok += 1
            except Exception as e:
                errors.append({'id': f'news.{key}', 'url': f['url'][:100], 'error': str(e)[:120]})
            time.sleep(0.5)
        seen, uniq = set(), []
        for n in sorted(items, key=lambda x: x['time'], reverse=True):
            sig = re.sub(r'\W+', '', n['headline'])[:40]
            if sig in seen: continue
            seen.add(sig); uniq.append(n)
        if ok == 0 or not uniq:
            regions[key] = old.get('regions', {}).get(key, [])   # 全滅時のみ前回を保持
        else:
            regions[key] = uniq[:PER_REGION]
    save('news.json', {'generatedAt': NOW.strftime('%Y-%m-%dT%H:%MZ'), 'regions': regions})
    return errors

if __name__ == '__main__':
    os.makedirs(DATA, exist_ok=True)
    me = update_market() + update_flow(); ne = update_news()
    mk = load('market.json', {}).get('series', {})
    news = load('news.json', {}).get('regions', {})
    status = {'generatedAt': NOW.strftime('%Y-%m-%dT%H:%MZ'),
              'marketSeries': sum(len(v) for v in mk.values()),
              'newsCounts': {k: len(v) for k, v in news.items()},
              'errors': (me + ne)[:60]}
    save('status.json', status)
    print(json.dumps(status, ensure_ascii=False, indent=1))
    # 市場データが1本も取れなかった場合は失敗扱いにして、Actionsに赤信号を出す
    sys.exit(1 if status['marketSeries'] == 0 else 0)
