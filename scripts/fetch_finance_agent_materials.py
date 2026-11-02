"""Download task-scoped FinanceAgent sources and build answer-free local cases.

The official tasks stay unchanged. Raw discovery APIs are evaluator-side;
case bundles expose dated filings and filtered API/price snapshots only.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta, date
import csv
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import requests
except ImportError:
    requests = None


def stamp():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows))


class Fetcher:
    def __init__(self, root, rate, user_agent):
        self.root, self.rate, self.user_agent = root, rate, user_agent
        self.lock = threading.Lock()
        self.next_request = 0.0
        self.records = {}
        self.journal = root / 'download_journal.jsonl'
        if self.journal.exists():
            for line in self.journal.read_text().splitlines():
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                self.records[record['path']] = record
        self.count = 0
        self.sessions = threading.local()

    def throttle(self):
        with self.lock:
            now = time.monotonic()
            scheduled = max(now, self.next_request)
            self.next_request = scheduled + 1 / self.rate
        time.sleep(max(0, scheduled - now))

    def fetch(self, relative, url, kind, **metadata):
        path = self.root / relative
        old = self.records.get(relative)
        if old and old['url'] == url and path.is_file() and sha(path) == old['sha256']:
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        error = None
        for attempt in range(4):
            self.throttle()
            try:
                headers = {
                    'User-Agent': self.user_agent, 'Accept': '*/*',
                }
                if requests:
                    if not hasattr(self.sessions, 'session'):
                        self.sessions.session = requests.Session()
                    with self.sessions.session.get(url, headers=headers, timeout=(15, 45), stream=True) as response:
                        if response.status_code >= 400:
                            raise urllib.error.HTTPError(url, response.status_code, response.reason, response.headers, None)
                        pieces, size = [], 0
                        for piece in response.iter_content(1024 * 1024):
                            pieces.append(piece); size += len(piece)
                            if size > 80 * 1024 * 1024:
                                raise ValueError('oversized source')
                        blob = b''.join(pieces)
                        content_type = response.headers.get('Content-Type', '')
                        final_url = response.url
                else:
                    request = urllib.request.Request(url, headers=headers)
                    with urllib.request.urlopen(request, timeout=45) as response:
                        blob = response.read(80 * 1024 * 1024 + 1)
                        content_type = response.headers.get('Content-Type', '')
                        final_url = response.url
                if not blob or len(blob) > 80 * 1024 * 1024:
                    raise ValueError('empty or oversized source')
                prefix = blob[:30000].lower()
                if any(marker in prefix for marker in (
                    b'your request originates from an undeclared automated tool',
                    b'your request rate has exceeded', b'request rate threshold exceeded',
                )):
                    raise ValueError('SEC access-denied page returned instead of source')
                if path.suffix == '.json':
                    json.loads(blob)
                elif path.suffix.lower() == '.pdf' and not blob.startswith(b'%PDF'):
                    raise ValueError('invalid PDF source')
                temporary = path.with_name(path.name + '.part')
                temporary.write_bytes(blob)
                temporary.replace(path)
                record = {'path': relative, 'url': url, 'final_url': final_url,
                    'kind': kind, 'bytes': len(blob), 'sha256': sha(path),
                    'downloaded_at': stamp(), 'content_type': content_type, **metadata}
                with self.lock:
                    self.records[relative] = record
                    with self.journal.open('a') as f:
                        f.write(json.dumps(record, ensure_ascii=False) + '\n')
                    self.count += 1
                    if self.count % 25 == 0:
                        print('FETCHED', self.count, relative, flush=True)
                return path
            except (OSError, ValueError) as exc:
                error = exc
                if isinstance(exc, urllib.error.HTTPError) and exc.code in (400, 404):
                    break
                time.sleep(min(2 ** attempt, 8))
        raise RuntimeError(f'{url}: {error}')


class DocumentIndex(HTMLParser):
    """Read SEC filing-detail table document names and exhibit types."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.row, self.cell, self.href = [], None, None, None
    def handle_starttag(self, tag, attrs):
        if tag == 'tr': self.row = []
        elif tag == 'td' and self.row is not None: self.cell, self.href = [], None
        elif tag == 'a' and self.cell is not None:
            self.href = dict(attrs).get('href')
    def handle_data(self, data):
        if self.cell is not None: self.cell.append(data)
    def handle_endtag(self, tag):
        if tag == 'td' and self.cell is not None:
            self.row.append((''.join(self.cell).strip(), self.href))
            self.cell = None
        elif tag == 'tr' and self.row is not None:
            if len(self.row) >= 4 and self.row[2][1]:
                self.rows.append({'type': self.row[3][0], 'href': self.row[2][1],
                                  'description': self.row[1][0]})
            self.row = None


