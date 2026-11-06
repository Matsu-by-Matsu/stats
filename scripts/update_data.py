#!/usr/bin/env python3
"""毎日1回、GitHub Actionsから実行される。AI・APIキー・課金は一切使わない。
出力: data/market.json (金利・株価・為替・商品の日次終値)
      data/news.json   (ニュース。媒体名・URL付き)
      data/status.json (取得成否の記録。サイト下部に表示)
取得できなかった指標は「前回の値を保持」し、推定値は絶対に入れない。
"""
import csv, io, json, os, re, sys, time, unicodedata, urllib.request, urllib.parse, zlib, datetime as dt
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, 'data')
UA = {'User-Agent': 'Mozilla/5.0 (stats-dashboard daily updater)'}
NOW = dt.datetime.now(dt.timezone.utc)

# ---- 取得対象(index.htmlのLIVE_SYMBOLSと同じ) ----
SYMBOLS = {
  # TOPIX指数そのものはYahooにないため、連動ETF(1348)を「ETF」と明記して載せる
  # 1306と1475はYahooの過去データに10分の1になる誤りがあるので使わない
  'stocks': {'nikkei': '^N225', 'topix': '1348.T', 'reit_idx': '1343.T'},
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
STALE_DAYS = {'daily': 10, 'monthly': 100, 'quarterly': 300}   # 最新日がこの日数より古い系列は「古いまま」と報告
REVERT_DAYS = 5        # この営業日数以内に元の水準へ戻る急変はデータの誤りとして扱う
MAX_JUMP = 0.35        # 前日比35%超の変化は異常値候補(validate参照)
DAILY_DAYS = 3700      # 約10年分は日次のまま持つ。それ以前(国債の古い分)は月末のみ

DEADLINE = time.time() + 600   # 取得は最長10分。残りの取得は諦めて、取れた分を必ず公開する
HOST_LIMIT = 4                 # 同じ取得先が続けてこの回数失敗したら、今回はその取得先を飛ばす
HOST_FAILS = {}

def log(message):
    print(time.strftime('%H:%M:%S'), message, flush=True)

def get(url, timeout=30, tries=2):
    host = urllib.parse.urlsplit(url).hostname
    last = None
    for i in range(tries):
        left = DEADLINE - time.time()
        if left <= 0:
            raise RuntimeError('制限時間のため取得を中止')
        if HOST_FAILS.get(host, 0) >= HOST_LIMIT:
            raise RuntimeError(host + ' が応答しないため今回は中止')
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=min(timeout, left)) as r:
                data = r.read()
            HOST_FAILS[host] = 0
            return data
        except Exception as e:
            last = e
            code = getattr(e, 'code', None)
            if code is None or code == 429 or code >= 500:   # 404などは取得先の不調ではない
                HOST_FAILS[host] = HOST_FAILS.get(host, 0) + 1
            log('失敗 %s %s' % (url[:80], str(e)[:80]))
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
    """明らかな異常を除く。除いた件数も返す。
    - 非数は除外。0以下は金利以外で除外(日本国債などのマイナス金利は実在する)
    - 前日比35%超は、数日以内に元の水準へ戻る値と、翌日の裏付けがない最新値を除外
      (水準が本当に変わった場合は採用する。古い値を基準に以後を全部捨てないため)
    """
    is_rate = label.startswith('rates')
    vals, bad = [], 0
    for k in sorted(daily):
        v = daily[k]
        if not isinstance(v, (int, float)) or v != v or (v <= 0 and not is_rate):
            bad += 1; continue
        vals.append((k, v))
    if is_rate:
        return dict(vals), bad
    clean, prev = {}, None
    for i, (k, v) in enumerate(vals):
        if prev is not None and abs(v / prev - 1) > MAX_JUMP:
            later = vals[i + 1:i + 1 + REVERT_DAYS]
            if not later or any(abs(x / prev - 1) <= MAX_JUMP for _, x in later):
                bad += 1; continue
        clean[k] = v; prev = v
    return clean, bad

def fetch_yahoo(sym, rng='10y', start=None):
    """日次の確定終値を返す。取引中の当日分は確定前なので含めない。
    range=max は日次を指定しても月次や3か月ごとで返るため、長期は start で期間指定する。"""
    q = urllib.parse.quote(sym, safe='')
    span = f'period1={int(start.timestamp())}&period2={int(time.time())}' if start else f'range={rng}'
    url = f'https://query1.finance.yahoo.com/v8/finance/chart/{q}?{span}&interval=1d&events=history'
    j = json.loads(get(url))
    res = j['chart']['result'][0]
    meta = res['meta']
    if meta.get('dataGranularity') != '1d':
        raise RuntimeError('日次ではないデータ(%s)' % meta.get('dataGranularity'))
    ts = res['timestamp']; closes = res['indicators']['quote'][0]['close']
    tz = meta.get('exchangeTimezoneName') or 'UTC'
    try:
        from zoneinfo import ZoneInfo
        z = ZoneInfo(tz)
    except Exception:
        z = dt.timezone.utc
    reg = (meta.get('currentTradingPeriod') or {}).get('regular') or {}
    now = time.time()
    # Yahooの値は遅れて確定するので、取引終了から30分までは未確定として扱う
    open_from = reg['start'] if reg.get('start', now + 1) <= now < reg.get('end', 0) + 1800 else None
    out = {}
    for t, c in zip(ts, closes):
        if c is None: continue
        if open_from is not None and t >= open_from: continue
        d = dt.datetime.fromtimestamp(t, z).strftime('%Y-%m-%d')
        out[d] = round(float(c), 4)
    return out

