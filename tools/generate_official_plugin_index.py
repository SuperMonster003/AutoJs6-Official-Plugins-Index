#!/usr/bin/env python3
import argparse
import html
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from xml.etree import ElementTree
from native_page_alignment import release_alignment


OFFICIAL_OWNER = "SuperMonster003"
OFFICIAL_REPO_PREFIX = "AutoJs6-Plugin-"
INDEX_REPO = "AutoJs6-Official-Plugins-Index"
INDEX_BRANCH = "main"
OUTPUT_FILE = "plugins.official.generated.json"
USER_AGENT = "AutoJs6-Official-Plugin-Index-Generator"
SCHEMA_VERSION = 2
DEFAULT_ADMISSION_ROOT = Path(__file__).resolve().parent.parent / "release-manifests"
DEFAULT_REQUIRED_REPOSITORIES = Path(__file__).resolve().parent.parent / "official-repositories.json"
ADMISSION_MANIFEST_SCHEMA_VERSION = 1
MAX_SIGNED_LONG = (1 << 63) - 1
ROUTING_RESOURCE_KEYS = {
    "engine": "plugin_engine",
    "variant": "plugin_variant",
    "engineId": "plugin_id",
}
CONTRACT_DECLARATIONS = {
    "requiresHostVersion": ("plugin_requires_host_version", "requiresHostVersion"),
    "maxHostVersion": ("plugin_max_host_version", "org.autojs.plugin.contract.MAX_HOST_VERSION"),
    "runtimeComponent": ("plugin_runtime_component", "org.autojs.plugin.contract.RUNTIME_COMPONENT"),
    "protocolApiMin": ("plugin_protocol_api_min", "org.autojs.plugin.contract.PROTOCOL_API_MIN"),
    "protocolApiMax": ("plugin_protocol_api_max", "org.autojs.plugin.contract.PROTOCOL_API_MAX"),
    "backend": ("plugin_backend", "org.autojs.plugin.contract.BACKEND"),
    "task": ("plugin_task", "org.autojs.plugin.contract.TASK"),
    "decoder": ("plugin_decoder", "org.autojs.plugin.contract.DECODER"),
    "supportedAbis": ("plugin_supported_abis", "org.autojs.plugin.contract.SUPPORTED_ABIS"),
    "nativePageAlignment": ("plugin_native_page_alignment", "org.autojs.plugin.contract.NATIVE_PAGE_ALIGNMENT"),
}
REQUIRES_HOST_VERSION_RESOURCE = CONTRACT_DECLARATIONS["requiresHostVersion"][0]
ROUTING_VALUE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
PROTOCOL_VERSION_PATTERN = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
CLASS_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)+")
SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}")
COMMIT_SHA_PATTERN = re.compile(r"[0-9a-fA-F]{40}")
PACKAGE_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")
SUPPORTED_ABIS = {
    "arm64-v8a",
    "armeabi-v7a",
    "x86_64",
    "x86",
    "armeabi",
    "mips64",
    "mips",
    "riscv64",
}

FEATURED_DISTRIBUTIONS = {
    "AutoJs6-Plugin-Paddle-OCR-PP-OCRv4": {"mobile"},
    "AutoJs6-Plugin-Paddle-OCR-PP-OCRv5": {"mobile"},
    "AutoJs6-Plugin-Paddle-OCR-PP-OCRv6": {"small"},
}

ABI_ASSET_TOKEN_PATTERN = (
    r"(?:arm64[-_]?v8a|aarch64|armeabi[-_]?v7a|armv7a?|x86[-_]?64|amd64|x86|"
    r"armeabi|mips64|mips|riscv[-_]?64|universal|noarch|all[-_]?abi(?:s)?|all[-_]?arch(?:es)?)"
)


@dataclass(frozen=True)
class ProductFlavor:
    name: str
    application_id_suffix: str
    version_name_suffix: str
    res_values: dict[str, str]

    @property
    def distribution_variant(self) -> str:
        suffix = self.version_name_suffix.strip().lstrip("-_.")
        return suffix or camel_case_to_kebab(self.name)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the AutoJs6 official plugins index.")
    parser.add_argument("--output", type=Path, default=Path(OUTPUT_FILE))
    parser.add_argument("--admission-root", type=Path, default=DEFAULT_ADMISSION_ROOT)
    parser.add_argument("--required-repositories", type=Path, default=DEFAULT_REQUIRED_REPOSITORIES)
    parser.add_argument("--native-cache", type=Path, default=Path(".cache/native-alignment"))
    parser.add_argument("--augment-native-alignment", action="store_true", help="Measure assets in the existing index without refreshing unrelated metadata")
    args = parser.parse_args()

    if args.augment_native_alignment:
        payload = json.loads(args.output.read_text(encoding="utf-8"))
        for item in payload["items"]:
            for release in item.get("releases", []):
                declared = release.get("nativePageAlignment") if release.get("nativePageAlignmentSource") == "declared" else None
                release.update(release_alignment(release.get("assets", []), declared, cache_root=args.native_cache))
            copy_latest_native_alignment(item)
            print(f"Measured {item['packageName']}: {item.get('nativePageAlignment', 'unknown')}", flush=True)
        args.output.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        return 0

    repos = fetch_official_repos()
    items = []
    with ThreadPoolExecutor(max_workers=4) as workers:
        results = workers.map(
            lambda repo: build_entries(repo, admission_root=args.admission_root, native_cache=args.native_cache),
            repos,
        )
        for repo, entries in zip(repos, results):
            items.extend(entries)
            print(f"Indexed {repo['name']}: {len(entries)} distributions", flush=True)

    validate_repository_coverage(items, json.loads(args.required_repositories.read_text(encoding="utf-8")))
    payload = build_payload(items)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    print(f"Generated {args.output.resolve()} with {len(items)} official plugin entries.")
    return 0


def validate_repository_coverage(items: list[dict], required_repositories: list[str]) -> None:
    if not isinstance(required_repositories, list) or not required_repositories:
        raise RuntimeError("Required official repositories must be a nonempty array.")
    if any(not isinstance(name, str) or not re.fullmatch(r"AutoJs6-Plugin-[A-Za-z0-9-]+", name) for name in required_repositories):
        raise RuntimeError("Required official repository names are malformed.")
    if len(set(required_repositories)) != len(required_repositories):
        raise RuntimeError("Required official repositories contain duplicates.")
    packages = [item["packageName"] for item in items]
    if len(packages) != len(set(packages)):
        raise RuntimeError("Official plugin entries contain duplicate package names.")
    featured_repositories = {
        item.get("repository", {}).get("name") for item in items
        if item.get("featured", True) and item.get("releases")
        and item.get("repository", {}).get("owner") == OFFICIAL_OWNER
        and item["releases"][0].get("assets")
    }
    missing = sorted(set(required_repositories) - featured_repositories)
    if missing:
        raise RuntimeError("Required repositories have no featured, published APK release: " + ", ".join(missing))


def build_payload(items: list[dict]) -> dict:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "source": "OFFICIAL",
        "repository": {
            "owner": OFFICIAL_OWNER,
            "repo": INDEX_REPO,
            "branch": INDEX_BRANCH,
        },
        "items": items,
    }


def fetch_official_repos() -> list[dict]:
    repos = []
    page = 1
    while True:
        url = (
            f"https://api.github.com/users/{OFFICIAL_OWNER}/repos"
            f"?per_page=100&type=public&sort=updated&page={page}"
        )
        chunk = api_json(url)
        if not isinstance(chunk, list):
            raise RuntimeError("GitHub repository list response is not an array.")
        repos.extend(
            repo
            for repo in chunk
            if not repo.get("fork")
            and not repo.get("archived")
            and str(repo.get("name", "")).startswith(OFFICIAL_REPO_PREFIX)
        )
        if len(chunk) < 100:
            break
        page += 1
    return sorted(repos, key=lambda repo: str(repo.get("name", "")).lower())


