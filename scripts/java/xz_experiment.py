"""Fork-only XZ comparison. Not a release or Maven publication entry point."""
import argparse
import binascii
import hashlib
import io
import json
import lzma
import os
from pathlib import Path
import statistics
import struct
import subprocess
import sys
import time
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mk_java_package import (LIBRARIES, RESOURCE_PREFIX, assemble, checked_zip,
                             json_bytes, sha256, validate_binary)
from prepare import download

HERE = Path(__file__).resolve().parent
DECODER_URL = 'https://tukaani.org/xz/xz-java-1.12.zip'
DECODER_SHA256 = 'd563ba12babaf977f02207c7e474ad41f8ca701efd35ff82abe26c54d66ab68d'
PRIVATE = 'com/microsoft/z3/internal/xz/'


def run(command, log=None, env=None, check=True):
    result = subprocess.run([str(x) for x in command], capture_output=True, text=True,
                            encoding='utf-8', errors='replace', env=env, timeout=180)
    output = result.stdout + result.stderr
    if log:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(output, encoding='utf-8')
    if check and result.returncode:
        raise ValueError('Command failed: ' + str(command[0]) + '\n' + output[-5000:])
    return result, output


def write(path, entries):
    with zipfile.ZipFile(path, 'w') as z:
        for name in sorted(entries, key=lambda n: (n != 'META-INF/MANIFEST.MF', n)):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_STORED if name.endswith('.xz') else zipfile.ZIP_DEFLATED
            z.writestr(info, entries[name], compresslevel=9)


def decoder(work):
    source = download(DECODER_URL, DECODER_SHA256, work / 'xz-java-1.12.zip')
    entries = checked_zip(source)
    # Compile only the upstream Java 8 source tree. No multi-release/JDK 9 classes,
    # bytecode rewriting, external build plugin, or public org.tukaani namespace.
    sources = []
    for name, data in sorted(entries.items()):
        if name.startswith('src/org/tukaani/xz/') and name.endswith('.java'):
            path = work / 'decoder-src' / name[len('src/'):].replace('org/tukaani/xz/', PRIVATE, 1)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(data.decode('utf-8').replace('org.tukaani.xz', 'com.microsoft.z3.internal.xz'),
                            encoding='utf-8', newline='\n')
            sources.append(path.resolve())
    classes = work / 'decoder-classes'
    classes.mkdir()
    args = work / 'decoder-javac.args'
    args.write_text('\n'.join('"' + str(p).replace('\\', '/') + '"' for p in sources), encoding='utf-8')
    run(['javac', '--release', '8', '-g:none', '-d', classes, '@' + str(args)], work / 'decoder-compile.log')
    result = {p.relative_to(classes).as_posix(): p.read_bytes() for p in classes.rglob('*.class')}
    if not result or any(not n.startswith(PRIVATE) or int.from_bytes(b[6:8], 'big') != 52 for n, b in result.items()):
        raise ValueError('Unexpected decoder class set or Java baseline')
    result['META-INF/z3/xz/COPYING'] = entries['COPYING']
    result['META-INF/z3/xz/AUTHORS.md'] = entries['AUTHORS.md']
    result['META-INF/z3/xz/NOTICE.txt'] = (
        'XZ for Java 1.12, 0BSD. ' + DECODER_URL + '\nSource SHA-256: ' + DECODER_SHA256 +
        '\nExperimental relocation org.tukaani.xz -> com.microsoft.z3.internal.xz.\n'
        'Java 8 baseline sources only; Java 9 multi-release optimizations omitted.\n').encode()
    return result


