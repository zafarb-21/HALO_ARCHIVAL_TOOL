"""Offline regression checks; never contact drones or send flight commands."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import halo_pipeline as pipeline
import finalize_halo_archive as finalizer


class PipelineTests(unittest.TestCase):
    def test_failed_launcher_cannot_report_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            job = Path(temp) / 'job'; job.mkdir()
            config = dict(archive_root=temp, mission_id='test', name='test', operator='test',
                          duration=10, workspace='/unused', preflight_only=True, enable_audio=False)
            (job / 'config.json').write_text(json.dumps(config))
            (job / 'pipeline.log').write_text('')
            fake = [sys.executable, '-c', "print('SWARM ARCHIVE READY'); raise SystemExit(2)"]
            with patch.object(pipeline, 'launcher_command', return_value=fake), contextlib.redirect_stdout(io.StringIO()):
                code = pipeline.run_job(job)
            state = json.loads((job / 'state.json').read_text())
            self.assertEqual(code, 1)
            self.assertEqual(state['phase'], 'FAILED')
            self.assertEqual(state['launcher_exit_code'], 2)

    def test_successful_preflight_finishes_without_finalizer(self):
        with tempfile.TemporaryDirectory() as temp:
            job = Path(temp) / 'job'; job.mkdir()
            config = dict(archive_root=temp, mission_id='test', name='test', operator='test',
                          duration=10, workspace='/unused', preflight_only=True, enable_audio=False)
            (job / 'config.json').write_text(json.dumps(config)); (job / 'pipeline.log').write_text('')
            fake = [sys.executable, '-c', "print('SWARM ARCHIVE READY')"]
            with patch.object(pipeline, 'launcher_command', return_value=fake), patch.object(pipeline.subprocess, 'run') as finalize, contextlib.redirect_stdout(io.StringIO()):
                code = pipeline.run_job(job)
            finalize.assert_not_called()
            self.assertEqual(code, 0)
            self.assertEqual(json.loads((job / 'state.json').read_text())['phase'], 'COMPLETE')

    def test_stale_pid_is_not_treated_as_running(self):
        self.assertFalse(pipeline.alive({'pid': 99999999, 'process_token': '123'}))

    def test_existing_raw_log_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            dest = folder / 'px4_logs/all_mission_logs/sess1/log100.ulg'
            dest.parent.mkdir(parents=True); dest.write_bytes(b'ULog\x01\x12\x35original')
            remote = [{'remote_path': '/data/px4/log/sess1/log100.ulg', 'sha256': 'changed'}]
            response = subprocess.CompletedProcess([], 0, json.dumps(remote), '')
            with patch.object(finalizer.subprocess, 'run', return_value=response) as run:
                result = finalizer.collect_ulogs(folder, 'fake', 1, 2)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(dest.read_bytes(), b'ULog\x01\x12\x35original')
            self.assertFalse(result[0]['hash_matches'])

    def test_repeated_log_names_keep_session_directories(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp); payload = b'ULog\x01\x12\x35test'
            digest = __import__('hashlib').sha256(payload).hexdigest()
            records = [{'remote_path': f'/data/px4/log/sess{i}/log100.ulg', 'sha256': digest} for i in (1, 2)]
            def run(command, **kwargs):
                if command[0] == 'ssh':
                    return subprocess.CompletedProcess(command, 0, json.dumps(records), '')
                Path(command[-1]).write_bytes(payload)
                return subprocess.CompletedProcess(command, 0, '', '')
            with patch.object(finalizer.subprocess, 'run', side_effect=run):
                result = finalizer.collect_ulogs(folder, 'fake', 1, 2)
            self.assertEqual(len({r['local_path'] for r in result}), 2)
            self.assertTrue(all(r['hash_matches'] and r['ulog_magic_ok'] for r in result))

    def test_failed_drone_does_not_claim_success(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            with patch.object(finalizer, 'collect_ulogs', return_value=[]):
                result = finalizer.finalize_drone(Path(temp), 'D0012', 'fake', 1, 2)
            self.assertFalse(result['ok'])
            self.assertIn('No mission-window ULogs collected', result['errors'])
            self.assertIn('No arming was observed', result['errors'])


if __name__ == '__main__':
    unittest.main()
