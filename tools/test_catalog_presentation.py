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
        self.assertTrue(artwork)
        # Three uses different neutral foregrounds in each theme. Other plugins
        # may intentionally share a colored, transparent image across both modes.
        self.assertTrue(all(value['day']['sha256'] != value['night']['sha256']
                            for value in artwork.values()
                            if value.get('repository', '').startswith('AutoJs6-Plugin-Three-')))

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
        self.assertEqual('#fafafa', entry['iconBackgroundColor'])
        self.assertEqual('#212121', entry['nightIconBackgroundColor'])

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

    def test_non_three_artwork_can_be_shared_across_themes(self):
        package = 'example.runtime.plugin'
        entry = {'packageName': package}
        art = {'repository': 'AutoJs6-Plugin-Example-Runtime',
               'day': {'path': 'shared.png'}, 'night': {'path': 'shared.png'},
               'backgrounds': {'day': 'transparent', 'night': 'transparent'}}
        with patch.object(catalog, 'load_artwork', return_value={package: art}):
            catalog.apply_catalog_presentation([entry])
        self.assertEqual(entry['iconUrl'], entry['nightIconUrl'])
        self.assertEqual('transparent', entry['iconBackgroundColor'])
        self.assertEqual('transparent', entry['nightIconBackgroundColor'])
        self.assertTrue(entry['forceIgnoreLocalIcon'])

    def test_custom_backgrounds_remain_independent_of_release_facts(self):
        package = 'example.custom.plugin'
        entry = {'packageName': package, 'iconUrl': 'brand.png'}
        art = {'day': {'path': 'day.png'}, 'night': {'path': 'night.png'},
               'backgrounds': {'day': '#336699', 'night': 'transparent'}}
        with patch.object(catalog, 'load_artwork', return_value={package: art}):
            catalog.apply_catalog_presentation([entry])
        self.assertEqual('#336699', entry['iconBackgroundColor'])
        self.assertEqual('transparent', entry['nightIconBackgroundColor'])

    def test_three_backgrounds_cannot_be_overridden_with_color(self):
        package = 'example.three.plugin'
        entry = {'packageName': package, 'repository': {'name': 'AutoJs6-Plugin-Three-Test'}}
        art = {'day': {'path': 'day.png'}, 'night': {'path': 'night.png'},
               'backgrounds': {'day': '#ff0000', 'night': 'transparent'}}
        with patch.object(catalog, 'load_artwork', return_value={package: art}):
            catalog.apply_catalog_presentation([entry])
        self.assertEqual('#fafafa', entry['iconBackgroundColor'])
        self.assertEqual('#212121', entry['nightIconBackgroundColor'])

    def test_changed_bytes_cannot_reuse_an_existing_icon_url(self):
        with patch.object(Path, 'read_bytes', return_value=b'changed-image'):
            with self.assertRaisesRegex(RuntimeError, 'digest mismatch'):
                catalog.load_artwork()


if __name__ == '__main__':
    unittest.main()
