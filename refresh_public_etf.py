"""Rebuild or extend the dashboard from public ETF disclosures (Python 3.10+)."""

import argparse
import calendar
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

ROOT = Path(__file__).resolve().parent
CHINA = timezone(timedelta(hours=8))
START = date(2024, 10, 15)
SSE = 'https://www.sse.com.cn/market/funddata/volumn/etfvolumn/'
SZSE = 'https://www.szse.cn/market/fund/volume/etf/index.html'
INDICES = [
    ('000300', '沪深300'), ('000016', '上证50'), ('399006', '创业板指'),
    ('000688', '科创50'), ('000510', '中证A500'), ('000905', '中证500'),
    ('000852', '中证1000'), ('932000', '中证2000'),
]
Q = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
HUNDRED_MILLION = Decimal('100000000')


def fetch(url, params=None, referer=None):
    if params:
        url += '?' + urlencode(params)
    request = Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Referer': referer or url})
    for wait in (0, 1, 3):
        time.sleep(wait)
        try:
            with urlopen(request, timeout=25) as response:
                return response.read()
        except OSError as exc:
            error = exc
    raise RuntimeError(f'{url}: {error}')


def cached(cache, name, request):
    if not cache:
        return request()
    path = cache / name
    if path.exists():
        return path.read_bytes()
    value = request()
    path.write_bytes(value)
    return value


def month_ranges(start, end):
    while start <= end:
        stop = min(end, start.replace(day=calendar.monthrange(start.year, start.month)[1]))
        yield start, stop
        start = stop + timedelta(days=1)


def exchange_dates(start, end):
    dates = []
    for first, last in month_ranges(start, end):
        url = 'https://www.szse.cn/api/report/exchange/onepersistenthour/monthList'
        rows = json.loads(fetch(url, {'month': first.strftime('%Y-%m')}, SZSE))['data']
        published = {date.fromisoformat(row['jyrq']): row['jybz'] for row in rows}
        expected = {first + timedelta(days=i) for i in range((last - first).days + 1)}
        if not expected <= published.keys() or any(published[d] not in ('0', '1') for d in expected):
            raise ValueError(f'深交所交易日历不完整：{first} 至 {last}')
        dates.extend(sorted(d for d in expected if published[d] == '1'))
    return dates


def public_members():
    # Retain historical products present in the verified exchange history even if a current list drops them.
    archive = json.loads((ROOT / 'data' / 'index-members.json').read_text(encoding='utf-8'))
    members = {index: {row[0]: row[2] for row in archive[index]} for index, _ in INDICES}
    current = {}
    for index, _ in INDICES:
        if index == '399006':
            source = 'https://www.cnindex.com.cn/info/fund'
            data = json.loads(fetch(source, {'indexCode': index, 'pageNum': 1, 'rows': 200}))['data']
            if len(data['rows']) != data['total']:
                raise ValueError('创业板指产品列表未完整返回')
            found = {row['fundCode']: 'SZ' for row in data['rows']
                     if row['fundType'] == 'ETF' and re.fullmatch(r'\d{6}', row['fundCode'])}
        else:
            source = f'https://www.csindex.com.cn/csindex-home/index-list/queryByIndexCode/{index}'
            data = json.loads(fetch(source))
            exchange = {'Shanghai Stock Exchange': 'SH', 'Shenzhen Stock Exchange': 'SZ'}
            found = {row['productCode']: exchange[row['exchangeEn']] for row in data['data']
                     if row['exchangeEn'] in exchange and row['fundType'] in ('ETF', '增强ETF')
                     and re.fullmatch(r'\d{6}', row['productCode'])}
        if not found:
            raise ValueError(f'产品列表未返回境内ETF：{index}')
        for code, exchange_code in found.items():
            if code in members[index] and members[index][code] != exchange_code:
                raise ValueError(f'产品交易所发生变化：{code}')
        members[index].update(found)
        current[index] = found
    all_codes = [code for group in members.values() for code in group]
    if len(all_codes) != len(set(all_codes)):
        raise ValueError('发现同一基金对应多个目标指数，请核实跟踪指数生效日期')
    return members, current