def build_entries(repo: dict, *, admission_root: Path | None = DEFAULT_ADMISSION_ROOT, native_cache: Path | None = None) -> list[dict]:
    owner = repo.get("owner", {}).get("login") or OFFICIAL_OWNER
    repo_name = repo.get("name")
    branch = repo.get("default_branch") or "master"
    if not repo_name:
        return []

    releases = api_json(f"https://api.github.com/repos/{owner}/{repo_name}/releases?per_page=100")
    release = latest_published_release(releases)
    if not isinstance(release, dict):
        print(f"Warning: skip {repo_name}, latest release unavailable.", file=sys.stderr)
        return []

    metadata_ref = release_metadata_ref(release, branch)
    tree_paths = fetch_tree_paths(owner, repo_name, metadata_ref)
    strings_by_dir = fetch_string_resources(owner, repo_name, metadata_ref, tree_paths)

    def read_source(path):
        return fetch_source_text(owner, repo_name, metadata_ref, path, tree_paths)

    version_map = parse_properties(read_source("version.properties") or "")
    manifest_text = read_source("app/src/main/AndroidManifest.xml")
    build_gradle = read_source("app/build.gradle.kts") or ""
    manifest_placeholders = resolve_manifest_placeholders(
        build_gradle, manifest_text,
        read_source=read_source,
        version_map=version_map,
    )

    return build_entries_from_release(
        owner=owner,
        repo_name=repo_name,
        ref=metadata_ref,
        release=release,
        tree_paths=tree_paths,
        strings_by_dir=strings_by_dir,
        version_map=version_map,
        manifest_text=manifest_text,
        build_gradle=build_gradle,
        manifest_placeholders=manifest_placeholders,
        admission_root=admission_root,
        native_cache=native_cache,
    )


def latest_published_release(releases: list[dict]) -> dict | None:
    """Include published milestone/RC builds without promoting them to stable."""
    if not isinstance(releases, list):
        raise RuntimeError("GitHub releases response is not an array.")
    published = [
        release for release in releases
        if not release.get("draft", False) and release.get("published_at")
    ]
    return max(
        published,
        key=lambda release: (str(release["published_at"]), int(release.get("id") or 0)),
        default=None,
    )


def build_entries_from_release(
    *,
    owner: str,
    repo_name: str,
    ref: str,
    release: dict,
    tree_paths: set[str],
    strings_by_dir: dict[str, dict[str, str]],
    version_map: dict[str, str],
    manifest_text: str | None,
    build_gradle: str,
    manifest_placeholders: dict[str, str] | None = None,
    admission_root: Path | None = None,
    admission_manifest_text: str | None = None,
    source_commit: str | None = None,
    native_cache: Path | None = None,
) -> list[dict]:

    base_package_name = (
        parse_manifest_package_name(manifest_text)
        or parse_application_id_from_build_gradle(build_gradle)
        or f"unknown.{re.sub(r'[^a-z0-9._-]', '-', repo_name.lower())}"
    )
    manifest_label = parse_manifest_application_label(manifest_text)
    release_name = str(release.get("name") or "").split(" @", 1)[0].strip()
    base_title = (
        resolve_string_reference(manifest_label, strings_by_dir)
        or literal_resource_value(manifest_label)
        or repo_name.removeprefix(OFFICIAL_REPO_PREFIX)
        or release_name
        or base_package_name
    )

    manifest_author = parse_manifest_author(manifest_text)
    release_author = release.get("author", {}).get("login")
    author = (
        resolve_string_reference(manifest_author, strings_by_dir)
        or literal_resource_value(manifest_author)
        or release_author
        or OFFICIAL_OWNER
    )

    localized_descriptions = localized_string_map(strings_by_dir, "plugin_description")
    localized_instructions = localized_string_map(strings_by_dir, "plugin_instruction")
    localized_instruction_urls = localized_instruction_markdown_urls(owner, repo_name, ref, tree_paths)

    icon_ref = parse_manifest_application_icon(manifest_text)
    icon_path = resolve_icon_path(icon_ref, tree_paths) or resolve_fallback_icon_path(tree_paths)
    night_icon_path = resolve_night_icon_path(icon_ref, tree_paths)

    release_tag = str(release.get("tag_name") or "").strip()
    base_version_name = (
        version_map.get("VERSION_NAME")
        or release_tag.removeprefix("v").removeprefix("V")
        or "0.0.0"
    )
    version_code = int(version_map.get("VERSION_CODE") or version_map.get("VERSION_BUILD") or 0)
    if "VERSION_CODE" not in version_map:
        offset = version_map.get("VERSION_CODE_OFFSET", "0")
        if not re.fullmatch(r"0|[1-9][0-9]*", offset):
            raise RuntimeError(f"{repo_name}: VERSION_CODE_OFFSET must be a non-negative integer.")
        version_code += int(offset)
    if repo_name == "AutoJs6-Plugin-APK-Builder-Template" and "HOST_VERSION_BUILD" in version_map:
        # The template is paired with a host build; its Android identity is composite.
        if not re.search(r"versions\.appVersionCode\s*\*\s*100\s*\+\s*versions\.pluginReleaseSeq", build_gradle):
            raise RuntimeError(f"{repo_name}: unsupported composite versionCode expression.")
        host_code = parse_positive_long(version_map["HOST_VERSION_BUILD"], source="HOST_VERSION_BUILD")
        sequence = version_map.get("PLUGIN_RELEASE_SEQ", "")
        if not re.fullmatch(r"[0-9]{1,2}", sequence):
            raise RuntimeError(f"{repo_name}: PLUGIN_RELEASE_SEQ must be in 0..99.")
        host_name = version_map.get("HOST_VERSION_NAME", "").strip()
        if not host_name:
            raise RuntimeError(f"{repo_name}: HOST_VERSION_NAME is missing.")
        version_code = host_code * 100 + int(sequence)
        base_version_name += "+autojs6-" + re.sub(r"\s", "-", host_name).lower()
    assets = release_assets(release)
    flavors = parse_product_flavors(build_gradle)
    asset_groups = group_release_assets_by_flavor(repo_name, assets, flavors, base_version_name=base_version_name) if flavors else [(None, assets)]
    default_res_values = parse_default_config_res_values(
        build_gradle,
        allow_global_fallback=not flavors,
        version_map=version_map,
    )
    manifest_contract_values = {
        field: parse_manifest_metadata_values(manifest_text, manifest_key)
        for field, (_, manifest_key) in CONTRACT_DECLARATIONS.items()
    }
    manifest_service_names = parse_manifest_service_names(manifest_text)

    entries = []
    for flavor, flavor_assets in asset_groups:
        if flavor is not None and not flavor_assets:
            continue

        res_values = dict(default_res_values)
        if flavor is not None:
            res_values.update(flavor.res_values)

        application_id_suffix = flavor.application_id_suffix if flavor is not None else ""
        version_name_suffix = flavor.version_name_suffix if flavor is not None else ""
        distribution_variant = flavor.distribution_variant if flavor is not None else None
        package_name = base_package_name + application_id_suffix
        version_name = base_version_name + version_name_suffix
        title = res_values.get("app_name") or base_title
        asset_supported_abis = supported_abis_from_assets(flavor_assets)
        entry_context = repo_name + (f"/{distribution_variant}" if distribution_variant else "")
        routing = {
            field: parse_optional_routing_value(
                res_values.get(resource_key),
                source=f'{entry_context} resValue("{resource_key}")',
            )
            for field, resource_key in ROUTING_RESOURCE_KEYS.items()
        }
        contract_parsers = {
            "requiresHostVersion": parse_positive_long,
            "maxHostVersion": parse_positive_long,
            "runtimeComponent": lambda value, source: parse_runtime_component(
                value,
                source=source,
                application_id=package_name,
                manifest_package=base_package_name,
                manifest_service_names=manifest_service_names,
            ),
            "protocolApiMin": parse_protocol_version,
            "protocolApiMax": parse_protocol_version,
            "backend": parse_contract_identifier,
            "task": parse_contract_identifier,
            "decoder": parse_contract_identifier,
            "supportedAbis": parse_supported_abis,
            "nativePageAlignment": parse_native_alignment,
        }
        contract = {
            field: resolve_optional_declared_value(
                context=entry_context,
                field=field,
                resource_name=resource_name,
                resource_value=res_values.get(resource_name),
                manifest_name=manifest_name,
                manifest_values=manifest_contract_values[field],
                res_values=res_values,
                strings_by_dir=strings_by_dir,
                parser=contract_parsers[field],
                manifest_placeholders=manifest_placeholders,
            )
            for field, (resource_name, manifest_name) in CONTRACT_DECLARATIONS.items()
        }
        requires_host_version = contract["requiresHostVersion"]
        max_host_version = contract["maxHostVersion"]
        if max_host_version is not None:
            if requires_host_version is None:
                raise RuntimeError(
                    f"{entry_context} maxHostVersion requires a requiresHostVersion lower bound."
                )
            if max_host_version < requires_host_version:
                raise RuntimeError(
                    f"{entry_context} maxHostVersion {max_host_version} is lower than "
                    f"requiresHostVersion {requires_host_version}."
                )

        runtime_component = contract["runtimeComponent"]
        protocol_api_min = contract["protocolApiMin"]
        protocol_api_max = contract["protocolApiMax"]
        if (protocol_api_min is None) != (protocol_api_max is None):
            raise RuntimeError(
                f"{entry_context} must declare protocolApiMin and protocolApiMax together."
            )
        if protocol_api_min is not None and protocol_api_max is not None:
            if protocol_version_key(protocol_api_max) < protocol_version_key(protocol_api_min):
                raise RuntimeError(
                    f"{entry_context} protocolApiMax {protocol_api_max} is lower than "
                    f"protocolApiMin {protocol_api_min}."
                )

        declared_supported_abis = contract["supportedAbis"]
        if (
            declared_supported_abis is not None
            and asset_supported_abis is not None
            and declared_supported_abis != asset_supported_abis
        ):
            raise RuntimeError(
                f"{entry_context} supportedAbis declaration {declared_supported_abis} conflicts "
                f"with release asset names {asset_supported_abis}."
            )
        supported_abis = declared_supported_abis or asset_supported_abis
        entry_admission_text = admission_manifest_text
        if entry_admission_text is None and admission_root is not None:
            entry_admission_text = read_admission_manifest(
                admission_root,
                package_name=package_name,
                version_code=version_code,
            )
        if entry_admission_text is not None and source_commit is None:
            source_commit = resolve_metadata_commit(owner, repo_name, ref)
        bound_assets = bind_release_artifacts(
            context=entry_context,
            assets=flavor_assets,
            admission_manifest_text=entry_admission_text,
            owner=owner,
            repo_name=repo_name,
            release_tag=release_tag,
            source_commit=source_commit,
            version_name=version_name,
            version_code=version_code,
            package_name=package_name,
            runtime_component=runtime_component,
            supported_abis=supported_abis,
        )

        release_entry = {
            "versionName": version_name,
            "versionCode": version_code,
            "versionDate": str(release.get("published_at") or "")[:10] or None,
            "changelogUrl": release.get("html_url"),
            "changelogText": str(release.get("body") or "").strip() or None,
            "assets": bound_assets,
            "prerelease": bool(release.get("prerelease", False)),
        }
        if native_cache is not None:
            release_entry.update(release_alignment(bound_assets, contract["nativePageAlignment"], cache_root=native_cache))
        elif contract["nativePageAlignment"] is not None:
            release_entry.update(nativePageAlignment=contract["nativePageAlignment"], nativePageAlignmentSource="declared")

        entry = {
            "packageName": package_name,
            "repository": {"owner": owner, "name": repo_name},
            "iconUrl": raw_url(owner, repo_name, ref, icon_path) if icon_path else None,
            "nightIconUrl": raw_url(owner, repo_name, ref, night_icon_path) if night_icon_path else None,
            "title": title,
            "description": choose_default_localized(localized_descriptions),
            "localizedDescriptions": localized_descriptions,
            "instructionHardCoded": choose_default_localized(localized_instructions),
            "localizedInstructionHardCoded": localized_instructions,
            "instructionMarkdownUrl": choose_default_localized(localized_instruction_urls),
            "localizedInstructionMarkdownUrls": localized_instruction_urls,
            "author": author,
            "collaborators": [],
            "engine": routing["engine"],
            "variant": routing["variant"],
            "engineId": routing["engineId"],
            "requiresHostVersion": requires_host_version,
            "maxHostVersion": max_host_version,
            "runtimeComponent": runtime_component,
            "protocolApiMin": protocol_api_min,
            "protocolApiMax": protocol_api_max,
            "backend": contract["backend"],
            "task": contract["task"],
            "decoder": contract["decoder"],
            "distributionVariant": distribution_variant,
            "featured": is_featured_distribution(repo_name, distribution_variant),
            "releases": [release_entry],
            "supportedAbis": supported_abis,
            "tags": ["official"],
            "source": "OFFICIAL",
        }
        copy_latest_native_alignment(entry)
        entries.append(prune_nulls(entry))
    return entries


