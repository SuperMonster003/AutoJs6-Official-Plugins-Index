import unittest

import generate_official_plugin_index as generator


class CompatibilityPropertiesTest(unittest.TestCase):
    GRADLE = '''
val compatibilityFile = rootProject.file("gradle/explorer-action-compatibility.properties")
val compatibility = Properties().apply {
    compatibilityFile.inputStream().use(::load)
}
fun compatibilityProperty(name: String): String =
    compatibility.getProperty(name)
        ?.trim()
        ?.takeIf(String::isNotEmpty)
        ?: error("Missing property: $name")
val minimumHost = compatibilityProperty("minimumHostVersionCode").toLong()
android {
    defaultConfig {
        applicationId = "io.github.example.folio"
        resValue("string", "plugin_requires_host_version", minimumHost.toString())
    }
}
'''

    def parse(self, source="minimumHostVersionCode=5318\n", gradle=None):
        requested = []

        def read(path):
            requested.append(path)
            return source

        values = generator.parse_default_config_res_values(gradle or self.GRADLE, read_source=read)
        self.assertEqual(["gradle/explorer-action-compatibility.properties"], requested)
        return values

    def test_release_entry_keeps_the_published_host_minimum(self):
        entries = generator.build_entries_from_release(
            owner="example", repo_name="AutoJs6-Plugin-Example", ref="refs/tags/v2.0.0",
            release={"tag_name": "v2.0.0", "published_at": "2026-10-03T00:00:00Z", "assets": [
                {"name": "example-v2.0.0-universal.apk", "browser_download_url": "https://example.test/release.apk", "size": 1234},
            ]},
            tree_paths=set(), strings_by_dir={}, version_map={"VERSION_NAME": "2.0.0", "VERSION_BUILD": "83"},
            manifest_text='<manifest xmlns:android="http://schemas.android.com/apk/res/android"><application /></manifest>',
            build_gradle=self.GRADLE, read_source=lambda _: "minimumHostVersionCode=5318\n",
        )
        self.assertEqual(5318, entries[0]["requiresHostVersion"])

    def test_trimmed_numeric_property_and_missing_source(self):
        self.assertEqual("5318", self.parse("minimumHostVersionCode= 5318 \n")["plugin_requires_host_version"])
        with self.assertRaises(RuntimeError):
            generator.parse_default_config_res_values(self.GRADLE)

    def test_missing_duplicate_and_invalid_values_fail_closed(self):
        for value in (None, "", "other=5318\n", "minimumHostVersionCode=0\n",
                      "minimumHostVersionCode=dynamic()\n", "minimumHostVersionCode=9223372036854775808\n",
                      "minimumHostVersionCode=5318\nminimumHostVersionCode=5282\n"):
            with self.subTest(source=value), self.assertRaises(RuntimeError):
                self.parse(value)

    def test_literal_numeric_variable_is_supported_without_a_property_file(self):
        gradle = self.GRADLE.replace('compatibilityProperty("minimumHostVersionCode").toLong()', '5318L')
        self.assertEqual("5318", generator.parse_default_config_res_values(gradle)["plugin_requires_host_version"])

    def test_untrusted_paths_ambiguous_loaders_and_changed_accessors_are_rejected(self):
        for gradle in (
            self.GRADLE.replace('gradle/explorer-action-compatibility.properties', '../private.properties'),
            self.GRADLE.replace('gradle/explorer-action-compatibility.properties', 'local.properties'),
            self.GRADLE.replace('?.trim()', '?.let { "5282" }'),
            self.GRADLE + '\nval minimumHost = 5282\n',
            self.GRADLE + '\nval compatibility = unknown()\n',
            self.GRADLE.replace('minimumHost.toString()', 'unknown.toString()'),
            self.GRADLE.replace('minimumHost.toString()', 'unknown()'),
        ):
            with self.subTest(gradle=gradle), self.assertRaises(RuntimeError):
                self.parse(gradle=gradle)

    def test_source_download_failure_is_not_treated_as_an_absent_minimum(self):
        def fail(_):
            raise OSError("source download failed")

        with self.assertRaisesRegex(OSError, "source download failed"):
            generator.parse_default_config_res_values(self.GRADLE, read_source=fail)


if __name__ == '__main__':
    unittest.main()