def build(prepared, out, platforms, decoder_work):
    if out.exists():
        raise ValueError('Use a fresh output directory')
    out.mkdir(parents=True)
    lock = json.loads((HERE / 'release.json').read_text())
    preparation = json.loads((prepared / 'preparation.json').read_text())
    raw = out / 'raw'
    assemble(lock, prepared / 'downloads', (prepared / 'prepared-api.jar').read_bytes(), preparation,
             raw, platforms, 'invalid.z3.prototype', 'z3', lock['version'] + '-xz-experiment')
    extra = decoder(decoder_work)
    report = {'experiment': 'fork-only-xz-v1', 'source_commit': preparation['source_commit'],
              'loader_sha256': preparation['loader_sha256'], 'preparation': preparation,
              'workflow': {k: os.environ.get('GITHUB_' + k.upper(), '') for k in ('repository', 'run_id', 'run_attempt', 'sha')},
              'decoder': {'url': DECODER_URL, 'sha256': DECODER_SHA256, 'java_release': 8,
                          'classes': {k: sha256(v) for k, v in extra.items() if k.endswith('.class')}},
              'python': sys.version, 'lzma_version': 'Python stdlib/liblzma', 'preset': 6,
              'api_classes': {n: sha256(b) for n, b in checked_zip((prepared / 'prepared-api.jar').read_bytes()).items()
                              if n.endswith('.class')},
              'jars': {}, 'platforms': sorted(platforms), 'published': False}
    compressed = {}
    for jar in sorted(raw.glob('*.jar')):
        entries = checked_zip(jar.read_bytes())
        embedded = json.loads(entries['META-INF/z3/provenance.json'])
        classifier = embedded.get('classifier')
        if not classifier:
            continue
        native = {n: b for n, b in entries.items() if n.startswith(RESOURCE_PREFIX)}
        # Compute once per distinct input, reuse exactly the same streams in all.jar.
        for n, b in native.items():
            if n not in compressed:
                compressed[n] = lzma.compress(b, format=lzma.FORMAT_XZ, check=lzma.CHECK_CRC64, preset=6)
        for mode in ('deflate9', 'xz6'):
            content = dict(entries)
            if mode == 'xz6':
                content.update(extra)
                for n, b in native.items():
                    del content[n]
                    content[n + '.xz'] = compressed[n]
                    content[n + '.sha256'] = (sha256(b) + '\n').encode('ascii')
            name = mode + '-' + classifier + '.jar'
            path = out / name
            write(path, content)
            record = {'sha256': sha256(path.read_bytes()), 'bytes': path.stat().st_size,
                      'classifier': classifier, 'format': mode,
                      'libraries': {n: sha256(b) for n, b in native.items()}}
            report['jars'][name] = record
            repeat = io.BytesIO()
            write(repeat, content)
            if sha256(repeat.getvalue()) != record['sha256']:
                raise ValueError('Nonreproducible JAR ' + name)
            verify(path, record, report)
    (out / 'experiment.json').write_bytes(json_bytes(report))
    # Only these jars/manifest are transported to native jobs; raw inputs stay local.
    print(json.dumps({n: r['bytes'] for n, r in report['jars'].items()}, indent=2))


