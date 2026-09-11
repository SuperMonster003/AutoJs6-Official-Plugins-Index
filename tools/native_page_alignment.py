"""Measure released APKs, including ELF payloads nested in Chaquopy assets.

Only successful measurements are cached, keyed by the actual APK SHA-256.
An unavailable artifact is unknown; a malformed artifact is an error.
"""
import hashlib
import json
import re
import shutil
import struct
import tempfile
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

STRICT_MACHINES = {62, 183}
CACHE_REVISION = 1


def elf_alignment(prefix):
    if len(prefix) < 52 or prefix[:4] != b'\x7fELF' or prefix[4] not in (1, 2) or prefix[5] not in (1, 2):
        raise ValueError('Invalid ELF header')
    endian = '<' if prefix[5] == 1 else '>'
    is64 = prefix[4] == 2
    def read(fmt, offset):
        return struct.unpack_from(endian + fmt, prefix, offset)[0]
    machine = read('H', 18)
    offset = read('Q' if is64 else 'I', 32 if is64 else 28)
    stride, count = read('H', 54 if is64 else 42), read('H', 56 if is64 else 44)
    if stride < (56 if is64 else 32) or not count or offset + stride * count > len(prefix):
        raise ValueError('Invalid or unbounded ELF program table')
    loads = []
    for i in range(count):
        start = offset + i * stride
        if read('I', start) != 1:
            continue
        alignment = read('Q' if is64 else 'I', start + (48 if is64 else 28))
        file_offset = read('Q' if is64 else 'I', start + (8 if is64 else 4))
        address = read('Q' if is64 else 'I', start + (16 if is64 else 8))
        if alignment <= 0 or alignment & (alignment - 1) or file_offset % alignment != address % alignment:
            raise ValueError('Invalid PT_LOAD alignment')
        loads.append(alignment)
    if not loads:
        raise ValueError('ELF has no PT_LOAD')
    return machine, min(loads)


def measure_apk(path):
    entries = []
    def visit(stream, name, depth=0):
        if depth > 8:
            raise ValueError('Native archive nesting exceeds eight levels')
        prefix = stream.read(65536)
        if prefix.startswith(b'\x7fELF'):
            machine, alignment = elf_alignment(prefix)
            entries.append({'name': name, 'machine': machine, 'minLoadAlign': alignment})
        elif prefix.startswith(b'PK\x03\x04'):
            # Nested ZIP needs random access; spill large Python archives to disk.
            with tempfile.SpooledTemporaryFile(max_size=4 * 1024 * 1024) as nested:
                nested.write(prefix)
                shutil.copyfileobj(stream, nested)
                nested.seek(0)
                with zipfile.ZipFile(nested) as archive:
                    for info in archive.infolist():
                        if not info.is_dir():
                            with archive.open(info) as child:
                                visit(child, name + '!/' + info.filename, depth + 1)
        elif name.endswith('.so'):
            raise ValueError(f'Native payload is not ELF: {name}')
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if not info.is_dir() and (info.filename.startswith('lib/') or info.filename.startswith('assets/')):
                with archive.open(info) as stream:
                    visit(stream, info.filename)
    strict = [e['minLoadAlign'] for e in entries if e['machine'] in STRICT_MACHINES]
    return {'nativePageAlignment': min(strict or [e['minLoadAlign'] for e in entries], default=0), 'entries': entries}


def measure_asset(asset, cache_root):
    url = asset.get('browser_download_url') or asset.get('url')
    expected = asset.get('sha256') or str(asset.get('digest') or '').removeprefix('sha256:')
    if expected and not re.fullmatch('[a-fA-F0-9]{64}', expected):
        raise ValueError('Invalid asset SHA-256')
    expected = expected.lower()
    cache_root = Path(cache_root)
    cache_root.mkdir(parents=True, exist_ok=True)
    cache = cache_root / (expected + '.json') if expected else None
    if cache and cache.is_file():
        saved = json.loads(cache.read_text(encoding='utf-8'))
        if saved.get('sha256') == expected and saved.get('schemaRevision') == CACHE_REVISION:
            return saved['nativePageAlignment']
    if not url:
        return None
    try:
        response = urlopen(Request(url, headers={'User-Agent': 'AutoJs6-Native-Alignment'}), timeout=60)
    except (HTTPError, URLError, TimeoutError):
        return None
    with response, tempfile.TemporaryFile() as apk:
        sha = hashlib.sha256()
        total = 0
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > 2 * 1024 ** 3:
                raise ValueError('APK exceeds 2 GiB measurement limit')
            sha.update(chunk)
            apk.write(chunk)
        actual = sha.hexdigest()
        if expected and actual != expected:
            raise ValueError('Released APK SHA-256 mismatch')
        apk.seek(0)
        result = measure_apk(apk)
        result.update(schemaRevision=CACHE_REVISION, sha256=actual)
        (cache_root / (actual + '.json')).write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
        return result['nativePageAlignment']


def release_alignment(assets, declared=None, *, cache_root):
    # A universal artifact contains all released ABIs. Otherwise inspect every split,
    # so a pure/32-bit APK cannot hide a 4 KB arm64 or x86_64 library.
    universal = [a for a in assets if 'universal' in str(a.get('name', '')).lower()]
    strict = [a for a in assets if re.search(r'arm64|aarch64|x86[-_]?64|amd64', str(a.get('name', '')), re.I)]
    selected = universal[:1] or strict or assets
    values = [measure_asset(a, cache_root) for a in selected]
    if values and all(v is not None for v in values):
        nonzero = [v for v in values if v]
        return {'nativePageAlignment': min(nonzero, default=0), 'nativePageAlignmentSource': 'measured'}
    if declared is not None:
        return {'nativePageAlignment': declared, 'nativePageAlignmentSource': 'declared'}
    return {}
