"""Fetch pinned public task assets from data/sources.json without running code."""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
import urllib.error
import urllib.request


def hf_token() -> str | None:
    """Use environment overrides or the local `hf auth login` cache."""
    token = os.getenv('HF_TOKEN') or os.getenv('HUGGING_FACE_HUB_TOKEN')
    if token:
        return token
    try:
        from huggingface_hub import get_token
    except ImportError:
        cache = Path(os.getenv('HF_HOME') or
                     str(Path(os.getenv('XDG_CACHE_HOME') or str(Path.home() / '.cache')) / 'huggingface'))
        path = Path(os.getenv('HF_TOKEN_PATH') or str(cache / 'token'))
        try:
            return path.read_text().strip() or None
        except FileNotFoundError:
            return None
    return get_token()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def ranged_download(url: str, headers: dict, partial: Path, size: int, workers: int) -> None:
    """Parallel bounded ranges for GitHub LFS endpoints that throttle streams."""
    parts = partial.with_name(partial.name + '.segments')
    parts.mkdir(exist_ok=True)
    width = (size + workers - 1) // workers
    ranges = [(index, index * width, min((index + 1) * width, size) - 1)
              for index in range(workers) if index * width < size]
    def segment(bounds):
        index, start, end = bounds
        target = parts / str(index)
        if target.is_file() and target.stat().st_size == end - start + 1:
            return target
        request = urllib.request.Request(url, headers={**headers, 'Range': f'bytes={start}-{end}'})
        with urllib.request.urlopen(request, timeout=60) as response:
            if response.status != 206 or not response.headers.get('Content-Range', '').startswith(f'bytes {start}-{end}/'):
                raise ValueError('server did not honor bounded byte range')
            with target.open('wb') as stream:
                shutil.copyfileobj(response, stream, 64 * 1024)
        if target.stat().st_size != end - start + 1:
            raise ValueError('incomplete byte range')
        print(f'DONE range {partial.name}: {index + 1}/{len(ranges)}', flush=True)
        return target
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        completed = list(pool.map(segment, ranges))
    with partial.open('wb') as stream:
        for file in completed:
            with file.open('rb') as part:
                shutil.copyfileobj(part, stream, 1024 * 1024)


def file_ready(root: Path, spec: dict) -> bool:
    target = root / spec['path']
    expected = spec.get('sha256')
    expected_size = spec.get('bytes')
    return (target.is_file() and (expected_size is None or target.stat().st_size == expected_size)
            and (expected is None or sha256(target) == expected))