def verify(path, record, report):
    if sha256(path.read_bytes()) != record['sha256']:
        raise ValueError('JAR checksum mismatch ' + path.name)
    entries = checked_zip(path.read_bytes())
    meta = json.loads(entries['META-INF/z3/provenance.json'])
    if meta['source_commit'] != report['source_commit'] or meta['loader_sha256'] != report['loader_sha256']:
        raise ValueError('Wrong branch loader provenance')
    expected = set()
    for n, digest in record['libraries'].items():
        data = entries.get(n)
        if record['format'] == 'xz6':
            expected.update((n + '.xz', n + '.sha256'))
            if entries[n + '.sha256'] != (digest + '\n').encode():
                raise ValueError('XZ digest manifest mismatch')
            data = lzma.decompress(entries[n + '.xz'], memlimit=64 * 1024 * 1024)
            with zipfile.ZipFile(path) as z:
                if z.getinfo(n + '.xz').compress_type != zipfile.ZIP_STORED:
                    raise ValueError('XZ stream compressed twice')
        else:
            expected.add(n)
        if data is None or sha256(data) != digest:
            raise ValueError('Wrong native bytes ' + n)
        validate_binary(data, n[len(RESOURCE_PREFIX):].split('/')[0])
    if {n for n in entries if n.startswith(RESOURCE_PREFIX)} != expected:
        raise ValueError('Unexpected native resources')
    decoder_classes = {n: sha256(b) for n, b in entries.items() if n.startswith(PRIVATE) and n.endswith('.class')}
    if decoder_classes != (report['decoder']['classes'] if record['format'] == 'xz6' else {}):
        raise ValueError('Wrong embedded decoder')
    api_classes = {n: sha256(b) for n, b in entries.items() if n.endswith('.class') and not n.startswith(PRIVATE)}
    if api_classes != report['api_classes']:
        raise ValueError('Experimental JAR API differs from current prepared API')
    for n, data in entries.items():
        if n.endswith('.class') and int.from_bytes(data[6:8], 'big') > 52:
            raise ValueError('Not Java 8 bytecode ' + n)
    if any(n.startswith(('org/tukaani/', 'META-INF/versions/')) for n in entries):
        raise ValueError('Unrelocated or multi-release decoder')
    return entries


def native_environment():
    env = dict(os.environ)
    for n in ('LD_LIBRARY_PATH', 'DYLD_LIBRARY_PATH', 'DYLD_FALLBACK_LIBRARY_PATH',
              'JDK_JAVA_OPTIONS', '_JAVA_OPTIONS', 'CLASSPATH'):
        env.pop(n, None)
    return env


