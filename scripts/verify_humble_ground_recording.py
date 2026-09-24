#!/usr/bin/env python3
"""Record and decode a 15-second passive telemetry sample from all five lab drones.

Source Humble and the firmware-matched px4_msgs workspace before running.
No flight commands are sent. Each run creates a new diagnostics archive.
"""
import concurrent.futures
import datetime
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import time
from rclpy.serialization import deserialize_message
from rosidl_runtime_py.utilities import get_message

repo = Path(__file__).resolve().parents[1]
root = repo / 'diagnostics' / (datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d_%H%M%S_UTC') + '_humble_passive_bag_test')
root.mkdir(parents=True, exist_ok=False)
print('Archive:', root, flush=True)
topics = ['/fmu/out/vehicle_status', '/fmu/out/sensor_combined', '/fmu/out/timesync_status']

def verify(number):
    drone = f'D{number:04d}'
    folder = root / drone
    folder.mkdir()
    env = os.environ.copy()
    env.update(ROS_DOMAIN_ID=str(number - 9), ROS_LOCALHOST_ONLY='0', RMW_IMPLEMENTATION='rmw_fastrtps_cpp', FASTRTPS_DEFAULT_PROFILES_FILE=str(repo / 'config' / 'fastdds' / f'humble_{drone}.xml'))
    result = {'drone_id': drone, 'domain': number - 9, 'record_seconds': 15, 'topics': {}, 'errors': []}
    try:
        discovery = subprocess.run(['ros2', 'topic', 'list', '--no-daemon', '--spin-time', '5', '-t'], env=env, capture_output=True, text=True, timeout=20)
        (folder / 'topics.txt').write_text(discovery.stdout + discovery.stderr)
        if discovery.returncode:
            raise RuntimeError('Topic discovery failed')
        with (folder / 'recorder.log').open('w') as log:
            process = subprocess.Popen(['ros2', 'bag', 'record', '-s', 'sqlite3', '-o', str(folder / 'bag'), *topics], env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                time.sleep(15)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGINT)
                try:
                    result['recorder_exit_code'] = process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=10)
                    raise RuntimeError('Recorder did not finalize with SIGINT')
        if not (folder / 'bag' / 'metadata.yaml').is_file():
            raise RuntimeError('Bag metadata missing')
        info = subprocess.run(['ros2', 'bag', 'info', str(folder / 'bag')], env=env, capture_output=True, text=True, timeout=15)
        (folder / 'bag_info.txt').write_text(info.stdout + info.stderr)
        result['bag_info_ok'] = info.returncode == 0
        database = next((folder / 'bag').glob('*.db3'))
        with sqlite3.connect(f'file:{database}?mode=ro', uri=True) as db:
            for topic in topics:
                row = db.execute('SELECT id,type FROM topics WHERE name=?', (topic,)).fetchone()
                if row is None:
                    raise RuntimeError(f'Missing topic {topic}')
                count = db.execute('SELECT COUNT(*) FROM messages WHERE topic_id=?', (row[0],)).fetchone()[0]
                sample = db.execute('SELECT data FROM messages WHERE topic_id=? ORDER BY timestamp LIMIT 1', (row[0],)).fetchone()
                if sample is None:
                    raise RuntimeError(f'No messages on {topic}')
                decoded = deserialize_message(bytes(sample[0]), get_message(row[1]))
                result['topics'][topic] = {'type': row[1], 'count': count, 'decoded': True, 'px4_timestamp': int(decoded.timestamp)}
                if hasattr(decoded, 'arming_state'):
                    result['topics'][topic]['arming_state'] = int(decoded.arming_state)
        result['ok'] = result['bag_info_ok'] and result['recorder_exit_code'] == 0
    except Exception as exc:
        result['ok'] = False
        result['errors'].append(str(exc))
    (folder / 'verification.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result), flush=True)
    return result

with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
    results = list(pool.map(verify, range(12, 17)))
summary = {'test': 'passive ground recording; no flight commands, no audio, no ULog expected', 'archive': str(root), 'all_passed': all(x['ok'] for x in results), 'drones': results}
(root / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
print('ALL PASSED:', summary['all_passed'], flush=True)
raise SystemExit(0 if summary['all_passed'] else 1)