def parse_native_alignment(value: str, *, source: str) -> int:
    if not re.fullmatch(r"0|[1-9][0-9]*", str(value)):
        raise RuntimeError(f"{source} must be zero or a positive power of two.")
    result = int(value)
    if result > MAX_SIGNED_LONG or result & (result - 1):
        raise RuntimeError(f"{source} must be zero or a positive power of two.")
    return result


def copy_latest_native_alignment(item: dict):
    latest = max(item.get("releases", []), key=lambda r: int(r.get("versionCode") or 0), default={})
    for field in ("nativePageAlignment", "nativePageAlignmentSource"):
        item.pop(field, None)
        if field in latest:
            item[field] = latest[field]


def fetch_tree_paths(owner: str, repo: str, ref: str) -> set[str]:
    encoded_ref = quote(ref, safe="")
    tree = safe_api_json(f"https://api.github.com/repos/{owner}/{repo}/git/trees/{encoded_ref}?recursive=1")
    if not isinstance(tree, dict) or not isinstance(tree.get("tree"), list):
        raise RuntimeError(f"Cannot read the published source tree for {owner}/{repo}@{ref}.")
    if tree.get("truncated"):
        raise RuntimeError(f"Published source tree is truncated for {owner}/{repo}@{ref}.")
    return {
        str(node.get("path", "")).strip()
        for node in tree.get("tree", [])
        if node.get("type") == "blob" and str(node.get("path", "")).strip()
    }


def fetch_source_text(owner: str, repo: str, ref: str, path: str, tree_paths: set[str]) -> str | None:
    if path not in tree_paths:
        return None
    text = raw_text(owner, repo, ref, path)
    if text is None:
        raise RuntimeError(f"Cannot read declared published source {owner}/{repo}@{ref}/{path}.")
    return text


def fetch_string_resources(owner: str, repo: str, ref: str, tree_paths: set[str]) -> dict[str, dict[str, str]]:
    result = {}
    for path in sorted(tree_paths):
        match = re.fullmatch(r"app/src/main/res/(values(?:-[^/]+)?)/strings(?:_[^/]+)?\.xml", path)
        if not match:
            continue
        text = fetch_source_text(owner, repo, ref, path, tree_paths)
        strings = parse_strings_xml(text)
        if strings:
            localized = result.setdefault(match.group(1), {})
            duplicates = localized.keys() & strings.keys()
            if duplicates:
                raise RuntimeError(f"Duplicate string resources in {owner}/{repo}@{ref}/{match.group(1)}: {sorted(duplicates)}")
            localized.update(strings)
    return result


def localized_string_map(strings_by_dir: dict[str, dict[str, str]], name: str) -> dict[str, str]:
    result = {}
    for directory in sorted(strings_by_dir):
        value = strings_by_dir[directory].get(name)
        if value:
            result[directory] = value
    return result