def test(packages, platform, work, repeats):
    packages = packages.resolve()
    report = json.loads((packages / 'experiment.json').read_text())
    if os.environ.get('GITHUB_SHA') and report['source_commit'] != os.environ['GITHUB_SHA']:
        raise ValueError('CI artifact belongs to another commit')
    if os.environ.get('GITHUB_RUN_ID') and report['workflow'] != {
            k: os.environ.get('GITHUB_' + k.upper(), '') for k in ('repository', 'run_id', 'run_attempt', 'sha')}:
        raise ValueError('CI artifact belongs to another workflow run/attempt')
    work.mkdir(parents=True, exist_ok=False)
    selected = {n: r for n, r in report['jars'].items() if r['classifier'] in (platform, 'all')}
    if len(selected) != 4:
        raise ValueError('Expected two formats and two classifier forms')
    for n, r in selected.items():
        verify(packages / n, r, report)
    classes = work / 'classes'
    classes.mkdir()
    run(['javac', '-source', '8', '-target', '8', '-cp', packages / ('deflate9-' + platform + '.jar'),
         '-d', classes, HERE / 'Smoke.java', HERE / 'CacheClassLoaderTest.java', HERE / 'XZProbe.java'], work / 'compile.log')
    env = native_environment()
    libs = LIBRARIES[platform.split('-')[0]]
    results = []
    empty = work / 'empty'
    empty.mkdir()
    # Keep benchmarks and ownership checks away from the runner's real profile.
    home = work / 'isolated-profile'
    home.mkdir()

    def invoke(name, label, cache, temp, main='XZProbe', args=(), cached=True, cp_prefix=()):
        temp.mkdir(parents=True, exist_ok=True)
        command = ['java', '-Xms16m', '-Xmx128m', '-Duser.home=' + str(home.resolve()),
                   '-Djava.library.path=' + str(empty.resolve()),
                   '-Dz3.nativeCacheDir=' + str(cache.resolve()),
                   '-Dz3.nativeCache=' + str(cached).lower(), '-Djava.io.tmpdir=' + str(temp.resolve()),
                   '-cp', os.pathsep.join([*(str(p.resolve()) for p in cp_prefix), str(packages / name), str(classes.resolve())]), main, *args]
        started = time.perf_counter()
        result, output = run(command, work / 'logs' / (label + '.log'), env)
        wall = (time.perf_counter() - started) * 1000
        if main == 'XZProbe':
            line = next((l for l in output.splitlines() if l.startswith('PROBE ')), None)
            if line is None:
                raise ValueError('Missing actual solver probe receipt')
            probe = json.loads(line[6:])
            if probe['version'] != report['preparation']['release_runtime_version']:
                raise ValueError('JNI version mismatch')
            return {'jar': name, 'label': label, 'wall_ms': wall, **probe}
        return {'jar': name, 'label': label, 'wall_ms': wall}

    # Check concurrent/classloader failure cases before spending time on timings.
    from cache_smoke import cache_smoke
    regression = {}
    def smoke_run(args, environment=None):
        return run(args, env=environment)[1]
    for classifier in (platform, 'all'):
        name = 'xz6-' + classifier + '.jar'
        regression[name] = cache_smoke(packages / name, classes, work / ('regression-' + classifier),
                                     report['preparation']['release_runtime_version'], libs, env, smoke_run,
                                     jvm_options=('-Xms16m', '-Xmx128m'))
    for classifier in (platform, 'all'):
        for i in range(repeats):
            # Alternate order per repetition to reduce a fixed format-order bias.
            for mode in (('deflate9', 'xz6') if i % 2 == 0 else ('xz6', 'deflate9')):
                name = mode + '-' + classifier + '.jar'
                stem = f'{classifier}-{mode}-{i}'
                cache = work / (stem + '-cache')
                for stage in ('cold', 'warm'):
                    results.append({**invoke(name, stem + '-' + stage, cache, work / (stem + '-' + stage + '-tmp')), 'stage': stage})
                before = {str(p.relative_to(cache)): (p.stat().st_size, p.stat().st_mtime_ns, sha256(p.read_bytes()))
                          for p in cache.rglob('*') if p.is_file() and p.name in libs}
                results.append({**invoke(name, stem + '-warm2', cache, work / (stem + '-warm2-tmp')), 'stage': 'warm'})
                after = {str(p.relative_to(cache)): (p.stat().st_size, p.stat().st_mtime_ns, sha256(p.read_bytes()))
                         for p in cache.rglob('*') if p.is_file() and p.name in libs}
                expected = {n.rsplit('/', 1)[-1]: d for n, d in report['jars'][name]['libraries'].items()
                            if n.startswith(RESOURCE_PREFIX + platform + '/')}
                if len(after) != 2 or before != after or {Path(n).name: d[2] for n, d in after.items()} != expected:
                    raise ValueError('Cache was not reused or decoded bytes differ')
                for stage in ('cold', 'warm', 'warm2'):
                    if any((work / (stem + '-' + stage + '-tmp')).iterdir()):
                        raise ValueError('Cache unexpectedly used private fallback')
                results.append({**invoke(name, stem + '-private', cache, work / (stem + '-private-tmp'), cached=False), 'stage': 'private'})
    (work / 'measurements.json').write_bytes(json_bytes(results))
    # A conflicting public decoder must not affect the privately relocated decoder.
    conflict = work / 'conflict'
    source = conflict / 'org/tukaani/xz/XZInputStream.java'
    source.parent.mkdir(parents=True)
    source.write_text('package org.tukaani.xz; public class XZInputStream { static { if (true) throw new Error("public decoder conflict"); } }')
    run(['javac', '-source', '8', '-target', '8', '-d', conflict, source])
    invoke('xz6-all.jar', 'public-decoder-conflict', work / 'conflict-cache', work / 'conflict-tmp', cp_prefix=(conflict,))
    failures = []
    name = 'xz6-' + platform + '.jar'
    original = checked_zip((packages / name).read_bytes())
    resource = RESOURCE_PREFIX + platform + '/' + libs[0]
    for kind in ('missing-decoder', 'corrupt', 'wrong-digest', 'missing-digest', 'memory-limit'):
        broken = dict(original)
        if kind == 'missing-decoder':
            broken = {n:b for n,b in broken.items() if not n.startswith(PRIVATE)}
        elif kind == 'corrupt':
            broken[resource + '.xz'] = broken[resource + '.xz'][:-16]
        elif kind == 'wrong-digest':
            broken[resource + '.sha256'] = b'0' * 64 + b'\n'
        elif kind == 'missing-digest':
            del broken[resource + '.sha256']
        else:
            # Change only the LZMA2 dictionary declaration in a tiny preset-0
            # stream. Recompute the block-header CRC; do not allocate a huge
            # encoder dictionary merely to test the decoder's memory rejection.
            data = bytearray(lzma.compress(b'tiny', preset=0))
            header_size = (data[12] + 1) * 4
            if data[13:16] != b'\x00\x21\x01':
                raise ValueError('Unexpected XZ fixture block header')
            data[16] = 30  # 128 MiB LZMA2 dictionary, above the 64 MiB limit.
            crc = 12 + header_size - 4
            data[crc:crc+4] = struct.pack('<I', binascii.crc32(data[12:crc]))
            broken[resource + '.xz'] = bytes(data)
        jar = work / (kind + '.jar')
        write(jar, broken)
        temp = work / (kind + '-tmp')
        temp.mkdir()
        command = ['java', '-Xms16m', '-Xmx128m', '-Duser.home=' + str(home.resolve()),
                   '-Djava.library.path=' + str(empty.resolve()),
                   '-Dz3.nativeCacheDir=' + str((work / (kind + '-cache')).resolve()),
                   '-Djava.io.tmpdir=' + str(temp.resolve()), '-cp', str(jar.resolve()) + os.pathsep + str(classes.resolve()), 'XZProbe']
        result, output = run(command, work / 'logs' / (kind + '.log'), env, check=False)
        if result.returncode == 0 or 'System attempts:' not in output or 'OutOfMemoryError' in output:
            raise ValueError('Missing bounded failure diagnostics for ' + kind + '\n' + output)
        if kind == 'memory-limit' and 'MemoryLimitException' not in output:
            raise ValueError('Memory limit was not enforced')
        failures.append(kind)
    medians = {}
    for name in selected:
        medians[name] = {stage: {metric: round(statistics.median(r[metric] for r in results if r['jar'] == name and r['stage'] == stage), 3)
                               for metric in ('load_ms', 'wall_ms')} for stage in ('cold', 'warm', 'private')}
    receipt = {'passed': True, 'platform': platform, 'source_commit': report['source_commit'],
               'loader_sha256': report['loader_sha256'], 'workflow': report['workflow'],
               'experiment_sha256': sha256((packages / 'experiment.json').read_bytes()),
               'java': run(['java', '-version'])[1].strip(), 'samples': results, 'medians_ms': medians,
               'java_spec': results[0]['java_spec'], 'isolated_profile': True,
               'jar_sha256': {n:r['sha256'] for n,r in selected.items()}, 'cache_regression_modes': regression,
               'failures_checked': failures, 'public_decoder_conflict_passed': True, 'published': False}
    (work / 'receipt.json').write_bytes(json_bytes(receipt))
    print(json.dumps({'passed': True, 'platform': platform, 'medians_ms': medians}, indent=2))