ERA = {'R': 2018, 'H': 1988, 'S': 1925}   # 和暦の元年の前年
def parse_jgb(text):
    """財務省CSVの10年物を {日付: 利回り} にする。西暦(2026/10/30)と和暦(R8.11.4)の両方に対応。"""
    lines = [l for l in text.splitlines() if l.strip()]
    hi = next(i for i, l in enumerate(lines) if re.match(r'\s*(Date|基準日)\s*,', l))
    cols = [c.strip() for c in lines[hi].split(',')]
    ci = cols.index('10Y') if '10Y' in cols else cols.index('10年')
    out = {}
    for l in lines[hi + 1:]:
        p = [c.strip() for c in l.split(',')]
        if len(p) <= ci: continue
        m = re.match(r'(\d{4})[/-](\d{1,2})[/-](\d{1,2})$', p[0])
        w = re.match(r'([RHS])(\d{1,2})\.(\d{1,2})\.(\d{1,2})$', p[0])
        if m: y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        elif w: y, mo, d = ERA[w.group(1)] + int(w.group(2)), int(w.group(3)), int(w.group(4))
        else: continue
        try: v = float(p[ci])
        except ValueError: continue   # '-' などは欠損
        out['%04d-%02d-%02d' % (y, mo, d)] = v
    return out

def fetch_jgb10y():
    """財務省 国債金利情報(公式CSV)。過去分は月に1度の更新なので、当月分のCSVを重ねる。
    返り値: (日次データ, 当月分の取得に失敗した場合の理由 or None)"""
    hist = parse_jgb(get('https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/historical/jgbcme_all.csv',
                         timeout=60).decode('utf-8', 'ignore'))
    if len(hist) <= 100:
        raise RuntimeError('too few rows')
    try:
        cur = parse_jgb(get('https://www.mof.go.jp/jgbs/reference/interest_rate/jgbcm.csv',
                            timeout=60).decode('cp932', 'ignore'))
        if not cur: return hist, '当月分CSVに日次データなし'
        hist.update(cur)
        return hist, None
    except Exception as e:
        return hist, '当月分CSVの取得失敗: ' + str(e)[:120]

def fetch_effr():
    """ニューヨーク連銀の米FF実効金利(登録不要)。"""
    start = (NOW - dt.timedelta(days=DAILY_DAYS)).strftime('%Y-%m-%d')
    end = NOW.strftime('%Y-%m-%d')
    j = json.loads(get(f'https://markets.newyorkfed.org/api/rates/unsecured/effr/search.json?startDate={start}&endDate={end}&type=rate', timeout=60))
    return {r['effectiveDate']: float(r['percentRate']) for r in j['refRates'] if r.get('percentRate') is not None}

def fetch_ecb_rate():
    """欧州中央銀行の主要リファイナンス金利(登録不要)。"""
    start = (NOW - dt.timedelta(days=DAILY_DAYS)).strftime('%Y-%m-%d')
    text = get(f'https://data-api.ecb.europa.eu/service/data/FM/D.U2.EUR.4F.KR.MRR_FR.LEV?format=csvdata&startPeriod={start}', timeout=60).decode('utf-8', 'ignore')
    lines = text.splitlines()
    head = lines[0].split(',')
    day, value = head.index('TIME_PERIOD'), head.index('OBS_VALUE')
    out = {}
    for l in lines[1:]:
        p = l.split(',')
        if len(p) > value and p[value]:
            out[p[day]] = float(p[value])
    return out

def fetch_boj(db, code):
    """日本銀行 時系列統計データ検索サイトのAPI(登録不要)。日次の系列を返す。"""
    q = urllib.parse.quote(code, safe='')
    start = (NOW - dt.timedelta(days=DAILY_DAYS)).strftime('%Y%m')
    out, pos = {}, ''
    for _ in range(20):
        j = json.loads(get(f'https://www.stat-search.boj.or.jp/api/v1/getDataCode?format=json&lang=jp&db={db}&code={q}&startDate={start}{pos}'))
        v = j['RESULTSET'][0]['VALUES']
        for d, x in zip(v['SURVEY_DATES'], v['VALUES']):
            if x is not None:
                s = str(d); out['%s-%s-%s' % (s[:4], s[4:6], s[6:8])] = float(x)
        nxt = j.get('NEXTPOSITION')
        if not nxt: break
        pos = f'&startPosition={nxt}'
        time.sleep(1.2)
    time.sleep(1.2)
    return out