def report_rows(blob):
    with zipfile.ZipFile(io.BytesIO(blob)) as book:
        strings = []
        if 'xl/sharedStrings.xml' in book.namelist():
            shared = ET.fromstring(book.read('xl/sharedStrings.xml'))
            strings = [''.join(t.text or '' for t in cell.iter(Q + 't')) for cell in shared]
        with book.open('xl/worksheets/sheet1.xml') as sheet:
            for _, row in ET.iterparse(sheet, events=('end',)):
                if row.tag != Q + 'row':
                    continue
                cells = {}
                for cell in row.findall(Q + 'c'):
                    column = re.sub(r'\d+', '', cell.attrib['r'])
                    value = cell.findtext(Q + 'v')
                    if cell.get('t') == 's':
                        value = strings[int(value)]
                    elif cell.get('t') == 'inlineStr':
                        value = ''.join(t.text or '' for t in cell.iter(Q + 't'))
                    cells[column] = value or ''
                yield tuple(cells.get(c, '') for c in 'ABCD')
                row.clear()


def parse_shares(value):
    shares = Decimal(str(value).replace(',', ''))
    if not shares.is_finite() or shares < 0:
        raise ValueError(f'无效总份额：{value}')
    return shares


def sh_day(day, needed, cache):
    params = {'isPagination': 'false', 'sqlId': 'COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L', 'STAT_DATE': str(day)}
    url = 'https://query.sse.com.cn/commonQuery.do'
    payload = cached(cache, f'sh{day}.raw', lambda: fetch(url, params, SSE))
    rows = json.loads(payload)['result']
    if not rows or any(row['STAT_DATE'] != str(day) for row in rows):
        raise ValueError(f'上交所未返回所请求交易日的份额：{day}')
    values = {}
    for row in rows:
        code = row['SEC_CODE']
        if code in needed:
            if code in values:
                raise ValueError(f'沪市份额记录重复：{day} {code}')
            values[code] = parse_shares(row['TOT_VOL']) * 10000
    return str(day), values


def sz_period(first, last, needed, cache):
    params = {'SHOWTYPE': 'xlsx', 'CATALOGID': 'scsj_fund_jjgm', 'TABKEY': 'tab1',
              'jjlb': 'ETF', 'txtStart': str(first), 'txtEnd': str(last), 'txtDm': ''}
    url = 'https://www.szse.cn/api/report/ShowReport'
    blob = cached(cache, f'sz-{first}-{last}.xlsx', lambda: fetch(url, params, SZSE))
    rows = report_rows(blob)
    if next(rows) != ('日期', '基金代码', '基金简称', '基金规模(份)'):
        raise ValueError(f'深交所份额报表列名或单位变化：{first} 至 {last}')
    values = {}
    for day, code, _, shares in rows:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
            continue
        if not str(first) <= day <= str(last):
            raise ValueError(f'深交所报表日期越界：{day}')
        daily = values.setdefault(day, {})
        if code in needed:
            if code in daily:
                raise ValueError(f'深市份额记录重复：{day} {code}')
            daily[code] = parse_shares(shares)
    return values


def nav_history(code, days, cache):
    cached_path = cache / f'nav{code}.json' if cache else None
    if cached_path and cached_path.exists():
        navs = json.loads(cached_path.read_text(encoding='utf-8'), parse_float=Decimal)
        if all(day in navs and navs[day] > 0 for day in days):
            return code, navs
    if len(days) <= 20:
        url = 'https://api.fund.eastmoney.com/f10/lsjz'
        ref = f'https://fundf10.eastmoney.com/jjjz_{code}.html'
        params = {'fundCode': code, 'pageIndex': 1, 'pageSize': 20, 'startDate': min(days), 'endDate': max(days)}
        try:
            obj = json.loads(fetch(url, params, ref)).get('Data') or {}
            navs = {row['FSRQ']: Decimal(row['DWJZ']) for row in obj.get('LSJZList', []) if row.get('DWJZ')}
            if all(day in navs and navs[day] > 0 for day in days):
                return code, navs
        except (ValueError, RuntimeError, ArithmeticError):
            pass
    url = f'https://fund.eastmoney.com/pingzhongdata/{code}.js'
    source = fetch(url, referer=f'https://fund.eastmoney.com/{code}.html').decode('utf-8-sig')
    if not re.search(r'var fS_code\s*=\s*"' + code + r'"', source):
        raise ValueError(f'净值响应代码不符：{code}')
    matched = re.search(r'var Data_netWorthTrend\s*=\s*(\[.*?\]);', source, re.S)
    navs = {datetime.fromtimestamp(row['x'] / 1000, CHINA).date().isoformat(): row['y']
            for row in json.loads(matched.group(1), parse_float=Decimal)}
    missing = [day for day in days if day not in navs or not navs[day] > 0]
    if missing:
        raise ValueError(f'净值数据尚未齐全：{code} {", ".join(missing[:5])}')
    return code, navs


