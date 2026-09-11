import hashlib
import io
import json
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import native_page_alignment as native
import generate_official_plugin_index as generator


def elf(alignment=16384, machine=183):
    data = bytearray(120)
    data[:7] = b'\x7fELF\x02\x01\x01'
    struct.pack_into('<H', data, 18, machine)
    struct.pack_into('<Q', data, 32, 64)
    struct.pack_into('<HH', data, 54, 56, 1)
    struct.pack_into('<I', data, 64, 1)
    struct.pack_into('<Q', data, 112, alignment)
    return data


def archive(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, data in entries.items(): z.writestr(name, data)
    return output.getvalue()


class NativePageAlignmentTest(unittest.TestCase):
    def test_pure_java_and_nested_native_modules(self):
        self.assertEqual(0, native.measure_apk(io.BytesIO(archive({'classes.dex':b'dex'})))['nativePageAlignment'])
        payload = archive({'ssl.so':elf(4096)})
        result = native.measure_apk(io.BytesIO(archive({'assets/stdlib.imy':payload, 'lib/arm64-v8a/aligned.so':elf()})))
        self.assertEqual(4096, result['nativePageAlignment'])
        self.assertEqual(2, len(result['entries']))

    def test_malformed_and_incongruent_elf_fail(self):
        for data in (bytes(80), elf(0), elf(12288), elf()[:80]):
            with self.assertRaises((ValueError, struct.error)): native.elf_alignment(data)
        data=elf(); struct.pack_into('<Q', data, 72, 1)
        with self.assertRaises(ValueError): native.elf_alignment(data)

    def test_measurements_override_declarations_and_32_bit_is_advisory(self):
        assets=[{'name':'arm64-v8a.apk'}, {'name':'armeabi-v7a.apk'}]
        with tempfile.TemporaryDirectory() as directory, patch.object(native,'measure_asset',return_value=4096) as measure:
            value=native.release_alignment(assets,16384,cache_root=directory)
            self.assertEqual({'nativePageAlignment':4096,'nativePageAlignmentSource':'measured'},value)
            self.assertEqual(1,measure.call_count)

    def test_unavailable_is_unknown_or_declared_not_pure_java(self):
        with patch.object(native,'measure_asset',return_value=None):
            self.assertEqual({},native.release_alignment([{}],cache_root='unused'))
            self.assertEqual({'nativePageAlignment':0,'nativePageAlignmentSource':'declared'},native.release_alignment([{}],0,cache_root='unused'))

    def test_sha_cache_is_content_bound_and_mismatches_fail(self):
        data=archive({'lib/arm64-v8a/native.so':elf()})
        digest=hashlib.sha256(data).hexdigest()
        asset={'browser_download_url':'https://example.invalid/native.apk','digest':'sha256:'+digest}
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(native,'urlopen',return_value=io.BytesIO(data)):
                self.assertEqual(16384,native.measure_asset(asset,directory))
            with patch.object(native,'urlopen',side_effect=AssertionError('Cache should avoid network')):
                self.assertEqual(16384,native.measure_asset(asset,directory))
            asset['digest']='sha256:'+'0'*64
            with patch.object(native,'urlopen',return_value=io.BytesIO(data)):
                with self.assertRaises(ValueError): native.measure_asset(asset,directory)

    def test_latest_release_and_declaration_validation(self):
        item={'nativePageAlignment':4096,'releases':[{'versionCode':1,'nativePageAlignment':4096},{'versionCode':2}]}
        generator.copy_latest_native_alignment(item)
        self.assertNotIn('nativePageAlignment',item)
        for invalid in ('-1','16384.0','16385','9223372036854775808'):
            with self.assertRaises(RuntimeError): generator.parse_native_alignment(invalid,source='test')
        self.assertEqual(0,generator.parse_native_alignment('0',source='test'))
        self.assertEqual(16384,generator.parse_native_alignment('16384',source='test'))