def localized_instruction_markdown_urls(
    owner: str,
    repo: str,
    ref: str,
    tree_paths: set[str],
) -> dict[str, str]:
    result = {}
    for path in sorted(tree_paths):
        match = re.fullmatch(r"app/src/main/res/(raw(?:-[^/]+)?)/plugin_instruction\.md", path)
        if match:
            result[match.group(1)] = raw_url(owner, repo, ref, path)
    return result


def release_assets(release: dict) -> list[dict]:
    result = []
    for asset in release.get("assets") or []:
        name = str(asset.get("name") or "")
        if not name.lower().endswith(".apk"):
            continue
        item = {
            "name": name,
            "browser_download_url": asset.get("browser_download_url"),
            "size": asset.get("size"),
            "digest": asset.get("digest"),
        }
        result.append(prune_nulls(item))
    return sorted(result, key=lambda item: str(item.get("name", "")).lower())


def read_admission_manifest(root: Path, *, package_name: str, version_code: int) -> str | None:
    if not PACKAGE_NAME_PATTERN.fullmatch(package_name) or version_code <= 0:
        return None
    path = root / package_name / f"{version_code}.json"
    return path.read_text(encoding="utf-8") if path.is_file() else None


def bind_release_artifacts(
    *,
    context: str,
    assets: list[dict],
    admission_manifest_text: str | None,
    owner: str,
    repo_name: str,
    release_tag: str,
    source_commit: str | None,
    version_name: str,
    version_code: int,
    package_name: str,
    runtime_component: str | None,
    supported_abis: list[str] | None,
) -> list[dict]:
    if admission_manifest_text is None:
        return assets
    source = f"{context} admission manifest"
    document = parse_json_object(admission_manifest_text, source=source)
    assert_exact_keys(
        document,
        required={
            "schemaVersion",
            "owner",
            "repository",
            "releaseTag",
            "sourceCommit",
            "packageName",
            "versionName",
            "versionCode",
            "signerSha256",
            "artifacts",
        },
        optional={"runtimeComponent", "supportedAbis"},
        source=source,
    )
    if document.get("schemaVersion") != ADMISSION_MANIFEST_SCHEMA_VERSION:
        raise RuntimeError(f"{source} schemaVersion must be {ADMISSION_MANIFEST_SCHEMA_VERSION}.")
    if not release_tag or version_code <= 0:
        raise RuntimeError(f"{source} requires a tagged release with a positive source versionCode.")

    expected_identity = {
        "owner": owner,
        "repository": repo_name,
        "releaseTag": release_tag,
        "packageName": package_name,
        "versionName": version_name,
        "versionCode": version_code,
    }
    for field, expected in expected_identity.items():
        actual = document.get(field)
        if actual != expected:
            raise RuntimeError(f"{source} {field} {actual!r} does not match {expected!r}.")
    resolved_source_commit = normalize_commit_sha(
        source_commit,
        source=f"{source} resolved sourceCommit",
    )
    admitted_source_commit = normalize_commit_sha(
        document.get("sourceCommit"),
        source=f"{source} sourceCommit",
    )
    if admitted_source_commit != resolved_source_commit:
        raise RuntimeError(
            f"{source} sourceCommit {admitted_source_commit!r} does not match "
            f"{resolved_source_commit!r}."
        )

    admitted_component = optional_nonempty_string(
        document.get("runtimeComponent"), source=f"{source} runtimeComponent"
    )
    if admitted_component is not None:
        admitted_component = normalize_component_reference(
            admitted_component,
            source=f"{source} runtimeComponent",
            application_id=package_name,
        )
    if admitted_component != runtime_component:
        raise RuntimeError(
            f"{source} runtimeComponent {admitted_component!r} does not match {runtime_component!r}."
        )

    admitted_abis = parse_json_abis(document.get("supportedAbis"), source=f"{source} supportedAbis")
    if admitted_abis != supported_abis:
        raise RuntimeError(f"{source} supportedAbis {admitted_abis} do not match {supported_abis}.")
    signers = parse_sha256_list(document.get("signerSha256"), source=f"{source} signerSha256")

    records = parse_admitted_artifacts(document.get("artifacts"), source=source)
    asset_names = [str(asset.get("name") or "") for asset in assets]
    if len(asset_names) != len(set(asset_names)) or set(asset_names) != records.keys():
        raise RuntimeError(f"{source} must match the selected release APK asset names exactly.")

    bound = []
    for asset in assets:
        name = str(asset["name"])
        record = records[name]
        artifact_source = f"{source} artifact {name!r}"
        admitted_size = require_positive_json_integer(record.get("sizeBytes"), source=f"{artifact_source} sizeBytes")
        admitted_sha256 = parse_sha256(record.get("sha256"), source=f"{artifact_source} sha256")
        asset_size = asset.get("size")
        if type(asset_size) is not int or asset_size <= 0 or asset_size != admitted_size:
            raise RuntimeError(f"{artifact_source} size does not match GitHub release size {asset_size!r}.")
        digest = asset.get("digest")
        if digest is not None:
            github_sha256 = parse_prefixed_sha256(digest, source=f"{artifact_source} GitHub digest")
            if github_sha256 != admitted_sha256:
                raise RuntimeError(f"{artifact_source} sha256 does not match GitHub release digest.")

        item = dict(asset)
        item.update(
            {
                "digest": f"sha256:{admitted_sha256}",
                "sha256": admitted_sha256,
                "signerSha256": signers,
                "versionName": version_name,
                "versionCode": version_code,
                "packageName": package_name,
                "runtimeComponent": admitted_component,
                "supportedAbis": admitted_abis,
                "sourceCommit": resolved_source_commit,
            }
        )
        bound.append(prune_nulls(item))
    return bound


def parse_admitted_artifacts(value, *, source: str) -> dict[str, dict]:
    if not isinstance(value, list) or not value:
        raise RuntimeError(f"{source} artifacts must be a non-empty array.")
    records = {}
    for index, item in enumerate(value, start=1):
        item_source = f"{source} artifact #{index}"
        if not isinstance(item, dict):
            raise RuntimeError(f"{item_source} must be an object.")
        assert_exact_keys(
            item,
            required={"name", "sha256", "sizeBytes"},
            optional=set(),
            source=item_source,
        )
        name = require_nonempty_string(item.get("name"), source=f"{item_source} name")
        if not name.lower().endswith(".apk") or name in records:
            raise RuntimeError(f"{item_source} must name one unique APK.")
        records[name] = item
    return records


def release_metadata_ref(release: dict, default_branch: str) -> str:
    release_tag = str(release.get("tag_name") or "").strip()
    if release_tag:
        return release_tag if release_tag.startswith("refs/") else f"refs/tags/{release_tag}"
    branch = default_branch.strip() or "master"
    return branch if branch.startswith("refs/") else f"refs/heads/{branch}"


def resolve_metadata_commit(owner: str, repo: str, ref: str) -> str:
    encoded_ref = quote(ref.strip(), safe="")
    document = api_json(f"https://api.github.com/repos/{owner}/{repo}/commits/{encoded_ref}")
    if not isinstance(document, dict):
        raise RuntimeError(f"Unable to resolve source commit for {owner}/{repo}@{ref}.")
    return normalize_commit_sha(document.get("sha"), source=f"{owner}/{repo}@{ref} sourceCommit")


def parse_product_flavors(build_gradle: str) -> list[ProductFlavor]:
    product_flavors_block = extract_named_block(build_gradle, "productFlavors")
    if product_flavors_block is None:
        return []

    flavors = []
    cursor = 0
    create_pattern = re.compile(r'\bcreate\s*\(\s*"([^"]+)"\s*\)\s*\{')
    while match := create_pattern.search(product_flavors_block, cursor):
        opening_brace = match.end() - 1
        closing_brace = find_matching_brace(product_flavors_block, opening_brace)
        flavor_block = product_flavors_block[opening_brace + 1 : closing_brace]
        res_values = parse_res_values(flavor_block)
        add_build_config_fallbacks(res_values, flavor_block)
        flavors.append(
            ProductFlavor(
                name=match.group(1),
                application_id_suffix=parse_string_assignment(flavor_block, "applicationIdSuffix") or "",
                version_name_suffix=parse_string_assignment(flavor_block, "versionNameSuffix") or "",
                res_values=res_values,
            )
        )
        cursor = closing_brace + 1

    if not flavors:
        raise RuntimeError(
            "Found a productFlavors block, but no create(\"...\") flavor declarations could be parsed."
        )
    return flavors