def collect(history, end, cache):
    previous = history['records'][-1] if history else None
    start = date.fromisoformat(previous['date']) + timedelta(days=1) if previous else START - timedelta(days=1)
    dates = exchange_dates(start, end) if start <= end else []
    if not dates:
        return history
    members, current = public_members()
    needed = {ex: {code for group in members.values() for code, market in group.items() if market == ex}
              for ex in ('SH', 'SZ')}
    print(f'读取 {dates[0]} 至 {dates[-1]}，共 {len(dates)} 个交易日', flush=True)
    sz = {}
    for first, last in month_ranges(start, end):
        if any(first <= day <= last for day in dates):
            sz.update(sz_period(first, last, needed['SZ'], cache))
    sh = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        for i, (day, values) in enumerate(pool.map(lambda d: sh_day(d, needed['SH'], cache), dates), 1):
            sh[day] = values
            if i % 40 == 0:
                print(f'上交所份额 {i}/{len(dates)}', flush=True)
    required_navs = {}
    positions = {}
    # An ETF previously observed and still listed must have shares for the next trading day.
    prior_codes = {index: set(codes) for index, codes in (history or {}).get('last_members', {}).items()}
    for day in map(str, dates):
        if not sh.get(day) or not sz.get(day):
            raise ValueError(f'沪深份额尚未齐全：{day}')
        both = {'SH': sh[day], 'SZ': sz[day]}
        positions[day] = []
        for index, label in INDICES:
            found = {code: both[ex][code] for code, ex in members[index].items() if code in both[ex]}
            missing = (prior_codes.get(index, set()) & current[index].keys()) - found.keys()
            if not found or missing:
                raise ValueError(f'{day} {label} 份额缺失：{", ".join(sorted(missing)) or "全部"}')
            prior_codes[index] = set(found)
            positions[day].append(found)
            for code, shares in found.items():
                if shares > 0:
                    required_navs.setdefault(code, []).append(day)
    print(f'核对 {len(required_navs)} 只ETF的同日单位净值', flush=True)
    with ThreadPoolExecutor(max_workers=3) as pool:
        navs = dict(pool.map(lambda item: nav_history(*item, cache), required_navs.items()))
    old_records = list(history['records']) if history else []
    prev_sizes = [Decimal(str(value[1])) for value in previous['values']] if previous else None
    for day, groups in positions.items():
        sizes = [sum((shares * navs[code][day] for code, shares in group.items() if shares > 0), Decimal(0)) / HUNDRED_MILLION
                 for group in groups]
        if any(not size.is_finite() or size <= 0 for size in sizes):
            raise ValueError(f'汇总规模无效：{day}')
        if prev_sizes is not None and day >= str(START):
            old_records.append({'date': day, 'values': [[round(float(size - prev), 8), round(float(size), 8)]
                                                         for size, prev in zip(sizes, prev_sizes)]})
        prev_sizes = sizes
    return {'indices': [{'code': code, 'name': label} for code, label in INDICES],
            'records': old_records, 'last_members': {key: sorted(codes) for key, codes in prior_codes.items()}}


