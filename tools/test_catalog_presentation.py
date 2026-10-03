import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import catalog_presentation as catalog


class CatalogPresentationTest(unittest.TestCase):
    def test_checked_in_artwork_has_content_addressed_paths(self):
        artwork = catalog.load_artwork()
        self.assertEqual(12, len(artwork))
        self.assertTrue(all(value['day']['sha256'] != value['night']['sha256'] for value in artwork.values()))

    def test_presentation_does_not_change_release_identity_or_artifact_facts(self):
        package = 'io.github.supermonster003.autojs6.plugin.three.folio.epub'
        entry = {'packageName': package, 'title': '3-Folio EPUB',
                 'releases': [{'versionCode': 10, 'assets': [{'sha256': 'release-artifact-hash'}]}],
                 'iconUrl': 'old-brand.png'}
        release = copy.deepcopy(entry['releases'])
        catalog.apply_catalog_presentation([entry])
        self.assertEqual(release, entry['releases'])
        self.assertEqual(package, entry['packageName'])
        self.assertEqual('3-Folio EPUB', entry['title'])
        self.assertTrue(entry['forceIgnoreLocalIcon'])
        self.assertIn('/icons/' + package + '/', entry['iconUrl'])
        self.assertNotEqual(entry['iconUrl'], entry['nightIconUrl'])

    def test_retired_packages_cannot_be_reintroduced_by_an_old_release(self):
        retired = json.loads((catalog.ROOT / 'retired-packages.json').read_text())['replacements']
        for package in retired:
            with self.subTest(package=package), self.assertRaisesRegex(RuntimeError, 'Retired package'):
                catalog.apply_catalog_presentation([{'packageName': package}])

    def test_other_official_plugins_also_keep_the_catalog_icon_after_installation(self):
        entry = {'packageName': 'example.ocr.plugin', 'iconUrl': 'published-brand.png'}
        catalog.apply_catalog_presentation([entry])
        self.assertEqual('published-brand.png', entry['iconUrl'])
        self.assertTrue(entry['forceIgnoreLocalIcon'])

    def test_changed_bytes_cannot_reuse_an_existing_icon_url(self):
        with patch.object(Path, 'read_bytes', return_value=b'changed-image'):
            with self.assertRaisesRegex(RuntimeError, 'digest mismatch'):
                catalog.load_artwork()


if __name__ == '__main__':
    unittest.main()
