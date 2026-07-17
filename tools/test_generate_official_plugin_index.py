import unittest

import generate_official_plugin_index as generator


class OfficialPluginIndexGeneratorTest(unittest.TestCase):
    OWNER = "SuperMonster003"
    VERSION = "1.0.0"

    def test_schema_version_is_two(self):
        self.assertEqual(2, generator.build_payload([])["schemaVersion"])

    def test_release_metadata_uses_release_tag(self):
        self.assertEqual(
            "refs/tags/v1.0.0",
            generator.release_metadata_ref({"tag_name": "v1.0.0"}, "main"),
        )
        self.assertEqual(
            "refs/heads/main",
            generator.release_metadata_ref({}, "main"),
        )

    def test_mobile_asset_match_does_not_accept_mobile_en(self):
        self.assertTrue(
            generator.asset_name_matches_distribution(
                "plugin-v1.0.0-mobile-arm64-v8a-deadbeef.apk",
                "mobile",
            )
        )
        self.assertFalse(
            generator.asset_name_matches_distribution(
                "plugin-v1.0.0-mobile-en-arm64-v8a-deadbeef.apk",
                "mobile",
            )
        )

    def test_v4_flavors_expand_with_exact_mobile_boundary(self):
        repo_name = "AutoJs6-Plugin-Paddle-OCR-PP-OCRv4"
        gradle = self.gradle_with_flavors(
            base_package="io.github.supermonster003.autojs6.plugin.paddleocr.v4",
            plugin_variant="v4",
            flavors=[
                self.flavor(
                    "mobile", ".mobile", "-mobile", "Paddle OCR (PP-OCRv4 Mobile)", "paddle-ocr-pp-ocrv4-mobile"
                ),
                self.flavor(
                    "server", ".server", "-server", "Paddle OCR (PP-OCRv4 Server)", "paddle-ocr-pp-ocrv4-server"
                ),
                self.flavor(
                    "mobileEn",
                    ".mobile.en",
                    "-mobile-en",
                    "Paddle OCR (PP-OCRv4 Mobile EN)",
                    "paddle-ocr-pp-ocrv4-mobile-en",
                ),
            ],
        )
        entries = self.build_entries(repo_name, gradle, self.assets_for(repo_name, ["mobile", "server", "mobile-en"]))
        by_variant = {entry["distributionVariant"]: entry for entry in entries}

        self.assertEqual({"mobile", "server", "mobile-en"}, set(by_variant))
        self.assertEqual(
            "io.github.supermonster003.autojs6.plugin.paddleocr.v4.mobile",
            by_variant["mobile"]["packageName"],
        )
        self.assertEqual(
            "io.github.supermonster003.autojs6.plugin.paddleocr.v4.mobile.en",
            by_variant["mobile-en"]["packageName"],
        )
        self.assertEqual("1.0.0-mobile-en", by_variant["mobile-en"]["releases"][0]["versionName"])
        self.assertEqual("Paddle OCR (PP-OCRv4 Mobile EN)", by_variant["mobile-en"]["title"])
        self.assertEqual("paddle-ocr-pp-ocrv4-mobile-en", by_variant["mobile-en"]["engineId"])
        self.assertTrue(by_variant["mobile"]["featured"])
        self.assertFalse(by_variant["server"]["featured"])
        self.assertFalse(by_variant["mobile-en"]["featured"])
        self.assertAssetVariants(by_variant["mobile"], {"mobile"})
        self.assertAssetVariants(by_variant["mobile-en"], {"mobile-en"})

    def test_v5_mobile_preserves_base_application_id(self):
        repo_name = "AutoJs6-Plugin-Paddle-OCR-PP-OCRv5"
        profiles = [
            "mobile",
            "server",
            "english",
            "korean",
            "latin",
            "eslav",
            "thai",
            "greek",
            "arabic",
            "cyrillic",
            "devanagari",
            "telugu",
            "tamil",
        ]
        gradle = self.gradle_with_flavors(
            base_package="io.github.supermonster003.autojs6.plugin.paddleocr.v5",
            plugin_variant="v5",
            flavors=[
                self.flavor("mobile", "", "-mobile", "Paddle OCR (PP-OCRv5 Mobile)", "paddle-ocr-pp-ocrv5"),
                *[
                    self.flavor(
                        profile,
                        f".{profile}",
                        f"-{profile}",
                        f"Paddle OCR (PP-OCRv5 {profile.title()})",
                        f"paddle-ocr-pp-ocrv5-{profile}",
                    )
                    for profile in profiles
                    if profile != "mobile"
                ],
            ],
        )
        entries = self.build_entries(repo_name, gradle, self.assets_for(repo_name, profiles))
        by_variant = {entry["distributionVariant"]: entry for entry in entries}

        self.assertEqual(13, len(entries))
        self.assertEqual(
            "io.github.supermonster003.autojs6.plugin.paddleocr.v5",
            by_variant["mobile"]["packageName"],
        )
        self.assertEqual("1.0.0-mobile", by_variant["mobile"]["releases"][0]["versionName"])
        self.assertEqual("paddle-ocr-pp-ocrv5", by_variant["mobile"]["engineId"])
        self.assertTrue(by_variant["mobile"]["featured"])
        self.assertTrue(all(not by_variant[profile]["featured"] for profile in profiles if profile != "mobile"))
        self.assertAssetVariants(by_variant["mobile"], {"mobile"})

    def test_v6_small_and_tiny_are_featured(self):
        repo_name = "AutoJs6-Plugin-Paddle-OCR-PP-OCRv6"
        gradle = self.gradle_with_flavors(
            base_package="io.github.supermonster003.autojs6.plugin.paddleocr.v6",
            plugin_variant="v6",
            flavors=[
                self.flavor("tiny", ".tiny", "-tiny", "Paddle OCR (PP-OCRv6 Tiny)", "paddle-ocr-pp-ocrv6-tiny"),
                self.flavor("small", ".small", "-small", "Paddle OCR (PP-OCRv6 Small)", "paddle-ocr-pp-ocrv6-small"),
                self.flavor(
                    "medium", ".medium", "-medium", "Paddle OCR (PP-OCRv6 Medium)", "paddle-ocr-pp-ocrv6-medium"
                ),
            ],
        )
        entries = self.build_entries(repo_name, gradle, self.assets_for(repo_name, ["tiny", "small", "medium"]))
        by_variant = {entry["distributionVariant"]: entry for entry in entries}

        self.assertTrue(by_variant["tiny"]["featured"])
        self.assertTrue(by_variant["small"]["featured"])
        self.assertFalse(by_variant["medium"]["featured"])
        self.assertEqual(
            "io.github.supermonster003.autojs6.plugin.paddleocr.v6.small",
            by_variant["small"]["packageName"],
        )
        self.assertAssetVariants(by_variant["small"], {"small"})
        self.assertAssetVariants(by_variant["tiny"], {"tiny"})

    def test_single_variant_project_keeps_legacy_shape_and_assets(self):
        repo_name = "AutoJs6-Plugin-Paddle-OCR-PP-OCRv3"
        gradle = """
            val globalApplicationId = "io.github.supermonster003.autojs6.plugin.paddleocr.v3"
            android {
                defaultConfig {
                    applicationId = globalApplicationId
                    resValue("string", "app_name", "Paddle OCR (PP-OCRv3)")
                    resValue("string", "plugin_engine", "paddle-ocr")
                    resValue("string", "plugin_variant", "v3")
                    resValue("string", "plugin_id", "paddle-ocr-pp-ocrv3")
                }
            }
        """
        assets = self.assets_for(repo_name, [None], version="0.2.3")
        entries = self.build_entries(repo_name, gradle, assets, version="0.2.3")

        self.assertEqual(1, len(entries))
        entry = entries[0]
        self.assertEqual("io.github.supermonster003.autojs6.plugin.paddleocr.v3", entry["packageName"])
        self.assertEqual("0.2.3", entry["releases"][0]["versionName"])
        self.assertEqual(assets, entry["releases"][0]["assets"])
        self.assertTrue(entry["featured"])
        self.assertNotIn("distributionVariant", entry)

    def test_unmatched_multiflavor_asset_fails_generation(self):
        repo_name = "AutoJs6-Plugin-Paddle-OCR-PP-OCRv4"
        gradle = self.gradle_with_flavors(
            base_package="io.github.supermonster003.autojs6.plugin.paddleocr.v4",
            plugin_variant="v4",
            flavors=[
                self.flavor("mobile", ".mobile", "-mobile", "Mobile", "mobile-id"),
                self.flavor("server", ".server", "-server", "Server", "server-id"),
            ],
        )
        assets = self.assets_for(repo_name, ["mobile"])
        assets.append(self.asset(repo_name, "experimental", "arm64-v8a"))

        with self.assertRaisesRegex(RuntimeError, r"failed to assign.*experimental"):
            self.build_entries(repo_name, gradle, assets)

    def test_missing_featured_distribution_asset_fails_generation(self):
        repo_name = "AutoJs6-Plugin-Paddle-OCR-PP-OCRv6"
        gradle = self.gradle_with_flavors(
            base_package="io.github.supermonster003.autojs6.plugin.paddleocr.v6",
            plugin_variant="v6",
            flavors=[
                self.flavor("tiny", ".tiny", "-tiny", "Tiny", "tiny-id"),
                self.flavor("small", ".small", "-small", "Small", "small-id"),
                self.flavor("medium", ".medium", "-medium", "Medium", "medium-id"),
            ],
        )
        assets = self.assets_for(repo_name, ["tiny", "medium"])

        with self.assertRaisesRegex(RuntimeError, r"featured.*without assets=\['small'\]"):
            self.build_entries(repo_name, gradle, assets)

    def build_entries(self, repo_name, gradle, assets, *, version=None):
        version = version or self.VERSION
        return generator.build_entries_from_release(
            owner=self.OWNER,
            repo_name=repo_name,
            ref=f"refs/tags/v{version}",
            release={
                "tag_name": f"v{version}",
                "name": f"Release {version}",
                "published_at": "2026-07-17T10:00:00Z",
                "html_url": "https://example.test/release",
                "body": "Release notes",
                "author": {"login": self.OWNER},
                "assets": assets,
            },
            tree_paths=set(),
            strings_by_dir={"values-en": {"plugin_description": "Description"}},
            version_map={"VERSION_NAME": version, "VERSION_BUILD": "99"},
            manifest_text='<manifest><application android:label="@string/app_name" /></manifest>',
            build_gradle=gradle,
        )

    def assets_for(self, repo_name, variants, *, version=None):
        return [
            self.asset(repo_name, variant, abi, version=version)
            for variant in variants
            for abi in ("arm64-v8a", "armeabi-v7a", "universal")
        ]

    def asset(self, repo_name, variant, abi, *, version=None):
        stem = f"{repo_name.lower()}-v{version or self.VERSION}"
        if variant:
            stem += f"-{variant}"
        name = f"{stem}-{abi}-0123abcd.apk"
        return {
            "name": name,
            "browser_download_url": f"https://example.test/{name}",
            "size": 1234,
            "digest": "sha256:abc",
        }

    @staticmethod
    def flavor(name, application_id_suffix, version_name_suffix, title, plugin_id):
        suffix_line = f'applicationIdSuffix = "{application_id_suffix}"' if application_id_suffix else ""
        return f"""
            create("{name}") {{
                dimension = "ocrProfile"
                {suffix_line}
                versionNameSuffix = "{version_name_suffix}"
                buildConfigField("String", "PLUGIN_ID", "\\\"{plugin_id}\\\"")
                resValue("string", "app_name", "{title}")
                resValue("string", "plugin_id", "{plugin_id}")
            }}
        """

    @staticmethod
    def gradle_with_flavors(base_package, plugin_variant, flavors):
        return f"""
            val globalApplicationId = "{base_package}"
            android {{
                defaultConfig {{
                    applicationId = globalApplicationId
                    resValue("string", "plugin_engine", "paddle-ocr")
                    resValue("string", "plugin_variant", "{plugin_variant}")
                }}
                flavorDimensions += "ocrProfile"
                productFlavors {{
                    {''.join(flavors)}
                }}
            }}
        """

    def assertAssetVariants(self, entry, expected_variants):
        asset_names = [asset["name"] for asset in entry["releases"][0]["assets"]]
        for expected in expected_variants:
            self.assertTrue(all(f"-{expected}-" in name for name in asset_names), asset_names)
        self.assertEqual(3, len(asset_names))


if __name__ == "__main__":
    unittest.main()