def month_key(y, m):
    return '%04d-%02d' % (y, m)

def yoy(index, n):
    """nか月前(年次なら12、四半期なら4本前)との比。指数は公表値と同じ小数1桁にそろえて計算する。"""
    keys = sorted(index)
    out = {}
    for i, k in enumerate(keys):
        if i < n: continue
        y, m = int(k[:4]), int(k[5:7])
        prev = month_key(y - 1, m)
        if prev in index and index[prev]:
            out[k] = round((round(index[k], 1) / round(index[prev], 1) - 1) * 100, 1)
    return out

def fetch_unemployment():
    """総務省 労働力調査の長期時系列(季節調整値)。ファイルIDは毎月同じで中身が更新される。"""
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(get('https://www.e-stat.go.jp/stat-search/file-download?statInfId=000031831358&fileKind=0', timeout=60)),
                       read_only=True, data_only=True)
    year, prev, out = None, 0, {}
    for row in wb['季節調整値'].iter_rows(min_col=1, max_col=20, values_only=True):
        a = unicodedata.normalize('NFKC', str(row[0] or ''))
        b = re.fullmatch(r'(\d+)月', unicodedata.normalize('NFKC', str(row[1] or '')).strip())
        if not b: continue
        month = int(b.group(1))
        if year is None:
            m = re.search(r'(\d+)年', a)
            if not m: continue
            year = 1925 + int(m.group(1))   # 表の先頭は昭和28年
        elif month == 1 and prev == 12:
            year += 1
        prev = month
        if isinstance(row[19], (int, float)):
            out[month_key(year, month)] = float(row[19])
    return out

def fetch_gdp():
    """内閣府 国民経済計算の四半期別GDP速報。名目(季節調整・年率)と実質(前年同期比)。"""
    site = 'https://www.esri.cao.go.jp'
    menu = None
    for y in (NOW.year, NOW.year - 1):
        try:
            page = get(f'{site}/jp/sna/data/data_list/sokuhou/files/{y}/toukei_{y}.html', timeout=60).decode('utf-8', 'ignore')
        except Exception:
            continue
        m = re.search(r'href="([^"]+)gdemenuja\.html"', page)
        if m:
            menu = site + m.group(1)
            break
    if not menu:
        raise RuntimeError('内閣府の最新表が見つからない')
    html = get(menu + 'gdemenuja.html', timeout=60).decode('utf-8', 'ignore')
    def table(prefix):
        m = re.search(r'href="(tables/' + prefix + r'\d+\.csv)"', html)
        if not m:
            raise RuntimeError(prefix + ' の表が見つからない')
        rows, year = {}, None
        for r in csv.reader(io.StringIO(get(menu + m.group(1), timeout=60).decode('cp932', 'ignore'))):
            f = re.fullmatch(r'(?:(\d{4})/)?\s*(\d{1,2})-\s*\d{1,2}\.', r[0].strip()) if r else None
            if not f or len(r) < 2: continue
            if f.group(1): year = int(f.group(1))
            try: rows[month_key(year, int(f.group(2)))] = float(r[1].replace(',', '').strip())
            except ValueError: pass
        return rows
    return {'gdp_nom': {k: round(v / 1000, 1) for k, v in table('gaku-mk').items()}, 'gdp_growth': table('ritu-jg')}

def fetch_cpi():
    """総務省 消費者物価指数(全国・月次)。指数から前年同月比を出す。新しい基準を優先。"""
    out = {'cpi_all': {}, 'cpi_food': {}, 'cpi_energy': {}}
    names = {'cpi_all': '総合', 'cpi_food': '食料', 'cpi_energy': 'エネルギー'}
    for url in ['https://www.stat.go.jp/data/cpi/2020/csv/zmi2020aa.csv',
                'https://www.stat.go.jp/data/cpi/2025/csv/zmi2025aa.csv']:
        lines = get(url, timeout=60).decode('cp932', 'ignore').splitlines()
        head = [c.strip() for c in lines[0].split(',')]
        idx = {k: {} for k in names}
        for l in lines[1:]:
            p = l.split(',')
            d = p[0].strip()
            if not re.fullmatch(r'\d{6}', d): continue
            for k, name in names.items():
                try: idx[k][d[:4] + '-' + d[4:]] = float(p[head.index(name)])
                except (ValueError, IndexError): pass
        for k in names:
            out[k].update(yoy(idx[k], 12))
    return out

