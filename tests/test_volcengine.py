"""Exercise provider routing, task polling, word protection and secret isolation."""
import copy
import json
import os
import shutil
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import volcengine_asr as asr
import transcriber
import video_editor as editor

RAW = {'result': {'utterances': [{'text': 'Hello again.', 'start_time': 1200, 'end_time': 2300,
       'words': [{'text': 'Hello', 'start_time': 1200, 'end_time': 1600},
                 {'text': 'again', 'start_time': 1900, 'end_time': 2300}]}]}}


class VolcengineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.audio = self.root / 'audio.wav'
        with wave.open(str(self.audio), 'wb') as f:
            f.setnchannels(1); f.setsampwidth(2); f.setframerate(16000)
            f.writeframes(b'\x00\x00' * 48000)
        self.output = self.root / 'transcript.json'

    def test_real_word_times_preserved_and_missing_times_rejected(self):
        segments = asr.convert_response(RAW)
        self.assertEqual(segments[0]['words'][1], {'word': 'again', 'start': 1.9, 'end': 2.3})
        bad = copy.deepcopy(RAW)
        del bad['result']['utterances'][0]['words'][0]['start_time']
        with self.assertRaises(ValueError): asr.convert_response(bad)
        bad['result']['utterances'][0]['words'] = []
        with self.assertRaises(ValueError): asr.convert_response(bad)

    def test_submit_poll_cache_and_no_secret_in_artifacts(self):
        with patch.object(asr, 'load_api_key', return_value='test-secret'), patch.object(asr.time, 'sleep'), \
             patch.object(asr, '_post', side_effect=[('20000000', {}, 'log-test'),
                 ('20000002', {}, ''), ('20000001', {}, ''), ('20000000', RAW, '')]) as post:
            segments = asr.transcribe_audio_volcengine(self.audio, self.output)
            self.assertEqual(asr.transcribe_audio_volcengine(self.audio, self.output), segments)
        self.assertEqual(post.call_count, 4)
        submit = post.call_args_list[0]
        self.assertEqual(submit.args[0], asr.SUBMIT_ENDPOINT)
        self.assertFalse(submit.args[1]['request']['enable_ddc'])
        self.assertFalse(submit.args[1]['request']['enable_itn'])
        self.assertEqual(submit.args[2]['X-Api-Resource-Id'], 'volc.seedasr.auc')
        self.assertEqual(post.call_args_list[1].args[2]['X-Tt-Logid'], 'log-test')
        for file in self.root.glob('*.json'):
            self.assertNotIn('test-secret', file.read_text())

    def test_poll_failure_resumes_without_second_upload(self):
        with patch.object(asr, 'load_api_key', return_value='test-secret'), patch.object(asr.time, 'sleep'), \
             patch.object(asr, '_post', side_effect=[('20000000', {}, 'log-test'), RuntimeError('network')]):
            with self.assertRaises(RuntimeError): asr.transcribe_audio_volcengine(self.audio, self.output)
        self.assertFalse(self.output.exists())
        with patch.object(asr, 'load_api_key', return_value='test-secret'), patch.object(asr.time, 'sleep'), \
             patch.object(asr, '_post', return_value=('20000000', RAW, '')) as post:
            asr.transcribe_audio_volcengine(self.audio, self.output)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.args[0], asr.QUERY_ENDPOINT)

    def test_provider_change_invalidates_transcript_and_pending_job(self):
        source = self.root / 'source.mp4'; source.write_bytes(b'source')
        work = self.root / 'work'
        editor.prepare_cache(source, work, 'bailian')
        for name in ('transcript.json', 'transcript_volcengine_job.json'):
            (work / name).write_text('{}')
        editor.prepare_cache(source, work, 'volcengine')
        self.assertFalse((work / 'transcript.json').exists())
        self.assertFalse((work / 'transcript_volcengine_job.json').exists())

    def test_volcengine_never_uses_bailian_key(self):
        with patch.dict(os.environ, {'DASHSCOPE_API_KEY': 'wrong-provider', 'VOLCENGINE_ASR_API_KEY': ''}):
            with self.assertRaises(RuntimeError): transcriber.load_api_key('volcengine')

    @unittest.skipUnless(os.name == 'nt' and shutil.which('powershell'), 'Windows PowerShell required')
    def test_windows_launcher_forwards_short_output_option(self):
        script = Path(editor.__file__).with_name('run.ps1')
        result = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                                 str(script), 'cut', '--help', '-o', 'example.mp4'],
                                capture_output=True, encoding='utf-8', errors='replace')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('usage:', result.stdout)


if __name__ == '__main__': unittest.main()
