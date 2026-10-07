import base64
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
        with tempfile.TemporaryDirectory() as directory, patch.object(native,'measure_asset',return_value={'nativePageAlignment':4096}) as measure:
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
                self.assertEqual(16384,native.measure_asset(asset,directory)['nativePageAlignment'])
            with patch.object(native,'urlopen',side_effect=AssertionError('Cache should avoid network')):
                cached=native.measure_asset(asset,directory)
                self.assertEqual(16384,cached['nativePageAlignment'])
                self.assertIsNone(cached['supportedAbisMetaData'])
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

    # AndroidManifest.xml of sample/supported-abis (AutoJs6-Gradle-Platform-Versions), compiled by AGP 9.1.1:
    # two <meta-data> entries, SUPPORTED_ABIS="arm64-v8a,x86_64" written by the nativeAlignment plugin.
    MANIFEST = base64.b64decode(
        'AwAIALgGAAABABwAEAQAABgAAAAAAAAAAAAAAHwAAAAAAAAAAAAAAA4AAAAaAAAAMgAAAEAAAABeAAAAggAAAKgAAADOAAAABAEAAAwBAAA8AQAATgEAAGgBAACMAQAA5AEAAPgBAAAOAgAAcgIAAMgCAAAGAwAAGAMAAEwDAACAAwAABQBsAGEAYgBlAGwAAAAEAG4AYQBtAGUAAAAKAGQAZQBiAHUAZwBnAGEAYgBsAGUAAAAFAHYAYQBsAHUAZQAAAA0AbQBpAG4AUwBkAGsAVgBlAHIAcwBpAG8AbgAAABAAdABhAHIAZwBlAHQAUwBkAGsAVgBlAHIAcwBpAG8AbgAAABEAZQB4AHQAcgBhAGMAdABOAGEAdABpAHYAZQBMAGkAYgBzAAAAEQBjAG8AbQBwAGkAbABlAFMAZABrAFYAZQByAHMAaQBvAG4AAAAZAGMAbwBtAHAAaQBsAGUAUwBkAGsAVgBlAHIAcwBpAG8AbgBDAG8AZABlAG4AYQBtAGUAAAACADEANwAAABYAUwB1AHAAcABvAHIAdABlAGQAIABBAEIASQBzACAAZgBpAHgAdAB1AHIAZQAAAAcAYQBuAGQAcgBvAGkAZAAAAAsAYQBwAHAAbABpAGMAYQB0AGkAbwBuAAAAEABhAHIAbQA2ADQALQB2ADgAYQAsAHgAOAA2AF8ANgA0AAAAKgBoAHQAdABwADoALwAvAHMAYwBoAGUAbQBhAHMALgBhAG4AZAByAG8AaQBkAC4AYwBvAG0ALwBhAHAAawAvAHIAZQBzAC8AYQBuAGQAcgBvAGkAZAAAAAgAbQBhAG4AaQBmAGUAcwB0AAAACQBtAGUAdABhAC0AZABhAHQAYQAAADAAbwByAGcALgBhAHUAdABvAGoAcwAuAHAAbAB1AGcAaQBuAC4AYwBvAG4AdAByAGEAYwB0AC4ATgBBAFQASQBWAEUAXwBQAEEARwBFAF8AQQBMAEkARwBOAE0ARQBOAFQAAAApAG8AcgBnAC4AYQB1AHQAbwBqAHMALgBwAGwAdQBnAGkAbgAuAGMAbwBuAHQAcgBhAGMAdAAuAFMAVQBQAFAATwBSAFQARQBEAF8AQQBCAEkAUwAAAB0AbwByAGcALgBhAHUAdABvAGoAcwAuAHQAZQBzAHQALgBzAHUAcABwAG8AcgB0AGUAZABhAGIAaQBzAAAABwBwAGEAYwBrAGEAZwBlAAAAGABwAGwAYQB0AGYAbwByAG0AQgB1AGkAbABkAFYAZQByAHMAaQBvAG4AQwBvAGQAZQAAABgAcABsAGEAdABmAG8AcgBtAEIAdQBpAGwAZABWAGUAcgBzAGkAbwBuAE4AYQBtAGUAAAAIAHUAcwBlAHMALQBzAGQAawAAAIABCAAsAAAAAQABAQMAAQEPAAEBJAABAQwCAQFwAgEB6gQBAXIFAQFzBQEBAAEQABgAAAACAAAA/////wsAAAAOAAAAAgEQAIgAAAACAAAA//////////8PAAAAFAAUAAUAAAAAAAAADgAAAAcAAAD/////CAAAECUAAAAOAAAACAAAAAkAAAAIAAADCQAAAP////8UAAAAEwAAAAgAAAMTAAAA/////xUAAAD/////CAAAECUAAAD/////FgAAAP////8IAAAQEQAAAAIBEABMAAAABQAAAP//////////FwAAABQAFAACAAAAAAAAAA4AAAAEAAAA/////wgAABAYAAAADgAAAAUAAAD/////CAAAECUAAAADARAAGAAAAAUAAAD//////////xcAAAACARAAYAAAAAkAAAD//////////wwAAAAUABQAAwAAAAAAAAAOAAAAAAAAAAoAAAAIAAADCgAAAA4AAAACAAAA/////wgAABL/////DgAAAAYAAAD/////CAAAEgAAAAACARAATAAAAA0AAAD//////////xAAAAAUABQAAgAAAAAAAAAOAAAAAQAAABEAAAAIAAADEQAAAA4AAAADAAAA/////wgAABAAQAAAAwEQABgAAAANAAAA//////////8QAAAAAgEQAEwAAAAQAAAA//////////8QAAAAFAAUAAIAAAAAAAAADgAAAAEAAAASAAAACAAAAxIAAAAOAAAAAwAAAA0AAAAIAAADDQAAAAMBEAAYAAAAEAAAAP//////////EAAAAAMBEAAYAAAACQAAAP//////////DAAAAAMBEAAYAAAAAgAAAP//////////DwAAAAEBEAAYAAAAAgAAAP////8LAAAADgAAAA=='
    )

    def test_supported_abis_meta_data_is_read_from_the_compiled_manifest(self):
        import axml
        self.assertEqual({'org.autojs.plugin.contract.NATIVE_PAGE_ALIGNMENT':'16384','org.autojs.plugin.contract.SUPPORTED_ABIS':'arm64-v8a,x86_64'},
                         axml.manifest_meta_data(self.MANIFEST))
        with self.assertRaises(axml.AxmlError): axml.manifest_meta_data(b'<manifest/>')
        result=native.measure_apk(io.BytesIO(archive({'AndroidManifest.xml':self.MANIFEST,'lib/arm64-v8a/native.so':elf()})))
        self.assertEqual('arm64-v8a,x86_64',result['supportedAbisMetaData'])
        self.assertIsNone(native.measure_apk(io.BytesIO(archive({'classes.dex':b'dex'})))['supportedAbisMetaData'])

    def test_release_supported_abis_needs_every_selected_apk_to_agree(self):
        assets=[{'name':'p-arm64-v8a.apk'},{'name':'p-x86_64.apk'},{'name':'p-armeabi-v7a.apk'}]
        with patch.object(native,'measure_asset',return_value={'nativePageAlignment':16384,'supportedAbisMetaData':'arm64-v8a,x86_64'}) as measure:
            self.assertEqual('arm64-v8a,x86_64',native.release_supported_abis(assets,cache_root='unused'))
            self.assertEqual(2,measure.call_count)
        with patch.object(native,'measure_asset',side_effect=[{'supportedAbisMetaData':'arm64-v8a'},{'supportedAbisMetaData':'x86_64'}]):
            with self.assertRaises(ValueError): native.release_supported_abis(assets,cache_root='unused')
        with patch.object(native,'measure_asset',side_effect=[{'supportedAbisMetaData':'arm64-v8a'},None]):
            self.assertIsNone(native.release_supported_abis(assets,cache_root='unused'))
        with patch.object(native,'measure_asset',return_value={'nativePageAlignment':0,'supportedAbisMetaData':None}):
            self.assertIsNone(native.release_supported_abis([{'name':'p-universal.apk'}],cache_root='unused'))

    def test_apk_meta_data_outranks_declarations_and_asset_names(self):
        resolve=generator.resolve_supported_abis
        self.assertEqual(['arm64-v8a','x86_64'],resolve('t',apk=['arm64-v8a','x86_64'],declared=None,assets=['arm64-v8a','x86_64']))
        self.assertEqual(['arm64-v8a'],resolve('t',apk=None,declared=['arm64-v8a'],assets=None))
        self.assertEqual(['x86_64'],resolve('t',apk=None,declared=None,assets=['x86_64']))
        self.assertIsNone(resolve('t',apk=[],declared=None,assets=None))
        self.assertEqual([],generator.parse_apk_supported_abis('universal',source='t'))
        self.assertIsNone(generator.parse_apk_supported_abis(None,source='t'))
        self.assertEqual(['arm64-v8a','x86_64'],generator.parse_apk_supported_abis('x86_64,arm64-v8a',source='t'))
        with self.assertRaisesRegex(RuntimeError,'remove the hand-written declaration'):
            resolve('t',apk=['arm64-v8a','x86_64'],declared=['arm64-v8a'],assets=None)
        with self.assertRaisesRegex(RuntimeError,'release asset names'):
            resolve('t',apk=['arm64-v8a'],declared=None,assets=['arm64-v8a','x86_64'])
        with self.assertRaisesRegex(RuntimeError,'conflicts with release asset names'):
            resolve('t',apk=None,declared=['x86_64'],assets=['arm64-v8a'])
        with self.assertRaisesRegex(RuntimeError,'unsupported ABI values'):
            generator.parse_apk_supported_abis('arm64-v8a,sparc',source='t')