def parse_default_config_res_values(build_gradle: str, *, allow_global_fallback: bool = False, version_map: dict[str, str] | None = None) -> dict[str, str]:
    default_config_block = extract_named_block(build_gradle, "defaultConfig")
    scope = default_config_block if default_config_block is not None else build_gradle
    result = parse_res_values(scope)
    for resource, properties_name, key in re.findall(
        r'resValue\(\s*"string"\s*,\s*"([^"]+)"\s*,\s*([A-Za-z_][A-Za-z0-9_]*)\.getProperty\("([A-Z][A-Z0-9_]*)"\)\s*,?\s*\)', scope,
    ):
        if resource in result:
            raise RuntimeError(f"Duplicate static resource {resource!r}.")
        result[resource] = resolve_numeric_version_property(build_gradle, properties_name, key, version_map)
    add_build_config_fallbacks(result, scope)

    if allow_global_fallback and default_config_block is not None:
        global_values = parse_res_values(build_gradle)
        add_build_config_fallbacks(global_values, build_gradle)
        for key, value in global_values.items():
            result.setdefault(key, value)
    return resolve_static_resource_strings(result, build_gradle)


def resolve_static_resource_strings(values: dict[str, str], build_gradle: str) -> dict[str, str]:
    """Resolve only literal top-level string constants, never Gradle expressions."""
    def substitute(match):
        name = match.group(1) or match.group(2)
        declarations = re.findall(
            rf'\bval\s+{re.escape(name)}\s*=\s*"([^"$\\]*)"\s*(?:;|$)',
            build_gradle, re.MULTILINE,
        )
        if len(declarations) != 1:
            raise RuntimeError(f"Unresolved or ambiguous static resource constant {name!r}.")
        return declarations[0]
    return {
        key: re.sub(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)', substitute, value)
        for key, value in values.items()
    }


def resolve_numeric_version_property(build_gradle: str, properties_name: str, key: str, version_map: dict[str, str] | None) -> str:
    declaration = rf'\bval\s+{re.escape(properties_name)}\s*='
    use = r'(?:\(::load\)|\{\s*load\(it\)\s*\}|\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*->\s*load\(\1\)\s*\})'
    loaders = re.findall(
        declaration + r'\s*Properties\(\)\.apply\s*\{\s*rootProject\.file\("version\.properties"\)\.inputStream\(\)\.use\s*' + use + r'\s*\}',
        build_gradle,
    )
    value = (version_map or {}).get(key, "")
    if len(re.findall(declaration, build_gradle)) != 1 or len(loaders) != 1 or not re.fullmatch(r'[0-9]+', value):
        raise RuntimeError(f"Unresolved numeric version.properties value {properties_name}.{key}.")
    return value


def resolve_manifest_placeholders(build_gradle: str, manifest_text: str | None, *, read_source, version_map: dict[str, str] | None = None) -> dict[str, str]:
    """Resolve a small static Gradle subset, with source files loaded from the same release tag.

    Never evaluate Gradle code. Unsupported, missing, ambiguous or escaping references fail
    closed when a contract declaration uses them.
    """
    names = set()
    for _, manifest_key in CONTRACT_DECLARATIONS.values():
        for value in parse_manifest_metadata_values(manifest_text, manifest_key):
            match = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", value.strip())
            if match:
                names.add(match.group(1))
    result = {}
    for name in names:
        assignments = re.findall(
            rf'^\s*manifestPlaceholders\["{re.escape(name)}"\]\s*=\s*([^\r\n]+)',
            build_gradle, flags=re.MULTILINE,
        )
        if len(assignments) != 1:
            raise RuntimeError(f"Manifest placeholder {name!r} needs one static assignment.")
        expression = assignments[0].strip().removesuffix(";").removesuffix(".toString()")
        if re.fullmatch(r'"[^"$\\]*"|[0-9]+L?', expression):
            result[name] = expression[1:-1] if expression.startswith('"') else expression.removesuffix("L")
            continue
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", expression):
            raise RuntimeError(f"Unresolved manifest placeholder {name!r}.")
        if len(re.findall(rf'\bval\s+{re.escape(expression)}\s*=', build_gradle)) != 1:
            raise RuntimeError(f"Manifest placeholder {name!r} needs one value declaration.")
        literal = re.findall(rf'\bval\s+{re.escape(expression)}\s*=\s*([0-9]+)L?\s*(?:;|$)', build_gradle, re.MULTILINE)
        if len(literal) == 1:
            result[name] = literal[0]
            continue
        property_read = re.search(
            rf'\bval\s+{re.escape(expression)}\s*=\s*([A-Za-z_][A-Za-z0-9_]*)'
            r'\.getProperty\("([A-Z][A-Z0-9_]*)"\)\.to(?:Int|Long)\(\)\s*(?:;|$)',
            build_gradle, re.MULTILINE,
        )
        if property_read:
            properties_name, key = property_read.groups()
            result[name] = resolve_numeric_version_property(build_gradle, properties_name, key, version_map)
            continue
        extraction = re.search(
            rf'\bval\s+{re.escape(expression)}\s*=\s*Regex\("((?:\\.|[^"\\])*)"\)'
            r'\s*\.find\(\s*([A-Za-z_][A-Za-z0-9_]*)\.readText\(\)\s*\)'
            r'\s*\?\.groupValues\s*\?\.get\(1\)\s*\?\.toLong\(\)', build_gradle,
        )
        if not extraction:
            raise RuntimeError(f"Unresolved manifest placeholder {name!r}.")
        pattern = decode_kotlin_string(extraction.group(1))
        suffix = r"\s*=\s*(\d+)L"
        constant = pattern.removesuffix(suffix)
        if not pattern.endswith(suffix) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", constant):
            raise RuntimeError(f"Unsupported constant extraction for manifest placeholder {name!r}.")
        paths = re.findall(
            rf'\bval\s+{re.escape(extraction.group(2))}\s*=\s*file\(\s*"([^"$\\]+)"\s*,?\s*\)',
            build_gradle,
        )
        if len(paths) != 1 or not re.fullmatch(r"src/main/(?:java|kotlin)/[A-Za-z0-9_/]+\.(?:java|kt)", paths[0]):
            raise RuntimeError(f"Invalid source path for manifest placeholder {name!r}.")
        source = read_source("app/" + paths[0])
        values = re.findall(rf"\b{constant}\s*=\s*([0-9]+)L\b", source or "")
        if len(values) != 1:
            raise RuntimeError(f"Manifest placeholder {name!r} needs one source constant.")
        result[name] = values[0]
    return result


def parse_res_values(text: str) -> dict[str, str]:
    pattern = re.compile(
        r'resValue\(\s*"string"\s*,\s*"([^"]+)"\s*,\s*"((?:\\.|[^"\\])*)"\s*,?\s*\)'
    )
    return {
        match.group(1): decode_kotlin_string(match.group(2))
        for match in pattern.finditer(text)
    }


def add_build_config_fallbacks(values: dict[str, str], text: str) -> None:
    for resource_key, build_config_key in (
        ("plugin_engine", "PLUGIN_ENGINE"),
        ("plugin_variant", "PLUGIN_VARIANT"),
        ("plugin_id", "PLUGIN_ID"),
        (REQUIRES_HOST_VERSION_RESOURCE, "PLUGIN_REQUIRES_HOST_VERSION"),
    ):
        if resource_key not in values:
            value = parse_build_config_string(text, build_config_key)
            if value is not None:
                values[resource_key] = value


def parse_build_config_string(text: str, key: str) -> str | None:
    pattern = re.compile(
        rf'buildConfigField\(\s*"String"\s*,\s*"{re.escape(key)}"\s*,\s*"\\"([^"\\]+)\\""\s*\)'
    )
    match = pattern.search(text)
    return decode_kotlin_string(match.group(1)) if match else None