def fetch(root: Path, spec: dict) -> dict:
    target = root / spec['path']
    expected = spec.get('sha256')
    expected_size = spec.get('bytes')
    if file_ready(root, spec):
        return {'path': spec['path'], 'status': 'present'}
    headers = {'User-Agent': 'eval-finance-data/0.1'}
    token = hf_token() if spec['url'].startswith('https://huggingface.co/') else None
    if token and spec['url'].startswith('https://huggingface.co/'):
        headers['Authorization'] = 'Bearer ' + token
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + '.part')
    for attempt in range(10):
        try:
            url = spec['url']
            download_headers = headers
            if spec.get('git_lfs'):
                batch = urllib.request.Request(spec['git_lfs']['repository'] + '/info/lfs/objects/batch',
                    data=json.dumps({'operation': 'download', 'transfers': ['basic'],
                                     'objects': [{'oid': expected, 'size': expected_size}]}).encode(),
                    headers={'Accept': 'application/vnd.git-lfs+json',
                             'Content-Type': 'application/vnd.git-lfs+json', 'User-Agent': headers['User-Agent']})
                with urllib.request.urlopen(batch, timeout=60) as response:
                    item = json.load(response)['objects'][0]
                if item.get('error'):
                    raise ValueError('Git LFS object unavailable')
                action = item['actions']['download']
                url = action['href']
                download_headers = {**headers, **action.get('header', {})}
            if spec.get('range_workers'):
                ranged_download(url, download_headers, partial, expected_size, spec['range_workers'])
                if sha256(partial) != expected:
                    shutil.rmtree(partial.with_name(partial.name + '.segments'))
                    raise ValueError('SHA-256 mismatch after ranged download')
                partial.replace(target)
                shutil.rmtree(partial.with_name(partial.name + '.segments'))
                print(f"DONE {spec['path']}: {expected_size / 1024**2:.1f} MiB", flush=True)
                return {'path': spec['path'], 'status': 'downloaded', 'sha256': expected, 'bytes': expected_size}
            offset = partial.stat().st_size if partial.is_file() else 0
            request_headers = {**download_headers, **({'Range': f'bytes={offset}-'} if offset else {})}
            request = urllib.request.Request(url, headers=request_headers)
            with urllib.request.urlopen(request, timeout=60) as response:
                resumed = offset > 0 and response.status == 206
                if resumed and not response.headers.get('Content-Range', '').startswith(f'bytes {offset}-'):
                    raise ValueError('unexpected range response')
                digest, size, last = hashlib.sha256(), offset if resumed else 0, time.monotonic()
                if resumed:
                    with partial.open('rb') as prefix:
                        for chunk in iter(lambda: prefix.read(1024 * 1024), b''):
                            digest.update(chunk)
                stream = partial.open('ab' if resumed else 'wb')
                try:
                    for chunk in iter(lambda: response.read(1024 * 1024), b''):
                        stream.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                        if time.monotonic() - last > 20:
                            print(f"PROGRESS {spec['path']}: {size / 1024**2:.1f} MiB", flush=True)
                            last = time.monotonic()
                finally:
                    stream.close()
            if expected_size is not None and size != expected_size:
                raise ValueError(f'file size mismatch: {size} != {expected_size}')
            if expected is not None and digest.hexdigest() != expected:
                raise ValueError('SHA-256 mismatch')
            with partial.open('rb') as stream:
                if stream.read(50).startswith(b'version https://git-lfs.github.com/spec/v1'):
                    raise ValueError('received a Git LFS pointer instead of its data')
            partial.replace(target)
            print(f"DONE {spec['path']}: {size / 1024**2:.1f} MiB", flush=True)
            return {'path': spec['path'], 'status': 'downloaded', 'sha256': digest.hexdigest(), 'bytes': size}
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403} and spec.get('gated'):
                partial.unlink(missing_ok=True)
                print(f"GATED {spec['path']}: official access authorization required", flush=True)
                return {'path': spec['path'], 'status': 'gated', 'http_status': exc.code}
            error = f'HTTP {exc.code}'
        except (OSError, ValueError, urllib.error.URLError) as exc:
            error = str(exc)
        if attempt < 9:
            print(f"RETRY {spec['path']}: {error}", flush=True)
            time.sleep(attempt + 1)
    # Keep a partial public asset for a later retry instead of discarding a
    # large verified-prefix download after transient connection failures.
    print(f"FAILED {spec['path']}: {error}", flush=True)
    return {'path': spec['path'], 'status': 'failed', 'error': error}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, default=Path(__file__).resolve().parents[1] / 'data')
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--include-gated', action='store_true', help='use an authorized HF token or local login cache for gated files')
    args = parser.parse_args()
    if not 1 <= args.workers <= 16:
        parser.error('--workers must be between 1 and 16')
    root = args.data_root.resolve()
    sources = json.loads((root / 'sources.json').read_text())
    for spec in sources['downloads']:
        if not (root / spec['path']).resolve().is_relative_to(root):
            raise ValueError('download destination escapes data directory')
    selected = [spec for spec in sources['downloads'] if args.include_gated or not spec.get('gated')]
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda spec: fetch(root, spec), selected))
    for spec in sources['downloads']:
        if spec.get('gated') and not args.include_gated:
            results.append({'path': spec['path'], 'status': 'present'} if file_ready(root, spec) else
                           {'path': spec['path'], 'status': 'gated', 'reason': 'skipped; authorization required'})
    (root / 'download_report.json').write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n')
    return int(any(result['status'] == 'failed' for result in results))


if __name__ == '__main__':
    raise SystemExit(main())
