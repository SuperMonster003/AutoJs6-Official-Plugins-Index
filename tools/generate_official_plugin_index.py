#!/usr/bin/env python3
import argparse
import html
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from xml.etree import ElementTree


OFFICIAL_OWNER = "SuperMonster003"
OFFICIAL_REPO_PREFIX = "AutoJs6-Plugin-"
INDEX_REPO = "AutoJs6-Official-Plugins-Index"
INDEX_BRANCH = "main"
OUTPUT_FILE = "plugins.official.generated.json"
USER_AGENT = "AutoJs6-Official-Plugin-Index-Generator"
SCHEMA_VERSION = 2

FEATURED_DISTRIBUTIONS = {
    "AutoJs6-Plugin-Paddle-OCR-PP-OCRv4": {"mobile"},
    "AutoJs6-Plugin-Paddle-OCR-PP-OCRv5": {"mobile"},
    "AutoJs6-Plugin-Paddle-OCR-PP-OCRv6": {"small", "tiny"},
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
    args = parser.parse_args()

    repos = fetch_official_repos()
    items = []
    for repo in repos:
        items.extend(build_entries(repo))

    payload = build_payload(items)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    print(f"Generated {args.output.resolve()} with {len(items)} official plugin entries.")
    return 0


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


def build_entries(repo: dict) -> list[dict]:
    owner = repo.get("owner", {}).get("login") or OFFICIAL_OWNER
    repo_name = repo.get("name")
    branch = repo.get("default_branch") or "master"
    if not repo_name:
        return []

    release = safe_api_json(f"https://api.github.com/repos/{owner}/{repo_name}/releases/latest")
    if not isinstance(release, dict):
        print(f"Warning: skip {repo_name}, latest release unavailable.", file=sys.stderr)
        return []

    metadata_ref = release_metadata_ref(release, branch)
    tree_paths = fetch_tree_paths(owner, repo_name, metadata_ref)
    strings_by_dir = fetch_string_resources(owner, repo_name, metadata_ref, tree_paths)
    version_map = parse_properties(raw_text(owner, repo_name, metadata_ref, "version.properties") or "")
    manifest_text = raw_text(owner, repo_name, metadata_ref, "app/src/main/AndroidManifest.xml")
    build_gradle = raw_text(owner, repo_name, metadata_ref, "app/build.gradle.kts") or ""

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
    assets = release_assets(release)
    flavors = parse_product_flavors(build_gradle)
    asset_groups = group_release_assets_by_flavor(repo_name, assets, flavors) if flavors else [(None, assets)]
    default_res_values = parse_default_config_res_values(
        build_gradle,
        allow_global_fallback=not flavors,
    )

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
        supported_abis = supported_abis_from_assets(flavor_assets)

        release_entry = {
            "versionName": version_name,
            "versionCode": version_code,
            "versionDate": str(release.get("published_at") or "")[:10] or None,
            "changelogUrl": release.get("html_url"),
            "changelogText": str(release.get("body") or "").strip() or None,
            "assets": flavor_assets,
        }

        entry = {
            "packageName": package_name,
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
            "engine": res_values.get("plugin_engine"),
            "variant": res_values.get("plugin_variant"),
            "engineId": res_values.get("plugin_id"),
            "distributionVariant": distribution_variant,
            "featured": is_featured_distribution(repo_name, distribution_variant),
            "releases": [release_entry],
            "supportedAbis": supported_abis,
            "tags": ["official"],
            "source": "OFFICIAL",
        }
        entries.append(prune_nulls(entry))
    return entries


def fetch_tree_paths(owner: str, repo: str, ref: str) -> set[str]:
    encoded_ref = quote(ref, safe="")
    tree = safe_api_json(f"https://api.github.com/repos/{owner}/{repo}/git/trees/{encoded_ref}?recursive=1")
    if not isinstance(tree, dict):
        return set()
    if tree.get("truncated"):
        print(f"Warning: tree truncated for {owner}/{repo}@{ref}", file=sys.stderr)
    return {
        str(node.get("path", "")).strip()
        for node in tree.get("tree", [])
        if node.get("type") == "blob" and str(node.get("path", "")).strip()
    }


def fetch_string_resources(owner: str, repo: str, ref: str, tree_paths: set[str]) -> dict[str, dict[str, str]]:
    result = {}
    for path in sorted(tree_paths):
        match = re.fullmatch(r"app/src/main/res/(values(?:-[^/]+)?)/strings\.xml", path)
        if not match:
            continue
        text = raw_text(owner, repo, ref, path)
        if text is None:
            continue
        strings = parse_strings_xml(text)
        if strings:
            result[match.group(1)] = strings
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


def release_metadata_ref(release: dict, default_branch: str) -> str:
    release_tag = str(release.get("tag_name") or "").strip()
    if release_tag:
        return release_tag if release_tag.startswith("refs/") else f"refs/tags/{release_tag}"
    branch = default_branch.strip() or "master"
    return branch if branch.startswith("refs/") else f"refs/heads/{branch}"


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


def parse_default_config_res_values(build_gradle: str, *, allow_global_fallback: bool = False) -> dict[str, str]:
    default_config_block = extract_named_block(build_gradle, "defaultConfig")
    scope = default_config_block if default_config_block is not None else build_gradle
    result = parse_res_values(scope)
    add_build_config_fallbacks(result, scope)

    if allow_global_fallback and default_config_block is not None:
        global_values = parse_res_values(build_gradle)
        add_build_config_fallbacks(global_values, build_gradle)
        for key, value in global_values.items():
            result.setdefault(key, value)
    return result


def parse_res_values(text: str) -> dict[str, str]:
    pattern = re.compile(
        r'resValue\(\s*"string"\s*,\s*"([^"]+)"\s*,\s*"((?:\\.|[^"\\])*)"\s*\)'
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


def parse_application_id_from_build_gradle(text: str) -> str | None:
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
