#!/usr/bin/env python3
"""Collect all mission-window ULogs and verify finalized bags without flight commands."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shlex
import sqlite3
import subprocess

from halo_swarm_common import LAB_MAPPING, load_json, save_json_atomic


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def collect_ulogs(folder, host, start, end):
    remote = '''import os,json,hashlib
records=[]
for base,dirs,files in os.walk('/data/px4/log'):
 for name in files:
  if not name.endswith('.ulg'): continue
  path=os.path.join(base,name); st=os.stat(path)
  if START <= st.st_mtime <= END:
   digest=hashlib.sha256()
   with open(path,'rb') as stream:
    for block in iter(lambda:stream.read(1048576),b''): digest.update(block)
   records.append({'remote_path':path,'bytes':st.st_size,'mtime':st.st_mtime,'sha256':digest.hexdigest()})
print(json.dumps(records))
'''.replace('START', repr(start)).replace('END', repr(end))
    listing = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', host,
                              'python3 -c ' + shlex.quote(remote)], capture_output=True, text=True, timeout=90)
    if listing.returncode:
        raise RuntimeError('ULog search failed: ' + listing.stderr.strip())
    records = json.loads(listing.stdout)
    for item in records:
        relative = Path(item['remote_path']).relative_to('/data/px4/log')
        if '..' in relative.parts:
            raise ValueError('Invalid remote ULog path')
        target = folder / 'px4_logs/all_mission_logs' / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        # Re-verification never overwrites a previously collected raw file.
        if not target.exists():
            copied = subprocess.run(['rsync', '-a', '-e', 'ssh -o BatchMode=yes -o ConnectTimeout=10',
                                     host + ':' + item['remote_path'], str(target)],
                                    capture_output=True, text=True, timeout=180)
            if copied.returncode:
                raise RuntimeError('ULog copy failed: ' + copied.stderr.strip())
        item['local_path'] = str(target)
        item['local_sha256'] = sha256(target)
        item['hash_matches'] = item['local_sha256'] == item['sha256']
        with target.open('rb') as stream:
            item['ulog_magic_ok'] = stream.read(7) == b'ULog\x01\x12\x35'
    return records


def verify_bag(bag):
    from rclpy.serialization import deserialize_message
    from rosidl_runtime_py.utilities import get_message
    result = {'path': str(bag), 'errors': [], 'topics': {}, 'message_count': 0}
    if not (bag / 'metadata.yaml').is_file():
        result['errors'].append('Finalized bag metadata is missing')
    databases = list(bag.glob('*.db3'))
    if not databases:
        result['errors'].append('No SQLite bag files found')
    first, last = None, None
    for path in databases:
        with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                result['errors'].append('SQLite integrity check failed: ' + path.name)
            low, high, count = db.execute('SELECT MIN(timestamp),MAX(timestamp),COUNT(*) FROM messages').fetchone()
            result['message_count'] += count
            if low is not None:
                first = min(first, low) if first is not None else low
                last = max(last, high) if last is not None else high
            for tid, name, typ in db.execute('SELECT id,name,type FROM topics'):
                item = result['topics'].setdefault(name, {'type': typ, 'count': 0})
                item['count'] += db.execute('SELECT COUNT(*) FROM messages WHERE topic_id=?', (tid,)).fetchone()[0]
                sample = db.execute('SELECT data FROM messages WHERE topic_id=? LIMIT 1', (tid,)).fetchone()
                if sample:
                    try:
                        deserialize_message(bytes(sample[0]), get_message(typ))
                        item['sample_decodes'] = True
                    except Exception as exc:
                        item['sample_decodes'] = False
                        result['errors'].append(f'{name}: {exc}')
    result['duration_s'] = (last - first) / 1e9 if first is not None else 0
    for name in ('vehicle_status', 'sensor_combined', 'timesync_status'):
        if not result['topics'].get('/fmu/out/' + name, {}).get('count'):
            result['errors'].append('No recorded messages: ' + name)
    return result


def finalize_drone(mission, drone, host, start, end, collect=True):
    folder = mission / 'drones' / drone
    final = load_json(folder / 'metadata/worker_final.json')
    result = {'drone_id': drone, 'errors': list(final.get('errors', [])),
              'warnings': list(final.get('warnings', [])), 'ulogs': [],
              'connection_loss_events': final.get('connection_loss_events', []),
              'arm_detected_utc': final.get('arm_detected_utc'),
              'disarm_detected_utc': final.get('disarm_detected_utc')}
    try:
        bag_value = final.get('bag_path') or final.get('bag', {}).get('path')
        if not bag_value:
            raise ValueError('No bag was started for this drone')
        bag = Path(bag_value).resolve()
        bag.relative_to(folder.resolve())
        result['bag'] = verify_bag(bag)
        result['errors'].extend(result['bag']['errors'])
        if final.get('bag_return_code', final.get('bag', {}).get('return_code')) != 0:
            result['errors'].append('Recorder did not exit cleanly')
    except Exception as exc:
        result['errors'].append('Bag verification: ' + str(exc))
    try:
        if collect:
            result['ulogs'] = collect_ulogs(folder, host, start, end)
        else:
            previous = load_json(folder / 'metadata/final_verification.json')
            result['ulogs'] = previous.get('ulogs', [])
            for item in result['ulogs']:
                item['hash_matches'] = sha256(Path(item['local_path'])) == item['sha256']
        if not result['ulogs']:
            result['errors'].append('No mission-window ULogs collected')
        for item in result['ulogs']:
            if not item['hash_matches'] or not item['ulog_magic_ok']:
                result['errors'].append('ULog verification failed: ' + item['remote_path'])
    except Exception as exc:
        result['errors'].append('ULog collection: ' + str(exc))
    if not result['arm_detected_utc']:
        result['errors'].append('No arming was observed')
    if not result['disarm_detected_utc']:
        result['warnings'].append('No disarm transition recorded; inspect termination reason before using this run')
    if result['connection_loss_events']:
        result['warnings'].append('Ground telemetry interruptions occurred; bags may contain gaps')
    result['ok'] = not result['errors']
    result['verification_utc'] = datetime.now(timezone.utc).isoformat()
    save_json_atomic(folder / 'metadata/final_verification.json', result)
    print(f"{drone}: {'PASS' if result['ok'] else 'FAILED'} | {len(result['ulogs'])} ULogs | " + '; '.join(result['errors']), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mission', type=Path)
    parser.add_argument('--verify-only', action='store_true', help='Use already collected ULogs; no SSH')
    args = parser.parse_args()
    mission = args.mission.expanduser().resolve()
    metadata = load_json(mission / 'metadata/mission_metadata.json')
    start = datetime.fromisoformat(metadata['mission_start_utc']).timestamp()
    end = datetime.now(timezone.utc).timestamp()
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(finalize_drone, mission, drone, info['ssh_host'], start, end, not args.verify_only)
                   for drone, info in LAB_MAPPING.items()]
        results = [future.result() for future in futures]
    summary = {'mission_id': mission.name, 'all_passed': all(r['ok'] for r in results),
               'selection_start_epoch': start, 'selection_end_epoch': end, 'drones': results,
               'limitations': 'ULog hashes and headers verified; full ULog semantic decoding not performed. Bag sample decoding does not establish loss-free reception.'}
    save_json_atomic(mission / 'metadata/pipeline_verification.json', summary)
    report = ['# Archive verification', '', f'Mission: `{mission.name}`', '',
              '| Drone | Bag seconds | Messages | ULogs | Result |', '| --- | ---: | ---: | ---: | --- |']
    for r in results:
        bag = r.get('bag', {})
        report.append(f"| {r['drone_id']} | {bag.get('duration_s', 0):.1f} | {bag.get('message_count', 0)} | {len(r['ulogs'])} | {'PASS' if r['ok'] else 'FAILED'} |")
    report += ['', '## Notes', '', summary['limitations'], '',
               'Complete logs are in each drone’s `px4_logs/all_mission_logs/<session>/`. The launcher’s top-level latest ULog may duplicate one of these files.', '']
    for r in results:
        for note in r['errors'] + r['warnings']:
            report.append(f"- {r['drone_id']}: {note}")
    (mission / 'reports').mkdir(exist_ok=True)
    (mission / 'reports/pipeline_verification.md').write_text('\n'.join(report) + '\n')
    return 0 if summary['all_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