def parse_string_assignment(text: str, key: str) -> str | None:
    pattern = re.compile(rf'\b{re.escape(key)}\s*=\s*"((?:\\.|[^"\\])*)"')
    match = pattern.search(text)
    return decode_kotlin_string(match.group(1)) if match else None


def decode_kotlin_string(value: str) -> str:
    escapes = {
        r"\n": "\n",
        r"\r": "\r",
        r"\t": "\t",
        r'\"': '"',
        r"\\": "\\",
    }
    return re.sub(r'\\[nrt"\\]', lambda match: escapes.get(match.group(0), match.group(0)), value)


def extract_named_block(text: str, name: str) -> str | None:
    match = re.search(rf'\b{re.escape(name)}\s*\{{', text)
    if not match:
        return None
    opening_brace = match.end() - 1
    closing_brace = find_matching_brace(text, opening_brace)
    return text[opening_brace + 1 : closing_brace]


def find_matching_brace(text: str, opening_brace: int) -> int:
    if opening_brace >= len(text) or text[opening_brace] != "{":
        raise ValueError("opening_brace must point to '{'")

    depth = 0
    cursor = opening_brace
    quote = None
    triple_quote = False
    while cursor < len(text):
        if quote is not None:
            if triple_quote and text.startswith(quote * 3, cursor):
                cursor += 3
                quote = None
                triple_quote = False
                continue
            if not triple_quote and text[cursor] == "\\":
                cursor += 2
                continue
            if not triple_quote and text[cursor] == quote:
                quote = None
            cursor += 1
            continue

        if text.startswith("//", cursor):
            newline = text.find("\n", cursor + 2)
            cursor = len(text) if newline < 0 else newline + 1
            continue
        if text.startswith("/*", cursor):
            comment_end = text.find("*/", cursor + 2)
            if comment_end < 0:
                raise ValueError("Unterminated block comment while parsing Gradle script")
            cursor = comment_end + 2
            continue
        if text.startswith('"""', cursor):
            quote = '"'
            triple_quote = True
            cursor += 3
            continue
        if text[cursor] in ('"', "'"):
            quote = text[cursor]
            cursor += 1
            continue
        if text[cursor] == "{":
            depth += 1
        elif text[cursor] == "}":
            depth -= 1
            if depth == 0:
                return cursor
        cursor += 1
    raise ValueError("Unterminated Gradle block")


def group_release_assets_by_flavor(
    repo_name: str,
    assets: list[dict],
    flavors: list[ProductFlavor],
    *,
    base_version_name: str | None = None,
) -> list[tuple[ProductFlavor, list[dict]]]:
    grouped = {flavor.name: [] for flavor in flavors}
    unmatched = []
    ambiguous = []

    for asset in assets:
        asset_name = str(asset.get("name") or "")
        matched = [
            flavor
            for flavor in flavors
            if asset_name_matches_distribution(asset_name, flavor.distribution_variant)
        ]
        # An unsuffixed production flavor can coexist with isolated test flavors.
        # Accept only its exact version/ABI/CRC32 naming convention, never an
        # arbitrary unmatched asset or an ambiguous default flavor.
        if not matched and base_version_name is not None and re.fullmatch(
            rf"{re.escape(repo_name)}-v{re.escape(base_version_name)}-{ABI_ASSET_TOKEN_PATTERN}-[0-9a-f]{{8}}\.apk",
            asset_name, flags=re.IGNORECASE,
        ):
            matched = [flavor for flavor in flavors if not flavor.application_id_suffix and not flavor.version_name_suffix]
        if len(matched) == 1:
            grouped[matched[0].name].append(asset)
        elif not matched:
            unmatched.append(asset_name)
        else:
            ambiguous.append((asset_name, [flavor.name for flavor in matched]))

    if unmatched or ambiguous:
        details = []
        if unmatched:
            details.append(f"unmatched={unmatched}")
        if ambiguous:
            details.append(f"ambiguous={ambiguous}")
        raise RuntimeError(
            f"{repo_name}: failed to assign release APK assets to product flavors: {'; '.join(details)}"
        )

    required_featured = FEATURED_DISTRIBUTIONS.get(repo_name)
    if required_featured:
        declared_by_distribution = {
            flavor.distribution_variant.lower(): flavor
            for flavor in flavors
        }
        undeclared = sorted(required_featured - declared_by_distribution.keys())
        missing_assets = sorted(
            distribution
            for distribution in required_featured
            if distribution in declared_by_distribution
            and not grouped[declared_by_distribution[distribution].name]
        )
        if undeclared or missing_assets:
            details = []
            if undeclared:
                details.append(f"undeclared={undeclared}")
            if missing_assets:
                details.append(f"without assets={missing_assets}")
            raise RuntimeError(
                f"{repo_name}: required featured distributions are unavailable: {'; '.join(details)}"
            )

    return [(flavor, grouped[flavor.name]) for flavor in flavors]


def asset_name_matches_distribution(asset_name: str, distribution_variant: str) -> bool:
    token = distribution_variant.strip().strip("-_.")
    if not token:
        return False
    pattern = re.compile(
        rf"(?i)(?:^|[-_.]){re.escape(token)}[-_.](?={ABI_ASSET_TOKEN_PATTERN}(?:[-_.]|$))"
    )
    return pattern.search(asset_name) is not None


def is_featured_distribution(repo_name: str, distribution_variant: str | None) -> bool:
    if distribution_variant is None:
        return True
    featured = FEATURED_DISTRIBUTIONS.get(repo_name)
    return True if featured is None else distribution_variant.lower() in featured


def camel_case_to_kebab(value: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "-", value).replace("_", "-").lower()


def supported_abis_from_assets(assets: list[dict]) -> list[str] | None:
    found = set()
    for asset in assets:
        found.update(parse_abis_from_text(str(asset.get("name") or "")))
    found.discard("universal")
    return sort_abis(found) or None


def parse_abis_from_text(text: str) -> set[str]:
    patterns = {
        "arm64-v8a": r"(?i)(arm64[-_]?v8a|aarch64)",
        "armeabi-v7a": r"(?i)(armeabi[-_]?v7a|armv7a?)",
        "x86_64": r"(?i)(x86[-_]?64|amd64)",
        "x86": r"(?i)(?<![a-z0-9])x86(?![-_]?64)",
        "armeabi": r"(?i)(armeabi(?![-_]?v7a))",
        "mips64": r"(?i)mips64",
        "mips": r"(?i)(?<![a-z0-9])mips(?!64)",
        "riscv64": r"(?i)riscv[-_]?64",
        "universal": r"(?i)(universal|noarch|all[-_]?abi(?:s)?|all[-_]?arch(?:es)?)",
    }
    return {abi for abi, pattern in patterns.items() if re.search(pattern, text)}


def sort_abis(values: set[str]) -> list[str]:
    order = ["arm64-v8a", "armeabi-v7a", "x86_64", "x86", "armeabi", "mips64", "mips", "riscv64", "universal"]
    return sorted(values, key=lambda item: order.index(item) if item in order else len(order))


def parse_strings_xml(text: str) -> dict[str, str]:
    try:
        root = ElementTree.fromstring(text.lstrip("\ufeff"))
    except ElementTree.ParseError:
        return {}
    result = {}
    for element in root.findall("string"):
        name = element.attrib.get("name")
        if not name:
            continue
        value = html.unescape("".join(element.itertext()).strip())
        if value:
            result[name] = value
    return result


def parse_properties(text: str) -> dict[str, str]:
    result = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def parse_manifest_package_name(text: str | None) -> str | None:
    return regex_group(text, r'<manifest[^>]*\bpackage="([^"]+)"')


def parse_manifest_application_label(text: str | None) -> str | None:
    return regex_group(text, r'<application[^>]*\bandroid:label="([^"]+)"')


def parse_manifest_application_icon(text: str | None) -> str | None:
    return regex_group(text, r'<application[^>]*\bandroid:icon="([^"]+)"')


