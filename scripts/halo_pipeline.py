#!/usr/bin/env python3
"""Background five-drone archive lifecycle and operator controls."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

from halo_swarm_common import load_json, save_json_atomic

REPO = Path(__file__).resolve().parents[1]
POINTER = REPO / '.halo_pipeline.json'
# Use a new runtime lock name so an interrupted older supervisor cannot leave
# this repository permanently blocked by an inherited file descriptor.
LOCK = REPO / '.halo_pipeline_runtime2.lock'
TERMINAL = {'COMPLETE', 'FAILED', 'STOPPED'}


def process_token(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def alive(state):
    return bool(state.get('process_token') and process_token(state.get('pid')) == state['process_token'])


def current():
    pointer = load_json(POINTER)
    return load_json(Path(pointer['state_file'])) if pointer.get('state_file') else {}


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', nargs='?', default='start', choices=['start', 'status', 'stop', '_run'])
    p.add_argument('--name', default='five_drone_rosbag_px4')
    p.add_argument('--operator', default=os.environ.get('USER', 'operator'))
    p.add_argument('--archive-root', type=Path, default=REPO / 'archives')
    p.add_argument('--duration', type=int, default=600, help='Maximum monitoring seconds, including waiting for arm (default 600)')
    p.add_argument('--preflight-only', action='store_true', help='Check readiness without recording or expecting new ULogs')
    p.add_argument('--enable-audio', action='store_true', help='Also enable per-drone ReSpeaker audio')
    p.add_argument('--detach', action='store_true', help='Return immediately instead of waiting for READY')
    p.add_argument('--dry-run', action='store_true', help='Validate configuration without SSH or archive creation')
    p.add_argument('--px4-msgs-workspace', type=Path, default=Path(os.environ.get('HALO_PX4_MSGS_SETUP', REPO / 'runtime/px4_ros2_humble_ws/install/setup.bash')))
    p.add_argument('--job', type=Path, help=argparse.SUPPRESS)
    return p


def launcher_command(config):
    cmd = [str(REPO / 'scripts/launch_halo_swarm.sh'), '--mission-name', config['name'],
           '--operator', config['operator'], '--archive-root', config['archive_root'],
           '--mission-id', config['mission_id'], '--duration', str(config['duration']),
           '--px4-msgs-workspace', config['workspace']]
    if config['preflight_only']:
        cmd += ['--preflight-only', '--no-auto-ulog']
    if config['enable_audio']:
        cmd.append('--enable-audio')
    return cmd


def run_job(job):
    config = load_json(job / 'config.json')
    state_path = job / 'state.json'
    mission = Path(config['archive_root']) / config['mission_id']
    state = dict(config, pid=os.getpid(), process_token=process_token(os.getpid()),
                 mission_dir=str(mission), log=str(job / 'pipeline.log'), phase='STARTING')
    child = None
    stopping = False

    def update(phase, **extra):
        state.update(phase=phase, updated_utc=datetime.now(timezone.utc).isoformat(), **extra)
        save_json_atomic(state_path, state)
        if (mission / 'metadata').is_dir():
            save_json_atomic(mission / 'metadata/pipeline_status.json', state)

    def stop(signum, frame):
        nonlocal stopping
        stopping = True
        if child is not None and child.poll() is None:
            child.send_signal(signal.SIGTERM)
        print('Stopping archive workers cleanly; no drone flight command is sent.', flush=True)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    update('STARTING')
    try:
        child = subprocess.Popen(launcher_command(config), cwd=REPO, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, bufsize=1)
        for line in child.stdout:
            print(line, end='', flush=True)
            if 'SWARM ARCHIVE READY' in line:
                update('READY', readiness_passed=True)
                if not config['preflight_only']:
                    print('READY: You may arm under your normal test procedure. Disarm after your planned interval. Collection is automatic.', flush=True)
        launcher_code = child.wait()
        child.stdout.close()
        child = None
        update('COLLECTING', launcher_exit_code=launcher_code)
        if (mission / 'config').is_dir():
            shutil.copytree(REPO / 'config/fastdds', mission / 'config/fastdds', dirs_exist_ok=True)
            message_source = Path(config['workspace']).parent.parent / 'src/px4_msgs/msg'
            if message_source.is_dir():
                shutil.copytree(message_source, mission / 'config/px4_msgs/msg', dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns('*.idl'))
        if config['preflight_only']:
            final_code = launcher_code
        elif (mission / 'metadata/mission_metadata.json').exists():
            # Use a clean Humble setup plus the same message definitions as recording.
            command = ['python3', str(REPO / 'scripts/finalize_halo_archive.py'), str(mission)]
            shell = 'set -e; source /opt/ros/humble/setup.bash; source ' + shlex.quote(config['workspace']) + '; exec ' + shlex.join(command)
            # Finalization is allowed to finish even if a stop was requested.
            final_code = subprocess.run(['bash', '-c', shell], cwd=REPO).returncode
        else:
            final_code = 1
        phase = 'STOPPED' if stopping else 'COMPLETE' if launcher_code == 0 and final_code == 0 else 'FAILED'
        update(phase, verification_exit_code=final_code)
        print(f'{phase}: {mission}', flush=True)
    except Exception as exc:
        if child is not None and child.poll() is None:
            child.send_signal(signal.SIGTERM)
            child.wait(timeout=180)
        update('FAILED', error=f'{type(exc).__name__}: {exc}')
        print(state['error'], flush=True)
    finally:
        if (mission / 'metadata').is_dir():
            shutil.copy2(job / 'config.json', mission / 'metadata/pipeline_config.json')
            sys.stdout.flush()
            shutil.copy2(job / 'pipeline.log', mission / 'metadata/pipeline.log')
    return 0 if state['phase'] == 'COMPLETE' else 1


def main():
    args = parser().parse_args()
    if args.action == '_run':
        return run_job(args.job)
    if args.action in {'status', 'stop'}:
        state = current()
        if not state:
            print('No pipeline run has been started.'); return 1
        running = alive(state)
        print(f"{state['phase']} | running={running}\nArchive: {state['mission_dir']}\nLog: {state['log']}")
        mission = Path(state['mission_dir'])
        for path in sorted(mission.glob('drones/*/metadata/worker_status.json')):
            d = load_json(path)
            print(f"{path.parts[-3]}: {d.get('phase')} | {d.get('vehicle_state', 'finalized' if d.get('phase') == 'DONE' else 'checking')}")
        if args.action == 'stop' and running and state['phase'] not in TERMINAL:
            os.kill(state['pid'], signal.SIGTERM)
            print('Stop requested. Bags will finalize and available logs will be collected. This does not disarm drones.')
        return 0
    if args.duration <= 0:
        raise ValueError('--duration must be positive')
    workspace = args.px4_msgs_workspace.expanduser().resolve()
    if not workspace.is_file():
        raise ValueError(f'Missing message workspace: {workspace}; see docs/ONE_COMMAND_PIPELINE.md')
    slug = re.sub(r'[^A-Za-z0-9._-]+', '_', args.name).strip('._-') or 'halo_test'
    mission_id = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f_UTC_') + slug
    config = dict(name=args.name, operator=args.operator, archive_root=str(args.archive_root.expanduser().resolve()),
                  mission_id=mission_id, duration=args.duration, workspace=str(workspace),
                  preflight_only=args.preflight_only, enable_audio=args.enable_audio)
    if args.dry_run:
        return subprocess.run(launcher_command(config) + ['--dry-run'], cwd=REPO).returncode
    with LOCK.open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('A HALO pipeline is already running. Use ./halo.sh status or ./halo.sh stop.', file=sys.stderr)
            return 1
        job = Path(config['archive_root']) / '.pipeline' / mission_id
        job.mkdir(parents=True, exist_ok=False)
        save_json_atomic(job / 'config.json', config)
        with (job / 'pipeline.log').open('w') as log:
            proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '_run', '--job', str(job)],
                                    cwd=REPO, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                    start_new_session=True, pass_fds=(lock.fileno(),))
        initial = dict(config, pid=proc.pid, process_token=process_token(proc.pid), phase='STARTING',
                       mission_dir=str(Path(config['archive_root']) / mission_id), log=str(job / 'pipeline.log'))
        # Child owns its status; parent stores only the lookup pointer.
        save_json_atomic(POINTER, {'state_file': str(job / 'state.json')})
        print(f"Background pipeline PID: {proc.pid}\nArchive: {initial['mission_dir']}\nLog: {initial['log']}", flush=True)
        print('Status: ./halo.sh status | Stop recording/collect: ./halo.sh stop', flush=True)
        if args.detach:
            return 0
        print('Waiting for all five drones to be READY; the job continues if this terminal closes.', flush=True)
        deadline = time.monotonic() + 180
        try:
            while time.monotonic() < deadline:
                state = load_json(job / 'state.json')
                if state.get('readiness_passed') and state.get('phase') in {'READY', 'COLLECTING', 'COMPLETE'}:
                    if args.preflight_only:
                        print('Preflight checks passed; finishing diagnostic collection in the background.')
                    elif state['phase'] == 'READY':
                        print('ALL FIVE READY — arm under your normal test procedure, then disarm after your planned interval. Collection is automatic.')
                    else:
                        print(f"Pipeline phase: {state['phase']}. See ./halo.sh status.")
                    # Keep the foreground supervisor alive through recording and
                    # collection. This prevents terminal/session cleanup from
                    # killing the worker processes immediately after READY.
                    if state['phase'] == 'READY' and not args.preflight_only:
                        while True:
                            latest = load_json(job / 'state.json')
                            if latest.get('phase') in TERMINAL:
                                return 0 if latest.get('phase') == 'COMPLETE' else 1
                            time.sleep(1)
                    return 0
                if state.get('phase') in TERMINAL or proc.poll() is not None:
                    print('Pipeline did not become ready. Inspect: ' + initial['log'], file=sys.stderr)
                    return 1
                time.sleep(0.5)
        except KeyboardInterrupt:
            print('\nPipeline remains running. Use ./halo.sh stop to stop it cleanly.')
        print('Still working in the background. Use ./halo.sh status and follow the log before arming.')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