def summarize(manifest, receipts, out):
    report = json.loads(manifest.read_text())
    expected = {(p, j) for p in ('linux-x64', 'linux-arm64', 'win-x64') for j in ('1.8', '17')}
    expected.update({('osx-arm64', '17'), ('osx-arm64', '21')})
    found = {}
    for path in receipts.rglob('receipt.json'):
        receipt = json.loads(path.read_text())
        key = (receipt['platform'], receipt['java_spec'])
        if key not in expected or key in found or not receipt['passed'] or receipt['published']:
            raise ValueError('Unexpected, duplicate or failed native receipt ' + str(key))
        for field in ('source_commit', 'loader_sha256', 'workflow'):
            if receipt[field] != report[field]:
                raise ValueError('Receipt provenance mismatch ' + field)
        if receipt['experiment_sha256'] != sha256(manifest.read_bytes()):
            raise ValueError('Receipt describes another artifact manifest')
        names = {n for n, r in report['jars'].items() if r['classifier'] in ('all', key[0])}
        if receipt['jar_sha256'] != {n: report['jars'][n]['sha256'] for n in names}:
            raise ValueError('Tested JAR hashes differ from producer')
        if set(receipt['failures_checked']) != {'missing-decoder', 'corrupt', 'wrong-digest', 'missing-digest', 'memory-limit'}:
            raise ValueError('Missing negative coverage')
        if not receipt['public_decoder_conflict_passed'] or len(receipt['samples']) != 80:
            raise ValueError('Missing collision or timing coverage')
        from verify_release import MODES
        cache_modes = set(MODES) - {'bundled', 'system', 'system-base', 'missing', 'unsupported', 'skip', 'temp-failure'}
        if set(receipt['cache_regression_modes']) != {'xz6-all.jar', 'xz6-' + key[0] + '.jar'} or any(
                set(v) != cache_modes for v in receipt['cache_regression_modes'].values()):
            raise ValueError('Missing compact cache regression modes')
        for name in names:
            for stage, count in (('cold', 5), ('warm', 10), ('private', 5)):
                if sum(r['jar'] == name and r['stage'] == stage for r in receipt['samples']) != count:
                    raise ValueError('Wrong timing sample count')
        found[key] = receipt
    if set(found) != expected:
        raise ValueError('Missing native runners ' + str(expected - set(found)))
    out.mkdir(parents=True, exist_ok=False)
    comparisons = {}
    for classifier in report['platforms'] + ['all']:
        base = report['jars']['deflate9-' + classifier + '.jar']['bytes']
        compact = report['jars']['xz6-' + classifier + '.jar']['bytes']
        comparisons[classifier] = {'deflate9_bytes': base, 'xz6_bytes': compact,
                                   'saved_percent': round((1 - compact / base) * 100, 2)}
    summary = {'passed': True, 'source_commit': report['source_commit'], 'workflow': report['workflow'],
               'loader_sha256': report['loader_sha256'], 'sizes': comparisons, 'published': False,
               'native_receipts': {p + '-java' + j: r for (p, j), r in sorted(found.items())}}
    (out / 'summary.json').write_bytes(json_bytes(summary))
    print(json.dumps({'passed': True, 'native_receipts': len(found), 'sizes': comparisons}, indent=2))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='action', required=True)
    b = sub.add_parser('build')
    b.add_argument('--prepared', type=Path, required=True)
    b.add_argument('--out', type=Path, required=True)
    b.add_argument('--platform', action='append', required=True)
    b.add_argument('--decoder-work', type=Path, required=True)
    t = sub.add_parser('test')
    t.add_argument('--packages', type=Path, required=True)
    t.add_argument('--platform', required=True)
    t.add_argument('--work', type=Path, required=True)
    t.add_argument('--repeats', type=int, default=5)
    s = sub.add_parser('summarize')
    s.add_argument('--manifest', type=Path, required=True)
    s.add_argument('--receipts', type=Path, required=True)
    s.add_argument('--out', type=Path, required=True)
    a = p.parse_args()
    if a.action == 'build':
        a.decoder_work.mkdir(parents=True, exist_ok=True)
        build(a.prepared, a.out, a.platform, a.decoder_work)
    elif a.action == 'test':
        if a.repeats < 3:
            p.error('At least three independent cold starts per format are required')
        test(a.packages, a.platform, a.work, a.repeats)
    else:
        summarize(a.manifest, a.receipts, a.out)


if __name__ == '__main__':
    main()
