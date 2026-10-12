"""Preserve the dependency's index semantics while changing byte searching."""
import importlib.util
from pathlib import Path
import random
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
from fast_index import h264_nal_type, index_media


@unittest.skipUnless(importlib.util.find_spec('smartcut'), '需要 smartcut')
class FastIndexTests(unittest.TestCase):
    def test_matches_pinned_parser_on_length_prefixed_and_annexb_data(self):
        from smartcut.nal_tools import get_h264_nal_unit_type
        rng = random.Random(731)
        fixtures = [b'', b'\0\0\0\1\x65', b'\0\0\1\x65',
                    b'\0\0\0\2\x09\xf0\0\0\0\8\x65' + bytes(7),
                    b'\0\0\1\x67foo\0\0\0\1\x65bar']
        for _ in range(200):
            data = bytearray(rng.randbytes(rng.randrange(5, 2048)))
            for _ in range(rng.randrange(1, 5)):
                pos = rng.randrange(len(data) - 4)
                marker = rng.choice((b'\0\0\1', b'\0\0\0\1')) + bytes([rng.randrange(256)])
                data[pos:pos + len(marker)] = marker
            fixtures.append(bytes(data))
        for data in fixtures:
            self.assertEqual(h264_nal_type(data), get_h264_nal_unit_type(data))

    def test_module_parser_is_restored_on_index_failure(self):
        import smartcut.media_container as module
        original = module.get_h264_nal_unit_type
        def fail(_path):
            self.assertIs(module.get_h264_nal_unit_type, h264_nal_type)
            raise RuntimeError('broken input')
        with self.assertRaisesRegex(RuntimeError, 'broken input'):
            index_media('input.mp4', fail)
        self.assertIs(module.get_h264_nal_unit_type, original)
