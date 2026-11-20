"""Warm serial control. Run python3 -B serial.py [new-output.json] after building Release."""

from contextlib import ExitStack
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys
import tempfile

HERE = Path(__file__).resolve().parent
WARMUPS = 3
SAMPLES = 20


def run():
    launcher_path = HERE / 'run.py'
    spec = importlib.util.spec_from_file_location('upstream_launcher', launcher_path)
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    ref_path = launcher.REFERENCE / 'run.py'
    driver = launcher.load_driver(ref_path)
    dll = HERE / 'bin/Release/net10.0/UpstreamControl.dll'
    files = [dll, HERE / 'packages.lock.json', Path(__file__), launcher_path, ref_path, ref_path.with_name('observer.py')]
    result = {
        'label': launcher.RELEASE, 'client_concurrency': 1, 'warmups': WARMUPS, 'samples': SAMPLES,
        'sha256': {str(p.relative_to(HERE)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
        'lanes': {},
    }
    with ExitStack() as stack:
        scratch = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='elsa-upstream-serial-')))
        api, psql, image = launcher.run_host(stack, scratch, dll, driver, trace=False)
        observer = stack.enter_context(driver.session(psql))
        pg = int(observer('SHOW server_version_num')[0])
        routes = api.call('/routes')
        if pg // 10000 != 16 or routes['traceEnabled']:
            raise RuntimeError('Expected PostgreSQL 16 and undecorated store')
        result.update(pg_version=pg, pg_image=image, routes=routes,
                      migrations=observer('SELECT "MigrationId" FROM "Elsa"."__EFMigrationsHistory" ORDER BY 1'))
        instances = launcher.seed(api, WARMUPS + SAMPLES)
        result['suspended_sql'] = len(driver.committed(observer, launcher.DEFINITION, instances, finished=False))
        if result['suspended_sql'] != len(instances):
            raise RuntimeError('Incomplete seed')

        for lane in ('resume', 'save'):
            markers = instances if lane == 'resume' else {i: c + '-imported' for i, c in instances.items()}
            samples = []
            for index, (instance, marker) in enumerate(markers.items()):
                if lane == 'resume':
                    request = ('/elsa/api/events/store-bench/trigger',
                               {'workflowInstanceId': instance, 'correlationId': marker, 'workflowExecutionMode': 0})
                else:
                    state = api.call('/prepare/' + instance)['state']
                    request = ('/save/' + instance, {'state': state, 'marker': marker})
                timed = driver.timed(api, 'resume' if lane == 'resume' else 'import', request)
                if timed['error']:
                    raise RuntimeError(f'{lane} {instance}: {timed["error"]}')
                samples.append(dict(id=instance, phase='warmup' if index < WARMUPS else 'measured',
                                    ms=timed['ms'], error=timed['error'], timeout=timed['timeout']))
            durations = sorted(x['ms'] for x in samples[WARMUPS:])
            result['lanes'][lane] = {
                'requests': samples,
                'p50_ms': durations[math.ceil(.5 * SAMPLES) - 1],
                'p95_ms': durations[math.ceil(.95 * SAMPLES) - 1],
                'max_ms': durations[-1],
                'finished_sql': len(driver.committed(observer, launcher.DEFINITION, markers, finished=True)),
                'finished_api': launcher.audit_api(api, markers),
            }
            if any(result['lanes'][lane][audit] != len(markers) for audit in ('finished_sql', 'finished_api')):
                raise RuntimeError(f'{lane} final audit failed')
    return result


if __name__ == '__main__':
    destination = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent / 'evidence/upstream.serial.json'
    result = run()
    with destination.open('x') as output:
        json.dump(result, output, indent=2)
        output.write('\n')