def fetch_trade():
    """財務省 貿易統計の月別総額。千円を兆円へ。まだ公表のない月は0なので除く。"""
    text = get('https://www.customs.go.jp/toukei/suii/html/data/d41ma.csv', timeout=60).decode('cp932', 'ignore')
    exp, imp, bal = {}, {}, {}
    for l in text.splitlines():
        m = re.match(r'(\d{4})/(\d{2}),\s*(\d+)\s*,\s*(\d+)', l)
        if not m or int(m.group(3)) == 0: continue
        k = m.group(1) + '-' + m.group(2)
        e, i = int(m.group(3)), int(m.group(4))
        exp[k] = round(e / 1e9, 3); imp[k] = round(i / 1e9, 3); bal[k] = round((e - i) / 1e9, 3)
    return {'exports': exp, 'imports': imp, 'balance': bal}

def fetch_housing():
    """国交省 建築着工統計の時系列Excel。戸を万戸へ。"""
    import xlrd
    book = xlrd.open_workbook(file_contents=get('https://www.mlit.go.jp/sogoseisaku/jouhouka/content/jyuu_ken.xls', timeout=60))
    sh = book.sheet_by_name('jyuu')
    cols = {'starts_total': 1, 'starts_owned': 5, 'starts_rental': 9, 'starts_condo': 15, 'starts_detached': 18}
    out = {k: {} for k in cols}
    year, month = None, 0
    for r in range(sh.nrows):
        a = sh.cell_value(r, 0)
        if isinstance(a, str):
            m = re.fullmatch(r'([SHR])(\d+|元)年(\d+)月', unicodedata.normalize('NFKC', a).replace(' ', ''))
            if m:
                year = ERA[m.group(1)] + (1 if m.group(2) == '元' else int(m.group(2)))
                month = int(m.group(3))
            elif year and a.strip():
                year = None   # 表の終わり
                continue
            else:
                continue
        elif isinstance(a, float) and year and 1 <= a <= 12:
            month = int(a)
            if month == 1: year += 1
        else:
            continue
        if not year: continue
        for k, c in cols.items():
            v = sh.cell_value(r, c)
            if isinstance(v, float) and v > 0:
                out[k][month_key(year, month)] = round(v / 1e4, 2)
    return out

def fetch_inbound():
    """JNTO 訪日外客数(総数と主な国・地域)。Excelのファイル名は毎月変わるので一覧ページから探す。人を万人へ。"""
    from openpyxl import load_workbook
    html = get('https://www.jnto.go.jp/statistics/data/visitors-statistics/', timeout=60).decode('utf-8', 'ignore')
    m = re.search(r'href="([^"]+\.xlsx)"[^>]*>[^<]*国籍/月別', html)
    if not m:
        raise RuntimeError('一覧ページに月別Excelが見つからない')
    wb = load_workbook(io.BytesIO(get(urllib.parse.urljoin('https://www.jnto.go.jp/', m.group(1)), timeout=60)), read_only=True, data_only=True)
    rows = {'総数': 'inbound', '韓国': 'inbound_kr', '中国': 'inbound_cn', '台湾': 'inbound_tw', '香港': 'inbound_hk', '米国': 'inbound_us'}
    out = {k: {} for k in rows.values()}
    for name in wb.sheetnames:
        if not re.fullmatch(r'\d{4}', name): continue
        months = {}
        for row in wb[name].iter_rows(values_only=True):
            if not months:
                months = {i: int(re.match(r'(\d+)月', c.strip()).group(1)) for i, c in enumerate(row)
                          if isinstance(c, str) and re.fullmatch(r'\d+月', c.strip())}
                continue
            if row[0] in rows:
                for i, mo in months.items():
                    if i < len(row) and isinstance(row[i], (int, float)) and row[i] > 0:
                        out[rows[row[0]]][month_key(int(name), mo)] = round(row[i] / 1e4, 1)
    return out

# 年次PDFは数字が独自の文字番号で入っている(月次PDFは通常の文字)
TIBOR_GLYPHS = {0x3EDA: '.', 0x3EDB: '/'}
TIBOR_GLYPHS.update({0x3EDC + i: str(i) for i in range(10)})

def pdf_text(raw):
    """PDFの本文の文字を順につなげる。全銀協TIBORのPDF専用の簡易読み取り。"""
    body = raw.decode('latin1')
    parts = []
    for m in re.finditer(r'stream\r?\n', body):
        start = m.end()
        try:
            page = zlib.decompressobj().decompress(raw[start:body.find('endstream', start)]).decode('latin1')
        except zlib.error:
            continue
        if 'BT' not in page: continue
        for block in re.finditer(r'BT(.*?)ET', page, re.S):
            for a in re.finditer(r'<([0-9A-Fa-f]+)>|\(((?:\\.|[^\\)])*)\)', block.group(1)):
                if a.group(1) is not None:
                    h = a.group(1)
                    parts += [TIBOR_GLYPHS.get(int(h[i:i + 4], 16), '') for i in range(0, len(h), 4)]
                else:
                    parts.append(a.group(2))
    return ''.join(parts)

