"""Versioned catalog artwork and retired installation identities.

Presentation is index-owned; APK versions, identities and admission evidence still
come exclusively from published releases. No sibling checkout is read here.
"""
from pathlib import Path
import hashlib
import json
import re
import struct

ROOT = Path(__file__).resolve().parents[1]
SHA256 = re.compile(r"[0-9a-f]{64}")
PACKAGE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")
BASE_URL = "https://raw.githubusercontent.com/SuperMonster003/AutoJs6-Official-Plugins-Index/main/"


def load_artwork(root: Path = ROOT) -> dict[str, dict]:
    manifest = json.loads((root / "catalog-icons.json").read_text(encoding="utf-8"))
    if manifest.get("schemaVersion") != 1 or not isinstance(manifest.get("icons"), dict):
        raise RuntimeError("Unsupported catalog icon manifest")
    for package, icon in manifest["icons"].items():
        if not PACKAGE.fullmatch(package):
            raise RuntimeError(f"Invalid catalog icon package: {package}")
        backgrounds = icon.get("backgrounds")
        if backgrounds is not None and (
            not isinstance(backgrounds, dict) or set(backgrounds) != {"day", "night"}
            or any(not isinstance(c, str) or not re.fullmatch(r"transparent|#[0-9a-fA-F]{6}", c) for c in backgrounds.values())
        ):
            raise RuntimeError(f"Invalid catalog background colors: {package}")
        for mode in ("day", "night"):
            digest = icon.get(mode, {}).get("sha256", "")
            if not SHA256.fullmatch(digest):
                raise RuntimeError(f"Invalid {mode} catalog icon digest: {package}")
            expected = f"icons/{package}/{digest}.png"
            if icon[mode].get("path") != expected:
                raise RuntimeError(f"Catalog icon path must contain its content digest: {package}")
            path = (root / expected).resolve()
            if not path.is_relative_to(root.resolve()):
                raise RuntimeError("Catalog icon escaped its repository")
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != digest:
                raise RuntimeError(f"Catalog icon digest mismatch: {expected}")
            if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
                raise RuntimeError(f"Catalog icon is not a PNG: {expected}")
            if struct.unpack(">IIBB", data[16:26]) != (432, 432, 8, 6):
                raise RuntimeError(f"Catalog icon must be 432 px RGBA: {expected}")
    return manifest["icons"]


def apply_catalog_presentation(items: list[dict], root: Path = ROOT) -> None:
    artwork = load_artwork(root)
    retired = json.loads((root / "retired-packages.json").read_text(encoding="utf-8"))
    if retired.get("schemaVersion") != 1 or not isinstance(retired.get("replacements"), dict):
        raise RuntimeError("Unsupported retired package manifest")
    for package, replacement in retired["replacements"].items():
        if not PACKAGE.fullmatch(package) or not PACKAGE.fullmatch(replacement) or package == replacement:
            raise RuntimeError(f"Invalid retired package replacement: {package}")
        if package in artwork:
            raise RuntimeError(f"Retired package {package} cannot register catalog artwork")
        for directory in ("release-manifests", "icons"):
            retired_path = root / directory / package
            # Git does not publish empty local directories.
            if retired_path.is_file() or any(path.is_file() for path in retired_path.rglob("*")):
                raise RuntimeError(f"Retired package files must be removed: {directory}/{package}")
    for item in items:
        package = item["packageName"]
        if package in retired["replacements"]:
            raise RuntimeError(f"Retired package {package} cannot re-enter the official index; publish {retired['replacements'][package]}")
        # All official entries keep the catalog's chosen presentation when their
        # installation state changes, not only the normalized standalone group.
        item["forceIgnoreLocalIcon"] = True
        icon = artwork.get(package)
        repository = item.get("repository", {})
        name = repository.get("name", "") if isinstance(repository, dict) else str(repository)
        if name.startswith("AutoJs6-Plugin-Three-") or (icon or {}).get("repository", "").startswith("AutoJs6-Plugin-Three-"):
            background = {"day": "#fafafa", "night": "#212121"}
        else:
            background = (icon or {}).get("backgrounds", {"day": "transparent", "night": "transparent"})
        item["iconBackgroundColor"] = background["day"]
        item["nightIconBackgroundColor"] = background["night"]
        if icon is None:
            continue
        item["iconUrl"] = BASE_URL + icon["day"]["path"]
        item["nightIconUrl"] = BASE_URL + icon["night"]["path"]
        # The same art must be used before and after installation, including when
        # the installed APK predates this presentation revision.
