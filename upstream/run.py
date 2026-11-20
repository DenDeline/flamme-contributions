#!/usr/bin/env python3
"""Measure the public Elsa control using the reference benchmark's trial and SQL observer."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
import uuid

HERE = Path(__file__).resolve().parent
# I7 timing and SQL observation helpers. This host supplies its own authentication and fixtures.
REFERENCE = HERE / 'reference'
DEFINITION = 'upstream-store-control'
RELEASE = 'elsa-3.10.2'


def load_driver(path):
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(path.parent))  # Use the selected driver's own observer module.
    spec = importlib.util.spec_from_file_location('reference_workload', path)
    driver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(driver)
    return driver


def command(*args, timeout=30, **kwargs):
    proc = subprocess.run(args, text=True, capture_output=True, timeout=timeout, **kwargs)
    if proc.returncode != 0:
        raise RuntimeError(f'{args[0]} {args[1]} failed ({proc.returncode}): {proc.stderr.strip()}')
    return proc.stdout.strip()


def wait(probe, label, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if probe():
            return
        time.sleep(0.20)
    raise RuntimeError(f'Timed out waiting for {label}')


def stop_host(host):
    if host.poll() is None:
        host.terminate()
        try:
            host.wait(timeout=10)
        except subprocess.TimeoutExpired:
            host.kill()
            host.wait(timeout=10)


def run_host(stack, scratch, dll, driver, trace):
    pg_password = secrets.token_hex(32)
    api_key = secrets.token_hex(32)
    cidfile = scratch / 'pg.cid'

    def cleanup_pg():
        if cidfile.exists() and cidfile.read_text().strip():
            command('docker', 'rm', '-fv', cidfile.read_text().strip())

    stack.callback(cleanup_pg)
    cid = command('docker', 'create', '--cidfile', str(cidfile), '-e', 'POSTGRES_PASSWORD',
                  '-e', 'POSTGRES_DB=elsa', '-p', '127.0.0.1::5432', 'postgres:16',
                  env={**os.environ, 'POSTGRES_PASSWORD': pg_password}, timeout=180)
    command('docker', 'start', cid)
    wait(lambda: subprocess.run(['docker', 'exec', cid, 'pg_isready', '-qh', '127.0.0.1', '-U', 'postgres',
                                 '-d', 'elsa'], capture_output=True, timeout=5).returncode == 0, 'PostgreSQL TCP')
    db_port = int(command('docker', 'port', cid, '5432/tcp').split(':')[-1])
    psql = ['docker', 'exec', '-i', cid, 'psql', '-U', 'postgres', '-d', 'elsa']
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        origin = f'http://127.0.0.1:{sock.getsockname()[1]}'
    api = driver.Api(origin, 'ApiKey ' + api_key)
    env = {k: v for k, v in os.environ.items() if k in ('PATH', 'DOTNET_ROOT', 'LANG', 'LC_ALL', 'HOME')}
    env.update(ASPNETCORE_URLS=origin, ELSA_BENCH_API_KEY=api_key, Logging__LogLevel__Default='Warning',
               ConnectionStrings__PostgreSql=f'Host=127.0.0.1;Port={db_port};Database=elsa;Username=postgres;Password={pg_password}',
               DOTNET_ENVIRONMENT='Production', XDG_DATA_HOME=str(scratch), TMPDIR=str(scratch))
    if trace:
        env['ELSA_BENCH_TRACE'] = '1'
    log = stack.enter_context((scratch / 'host.log').open('w+'))
    host = subprocess.Popen(['dotnet', str(dll)], cwd=scratch, env=env, stdout=log, stderr=subprocess.STDOUT)
    stack.callback(stop_host, host)

    def ready():
        if host.poll() is not None:
            tail = (scratch / 'host.log').read_text(errors='replace')[-5500:]
            raise RuntimeError(f'Kestrel exited ({host.returncode}): {tail.replace(pg_password, "[redacted]").replace(api_key, "[redacted]")}')
        try:
            api.call('/ready')
            return True
        except (OSError, URLError):
            return False

    wait(ready, '/ready')
    return api, psql, command('docker', 'inspect', '-f', '{{.Image}}', cid)


def seed(api, count):
    def start(_):
        instance = uuid.uuid4().hex
        response = api.call('/seed/' + instance, {})
        if (response['id'], response['status'], response['subStatus']) != (instance, 'Running', 'Suspended') or not response['bookmarkId']:
            raise RuntimeError(f'Seed failed for {instance}: {response}')
        return instance, response['correlationId']

    with ThreadPoolExecutor(8) as pool:
        result = dict(pool.map(start, range(count)))
    if len(result) != count:
        raise RuntimeError('Expected distinct suspended IDs')
    return result


def audit_api(api, pairs):
    def check(pair):
        instance, marker = pair
        state = api.call('/audit/' + instance)
        return (state['id'] == state['stateId'] == instance and
                state['correlationId'] == state['stateCorrelationId'] == marker and
                state['status'] == state['stateStatus'] == state['subStatus'] == state['stateSubStatus'] == 'Finished' and
                state['bookmarkCount'] == state['incidentCount'] == state['stateIncidentCount'] == 0 and
                not state['tenantId'] and bool(state['finishedAt']))

    with ThreadPoolExecutor(8) as pool:
        return sum(pool.map(check, pairs.items()))


def exercise(driver, api, psql, pg_image, smoke, emit):
    n = 3 if smoke else 49
    with driver.session(psql) as observer, driver.session(psql) as holder:
        pg = int(observer('SHOW server_version_num')[0])
        migrations = observer('SELECT "MigrationId" FROM "Elsa"."__EFMigrationsHistory" ORDER BY 1')
        required = {'20261101100004_TenantScopedDefinitionIds', '20260520121653_V3_8'}
        if pg // 10000 != 16 or not required.issubset(migrations):
            raise RuntimeError(f'Unexpected database {pg}, migrations {migrations}')
        routes = api.call('/routes')
        if (routes['traceEnabled'] != smoke or routes['runtimeType'] != 'Elsa.Workflows.Runtime.LocalWorkflowRuntime'
                or routes['storeType'] != 'Elsa.Persistence.EFCore.Modules.Management.EFCoreWorkflowInstanceStore'):
            raise RuntimeError(f'Unexpected upstream registrations or trace mode: {routes}')
        emit(dict(kind='fixture', pg_version=pg, pg_image=pg_image, migrations=migrations, trace_enabled=smoke,
                  ready=api.call('/ready'), routes=routes, definition=DEFINITION))
        for index in range(1 if smoke else 4):
            phase = 'smoke' if smoke else 'warmup' if index == 0 else 'measured'
            for held in (False, True):
                instances = seed(api, n)
                if driver.committed(observer, DEFINITION, instances, finished=False) != instances.keys():
                    first = next(iter(instances))
                    raise RuntimeError(f'Seed failed durable audit: {api.call("/audit/" + first)}')
                for route in ('resume', 'save'):
                    if route == 'resume':
                        markers = instances
                        req = [('/elsa/api/events/store-bench/trigger', {'workflowInstanceId': i,
                                'correlationId': c, 'workflowExecutionMode': 0}) for i, c in instances.items()]
                    else:
                        markers = {i: c + '-imported' for i, c in instances.items()}

                        def prepare(i):
                            state = api.call('/prepare/' + i)['state']
                            return '/save/' + i, {'state': state, 'marker': markers[i]}

                        with ThreadPoolExecutor(8) as pool:
                            req = list(pool.map(prepare, instances))
                    before = api.call('/routes')['counts'] if smoke else {}
                    row = driver.trial(api, observer, holder, DEFINITION, markers, 'resume' if route == 'resume' else 'import', held, req)
                    after = api.call('/routes')['counts'] if smoke else {}
                    row.update(kind='trial', operation=route, phase=phase, trial=index,
                               public_store_calls={k: v-before.get(k, 0) for k, v in after.items() if v != before.get(k, 0)} if smoke else None,
                               independent_api_audit=audit_api(api, markers))
                    emit(row)
                    if row['errors'] or row['audit_failed_ids'] or row['independent_api_audit'] != n or not row.get('anchor_pending_before_release', True):
                        raise RuntimeError(f'Invalid {route} trial {index}; see JSONL trial')
        emit(dict(kind='completed', routes=api.call('/routes')))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dll', type=Path, default=HERE/'bin/Release/net10.0/UpstreamControl.dll')
    parser.add_argument('--driver', type=Path, default=REFERENCE/'run.py', help='I7 run.py with sibling observer.py')
    parser.add_argument('--output', type=Path, help='new JSONL path under an existing directory')
    parser.add_argument('--smoke', action='store_true')
    args = parser.parse_args()
    dll = args.dll.resolve(strict=True)
    driver_path = args.driver.resolve(strict=True)
    driver = load_driver(driver_path)
    out = args.output or HERE.parent/'evidence'/('upstream.smoke.jsonl' if args.smoke else 'upstream.trials.jsonl')
    with out.open('x') as out_file:
        def emit(row):
            print(json.dumps(dict(row, label=RELEASE), separators=(',', ':')), file=out_file, flush=True)
            if row['kind'] == 'trial':
                print(f"{row['phase']} {row['trial']} {row['case']} {row['operation']} "
                      f"{row['committed']}/{row['unrelated']+1}, pre-release={row['committed_before_release']}, p99={row['p99_ms']}ms", flush=True)

        try:
            files = {'launcher': Path(__file__), 'reference_driver': driver_path, 'reference_observer': driver_path.with_name('observer.py'),
                     'UpstreamControl.dll': dll, 'packages.lock.json': HERE/'packages.lock.json'}
            emit(dict(kind='source', sha256={k: hashlib.sha256(p.read_bytes()).hexdigest() for k, p in files.items()}))
            with ExitStack() as stack:
                scratch = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='elsa-3.10.2-')))
                api, psql, pg_image = run_host(stack, scratch, dll, driver, args.smoke)
                exercise(driver, api, psql, pg_image, args.smoke, emit)
        except BaseException as exc:
            emit(dict(kind='invalid', error=str(exc)))
            raise


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    main()