def parse_tibor(raw):
    """3か月物を取り出す。列は5列(1週,1か月,3か月,6か月,12か月)か、2019年春までの6列(2か月あり)。"""
    out = {}
    for m in re.finditer(r'(\d{4})/(\d{2})/(\d{2})((?:\d\.\d{5})+)', pdf_text(raw)):
        vals = re.findall(r'\d\.\d{5}', m.group(4))
        col = {5: 2, 6: 3}.get(len(vals))
        if col is not None:
            out['%s-%s-%s' % m.groups()[:3]] = float(vals[col])
    return out

def fetch_tibor3m():
    """全銀協TIBOR運営機関のPDF。過去分は初回だけ読み、以後は今年と今月の分だけ読む。"""
    base = 'https://www.jbatibor.or.jp/rate/pdf/'
    old = load('market.json', {}).get('series', {}).get('rates', {}).get('jp_tibor3m', {}).get('daily', {})
    out = dict(old)
    years = [NOW.year] if len(old) >= 1000 else range(2016, NOW.year + 1)
    for y in years:
        try:
            out.update(parse_tibor(get(f'{base}JAPANESEYEN{y}.pdf', timeout=60)))
        except Exception:
            pass   # 年初は今年のファイルがまだない。今月分で補う
        time.sleep(1)
    html = get('https://www.jbatibor.or.jp/rate/', timeout=60).decode('utf-8', 'ignore')
    m = re.search(r'JAPANESEYENTIBOR\d+\.pdf', html)
    if not m:
        raise RuntimeError('今月のレートのPDFが見つからない')
    out.update(parse_tibor(get(base + m.group(0), timeout=60)))
    return out

ESTAT_MONTH = {1: '110103', 2: '110103', 3: '110103', 4: '120406', 5: '120406', 6: '120406',
               7: '230709', 8: '230709', 9: '230709', 10: '241012', 11: '241012', 12: '241012'}
ESTAT_POP = 'page=1&layout=datalist&cycle=1&toukei=00200524&tstat=000000090001&tclass1=000001011678&tclass2val=0'
ESTAT_TRAVEL = 'page=1&layout=datalist&cycle=1&toukei=00250011&tstat=000001012480&tclass1=000001012481&tclass2=000001062142&tclass3val=0'

def estat_files(query, year, month):
    """e-Statの月別一覧から (ファイルID, 表題) を返す。"""
    html = get(f'https://www.e-stat.go.jp/stat-search/files?{query}&year={year}0&month={ESTAT_MONTH[month]}{month:02d}', timeout=60).decode('utf-8', 'ignore')
    out = []
    for part in html.split('stat-dataset_list-item')[1:]:
        m = re.search(r'statInfId=(\d+)', part)
        if m:
            out.append((m.group(1), re.sub(r'<[^>]+>', ' ', part)))
    return out

def recent_months(count):
    y, m = NOW.year, NOW.month
    out = []
    for _ in range(count):
        out.append((y, m))
        m -= 1
        if m == 0: y, m = y - 1, 12
    return out[::-1]   # 古い順。新しい確定値で上書きするため

def estat_book(file_id):
    from openpyxl import load_workbook
    raw = get(f'https://www.e-stat.go.jp/stat-search/file-download?statInfId={file_id}&fileKind=0', timeout=60)
    return load_workbook(io.BytesIO(raw), read_only=True, data_only=True)

def old_series(cat, key):
    return load('market.json', {}).get('series', {}).get(cat, {}).get(key, {}).get('daily', {})

def fetch_population():
    """総務省 人口推計(月次)。概算値と確定値が同じ表に入っているので全部読む。初回だけ過去分をさかのぼる。"""
    out = {'total_pop': dict(old_series('population', 'total_pop')), 'elderly_rate': dict(old_series('population', 'elderly_rate')),
           'working_age': dict(old_series('population', 'working_age'))}
    keys = {('総数', '人口'): ('total_pop', 0), ('65歳以上', '割合'): ('elderly_rate', 1), ('15歳~64歳', '割合'): ('working_age', 1)}
    # 古い表は千人単位。単位の違う値が混ざらないよう万人にそろえる
    out['total_pop'] = {k: (round(v / 10, 1) if v > 50000 else v) for k, v in out['total_pop'].items()}
    months = recent_months(4 if len(out['total_pop']) >= 20 else 30)
    for y, m in months:
        try:
            files = [f for f in estat_files(ESTAT_POP, y, m) if '年齢' in f[1]]
            if not files: continue
            for row in estat_book(files[0][0]).worksheets[0].iter_rows(values_only=True):
                if len(row) < 7 or row[1] != '総人口': continue
                d = re.match(r'(\d{4})年(\d+)月', str(row[2]).replace(' ', ''))
                k = keys.get((unicodedata.normalize('NFKC', str(row[3])).replace(' ', ''), row[4]))
                if d and k and isinstance(row[6], (int, float)):
                    value = float(row[6]) / (10 if str(row[5]).strip() == '千人' else 1)
                    out[k[0]][month_key(int(d.group(1)), int(d.group(2)))] = round(value, 1)
        except Exception as e:
            log('人口 %d-%d 読み取り失敗 %s' % (y, m, str(e)[:80]))
        time.sleep(0.5)
    return out