def filing_rows(columns):
    n = len(columns.get('accessionNumber', []))
    return [{k: v[i] if i < len(v) else None for k, v in columns.items() if isinstance(v, list)}
            for i in range(n)]


def matches(row, selector):
    if row.get('form') not in selector['forms']:
        return False
    if not selector['filed_from'] <= row.get('filingDate', '') <= selector['filed_to']:
        return False
    if selector.get('report_to') and row.get('reportDate', '') > selector['report_to']:
        return False
    wanted = selector.get('items_any')
    if wanted and row['form'].startswith('8-K'):
        return bool(set(wanted) & set(re.findall(r'\d\.\d{2}', row.get('items') or '')))
    return True


def discover(fetcher, entity, selectors):
    cik = str(entity['cik']).zfill(10)
    path = fetcher.fetch(f'cache/sec/submissions/CIK{cik}.json',
        f'https://data.sec.gov/submissions/CIK{cik}.json', 'discovery_submissions', cik=cik)
    native = json.loads(path.read_text())
    historical_names = [native['name']] + [item['name'] for item in native.get('formerNames', [])]
    if entity.get('verify_name') and not any(entity['verify_name'].lower() in name.lower() for name in historical_names):
        raise ValueError(f'CIK mismatch for {entity}: {native["name"]}')
    rows = filing_rows(native.get('filings', {}).get('recent', {}))
    earliest = min(s['filed_from'] for s in selectors)
    latest = max(s['filed_to'] for s in selectors)
    for archive in native.get('filings', {}).get('files', []):
        if archive.get('filingTo', '') < earliest or archive.get('filingFrom', '') > latest:
            continue
        name = archive['name']
        p = fetcher.fetch(f'cache/sec/submissions/{name}',
            f'https://data.sec.gov/submissions/{name}', 'discovery_submissions', cik=cik)
        rows.extend(filing_rows(json.loads(p.read_text())))
    rows = list({r['accessionNumber']: r for r in rows}.values())
    rows.sort(key=lambda r: (r['filingDate'], r['accessionNumber']))
    facts_path = fetcher.fetch(f'cache/sec/companyfacts/CIK{cik}.json',
        f'https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json', 'discovery_companyfacts', cik=cik)
    return {'identity': {'cik': cik, 'symbol': entity['symbol'], 'name': native['name']},
            'rows': rows, 'facts': str(facts_path)}


def download_filing(fetcher, cik, row, include_exhibits):
    accession = row['accessionNumber']
    base = f'https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace("-", "")}/'
    directory = f'assets/sec/{cik}/{accession.replace("-", "")}'
    metadata = {'cik': cik, 'accession': accession, 'form': row['form'],
                'filing_date': row['filingDate'], 'report_date': row.get('reportDate')}
    primary = row['primaryDocument']
    if not primary or '/' in primary or '\\' in primary:
        raise ValueError(f'invalid primary document: {primary!r}')
    p = fetcher.fetch(f'{directory}/{primary}', base + primary, 'filing', **metadata)
    files = [str(p.relative_to(fetcher.root))]
    exhibit_errors = []
    if include_exhibits:
        index_url = base + accession + '-index.html'
        p = fetcher.fetch(f'{directory}/{accession}-index.html', index_url, 'filing_index', **metadata)
        files.append(str(p.relative_to(fetcher.root)))
        parser = DocumentIndex()
        parser.feed(p.read_text(errors='replace'))
        for doc in parser.rows:
            if not re.match(r'^EX-(?:99|10|2|4)(?:\.|$)', doc['type']):
                continue
            url = urllib.parse.urljoin(base, doc['href'])
            # Some indexes point to the inline-XBRL viewer. Fetch the raw document.
            parsed = urllib.parse.urlparse(url)
            query = urllib.parse.parse_qs(parsed.query)
            if 'doc' in query:
                url = urllib.parse.urljoin('https://www.sec.gov', query['doc'][0])
            name = urllib.parse.unquote(urllib.parse.urlparse(url).path.rsplit('/', 1)[-1])
            if Path(name).suffix.lower() not in ('.htm', '.html', '.pdf', '.txt', '.xml'):
                continue
            if not name or '/' in name or '\\' in name:
                raise ValueError('unsafe exhibit name')
            try:
                p = fetcher.fetch(f'{directory}/{name}', url, 'filing_exhibit',
                    exhibit_type=doc['type'], description=doc['description'], **metadata)
                files.append(str(p.relative_to(fetcher.root)))
            except RuntimeError as exc:
                exhibit_errors.append(str(exc))
    return {'files': sorted(set(files)), 'exhibit_errors': exhibit_errors, **metadata}


