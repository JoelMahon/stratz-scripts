#!/usr/bin/env python3
"""Refresh the checked-in Dota hero icon bundle from trusted upstream sources."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import urllib.request
from pathlib import Path
from typing import Any, Mapping


HERO_METADATA_URL = "https://raw.githubusercontent.com/odota/dotaconstants/master/build/heroes.json"
VALVE_CDN_ORIGIN = "https://cdn.cloudflare.steamstatic.com"
ICON_PATH_PATTERN = re.compile(r"/apps/dota2/images/dota_react/heroes/icons/[a-z0-9_]+\.png\??")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_DOWNLOAD_BYTES = 1_000_000
REQUEST_TIMEOUT_SECONDS = 30
OUTPUT_DIRECTORY = Path(__file__).resolve().parents[1] / "assets" / "hero-icons"


def download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "stratz-scripts-hero-icon-sync/1"})
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        declared_size = int(response.headers.get("Content-Length", "0"))
        if declared_size > MAX_DOWNLOAD_BYTES:
            raise ValueError(f"Refusing oversized download from {url}")
        content = response.read(MAX_DOWNLOAD_BYTES + 1)
    if len(content) > MAX_DOWNLOAD_BYTES:
        raise ValueError(f"Download exceeded size limit: {url}")
    return content


def validated_heroes(raw: bytes) -> list[Mapping[str, Any]]:
    payload = json.loads(raw)
    if not isinstance(payload, Mapping):
        raise ValueError("Hero metadata must be a JSON object")
    heroes: list[Mapping[str, Any]] = []
    for key, value in payload.items():
        if not isinstance(value, Mapping) or int(key) != int(value.get("id", -1)):
            raise ValueError(f"Invalid hero record {key!r}")
        icon_path = str(value.get("icon", ""))
        if not ICON_PATH_PATTERN.fullmatch(icon_path):
            raise ValueError(f"Unexpected icon path for hero {key}: {icon_path!r}")
        heroes.append(value)
    return sorted(heroes, key=lambda hero: int(hero["id"]))


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temporary.write(content)
        temporary_path = Path(temporary.name)
    temporary_path.replace(path)


def main() -> int:
    heroes = validated_heroes(download(HERO_METADATA_URL))
    manifest: dict[str, Any] = {
        "metadata_source": HERO_METADATA_URL,
        "artwork_origin": VALVE_CDN_ORIGIN,
        "heroes": {},
    }
    for hero in heroes:
        hero_id = str(int(hero["id"]))
        icon_path = str(hero["icon"]).removesuffix("?")
        content = download(f"{VALVE_CDN_ORIGIN}{icon_path}")
        if not content.startswith(PNG_SIGNATURE):
            raise ValueError(f"Hero {hero_id} did not return a PNG")
        atomic_write(OUTPUT_DIRECTORY / f"{hero_id}.png", content)
        manifest["heroes"][hero_id] = {
            "name": str(hero.get("localized_name") or hero.get("name") or hero_id),
            "source_path": icon_path,
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    atomic_write(
        OUTPUT_DIRECTORY / "manifest.json",
        (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
    )
    print(f"Updated {len(heroes)} hero icons in {OUTPUT_DIRECTORY}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