def fetch_outbound():
    """出入国在留管理庁 出入国管理統計(月次)の全国の日本人出国者数。人を万人へ。"""
    out = dict(old_series('tourism', 'outbound'))
    for y, m in recent_months(4 if len(out) >= 20 else 30):
        try:
            files = [f for f in estat_files(ESTAT_TRAVEL, y, m) if '港別' in f[1]]
            if not files: continue
            for row in estat_book(files[0][0]).worksheets[0].iter_rows(values_only=True):
                if len(row) >= 6 and row[0] == '全国' and row[1] == '総数' and isinstance(row[5], (int, float)):
                    out[month_key(y, m)] = round(row[5] / 1e4, 1)
                    break
        except Exception as e:
            log('出国者 %d-%d 読み取り失敗 %s' % (y, m, str(e)[:80]))
        time.sleep(0.5)
    return out

def era_year(text):
    m = re.search(r'(平成|令和)(\d+|元)年', unicodedata.normalize('NFKC', text).replace(' ', ''))
    if not m: return None
    return (1988 if m.group(1) == '平成' else 2018) + (1 if m.group(2) == '元' else int(m.group(2)))

def fetch_occupancy():
    """観光庁 宿泊旅行統計の推移表。全国・全施設の客室稼働率(月次)。"""
    from openpyxl import load_workbook
    html = get('https://www.mlit.go.jp/kankocho/tokei_hakusyo/shukuhakutokei.html', timeout=60).decode('utf-8', 'ignore')
    m = re.search(r'href="([^"]+\.xlsx)"[^>]*>(?:\s|<[^>]+>)*推移表', html)
    if not m:
        raise RuntimeError('宿泊旅行統計の推移表が見つからない')
    wb = load_workbook(io.BytesIO(get('https://www.mlit.go.jp' + m.group(1), timeout=120)), read_only=True, data_only=True)
    out = {}
    for name in ('旧4-2', '4-1'):
        years, months = {}, {}
        for row in wb[name].iter_rows(values_only=True):
            if not years:
                cur = None
                for i, c in enumerate(row):
                    y = era_year(c) if isinstance(c, str) else None
                    cur = y or cur
                    if cur: years[i] = cur
                continue
            if not months:
                months = {i: int(re.match(r'\d+', c.strip()).group(0)) for i, c in enumerate(row)
                          if isinstance(c, str) and re.fullmatch(r'\d+月', c.strip())}
                continue
            if row[0] == '全国' and row[1] == '計':
                for i, mo in months.items():
                    if i in years and i < len(row) and isinstance(row[i], (int, float)):
                        out[month_key(years[i], mo)] = round(float(row[i]), 1)
                break
    return out

def update_stats(put, errors):
    """公的統計(日次・月次・四半期)。1項目の失敗が他を止めないよう、取得元ごとに分ける。"""
    def many(source, freq, cat, fn):
        try:
            for key, data in fn().items():
                put(cat, key, source, data, source, freq)
        except Exception as e:
            errors.append({'id': cat, 'symbol': source, 'error': str(e)[:160]})
    def one(cat, key, symbol, source, freq, fn):
        try:
            put(cat, key, symbol, fn(), source, freq)
        except Exception as e:
            errors.append({'id': f'{cat}.{key}', 'symbol': symbol, 'error': str(e)[:160]})
    one('rates', 'jp_policy', 'STRDCLUCON', '日本銀行', 'daily', lambda: fetch_boj('FM01', 'STRDCLUCON'))
    one('rates', 'jp_basic', 'MADR1Z@D', '日本銀行', 'daily', lambda: fetch_boj('IR01', 'MADR1Z@D'))
    one('rates', 'jp_tibor3m', 'TIBOR3M', '全銀協TIBOR運営機関', 'daily', fetch_tibor3m)
    one('rates', 'us_ff', 'EFFR', 'ニューヨーク連銀', 'daily', fetch_effr)
    one('rates', 'eu_policy', 'ECB MRO', '欧州中央銀行', 'daily', fetch_ecb_rate)
    many('総務省 消費者物価指数', 'monthly', 'cpi', fetch_cpi)
    many('財務省 貿易統計', 'monthly', 'trade', fetch_trade)
    many('国交省 建築着工統計', 'monthly', 'housing', fetch_housing)
    many('日本政府観光局(JNTO)', 'monthly', 'tourism', fetch_inbound)
    one('employment', 'unemp', 'LT01-A10', '総務省 労働力調査', 'monthly', fetch_unemployment)
    many('内閣府 国民経済計算', 'quarterly', 'gdp', fetch_gdp)
    many('総務省 人口推計', 'monthly', 'population', fetch_population)
    one('tourism', 'outbound', 'MOJ', '出入国在留管理庁 出入国管理統計', 'monthly', fetch_outbound)
    one('tourism', 'hotel_occ', 'KANKO', '観光庁 宿泊旅行統計', 'monthly', fetch_occupancy)