def check_price_dates(req, prices):
    # Current requirements use trading-day endpoints; explicit required_dates
    # can be supplied for ranges whose calendar endpoints are market holidays.
    required = req.get('required_dates', [req['start_date'], req['end_date']])
    observed = {row['date'] for row in prices}
    missing = sorted(set(required) - observed)
    if missing:
        raise ValueError(f'missing required price dates for {req["symbol"]}: {missing}')


def download_yahoo_price(fetcher, req):
    symbol, start, end = req['symbol'], req['start_date'], req['end_date']
    def epoch(day):
        return int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp())
    params = urllib.parse.urlencode({'period1': epoch(start), 'period2': epoch(end) + 86400,
                                    'interval': '1d', 'events': 'div,splits', 'includeAdjustedClose': 'true'})
    url = f'https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?{params}'
    key = f'{symbol}_{start}_{end}'
    raw = fetcher.fetch(f'cache/yahoo/{key}.json', url, 'discovery_prices', symbol=symbol)
    payload = json.loads(raw.read_text())['chart']
    if payload.get('error') or not payload.get('result'):
        raise ValueError(f'no Yahoo history for {symbol}: {payload.get("error")}')
    result = payload['result'][0]
    quotes = result['indicators']['quote'][0]
    adjusted = result['indicators'].get('adjclose', [{}])[0].get('adjclose', [])
    tz = timezone(timedelta(seconds=result.get('meta', {}).get('gmtoffset', 0)))
    prices = []
    for i, ts in enumerate(result.get('timestamp', [])):
        day = datetime.fromtimestamp(ts, tz).date().isoformat()
        if not start <= day <= end or quotes['close'][i] is None:
            continue
        item = {'date': day}
        for k in ('open', 'high', 'low', 'close', 'volume'):
            item[k] = quotes.get(k, [None] * (i + 1))[i]
        item['adj_close'] = adjusted[i] if i < len(adjusted) else None
        prices.append(item)
    if not prices:
        raise ValueError(f'empty requested price window: {key}')
    check_price_dates(req, prices)
    metadata = {k: result.get('meta', {}).get(k) for k in ('symbol', 'currency', 'exchangeName')}
    snapshot = {'source_url': url, 'provider': 'Yahoo Finance', 'start_date': start,
        'end_date': end, 'metadata': metadata,
        'price_convention': 'close is Yahoo quote.close; adj_close is Yahoo adjclose; vendor adjustment conventions may differ from Tiingo; timestamps use exchange offset',
        'rows': prices}
    target = fetcher.root / f'assets/prices/{key}.json'
    write_json(target, snapshot)
    csv_path = target.with_suffix('.csv')
    with csv_path.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(prices[0]))
        writer.writeheader(); writer.writerows(prices)
    return {'files': [str(p.relative_to(fetcher.root)) for p in (target, csv_path)],
            'symbol': symbol, 'source_url': url, 'row_count': len(prices),
            'dates': [p['date'] for p in prices]}