def parse_manifest_author(text: str | None) -> str | None:
    return regex_group(
        text,
        r'<meta-data[^>]*\bandroid:name="org\.autojs\.plugin\.info\.AUTHOR"[^>]*\bandroid:value="([^"]+)"',
    )


def parse_manifest_metadata_values(text: str | None, name: str) -> list[str]:
    if not text:
        return []

    result = []
    for tag in re.findall(r"<meta-data\b[^>]*?/?>", text, flags=re.DOTALL):
        tag_name = regex_group(tag, r'\bandroid:name\s*=\s*"([^"]+)"')
        if tag_name != name:
            continue
        value = regex_group(tag, r'\bandroid:value\s*=\s*"([^"]*)"')
        if value is None:
            raise RuntimeError(f'Manifest meta-data "{name}" must declare android:value.')
        result.append(html.unescape(value))
    return result


def parse_manifest_service_names(text: str | None) -> set[str]:
    if not text:
        return set()
    result = set()
    for tag in re.findall(r"<service\b[^>]*?/?>", text, flags=re.DOTALL):
        name = regex_group(tag, r'\bandroid:name\s*=\s*"([^"]*)"')
        if name is None:
            continue
        normalized = html.unescape(name).strip()
        if not normalized:
            raise RuntimeError("Manifest service android:name must not be empty.")
        result.add(normalized)
    return result


def parse_optional_routing_value(value: str | None, *, source: str) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not ROUTING_VALUE_PATTERN.fullmatch(normalized):
        raise RuntimeError(
            f"{source} must be a 1-128 character routing identifier containing only "
            "letters, digits, '.', '_' or '-'."
        )
    return normalized


def resolve_optional_declared_value(
    *,
    context: str,
    field: str,
    resource_name: str,
    resource_value: str | None,
    manifest_name: str,
    manifest_values: list[str],
    res_values: dict[str, str],
    strings_by_dir: dict[str, dict[str, str]],
    parser,
    manifest_placeholders: dict[str, str] | None = None,
):
    candidates = []
    if resource_value is not None:
        candidates.append((f'{context} resValue("{resource_name}")', resource_value))
    for index, manifest_value in enumerate(manifest_values, start=1):
        resolved = resolve_metadata_value(manifest_value, res_values, strings_by_dir, manifest_placeholders)
        if resolved is None:
            raise RuntimeError(
                f'{context} manifest meta-data "{manifest_name}" '
                f"#{index} has an unresolved value: {manifest_value!r}."
            )
        candidates.append(
            (
                f'{context} manifest meta-data "{manifest_name}" #{index}',
                resolved,
            )
        )
    if not candidates:
        return None
    parsed = [(source, parser(value, source=source)) for source, value in candidates]
    canonical = {json.dumps(value, sort_keys=True) for _, value in parsed}
    if len(canonical) != 1:
        details = ", ".join(f"{source}={value!r}" for source, value in parsed)
        raise RuntimeError(f"{context} has conflicting {field} declarations: {details}.")
    return parsed[0][1]


def parse_contract_identifier(value: str, *, source: str) -> str:
    normalized = value.strip()
    if not ROUTING_VALUE_PATTERN.fullmatch(normalized):
        raise RuntimeError(
            f"{source} must be a 1-128 character contract identifier containing only "
            "letters, digits, '.', '_' or '-'."
        )
    return normalized


def parse_protocol_version(value: str, *, source: str) -> str:
    normalized = value.strip()
    match = PROTOCOL_VERSION_PATTERN.fullmatch(normalized)
    if not match:
        raise RuntimeError(f"{source} must use canonical major.minor decimal form, got {value!r}.")
    major = int(match.group(1))
    minor = int(match.group(2))
    if major > MAX_SIGNED_LONG or minor > MAX_SIGNED_LONG:
        raise RuntimeError(f"{source} exceeds the signed 64-bit range: {value!r}.")
    return f"{major}.{minor}"


def protocol_version_key(value: str) -> tuple[int, int]:
    major, minor = value.split(".", 1)
    return int(major), int(minor)


def parse_supported_abis(value: str, *, source: str) -> list[str]:
    normalized = value.strip()
    if not normalized:
        raise RuntimeError(f"{source} must not be empty.")
    raw_values = [item.strip() for item in normalized.split(",")]
    if any(not item for item in raw_values):
        raise RuntimeError(f"{source} must be a comma-separated list without empty entries.")
    if len(raw_values) != len(set(raw_values)):
        raise RuntimeError(f"{source} must not contain duplicate ABI values.")
    unknown = sorted(set(raw_values) - SUPPORTED_ABIS)
    if unknown:
        raise RuntimeError(f"{source} contains unsupported ABI values: {unknown}.")
    return sort_abis(set(raw_values))


def parse_runtime_component(
    value: str,
    *,
    source: str,
    application_id: str,
    manifest_package: str,
    manifest_service_names: set[str],
) -> str:
    component = normalize_component_reference(
        value,
        source=source,
        application_id=application_id,
    )
    _, class_name = component.split("/", 1)
    declared_services = {
        normalize_manifest_class_name(name, manifest_package)
        for name in manifest_service_names
    }
    if class_name not in declared_services:
        raise RuntimeError(
            f"{source} references {class_name!r}, which is not a declared manifest service."
        )
    return component


def normalize_component_reference(value: str, *, source: str, application_id: str) -> str:
    normalized = value.strip()
    if normalized.count("/") != 1:
        raise RuntimeError(
            f"{source} must use exact package/fully.qualified.Service component form, got {value!r}."
        )
    package_name, class_name = normalized.split("/", 1)
    if package_name != application_id:
        raise RuntimeError(
            f"{source} component package {package_name!r} does not match applicationId {application_id!r}."
        )
    if not CLASS_NAME_PATTERN.fullmatch(class_name):
        raise RuntimeError(f"{source} service class is invalid: {class_name!r}.")
    return f"{package_name}/{class_name}"


def normalize_manifest_class_name(name: str, manifest_package: str) -> str:
    if name.startswith("."):
        return manifest_package + name
    if "." not in name:
        return manifest_package + "." + name
    return name