def update_market():
    old = load('market.json', {'series': {}})
    series = old.get('series', {})
    errors = []
    def put(cat, key, symbol, daily, source, freq='daily'):
        # 公的統計は負の値(物価の下落、貿易赤字)や大きな変動も実在するので、飛び値の判定はしない
        daily, bad = validate(daily, cat if freq == 'daily' else 'rates')
        if len(daily) < 20:
            raise RuntimeError('有効データ不足(%d件)' % len(daily))
        latest = max(daily)
        series.setdefault(cat, {})[key] = {
            'symbol': symbol, 'source': source, 'latest': latest, 'latestValue': daily[latest], 'freq': freq,
            'fetchedAt': NOW.strftime('%Y-%m-%dT%H:%MZ'), 'rejected': bad, 'daily': compress(daily)}
    for cat, m in SYMBOLS.items():
        for key, sym in m.items():
            try:
                put(cat, key, sym, fetch_yahoo(sym), 'Yahoo Finance')
            except Exception as e:
                errors.append({'id': f'{cat}.{key}', 'symbol': sym, 'error': str(e)[:160]})
            time.sleep(0.4)
    try:
        jgb, warn = fetch_jgb10y()
        put('rates', 'jp_10y', 'JGB10Y', jgb, '財務省 国債金利情報')
        if warn: errors.append({'id': 'rates.jp_10y', 'symbol': 'MOF', 'error': warn})
    except Exception as e:
        errors.append({'id': 'rates.jp_10y', 'symbol': 'MOF', 'error': str(e)[:160]})
    update_stats(put, errors)
    save('market.json', {'generatedAt': NOW.strftime('%Y-%m-%dT%H:%MZ'), 'series': series})
    return errors

# マネーフロー画面用: 2006年からの日次全期間(3系列のみ)
FLOW_SYMBOLS = ['^GSPC', '^IXIC', 'JPY=X']
FLOW_START = '2006-01-01'
FLOW_START_DT = dt.datetime(2006, 1, 1, tzinfo=dt.timezone.utc)
def update_flow():
    old = load('flow.json', {'series': {}}).get('series', {})
    errors = []
    for sym in FLOW_SYMBOLS:
        try:
            d = {k: v for k, v in fetch_yahoo(sym, start=FLOW_START_DT).items() if k >= FLOW_START}
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
def F(q, loc, cat): return {'url': gn(q, *loc), 'aim': cat}   # aimは検索の狙い。記事の分類は見出しで決める
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
PER_CATEGORY = 15      # 1分類が枠を埋め尽くさないよう、まず分類ごとにこの件数まで選ぶ(残りは新しい順)
OTHER_LIMIT = 5        # 「その他」を優先して選ぶ上限
with open(os.path.join(ROOT, 'scripts', 'news_rules.json'), encoding='utf-8') as _f:
    NEWS_RULES = [(r['category'], re.compile(r['local'] + '|(?<![A-Za-z])(?:' + r['en'] + ')(?![A-Za-z])', re.I))
                  for r in json.load(_f)]

def classify(headline):
    """見出しの語で分類する。上から順に判定し、どれにも当たらなければ「その他」。"""
    text = unicodedata.normalize('NFKC', headline)
    for category, pattern in NEWS_RULES:
        if pattern.search(text):
            return category
    return 'その他'

def pick_news(items):
    """新しい順の記事から、分類が偏らないように選ぶ。"""
    picked, rest, count = [], [], {}
    for n in items:
        c = n['category']
        limit = OTHER_LIMIT if c == 'その他' else PER_CATEGORY
        if count.get(c, 0) < limit:
            count[c] = count.get(c, 0) + 1
            picked.append(n)
        else:
            rest.append(n)
    chosen = picked[:PER_REGION]
    chosen += [n for n in rest if n['category'] != 'その他'][:PER_REGION - len(chosen)]
    return sorted(chosen, key=lambda x: x['time'], reverse=True)

def parse_feed(raw):
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
        out.append({'headline': title, 'source': source, 'url': link, 'category': classify(title),
                    'time': t.strftime('%Y-%m-%dT%H:%MZ')})
    return out