class PriceTable(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.row, self.cell = [], None, None
    def handle_starttag(self, tag, attrs):
        if tag == 'tr': self.row = []
        elif tag == 'td' and self.row is not None: self.cell = []
    def handle_data(self, value):
        if self.cell is not None: self.cell.append(value)
    def handle_endtag(self, tag):
        if tag == 'td' and self.cell is not None:
            self.row.append(''.join(self.cell).strip()); self.cell = None
        elif tag == 'tr' and self.row is not None:
            if len(self.row) >= 7 and re.match(r'^\d{4}-\d{2}-\d{2}', self.row[0]):
                self.rows.append(self.row)
            self.row = None


def download_price(fetcher, req):
    try:
        return download_yahoo_price(fetcher, req)
    except (RuntimeError, ValueError) as yahoo_error:
        # Delisted symbols can lose their Yahoo chart history. Preserve the
        # fallback's own provenance and adjustment convention rather than
        # synthesizing data from benchmark answers or a different ticker.
        symbol, start, end = req['symbol'], req['start_date'], req['end_date']
        url = f'https://chartexchange.com/symbol/nyse-{symbol.lower()}/historical/'
        source = fetcher.fetch(f'cache/chartexchange/{symbol}.html', url,
                               'discovery_prices', symbol=symbol)
        parser = PriceTable(); parser.feed(source.read_text())
        def number(value): return float(value.replace(',', '').replace('$', '').strip())
        prices = []
        for cells in parser.rows:
            day = cells[0][:10]
            if start <= day <= end:
                prices.append({'date': day, 'open': number(cells[1]), 'high': number(cells[2]),
                    'low': number(cells[3]), 'close': number(cells[4]),
                    'volume': int(number(cells[6])), 'adj_close': None})
        prices.sort(key=lambda p: p['date'])
        if not prices:
            raise ValueError(f'Yahoo unavailable ({yahoo_error}); no requested rows from {url}')
        check_price_dates(req, prices)
        key = f'{symbol}_{start}_{end}'
        target = fetcher.root / f'assets/prices/{key}.json'
        write_json(target, {'source_url': url, 'provider': 'ChartExchange',
            'fallback_reason': str(yahoo_error), 'start_date': start, 'end_date': end,
            'metadata': {'symbol': symbol, 'currency': 'USD', 'exchangeName': 'NYSE'},
            'price_convention': 'published historical OHLCV; adjusted close is not supplied; no adjustment inferred',
            'rows': prices})
        cp = target.with_suffix('.csv')
        with cp.open('w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=list(prices[0])); writer.writeheader(); writer.writerows(prices)
        print('PRICE FALLBACK', symbol, len(prices), url, flush=True)
        return {'files': [str(p.relative_to(fetcher.root)) for p in (target, cp)],
                'symbol': symbol, 'source_url': url, 'row_count': len(prices),
                'dates': [p['date'] for p in prices]}


def filtered_apis(root, benchmark, task, discovery, selected):
    files = []
    for symbol in sorted({r['symbol'] for r in task['requirements'] if r['kind'] == 'filing'}):
        if symbol not in discovery:
            continue
        entity = discovery[symbol]
        cik = entity['identity']['cik']
        directory = root / f'assets/api/{benchmark}/{task["task_id"]}/{cik}'
        rows = [r for r in entity['rows'] if r['filingDate'] <= task['as_of'] and
                any(r['accessionNumber'] == s['accession'] for s in selected if s['cik'] == cik)]
        submissions = directory / 'submissions.json'
        write_json(submissions, {'identity': {'cik': cik, 'symbol': symbol}, 'as_of': task['as_of'],
            'source_url': f'https://data.sec.gov/submissions/CIK{cik}.json', 'filings': rows})
        facts = json.loads(Path(entity['facts']).read_text())
        kept = {}
        for taxonomy, concepts in facts.get('facts', {}).items():
            values = {}
            for name, concept in concepts.items():
                units = {unit: [v for v in points if v.get('filed', '9999') <= task['as_of']]
                         for unit, points in concept.get('units', {}).items()}
                units = {unit: points for unit, points in units.items() if points}
                if units: values[name] = {**concept, 'units': units}
            if values: kept[taxonomy] = values
        fp = directory / 'companyfacts.json'
        write_json(fp, {'cik': facts.get('cik'), 'symbol': symbol,
            'as_of': task['as_of'], 'source_url': f'https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json', 'facts': kept})
        files.extend(str(p.relative_to(root)) for p in (submissions, fp))
    return files


def build(root, workers, rate, user_agent, discover_only=False):
    plan = json.loads((root / 'requirements.json').read_text())
    fetcher = Fetcher(root, rate, user_agent)
    tickers = json.loads(fetcher.fetch('cache/sec/company_tickers.json',
        'https://www.sec.gov/files/company_tickers.json', 'discovery_tickers').read_text())
    lookup = {v['ticker']: v for v in tickers.values()}
    selectors = {}
    prices = {}
    for tasks in plan['benchmarks'].values():
        for task in tasks:
            for r in task['requirements']:
                if r['kind'] == 'filing': selectors.setdefault(r['symbol'], []).append(r)
                elif r['kind'] == 'price': prices[json.dumps(r, sort_keys=True)] = r
    discovery, errors = {}, []
    def one_entity(symbol):
        if symbol in plan.get('entity_overrides', {}):
            item = plan['entity_overrides'][symbol]
            entity = {'symbol': symbol, 'cik': item['cik'], 'verify_name': item['name']}
        else:
            entity = {'symbol': symbol, 'cik': lookup[symbol]['cik_str']}
        return discover(fetcher, entity, selectors[symbol])
    print('DISCOVERING', len(selectors), 'entities', flush=True)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        jobs = {pool.submit(one_entity, symbol): symbol for symbol in selectors}
        for future in as_completed(jobs):
            symbol = jobs[future]
            try:
                discovery[symbol] = future.result()
                print('ENTITY', symbol, discovery[symbol]['identity']['cik'], len(discovery[symbol]['rows']), flush=True)
            except Exception as exc:
                errors.append({'stage': 'discovery', 'symbol': symbol, 'error': str(exc)})
                print('ERROR ENTITY', symbol, str(exc), flush=True)
    write_json(root / 'discovery.json', {s: {'identity': d['identity'], 'rows': d['rows']} for s, d in discovery.items()})
    chosen = {}
    for symbol, requirements in selectors.items():
        if symbol not in discovery: continue
        cik = discovery[symbol]['identity']['cik']
        for row in discovery[symbol]['rows']:
            matching = [s for s in requirements if matches(row, s)]
            if matching:
                chosen[(cik, row['accessionNumber'])] = (row, any(s['include_exhibits'] for s in matching))
    print('SELECTED', len(chosen), 'filings;', len(prices), 'price windows', flush=True)
    if discover_only:
        write_json(root / 'discovery_report.json', {'entities': len(discovery), 'selected_filings': len(chosen), 'errors': errors})
        return
    downloaded = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        jobs = {pool.submit(download_filing, fetcher, cik, row, exhibits): (cik, acc)
                for (cik, acc), (row, exhibits) in chosen.items()}
        for future in as_completed(jobs):
            key = jobs[future]
            try:
                downloaded[key] = future.result()
                if downloaded[key]['exhibit_errors']:
                    errors.extend({'stage': 'exhibit', 'accession': key[1], 'error': e}
                                  for e in downloaded[key]['exhibit_errors'])
            except Exception as exc:
                errors.append({'stage': 'filing', 'accession': key[1], 'error': str(exc)})
                print('ERROR FILING', key, str(exc), flush=True)
    price_results = {}
    for key, req in prices.items():
        try:
            price_results[key] = download_price(fetcher, req)
        except Exception as exc:
            errors.append({'stage': 'price', 'symbol': req['symbol'], 'error': str(exc)})
            print('ERROR PRICE', req['symbol'], str(exc), flush=True)
    # Preserve PDF originals and provide text for harnesses without a PDF tool.
    # The dependency is optional: missing extraction never discards an original.
    try:
        from pypdf import PdfReader
    except ImportError:
        PdfReader = None
    pdf_texts = {}
    if PdfReader:
        for path in (root / 'assets').rglob('*.pdf'):
            try:
                text = '\n\n'.join(page.extract_text() or '' for page in PdfReader(path).pages)
                if text.strip():
                    target = path.with_suffix('.pdf.txt')
                    target.write_text(text, encoding='utf-8')
                    pdf_texts[str(path.relative_to(root))] = str(target.relative_to(root))
            except Exception as exc:
                print('PDF TEXT UNAVAILABLE', path.name, str(exc), flush=True)
    web_spec = root / 'web_sources.json'
    web_results = {}
    requested_web = {r['key'] for ts in plan['benchmarks'].values() for t in ts
                     for r in t['requirements'] if r['kind'] == 'web'}
    if web_spec.exists():
        for key, specs in json.loads(web_spec.read_text()).items():
            if key not in requested_web:
                continue
            files = []
            for spec in specs:
                try:
                    path = fetcher.fetch(spec['path'], spec['url'], 'web_source', published_date=spec['published_date'])
                    files.append(str(path.relative_to(root)))
                    if PdfReader and path.suffix.lower() == '.pdf':
                        text = '\n\n'.join(page.extract_text() or '' for page in PdfReader(path).pages)
                        if text.strip():
                            target = path.with_suffix('.pdf.txt')
                            target.write_text(text, encoding='utf-8')
                            pdf_texts[str(path.relative_to(root))] = str(target.relative_to(root))
                except Exception as exc:
                    errors.append({'stage': 'web', 'key': key, 'error': str(exc)})
            web_results[key] = files
    reports = {}
    for benchmark, tasks in plan['benchmarks'].items():
        native = {t['task_id']: t for t in map(json.loads, (root.parent / benchmark / 'tasks.jsonl').read_text().splitlines())}
        local_tasks, materials = [], []
        for task in tasks:
            files, task_filings, missing, requirements_report = [], [], [], []
            for index, req in enumerate(task['requirements']):
                matches_count, requirement_files = 0, []
                if req['kind'] == 'filing' and req['symbol'] in discovery:
                    d = discovery[req['symbol']]
                    for row in d['rows']:
                        if matches(row, req):
                            matches_count += 1
                            record = downloaded.get((d['identity']['cik'], row['accessionNumber']))
                            if record:
                                task_filings.append(record)
                                requirement_files.extend(record['files'])
                                if record['exhibit_errors']: missing.append({'requirement': index, 'reason': 'exhibit_download_failed'})
                            else: missing.append({'requirement': index, 'reason': 'filing_download_failed', 'accession': row['accessionNumber']})
                elif req['kind'] == 'price':
                    record = price_results.get(json.dumps(req, sort_keys=True))
                    if record:
                        requirement_files = record['files']; matches_count = record['row_count']
                elif req['kind'] == 'web':
                    requirement_files = web_results.get(req['key'], []); matches_count = len(requirement_files)
                    if web_spec.exists() and len(requirement_files) < len(json.loads(web_spec.read_text()).get(req['key'], [])):
                        missing.append({'requirement': index, 'reason': 'web_download_failed'})
                files.extend(requirement_files)
                if not requirement_files and req.get('required', True):
                    missing.append({'requirement': index, 'reason': 'no_matching_source', 'selector': req})
                requirements_report.append({'selector': req, 'matching_records': matches_count,
                                            'downloaded_files': len(set(requirement_files))})
            files.extend(filtered_apis(root, benchmark, task, discovery, task_filings))
            files.extend(pdf_texts[r] for r in list(files) if r in pdf_texts)
            bundle = root.parent / benchmark / 'materials' / task['task_id']
            bundle.mkdir(parents=True, exist_ok=True)
            index_file = bundle / 'index.json'
            desired = {r.removeprefix('assets/') for r in files}
            if index_file.exists():
                previous = json.loads(index_file.read_text())
                for item in previous.get('files', []):
                    name = item['bundle_path']
                    old = bundle / name
                    if name not in desired and old.is_symlink():
                        old.unlink()
            inputs = []
            for relative in sorted(set(files)):
                source = root / relative
                target = bundle / relative.removeprefix('assets/')
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists(): target.symlink_to(os.path.relpath(source, target.parent))
                inputs.append(str(target.relative_to(root.parent)))
            write_json(index_file, {'task_id': task['task_id'], 'as_of': task['as_of'],
                'files': [{'path': str((root / r).relative_to(root.parent)),
                           'bundle_path': r.removeprefix('assets/'), 'sha256': sha(root / r)} for r in sorted(set(files))],
                'filings': [{k: f[k] for k in ('cik', 'accession', 'form', 'filing_date', 'report_date')} for f in task_filings]})
            inputs.append(str(index_file.relative_to(root.parent)))
            local_tasks.append({**native[task['task_id']], 'input_files': inputs, 'as_of': task['as_of'],
                'material_mode': 'local_public_source_snapshot',
                'material_status': 'downloaded' if not missing else 'partial',
                'material_directory': str(bundle.relative_to(root.parent))})
            materials.append({'task_id': task['task_id'], 'as_of': task['as_of'],
                'status': 'downloaded' if not missing else 'partial', 'input_file_count': len(inputs),
                'filing_count': len({f['accession'] for f in task_filings}),
                'requirements': requirements_report, 'missing': missing, 'scope_notes': task.get('scope_notes')})
        write_jsonl(root.parent / benchmark / 'tasks.local.jsonl', local_tasks)
        write_jsonl(root.parent / benchmark / 'materials.jsonl', materials)
        reports[benchmark] = {'task_count': len(tasks), 'downloaded_task_count': sum(not m['missing'] for m in materials),
            'partial_tasks': [m['task_id'] for m in materials if m['missing']], 'tasks': materials}
    manifest = []
    for relative, record in sorted(fetcher.records.items()):
        manifest.append({**record, 'path': 'finance_agent_materials/' + relative})
    for path in sorted((root / 'assets').rglob('*')):
        relative = str(path.relative_to(root))
        if not path.is_file() or relative in fetcher.records: continue
        parent = None
        if relative.endswith('.pdf.txt'):
            parent = relative[:-4]
        elif relative.startswith('assets/prices/'):
            parent = 'cache/yahoo/' + path.stem + '.json'
            if not (root / parent).exists():
                parent = 'cache/chartexchange/' + path.stem.split('_', 1)[0] + '.html'
        elif relative.startswith('assets/api/'):
            category = 'submissions' if path.name == 'submissions.json' else 'companyfacts'
            parent = f'cache/sec/{category}/CIK{path.parent.name}.json'
        origin = fetcher.records.get(parent, {})
        manifest.append({'path': str(path.relative_to(root.parent)), 'kind': 'derived_snapshot',
                         'bytes': path.stat().st_size, 'sha256': sha(path),
                         **({'derived_from': 'finance_agent_materials/' + parent,
                             'url': origin['url'], 'source_sha256': origin['sha256']} if origin else {}),
                         **{k: origin[k] for k in ('filing_date', 'published_date', 'cik', 'accession') if k in origin}})
    write_json(root / 'manifest.json', {'schema_version': 'eval_finance.finance_agent.material_manifest.v1', 'files': manifest})
    write_json(root / 'coverage_report.json', {'schema_version': 'eval_finance.finance_agent.material_coverage.v1',
        'generated_at': stamp(), 'entities': len(discovery), 'selected_filings': len(chosen),
        'downloaded_filings': len(downloaded), 'source_file_count': len(fetcher.records),
        'source_bytes': sum(r['bytes'] for r in fetcher.records.values()), 'benchmarks': reports,
        'errors': errors, 'verification': 'source selection and successful downloads; semantic answerability requires task review; no model/judge calls'})
    print('COMPLETE', json.dumps({b: {k:v for k,v in r.items() if k != 'tasks'} for b,r in reports.items()}),
          'errors=', len(errors), flush=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1] / 'data/finance_agent_materials')
    p.add_argument('--workers', type=int, default=4)
    p.add_argument('--requests-per-second', type=float, default=3)
    p.add_argument('--user-agent', default=os.environ.get('DATAELF_FINANCE_USER_AGENT',
        'DataElf-Finance-Domain/1.0 (analysis runtime; contact: admin@dataelf.local)'))
    p.add_argument('--discover-only', action='store_true')
    args = p.parse_args()
    if not 0 < args.requests_per_second <= 8 or not 1 <= args.workers <= 8:
        p.error('rate/workers must be positive and <= 8')
    build(args.root.resolve(), args.workers, args.requests_per_second, args.user_agent, args.discover_only)