def site_request(site, route, payload=None, etag=None):
    url = urlparse(site)
    if url.scheme != 'https' or not url.hostname or not url.hostname.endswith('.chatgpt.site') or url.path not in ('', '/'):
        raise ValueError('--site 必须是此看板的 HTTPS Sites 地址')
    token = os.environ.get('ETF_SITE_SERVICE_TOKEN')
    if not token or not re.fullmatch(r'[A-Za-z0-9._~+/=-]+', token):
        raise ValueError('发布数据需要在本次运行环境中设置有效的 ETF_SITE_SERVICE_TOKEN')
    headers = {'OAI-Sites-Authorization': 'Bearer ' + token, 'Accept': 'application/json'}
    if payload is not None:
        headers.update({'Content-Type': 'application/json', 'If-Match': etag or ''})
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    address = site.rstrip('/') + route
    if os.name == 'nt':
        # Windows curl uses the system trust store, which can differ from Python's trust store.
        with tempfile.TemporaryDirectory(prefix='etf_site_') as temporary:
            temp = Path(temporary)
            response_headers = temp / 'response-headers'
            command = ['curl.exe', '--silent', '--show-error', '--fail-with-body', '--max-time', '40',
                       '--proto', '=https', '--header', '@-', '--dump-header', str(response_headers), address]
            if payload is not None:
                body = temp / 'payload.json'
                body.write_bytes(payload)
                command += ['--request', 'PUT', '--data-binary', '@' + str(body)]
            header_bytes = ''.join(f'{key}: {value}\n' for key, value in headers.items()).encode('ascii')
            result = subprocess.run(command, input=header_bytes, capture_output=True, timeout=45)
            if result.returncode:
                raise RuntimeError('Sites 请求未成功：' + result.stderr.decode(errors='replace').strip())
            tag = re.findall(r'^etag:\s*(.+)', response_headers.read_text(encoding='latin1'), re.I | re.M)
            return json.loads(result.stdout), tag[-1].strip() if tag else None
    request = Request(address, data=payload, headers=headers,
                      method='PUT' if payload is not None else 'GET')
    with build_opener(NoRedirect()).open(request, timeout=40) as response:
        return json.load(response), response.headers.get('ETag')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--through', type=date.fromisoformat, help='按北京时间，默认最多取到昨天')
    parser.add_argument('--output', type=Path, default=ROOT / 'data' / 'history.json')
    parser.add_argument('--rebuild', action='store_true', help='忽略已有快照，重新取数至指定日期')
    parser.add_argument('--cache-dir', type=Path, help='可选历史响应缓存，适合从年初重建')
    parser.add_argument('--site', help='读取并更新已部署的私有看板，使用本次运行的服务令牌')
    args = parser.parse_args()
    yesterday = datetime.now(CHINA).date() - timedelta(days=1)
    end = args.through or yesterday
    if end < START or end > yesterday:
        parser.error(f'截止日须在 {START} 与北京时间昨天之间')
    if args.rebuild and args.site:
        parser.error('重建先输出本地快照，线上只允许追加交易日')
    if args.cache_dir:
        args.cache_dir.mkdir(parents=True, exist_ok=True)
    if args.site:
        existing, etag = site_request(args.site, '/api/update')
    else:
        existing = json.loads(args.output.read_text(encoding='utf-8')) if args.output.exists() and not args.rebuild else None
    if existing and date.fromisoformat(existing['records'][-1]['date']) > end:
        parser.error('截止日早于当前快照，请指定其他输出路径或使用 --rebuild')
    result = collect(existing, end, args.cache_dir)
    if result is existing:
        print('没有待补齐的交易日')
        return
    if not result or not result['records']:
        raise ValueError('截止日期内没有可用交易日')
    payload = json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode('utf-8') + b'\n'
    if args.site:
        response, _ = site_request(args.site, '/api/update', payload, etag)
        saved, _ = site_request(args.site, '/api/history')
        if saved['records'] != result['records'] or response['latest'] != result['records'][-1]['date']:
            raise RuntimeError('线上回读的交易日或数据与提交内容不一致')
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=args.output.parent, delete=False) as temporary:
            temporary.write(payload)
        Path(temporary.name).replace(args.output)
    print(f"已验证 {len(result['records'])} 个交易日，数据截至 {result['records'][-1]['date']}")


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, OSError, KeyError, zipfile.BadZipFile) as error:
        print(f'更新失败：{error}', file=sys.stderr)
        sys.exit(1)