def update_news():
    old = load('news.json', {'regions': {}})
    regions, errors = {}, []
    for key, feeds in REGIONS.items():
        items, ok = [], 0
        for f in feeds:
            try:
                items += parse_feed(get(f['url'], timeout=30)); ok += 1
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
            regions[key] = pick_news(uniq)
    save('news.json', {'generatedAt': NOW.strftime('%Y-%m-%dT%H:%MZ'), 'regions': regions})
    return errors

def stale_errors(series):
    """全滅時に保持した古い値を、成功に見せないための点検。"""
    def limit(s):
        return (NOW - dt.timedelta(days=STALE_DAYS.get(s.get('freq'), 10))).strftime('%Y-%m-%d')
    return [{'id': f'{c}.{k}', 'symbol': s.get('symbol'), 'error': '最新データが古いまま(%s)' % s.get('latest')}
            for c, m in series.items() for k, s in m.items() if str(s.get('latest')) < limit(s)]

WB_COUNTRIES = {'世界': 'WLD', 'インド': 'IND', '中国': 'CHN', '米国': 'USA', 'インドネシア': 'IDN', 'パキスタン': 'PAK',
                'ナイジェリア': 'NGA', 'ブラジル': 'BRA', 'バングラデシュ': 'BGD', 'ロシア': 'RUS', 'メキシコ': 'MEX', 'エチオピア': 'ETH',
                '日本': 'JPN', 'フィリピン': 'PHL', 'エジプト': 'EGY', 'ベトナム': 'VNM', 'コンゴ民主共和国': 'COD', 'トルコ': 'TUR',
                'イラン': 'IRN', 'ドイツ': 'DEU', 'タイ': 'THA', '英国': 'GBR', 'フランス': 'FRA', 'イタリア': 'ITA', 'タンザニア': 'TZA',
                '南アフリカ': 'ZAF', 'ミャンマー': 'MMR', 'ケニア': 'KEN', '韓国': 'KOR', 'コロンビア': 'COL', 'スペイン': 'ESP',
                'アルゼンチン': 'ARG', 'カナダ': 'CAN', '豪州': 'AUS', 'サウジ': 'SAU'}
WB_SERIES = {'world_population.population': ('SP.POP.TOTL', 1e6, 1), 'world_prices.inflation': ('FP.CPI.TOTL.ZG', 1, 2),
             'world_employment.unemployment': ('SL.UEM.TOTL.ZS', 1, 2), 'world_employment.income_proxy': ('NY.GDP.PCAP.KD.ZG', 1, 2),
             'world_rates.real_interest': ('FR.INR.RINR', 1, 2)}

def update_world():
    """World Bank(登録不要)の年次統計。国別のまま保存し、画面の既存データに重ねる。"""
    old = load('world.json', {}).get('series', {})
    names = {v: k for k, v in WB_COUNTRIES.items()}
    errors = []
    codes = ';'.join(names)
    for key, (ind, div, digits) in WB_SERIES.items():
        try:
            j = json.loads(get(f'https://api.worldbank.org/v2/country/{codes}/indicator/{ind}?format=json&per_page=2000&date=2005:{NOW.year}', timeout=60))
            data = {}
            for x in j[1]:
                if x.get('value') is not None and x.get('countryiso3code') in names:
                    data.setdefault(names[x['countryiso3code']], {})[x['date']] = round(x['value'] / div, digits)
            if len(data) < 10:
                raise RuntimeError('国数が少なすぎる(%d)' % len(data))
            old[key] = data
        except Exception as e:
            errors.append({'id': key, 'symbol': ind, 'error': str(e)[:160]})
        time.sleep(0.5)
    save('world.json', {'generatedAt': NOW.strftime('%Y-%m-%dT%H:%MZ'), 'series': old})
    return errors

if __name__ == '__main__':
    os.makedirs(DATA, exist_ok=True)
    log('市場データの取得開始')
    me = update_market()
    log('マネーフローの取得開始')
    me += update_flow()
    log('ニュースの取得開始')
    ne = update_news()
    log('世界統計の取得開始')
    ne += update_world()
    log('取得終了')
    mk = load('market.json', {}).get('series', {})
    news = load('news.json', {}).get('regions', {})
    status = {'generatedAt': NOW.strftime('%Y-%m-%dT%H:%MZ'),
              'marketSeries': sum(len(v) for v in mk.values()),
              'newsCounts': {k: len(v) for k, v in news.items()},
              'errors': (me + stale_errors(mk) + ne)[:60]}
    save('status.json', status)
    print(json.dumps(status, ensure_ascii=False, indent=1))
    # 市場データが1本も取れなかった場合は失敗扱いにして、Actionsに赤信号を出す
    sys.exit(1 if status['marketSeries'] == 0 else 0)
