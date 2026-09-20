import json
import tempfile
import unittest
from pathlib import Path

import generate_official_plugin_index as generator


class OfficialPluginIndexGeneratorTest(unittest.TestCase):
    OWNER = "SuperMonster003"
    VERSION = "1.0.0"

    def test_required_repository_coverage_rejects_missing_and_hidden_releases(self):
        item = {"packageName": "example.plugin", "repository": {"owner": self.OWNER, "name": "AutoJs6-Plugin-Example"}, "releases": [{"assets": [{"name": "example.apk"}]}]}
        required = ["AutoJs6-Plugin-Example"]
        generator.validate_repository_coverage([item], required)
        for entries in ([], [{**item, "featured": False}], [{**item, "releases": []}], [item, item]):
            with self.assertRaises(RuntimeError):
                generator.validate_repository_coverage(entries, required)
        for names in ([], required + required, ["../invalid"], "not an array"):
            with self.assertRaises(RuntimeError):
                generator.validate_repository_coverage([item], names)

    def test_latest_published_release_includes_newer_prerelease_and_ignores_drafts(self):
        stable = {"id": 1, "published_at": "2026-09-01T00:00:00Z", "prerelease": False}
        candidate = {"id": 2, "published_at": "2026-09-20T00:00:00Z", "prerelease": True}
        draft = {"id": 3, "published_at": "2026-09-21T00:00:00Z", "draft": True}
        unpublished = {"id": 4, "published_at": None}
        self.assertIs(candidate, generator.latest_published_release([draft, stable, unpublished, candidate]))
        self.assertTrue(candidate["prerelease"])
        self.assertIsNone(generator.latest_published_release([draft, unpublished]))

    def test_unsuffixed_production_flavor_does_not_publish_test_flavors(self):
        provider = generator.ProductFlavor("provider", "", "", {})
        test = generator.ProductFlavor("nativeTest", ".native_test", "", {})
        asset = {"name": "autojs6-plugin-lua-runtime-v0.1.2-rc.2-arm64-v8a-7B344BB9.apk"}
        groups = generator.group_release_assets_by_flavor("AutoJs6-Plugin-Lua-Runtime", [asset], [provider, test], base_version_name="0.1.2-rc.2")
        self.assertEqual([(provider, [asset]), (test, [])], groups)
        for flavors, assets in (([provider, generator.ProductFlavor("ambiguous", "", "", {})], [asset]), ([provider, test], [{"name": asset["name"].replace("-arm64", "-unknown-arm64") }])):
            with self.assertRaises(RuntimeError):
                generator.group_release_assets_by_flavor("AutoJs6-Plugin-Lua-Runtime", assets, flavors, base_version_name="0.1.2-rc.2")

    def test_latest_published_release_uses_publication_date_and_deterministic_tie_break(self):
        older = {"id": 99, "published_at": "2026-09-01T00:00:00Z"}
        newer = {"id": 2, "published_at": "2026-09-20T00:00:00Z"}
        tied = {"id": 3, "published_at": "2026-09-20T00:00:00Z"}
        self.assertIs(tied, generator.latest_published_release([newer, tied, older]))
        with self.assertRaises(RuntimeError):
            generator.latest_published_release({"message": "API error"})

    def test_multiline_resource_with_trailing_comma_resolves_package_constant(self):
        gradle = '''
            val codeNamespace = "io.github.example.archive"
            defaultConfig {
                applicationId = codeNamespace
                resValue(
                    "string", "plugin_runtime_component",
                    "$codeNamespace/${codeNamespace}.ExplorerActionService",
                )
            }
        '''
        self.assertEqual("io.github.example.archive", generator.parse_application_id_from_build_gradle(gradle))
        values = generator.parse_default_config_res_values(gradle)
        self.assertEqual("io.github.example.archive/io.github.example.archive.ExplorerActionService", values["plugin_runtime_component"])
        with self.assertRaises(RuntimeError):
            generator.parse_default_config_res_values(gradle.replace('val codeNamespace = "io.github.example.archive"', 'val codeNamespace = findPackage()'))

    def test_manifest_host_version_reads_only_explicit_version_properties_loader(self):
        manifest = '<meta-data android:name="requiresHostVersion" android:value="${requiredHostVersion}"/>'
        gradle = '''
            val projectProperties = Properties().apply {
                rootProject.file("version.properties").inputStream().use(::load)
            }
            val requiredHostVersion = projectProperties.getProperty("AUTOJS6_HOST_VERSION_BUILD").toInt()
            manifestPlaceholders["requiredHostVersion"] = requiredHostVersion
        '''
        values = generator.resolve_manifest_placeholders(gradle, manifest, read_source=lambda _: self.fail("unexpected source read"), version_map={"AUTOJS6_HOST_VERSION_BUILD": "5281"})
        self.assertEqual({"requiredHostVersion": "5281"}, values)
        for source, properties in ((gradle, {}), (gradle.replace('"version.properties"', '"local.properties"'), {"AUTOJS6_HOST_VERSION_BUILD": "5281"})):
            with self.assertRaises(RuntimeError):
                generator.resolve_manifest_placeholders(source, manifest, read_source=lambda _: None, version_map=properties)

    def test_android_version_code_offset_preserves_upgrade_identity(self):
        entry = self.build_entries("AutoJs6-Plugin-ImGui", "", self.assets_for("AutoJs6-Plugin-ImGui", [None]), version_map={"VERSION_NAME": "1.0.0", "VERSION_BUILD": "16", "VERSION_CODE_OFFSET": "4"})[0]
        self.assertEqual(20, entry["releases"][0]["versionCode"])
        with self.assertRaises(RuntimeError):
            self.build_entries("AutoJs6-Plugin-ImGui", "", [], version_map={"VERSION_BUILD": "16", "VERSION_CODE_OFFSET": "-1"})

    def test_numeric_resource_reads_version_properties_for_lua_provider(self):
        gradle = '''
            val versionProperties = Properties().apply {
                rootProject.file("version.properties").inputStream().use { stream -> load(stream) }
            }
            defaultConfig {
                resValue("string", "lua_runtime_requires_host_version", versionProperties.getProperty("REQUIRED_HOST_VERSION_CODE"),)
            }
        '''
        properties = {"REQUIRED_HOST_VERSION_CODE": "5281"}
        self.assertEqual({"lua_runtime_requires_host_version": "5281"}, generator.parse_default_config_res_values(gradle, version_map=properties))
        for source, values in ((gradle, {}), (gradle.replace('"version.properties"', '"local.properties"'), properties), (gradle + '\nval versionProperties = unknown()', properties)):
            with self.assertRaises(RuntimeError):
                generator.parse_default_config_res_values(source, version_map=values)

    def test_apk_builder_composite_version_matches_paired_host(self):
        entry = self.build_entries("AutoJs6-Plugin-APK-Builder-Template", "val pluginVersionCode = versions.appVersionCode * 100 + versions.pluginReleaseSeq", self.assets_for("AutoJs6-Plugin-APK-Builder-Template", [None]), version_map={"VERSION_NAME": "1.0.3", "VERSION_BUILD": "45", "HOST_VERSION_NAME": "6.8.0", "HOST_VERSION_BUILD": "5280", "PLUGIN_RELEASE_SEQ": "1"})[0]
        self.assertEqual("1.0.3+autojs6-6.8.0", entry["releases"][0]["versionName"])
        self.assertEqual(528001, entry["releases"][0]["versionCode"])

    PLACEHOLDER_MANIFEST = '<meta-data android:name="requiresHostVersion" android:value="${requiredHost}"/>'
    SOURCE_PLACEHOLDER_GRADLE = r'''
        val contractFile = file("src/main/java/example/Contract.java")
        val requiredHostVersion = Regex("REQUIRED_HOST_VERSION\\s*=\\s*(\\d+)L")
            .find(contractFile.readText())
            ?.groupValues?.get(1)?.toLong()
            ?: throw GradleException("Missing contract")
        defaultConfig {
            manifestPlaceholders["requiredHost"] = requiredHostVersion
        }
    '''

    def test_manifest_placeholder_reads_the_declared_source_constant(self):
        paths = []
        def read(path):
            paths.append(path)
            return "public static final long REQUIRED_HOST_VERSION = 3853L;"
        values = generator.resolve_manifest_placeholders(
            self.SOURCE_PLACEHOLDER_GRADLE, self.PLACEHOLDER_MANIFEST, read_source=read,
        )
        self.assertEqual(["app/src/main/java/example/Contract.java"], paths)
        entries = self.build_entries(
            "AutoJs6-Plugin-Example", self.SOURCE_PLACEHOLDER_GRADLE,
            self.assets_for("AutoJs6-Plugin-Example", [None]),
            manifest_text=self.PLACEHOLDER_MANIFEST, manifest_placeholders=values,
        )
        self.assertEqual(3853, entries[0]["requiresHostVersion"])

    def test_manifest_placeholder_literals_do_not_load_source_files(self):
        for expression in ('"3853"', '3853L', 'requiredHostVersion.toString()'):
            with self.subTest(expression=expression):
                gradle = 'val requiredHostVersion = 3853L\nmanifestPlaceholders["requiredHost"] = ' + expression
                def reject(path):
                    self.fail("Unexpected source read: " + path)
                self.assertEqual({"requiredHost": "3853"}, generator.resolve_manifest_placeholders(
                    gradle, self.PLACEHOLDER_MANIFEST, read_source=reject,
                ))

    def test_manifest_placeholder_rejects_missing_ambiguous_and_escaping_sources(self):
        for source in (None, "", "REQUIRED_HOST_VERSION = 3853L; REQUIRED_HOST_VERSION = 5279L;"):
            with self.subTest(source=source), self.assertRaisesRegex(RuntimeError, "one source constant"):
                generator.resolve_manifest_placeholders(
                    self.SOURCE_PLACEHOLDER_GRADLE, self.PLACEHOLDER_MANIFEST, read_source=lambda path: source,
                )
        gradle = self.SOURCE_PLACEHOLDER_GRADLE.replace("src/main/java/example/Contract.java", "../secret.java")
        with self.assertRaisesRegex(RuntimeError, "Invalid source path"):
            generator.resolve_manifest_placeholders(gradle, self.PLACEHOLDER_MANIFEST, read_source=lambda path: self.fail(path))

    def test_manifest_placeholder_unknown_and_duplicate_assignments_fail_closed(self):
        for gradle in ('', 'manifestPlaceholders["requiredHost"] = unknown()',
                       'manifestPlaceholders["requiredHost"] = 1\nmanifestPlaceholders["requiredHost"] = 2'):
            with self.subTest(gradle=gradle), self.assertRaises(RuntimeError):
                generator.resolve_manifest_placeholders(gradle, self.PLACEHOLDER_MANIFEST, read_source=lambda path: self.fail(path))
        with self.assertRaisesRegex(RuntimeError, "unresolved value"):
            self.build_entries("AutoJs6-Plugin-Example", "", self.assets_for("AutoJs6-Plugin-Example", [None]),
                               manifest_text=self.PLACEHOLDER_MANIFEST)

    def test_manifest_placeholder_keeps_host_version_validation_and_conflict_checks(self):
        for value, gradle, message in (
            ("0", "", "positive decimal integer"),
            ("3853", 'resValue("string", "plugin_requires_host_version", "5279")', "conflicting requiresHostVersion"),
        ):
            with self.subTest(value=value), self.assertRaisesRegex(RuntimeError, message):
                self.build_entries("AutoJs6-Plugin-Example", gradle, self.assets_for("AutoJs6-Plugin-Example", [None]),
                                   manifest_text=self.PLACEHOLDER_MANIFEST, manifest_placeholders={"requiredHost": value})

    def test_schema_version_remains_two_for_additive_optional_fields(self):
        self.assertEqual(2, generator.build_payload([])["schemaVersion"])

    def test_release_metadata_uses_release_tag(self):
        self.assertEqual(
            "refs/tags/v1.0.0",
            generator.release_metadata_ref({"tag_name": "v1.0.0"}, "main"),
        )

    def test_index_owned_admission_path_is_package_and_version_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "io.github.example.plugin" / "99.json"
            path.parent.mkdir(parents=True)
            path.write_text('{"schemaVersion":1}', encoding="utf-8")

            self.assertEqual(
                '{"schemaVersion":1}',
                generator.read_admission_manifest(
                    root,
                    package_name="io.github.example.plugin",
                    version_code=99,
                ),
            )
            self.assertIsNone(
                generator.read_admission_manifest(
                    root,
                    package_name="../escape",
                    version_code=99,
                )
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
        self.assertNotIn("requiresHostVersion", entry)

    def test_yolo_routing_and_required_host_version_are_emitted_without_repo_special_case(self):
        repo_name = "AutoJs6-Plugin-Yolo-NCNN"
        gradle = """
            val globalApplicationId = "io.github.supermonster003.autojs6.plugin.yolo.ncnn"
            android {
                defaultConfig {
                    applicationId = globalApplicationId
                    resValue("string", "plugin_engine", "yolo")
                    resValue("string", "plugin_variant", "ncnn")
                    resValue("string", "plugin_id", "yolo-ncnn")
                    resValue("string", "plugin_requires_host_version", "5275")
                }
            }
        """
        manifest = """
            <manifest xmlns:android="http://schemas.android.com/apk/res/android">
                <application>
                    <service android:name=".YoloPluginInfoService">
                        <meta-data
                            android:name="requiresHostVersion"
                            android:value="@string/plugin_requires_host_version" />
                    </service>
                    <service android:name=".provider.YoloProviderService">
                        <meta-data android:value="5275" android:name="requiresHostVersion" />
                    </service>
                </application>
            </manifest>
        """

        entry = self.build_entries(
            repo_name,
            gradle,
            self.assets_for(repo_name, [None]),
            manifest_text=manifest,
        )[0]

        self.assertEqual("yolo", entry["engine"])
        self.assertEqual("ncnn", entry["variant"])
        self.assertEqual("yolo-ncnn", entry["engineId"])
        self.assertEqual(5275, entry["requiresHostVersion"])

    def test_extended_runtime_contract_is_emitted_without_repo_special_case(self):
        package_name = "io.github.example.plugin.detector"
        service_name = f"{package_name}.provider.DetectorService"
        gradle = f'''
            val globalApplicationId = "{package_name}"
            android {{
                defaultConfig {{
                    applicationId = globalApplicationId
                    resValue("string", "plugin_engine", "detector")
                    resValue("string", "plugin_runtime_component", "{package_name}/{service_name}")
                    resValue("string", "plugin_protocol_api_min", "1.0")
                    resValue("string", "plugin_protocol_api_max", "1.2")
                    resValue("string", "plugin_requires_host_version", "5275")
                    resValue("string", "plugin_max_host_version", "5300")
                    resValue("string", "plugin_backend", "ncnn")
                    resValue("string", "plugin_task", "detect")
                    resValue("string", "plugin_decoder", "ultralytics-detect")
                    resValue("string", "plugin_supported_abis", "arm64-v8a")
                }}
            }}
        '''
        manifest = f'''
            <manifest xmlns:android="http://schemas.android.com/apk/res/android">
                <application>
                    <service android:name=".provider.DetectorService">
                        <meta-data android:name="org.autojs.plugin.contract.RUNTIME_COMPONENT"
                            android:value="{package_name}/{service_name}" />
                        <meta-data android:name="org.autojs.plugin.contract.PROTOCOL_API_MIN" android:value="1.0" />
                        <meta-data android:name="org.autojs.plugin.contract.PROTOCOL_API_MAX" android:value="1.2" />
                        <meta-data android:name="org.autojs.plugin.contract.MAX_HOST_VERSION" android:value="5300" />
                        <meta-data android:name="org.autojs.plugin.contract.BACKEND" android:value="ncnn" />
                        <meta-data android:name="org.autojs.plugin.contract.TASK" android:value="detect" />
                        <meta-data android:name="org.autojs.plugin.contract.DECODER" android:value="ultralytics-detect" />
                        <meta-data android:name="org.autojs.plugin.contract.SUPPORTED_ABIS" android:value="arm64-v8a" />
                    </service>
                </application>
            </manifest>
        '''
        asset = self.asset("AutoJs6-Plugin-Detector", None, "arm64-v8a")
        artifact_manifest = self.admission_manifest(
            asset,
            repo_name="AutoJs6-Plugin-Detector",
            package_name=package_name,
            runtime_component=f"{package_name}/{service_name}",
            supported_abis=["arm64-v8a"],
        )

        entry = self.build_entries(
            "AutoJs6-Plugin-Detector",
            gradle,
            [asset],
            manifest_text=manifest,
            admission_manifest_text=artifact_manifest,
        )[0]

        self.assertEqual(f"{package_name}/{service_name}", entry["runtimeComponent"])
        self.assertEqual("1.0", entry["protocolApiMin"])
        self.assertEqual("1.2", entry["protocolApiMax"])
        self.assertEqual(5275, entry["requiresHostVersion"])
        self.assertEqual(5300, entry["maxHostVersion"])
        self.assertEqual("ncnn", entry["backend"])
        self.assertEqual("detect", entry["task"])
        self.assertEqual("ultralytics-detect", entry["decoder"])
        self.assertEqual(["arm64-v8a"], entry["supportedAbis"])
        bound_asset = entry["releases"][0]["assets"][0]
        self.assertEqual("a" * 64, bound_asset["sha256"])
        self.assertEqual(["b" * 64], bound_asset["signerSha256"])
        self.assertEqual(1234, bound_asset["size"])
        self.assertEqual("1.0.0", bound_asset["versionName"])
        self.assertEqual(99, bound_asset["versionCode"])

    def test_legacy_plugin_omits_extended_contract_without_admission_manifest(self):
        entry = self.build_entries(
            "AutoJs6-Plugin-Legacy",
            '''
                android {
                    defaultConfig {
                        applicationId = "io.github.example.plugin.legacy"
                    }
                }
            ''',
            self.assets_for("AutoJs6-Plugin-Legacy", [None]),
        )[0]

        for field in (
            "runtimeComponent",
            "protocolApiMin",
            "protocolApiMax",
            "maxHostVersion",
            "backend",
            "task",
            "decoder",
        ):
            self.assertNotIn(field, entry)
        self.assertNotIn("signerSha256", entry["releases"][0]["assets"][0])

    def test_explicit_malformed_or_incomplete_contract_fails_closed(self):
        package_name = "io.github.example.plugin.invalid"
        service_name = f"{package_name}.ProviderService"
        base_manifest = f'''
            <manifest xmlns:android="http://schemas.android.com/apk/res/android">
                <application><service android:name="{service_name}" /></application>
            </manifest>
        '''
        cases = (
            ("plugin_runtime_component", f"other.package/{service_name}", r"component package"),
            ("plugin_runtime_component", f"{package_name}/{package_name}.MissingService", r"not a declared"),
            ("plugin_protocol_api_min", "1", r"major.minor"),
            ("plugin_protocol_api_min", "1.0", r"declare protocolApiMin and protocolApiMax together"),
            ("plugin_backend", "", r"contract identifier"),
            ("plugin_supported_abis", "arm64-v8a,,x86_64", r"empty entries"),
            ("plugin_supported_abis", "x86_64", r"conflicts with release asset names"),
            ("plugin_max_host_version", "5274", r"lower than requiresHostVersion"),
        )
        for resource, value, message in cases:
            with self.subTest(resource=resource, value=value):
                gradle = f'''
                    android {{
                        defaultConfig {{
                            applicationId = "{package_name}"
                            resValue("string", "plugin_requires_host_version", "5275")
                            resValue("string", "{resource}", "{value}")
                        }}
                    }}
                '''
                with self.assertRaisesRegex(RuntimeError, message):
                    self.build_entries(
                        "AutoJs6-Plugin-Invalid-Contract",
                        gradle,
                        [self.asset("AutoJs6-Plugin-Invalid-Contract", None, "arm64-v8a")],
                        manifest_text=base_manifest,
                    )

    def test_conflicting_contract_sources_fail_closed(self):
        package_name = "io.github.example.plugin.conflict"
        gradle = f'''
            android {{
                defaultConfig {{
                    applicationId = "{package_name}"
                    resValue("string", "plugin_protocol_api_min", "1.0")
                    resValue("string", "plugin_protocol_api_max", "1.0")
                }}
            }}
        '''
        manifest = '''
            <manifest xmlns:android="http://schemas.android.com/apk/res/android">
                <application><service android:name=".ProviderService">
                    <meta-data android:name="org.autojs.plugin.contract.PROTOCOL_API_MIN" android:value="1.1" />
                    <meta-data android:name="org.autojs.plugin.contract.PROTOCOL_API_MAX" android:value="1.1" />
                </service></application>
            </manifest>
        '''
        with self.assertRaisesRegex(RuntimeError, r"conflicting protocolApiMin"):
            self.build_entries(
                "AutoJs6-Plugin-Conflicting-Contract",
                gradle,
                [self.asset("AutoJs6-Plugin-Conflicting-Contract", None, "arm64-v8a")],
                manifest_text=manifest,
            )

    def test_reversed_protocol_range_fails_closed(self):
        gradle = '''
            android {
                defaultConfig {
                    applicationId = "io.github.example.plugin.protocol"
                    resValue("string", "plugin_protocol_api_min", "1.2")
                    resValue("string", "plugin_protocol_api_max", "1.1")
                }
            }
        '''
        with self.assertRaisesRegex(RuntimeError, r"protocolApiMax 1.1 is lower"):
            self.build_entries(
                "AutoJs6-Plugin-Reversed-Protocol",
                gradle,
                [self.asset("AutoJs6-Plugin-Reversed-Protocol", None, "arm64-v8a")],
            )

    def test_admission_manifest_fails_closed_on_unknown_or_drifting_evidence(self):
        package_name = "io.github.example.plugin.artifact"
        gradle = f'''
            android {{ defaultConfig {{ applicationId = "{package_name}" }} }}
        '''
        asset = self.asset("AutoJs6-Plugin-Artifact", None, "arm64-v8a")
        base = json.loads(
            self.admission_manifest(
                asset,
                repo_name="AutoJs6-Plugin-Artifact",
                package_name=package_name,
                supported_abis=["arm64-v8a"],
            )
        )
        mutations = (
            (("artifacts", 0, "sha256"), "short", r"64 hexadecimal"),
            (("artifacts", 0, "sha256"), "d" * 64, r"does not match GitHub release digest"),
            (("artifacts", 0, "sizeBytes"), 4321, r"does not match GitHub release size"),
            (("versionCode",), 100, r"does not match"),
            (("packageName",), "io.github.example.other", r"does not match"),
            (("sourceCommit",), "d" * 40, r"does not match"),
            (("signerSha256",), [], r"non-empty array"),
            (("signerSha256",), ["short"], r"64 hexadecimal"),
            (("unknownField",), True, r"invalid fields"),
        )
        for path, value, message in mutations:
            with self.subTest(path=path):
                document = json.loads(json.dumps(base))
                target = document
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                with self.assertRaisesRegex(RuntimeError, message):
                    self.build_entries(
                        "AutoJs6-Plugin-Artifact",
                        gradle,
                        [asset],
                        admission_manifest_text=json.dumps(document),
                    )

        extra = json.loads(json.dumps(base))
        duplicate = dict(extra["artifacts"][0])
        duplicate["name"] = "unpublished-extra.apk"
        extra["artifacts"].append(duplicate)
        with self.assertRaisesRegex(RuntimeError, r"must match the selected release APK asset names exactly"):
            self.build_entries(
                "AutoJs6-Plugin-Artifact",
                gradle,
                [asset],
                admission_manifest_text=json.dumps(extra),
            )

    def test_manifest_literal_required_host_version_is_supported_without_res_value(self):
        repo_name = "AutoJs6-Plugin-Manifest-Only"
        gradle = """
            android {
                defaultConfig {
                    applicationId = "org.example.manifest.only"
                }
            }
        """
        manifest = """
            <manifest xmlns:android="http://schemas.android.com/apk/res/android">
                <application>
                    <service android:name=".InfoService">
                        <meta-data android:name="requiresHostVersion" android:value="5275" />
                    </service>
                </application>
            </manifest>
        """

        entry = self.build_entries(
            repo_name,
            gradle,
            self.assets_for(repo_name, [None]),
            manifest_text=manifest,
        )[0]

        self.assertEqual(5275, entry["requiresHostVersion"])
        self.assertNotIn("engine", entry)
        self.assertNotIn("variant", entry)
        self.assertNotIn("engineId", entry)

    def test_invalid_required_host_version_fails_closed(self):
        repo_name = "AutoJs6-Plugin-Invalid-Host-Version"
        for value in ("", "0", "-1", "52.74", "latest", str(1 << 63)):
            with self.subTest(value=value):
                gradle = f"""
                    android {{
                        defaultConfig {{
                            applicationId = "org.example.invalid.host.version"
                            resValue("string", "plugin_requires_host_version", "{value}")
                        }}
                    }}
                """
                with self.assertRaisesRegex(RuntimeError, r"positive decimal integer|signed 64-bit"):
                    self.build_entries(repo_name, gradle, self.assets_for(repo_name, [None]))

    def test_unresolved_or_conflicting_manifest_required_host_version_fails_closed(self):
        repo_name = "AutoJs6-Plugin-Conflicting-Host-Version"
        gradle = """
            android {
                defaultConfig {
                    applicationId = "org.example.conflicting.host.version"
                    resValue("string", "plugin_requires_host_version", "5275")
                }
            }
        """
        unresolved = """
            <manifest xmlns:android="http://schemas.android.com/apk/res/android">
                <application><service><meta-data
                    android:name="requiresHostVersion"
                    android:value="@string/missing_host_version" /></service></application>
            </manifest>
        """
        conflicting = unresolved.replace("@string/missing_host_version", "5276")

        with self.assertRaisesRegex(RuntimeError, r"unresolved value"):
            self.build_entries(
                repo_name,
                gradle,
                self.assets_for(repo_name, [None]),
                manifest_text=unresolved,
            )
        with self.assertRaisesRegex(RuntimeError, r"conflicting requiresHostVersion"):
            self.build_entries(
                repo_name,
                gradle,
                self.assets_for(repo_name, [None]),
                manifest_text=conflicting,
            )

    def test_explicit_empty_host_version_resource_is_not_masked_by_strings_fallback(self):
        repo_name = "AutoJs6-Plugin-Resource-Precedence"
        manifest = """
            <manifest xmlns:android="http://schemas.android.com/apk/res/android">
                <application><service><meta-data
                    android:name="requiresHostVersion"
                    android:value="@string/plugin_requires_host_version" /></service></application>
            </manifest>
        """
        strings = {
            "values-en": {
                "plugin_description": "Description",
                "plugin_requires_host_version": "5275",
            }
        }
        explicit_empty_gradle = """
            android {
                defaultConfig {
                    applicationId = "org.example.resource.precedence"
                    resValue("string", "plugin_requires_host_version", "")
                }
            }
        """
        missing_resource_gradle = """
            android {
                defaultConfig {
                    applicationId = "org.example.resource.precedence"
                }
            }
        """

        with self.assertRaisesRegex(RuntimeError, r"positive decimal integer"):
            self.build_entries(
                repo_name,
                explicit_empty_gradle,
                self.assets_for(repo_name, [None]),
                manifest_text=manifest,
                strings_by_dir=strings,
            )

        entry = self.build_entries(
            repo_name,
            missing_resource_gradle,
            self.assets_for(repo_name, [None]),
            manifest_text=manifest,
            strings_by_dir=strings,
        )[0]
        self.assertEqual(5275, entry["requiresHostVersion"])

    def test_blank_or_malformed_routing_value_fails_closed(self):
        repo_name = "AutoJs6-Plugin-Invalid-Routing"
        for resource_key, value in (
            ("plugin_engine", ""),
            ("plugin_variant", "not a route"),
            ("plugin_id", "bad/route"),
        ):
            with self.subTest(resource_key=resource_key, value=value):
                gradle = f"""
                    android {{
                        defaultConfig {{
                            applicationId = "org.example.invalid.routing"
                            resValue("string", "{resource_key}", "{value}")
                        }}
                    }}
                """
                with self.assertRaisesRegex(RuntimeError, r"routing identifier"):
                    self.build_entries(repo_name, gradle, self.assets_for(repo_name, [None]))

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

    def build_entries(
        self,
        repo_name,
        gradle,
        assets,
        *,
        version=None,
        manifest_text=None,
        strings_by_dir=None,
        admission_manifest_text=None,
        manifest_placeholders=None,
        version_map=None,
    ):
        version = version or self.VERSION
        if strings_by_dir is None:
            strings_by_dir = {"values-en": {"plugin_description": "Description"}}
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
            strings_by_dir=strings_by_dir,
            version_map=version_map or {"VERSION_NAME": version, "VERSION_BUILD": "99"},
            manifest_text=manifest_text or '<manifest><application android:label="@string/app_name" /></manifest>',
            build_gradle=gradle,
            manifest_placeholders=manifest_placeholders,
            admission_manifest_text=admission_manifest_text,
            source_commit="c" * 40,
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
            "digest": "sha256:" + "a" * 64,
        }

    def admission_manifest(
        self,
        asset,
        *,
        repo_name,
        package_name,
        runtime_component=None,
        supported_abis=None,
    ):
        artifact = {
            "name": asset["name"],
            "sha256": "a" * 64,
            "sizeBytes": asset["size"],
        }
        document = {
            "schemaVersion": 1,
            "owner": self.OWNER,
            "repository": repo_name,
            "releaseTag": f"v{self.VERSION}",
            "sourceCommit": "c" * 40,
            "packageName": package_name,
            "versionName": self.VERSION,
            "versionCode": 99,
            "signerSha256": ["b" * 64],
            "artifacts": [artifact],
        }
        if runtime_component is not None:
            document["runtimeComponent"] = runtime_component
        if supported_abis is not None:
            document["supportedAbis"] = supported_abis
        return json.dumps(document)

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