def require_nonempty_string(value, *, source: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{source} must be a non-empty string.")
    return value.strip()


def parse_json_object(text: str, *, source: str) -> dict:
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{source} is not valid JSON: {exc}.") from exc
    if not isinstance(document, dict):
        raise RuntimeError(f"{source} root must be an object.")
    return document


def assert_exact_keys(value: dict, *, required: set[str], optional: set[str], source: str) -> None:
    missing = sorted(required - value.keys())
    unknown = sorted(value.keys() - required - optional)
    if missing or unknown:
        raise RuntimeError(f"{source} has invalid fields: missing={missing}, unknown={unknown}.")


def optional_nonempty_string(value, *, source: str) -> str | None:
    return None if value is None else require_nonempty_string(value, source=source)


def parse_json_abis(value, *, source: str) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not value or any(not isinstance(item, str) for item in value):
        raise RuntimeError(f"{source} must be a non-empty string array when present.")
    return parse_supported_abis(",".join(value), source=source)


def parse_sha256_list(value, *, source: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise RuntimeError(f"{source} must be a non-empty array.")
    result = sorted(
        parse_sha256(item, source=f"{source} #{index}")
        for index, item in enumerate(value, start=1)
    )
    if len(result) != len(set(result)):
        raise RuntimeError(f"{source} must not contain duplicates.")
    return result


def require_positive_json_integer(value, *, source: str) -> int:
    if type(value) is not int or value <= 0:
        raise RuntimeError(f"{source} must be a positive JSON integer.")
    if value > MAX_SIGNED_LONG:
        raise RuntimeError(f"{source} exceeds the signed 64-bit range.")
    return value


def parse_sha256(value, *, source: str) -> str:
    normalized = require_nonempty_string(value, source=source)
    if not SHA256_PATTERN.fullmatch(normalized):
        raise RuntimeError(f"{source} must be exactly 64 hexadecimal characters.")
    return normalized.lower()


def parse_prefixed_sha256(value, *, source: str) -> str:
    normalized = require_nonempty_string(value, source=source).lower()
    if not normalized.startswith("sha256:"):
        raise RuntimeError(f"{source} must start with 'sha256:'.")
    return parse_sha256(normalized.removeprefix("sha256:"), source=source)


def normalize_commit_sha(value, *, source: str) -> str:
    normalized = require_nonempty_string(value, source=source)
    if not COMMIT_SHA_PATTERN.fullmatch(normalized):
        raise RuntimeError(f"{source} must be exactly 40 hexadecimal characters.")
    return normalized.lower()


def resolve_metadata_value(
    value: str,
    res_values: dict[str, str],
    strings_by_dir: dict[str, dict[str, str]],
    manifest_placeholders: dict[str, str] | None = None,
) -> str | None:
    normalized = value.strip()
    placeholder = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", normalized)
    if placeholder:
        return (manifest_placeholders or {}).get(placeholder.group(1))
    if not normalized.startswith("@string/"):
        return normalized
    resource_name = normalized.removeprefix("@string/")
    if resource_name in res_values:
        return res_values[resource_name]
    return choose_default_localized(
        localized_string_map(strings_by_dir, resource_name)
    )


def parse_positive_long(value: str, *, source: str) -> int:
    normalized = value.strip()
    if not re.fullmatch(r"[1-9][0-9]*", normalized):
        raise RuntimeError(f"{source} must be a positive decimal integer, got {value!r}.")
    parsed = int(normalized)
    if parsed > MAX_SIGNED_LONG:
        raise RuntimeError(f"{source} exceeds the signed 64-bit range: {value!r}.")
    return parsed


def parse_application_id_from_build_gradle(text: str) -> str | None:
    scope = extract_named_block(text, "defaultConfig") or text
    variable = re.search(r'^\s*applicationId\s*=\s*([A-Za-z_][A-Za-z0-9_]*)\s*$', scope, re.MULTILINE)
    if variable:
        values = re.findall(rf'\bval\s+{re.escape(variable.group(1))}\s*=\s*"([^"$\\]+)"', text)
        if len(values) != 1 or not PACKAGE_NAME_PATTERN.fullmatch(values[0]):
            raise RuntimeError("applicationId needs one literal package-name constant.")
        return values[0]
    return (
        regex_group(text, r'val\s+globalApplicationId\s*=\s*"([^"]+)"')
        or regex_group(text, r'\bapplicationId\s*=\s*"([^"]+)"')
        or regex_group(text, r'\bnamespace\s*=\s*"([^"]+)"')
    )


def regex_group(text: str | None, pattern: str) -> str | None:
    if not text:
        return None
    match = re.search(pattern, text, flags=re.DOTALL)
    if not match:
        return None
    value = match.group(1).strip()
    return value or None


def resolve_string_reference(value: str | None, strings_by_dir: dict[str, dict[str, str]]) -> str | None:
    if not value or not value.startswith("@string/"):
        return None
    name = value.removeprefix("@string/")
    localized = localized_string_map(strings_by_dir, name)
    return choose_default_localized(localized)


def literal_resource_value(value: str | None) -> str | None:
    if not value or value.startswith("@"):
        return None
    return value.strip() or None


def choose_default_localized(values: dict[str, str]) -> str | None:
    for key in ("values-en", "values", "en", "default"):
        value = values.get(key)
        if value:
            return value
    return next((value for _, value in sorted(values.items()) if value), None)


def resolve_icon_path(icon_ref: str | None, tree_paths: set[str]) -> str | None:
    for candidate in build_icon_candidates(icon_ref):
        if candidate in tree_paths:
            return candidate
    return None


def resolve_night_icon_path(icon_ref: str | None, tree_paths: set[str]) -> str | None:
    for candidate in build_night_icon_candidates(icon_ref):
        if candidate in tree_paths:
            return candidate
    return None


def build_icon_candidates(icon_ref: str | None) -> list[str]:
    if not icon_ref or not icon_ref.startswith("@") or "/" not in icon_ref:
        return []
    resource_type = icon_ref[1:].split("/", 1)[0]
    name = icon_ref.split("/", 1)[1]
    dirs = [
        f"app/src/main/res/{resource_type}",
        f"app/src/main/res/{resource_type}-xxxhdpi",
        f"app/src/main/res/{resource_type}-xxhdpi",
        f"app/src/main/res/{resource_type}-xhdpi",
        f"app/src/main/res/{resource_type}-hdpi",
        f"app/src/main/res/{resource_type}-mdpi",
        f"app/src/main/res/{resource_type}-anydpi-v26",
        "app/src/main/res/drawable",
        "app/src/main/res/drawable-xxxhdpi",
        "app/src/main/res/drawable-xxhdpi",
        "app/src/main/res/drawable-xhdpi",
        "app/src/main/res/drawable-hdpi",
        "app/src/main/res/drawable-mdpi",
    ]
    return [f"{directory}/{name}.{ext}" for directory in dirs for ext in ("png", "webp", "jpg", "jpeg")]


def build_night_icon_candidates(icon_ref: str | None) -> list[str]:
    if not icon_ref or not icon_ref.startswith("@") or "/" not in icon_ref:
        return []
    resource_type = icon_ref[1:].split("/", 1)[0]
    name = icon_ref.split("/", 1)[1]
    dirs = [f"app/src/main/res/{resource_type}-night", "app/src/main/res/drawable-night"]
    return [f"{directory}/{name}.{ext}" for directory in dirs for ext in ("png", "webp", "jpg", "jpeg")]


def resolve_fallback_icon_path(tree_paths: set[str]) -> str | None:
    image_paths = [
        path
        for path in tree_paths
        if re.fullmatch(r"app/src/main/res/(?:drawable(?:-[^/]+)?|mipmap(?:-[^/]+)?)/[^/]+\.(?:png|webp|jpg|jpeg)", path, re.I)
    ]
    if not image_paths:
        return None
    return sorted(image_paths, key=lambda path: (score_icon_path(path), len(path), path))[0]


def score_icon_path(path: str) -> int:
    lower_path = path.lower()
    file_name = lower_path.rsplit("/", 1)[-1]
    score = 100
    if "/drawable" in lower_path:
        score -= 20
    if file_name.startswith("ic_plugin"):
        score -= 70
    elif file_name.startswith("ic_launcher"):
        score -= 60
    elif "plugin" in file_name:
        score -= 45
    elif "logo" in file_name:
        score -= 35
    elif "icon" in file_name:
        score -= 20
    return score


def api_json(url: str):
    return json.loads(request_text(url, accept="application/vnd.github+json"))


def safe_api_json(url: str):
    try:
        return api_json(url)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"Warning: GitHub API request failed for {url}: {exc}", file=sys.stderr)
        return None


def raw_text(owner: str, repo: str, ref: str, path: str) -> str | None:
    try:
        return request_text(raw_url(owner, repo, ref, path), accept="text/plain, */*")
    except HTTPError as exc:
        if exc.code != 404:
            print(f"Warning: raw request failed for {owner}/{repo}/{path}: {exc}", file=sys.stderr)
        return None
    except (URLError, TimeoutError) as exc:
        print(f"Warning: raw request failed for {owner}/{repo}/{path}: {exc}", file=sys.stderr)
        return None


def request_text(url: str, accept: str) -> str:
    headers = {
        "Accept": accept,
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
    req = Request(url, headers=headers)
    with urlopen(req, timeout=30) as response:
        return response.read().decode("utf-8")


def raw_url(owner: str, repo: str, ref: str, path: str) -> str:
    encoded_ref = quote(ref.strip(), safe="/")
    encoded_path = quote(path.lstrip("/"), safe="/")
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{encoded_ref}/{encoded_path}"


def prune_nulls(value):
    if isinstance(value, dict):
        return {key: prune_nulls(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [prune_nulls(item) for item in value]
    return value


if __name__ == "__main__":
    raise SystemExit(main())
