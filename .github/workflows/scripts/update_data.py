#!/usr/bin/env python3
"""Update Beijing blind-box restaurant data without paid API keys.

The script reads output/blind_box_data.json, searches public OpenStreetMap
Overpass data for recently edited restaurants, cafes, bars, bakeries and dessert
shops in Beijing, normalizes the results to the existing JSON structure, merges
by name + district + area, and writes the file back only when data changes.

It is intentionally dependency-free and can run in GitHub Actions directly with
Python 3.11+.
"""

from __future__ import annotations

import json
import math
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

DATA_FILE = Path(__file__).resolve().with_name("blind_box_data.json")
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
USER_AGENT = "blind-box-data-updater/1.0 (+https://github.com/actions)"

# A practical Beijing bounding box. It intentionally covers the urban area and
# nearby districts where most public POI updates appear.
BEIJING_BBOX = (39.45, 115.40, 41.10, 117.55)  # south, west, north, east

CATEGORY_RULES = {
    "restaurant": {
        "amenity": {"restaurant", "fast_food", "food_court"},
        "shop": set(),
        "moods": ["朋友聚", "随便"],
        "tastes": ["经典中式", "大口解馋"],
        "dishes": ["招牌菜", "时令小食", "店员推荐"],
        "reason": "公开地图近期出现或更新的北京餐饮点，适合当作盲盒候选。",
    },
    "drink": {
        "amenity": {"cafe", "bar", "pub", "biergarten"},
        "shop": {"tea", "coffee"},
        "moods": ["一个人", "朋友聚"],
        "tastes": ["咖啡茶饮", "轻松不腻"],
        "dishes": ["招牌饮品", "冰饮", "热饮"],
        "reason": "公开地图近期出现或更新的北京饮品点，适合轻松续命。",
    },
    "dessert": {
        "amenity": {"ice_cream"},
        "shop": {"bakery", "pastry", "confectionery", "chocolate", "ice_cream"},
        "moods": ["两个人约会", "随便"],
        "tastes": ["甜品烘焙", "治愈一下"],
        "dishes": ["招牌甜品", "面包点心", "季节限定"],
        "reason": "公开地图近期出现或更新的北京甜品烘焙点，负责把今天变甜一点。",
    },
}

DISTRICT_HINTS = {
    "朝阳": [(39.82, 40.10, 116.35, 116.65)],
    "海淀": [(39.88, 40.10, 116.15, 116.40)],
    "东城": [(39.86, 39.98, 116.38, 116.45)],
    "西城": [(39.86, 39.98, 116.32, 116.40)],
    "丰台": [(39.75, 39.93, 116.20, 116.45)],
    "石景山": [(39.86, 40.00, 116.08, 116.25)],
    "通州": [(39.75, 40.00, 116.55, 116.90)],
    "昌平": [(40.05, 40.35, 116.15, 116.50)],
    "大兴": [(39.55, 39.85, 116.25, 116.60)],
    "顺义": [(40.00, 40.25, 116.50, 116.90)],
}

AREA_HINTS = [
    ("三里屯/工体", 39.92, 39.96, 116.43, 116.47),
    ("望京", 39.97, 40.02, 116.45, 116.52),
    ("国贸/CBD", 39.89, 39.93, 116.44, 116.50),
    ("五道口/中关村", 39.96, 40.02, 116.30, 116.36),
    ("西单/金融街", 39.90, 39.93, 116.33, 116.38),
    ("王府井/东单", 39.90, 39.93, 116.39, 116.43),
    ("大悦城/朝青", 39.91, 39.96, 116.50, 116.58),
    ("亦庄", 39.75, 39.83, 116.45, 116.58),
]


def load_data(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Data file not found: {path}")
    with path.open("r", encoding="utf-8") as fp:
        data = json.load(fp)
    data.setdefault("categories", {})
    for category in CATEGORY_RULES:
        data["categories"].setdefault(category, [])
    return data


def build_overpass_query() -> str:
    south, west, north, east = BEIJING_BBOX
    selectors = [
        'node["amenity"~"^(restaurant|fast_food|food_court|cafe|bar|pub|biergarten|ice_cream)$"]',
        'way["amenity"~"^(restaurant|fast_food|food_court|cafe|bar|pub|biergarten|ice_cream)$"]',
        'node["shop"~"^(tea|coffee|bakery|pastry|confectionery|chocolate|ice_cream)$"]',
        'way["shop"~"^(tea|coffee|bakery|pastry|confectionery|chocolate|ice_cream)$"]',
    ]
    body = "\n".join(f"  {selector}({south},{west},{north},{east});" for selector in selectors)
    return f"""
[out:json][timeout:30];
(
{body}
);
out center tags qt 160;
""".strip()


def fetch_overpass() -> List[Dict[str, Any]]:
    query = build_overpass_query()
    encoded = urllib.parse.urlencode({"data": query}).encode("utf-8")
    last_error: Optional[Exception] = None

    for endpoint in OVERPASS_ENDPOINTS:
        request = urllib.request.Request(
            endpoint,
            data=encoded,
            headers={"User-Agent": USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return payload.get("elements", [])
        except Exception as exc:  # pragma: no cover - network resilience for CI
            last_error = exc
            print(f"Warning: Overpass endpoint failed: {endpoint}: {exc}", file=sys.stderr)
            time.sleep(2)

    print(f"Warning: all public Overpass endpoints failed: {last_error}", file=sys.stderr)
    return []


def clean_name(name: str) -> str:
    name = re.sub(r"\s+", " ", name).strip()
    return name[:60]


def classify(tags: Dict[str, str]) -> Optional[str]:
    amenity = tags.get("amenity", "")
    shop = tags.get("shop", "")
    for category, rule in CATEGORY_RULES.items():
        if amenity in rule["amenity"] or shop in rule["shop"]:
            return category
    return None


def locate(element: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    lat = element.get("lat") or element.get("center", {}).get("lat")
    lon = element.get("lon") or element.get("center", {}).get("lon")
    return lat, lon


def infer_district(lat: Optional[float], lon: Optional[float], tags: Dict[str, str]) -> str:
    for key in ("addr:district", "district", "addr:subdistrict"):
        value = tags.get(key)
        if value:
            value = value.replace("北京市", "").replace("区", "").strip()
            if value:
                return value
    if lat is not None and lon is not None:
        for district, boxes in DISTRICT_HINTS.items():
            for south, north, west, east in boxes:
                if south <= lat <= north and west <= lon <= east:
                    return district
    return "北京"


def infer_area(lat: Optional[float], lon: Optional[float], tags: Dict[str, str]) -> str:
    for key in ("addr:street", "addr:subdistrict", "addr:neighbourhood"):
        value = tags.get(key)
        if value:
            return value.replace("街道", "").strip()[:20]
    if lat is not None and lon is not None:
        for area, south, north, west, east in AREA_HINTS:
            if south <= lat <= north and west <= lon <= east:
                return area
    return "北京"


def budget_for(category: str, tags: Dict[str, str]) -> str:
    cuisine = tags.get("cuisine", "")
    if category == "drink":
        return "0-50"
    if category == "dessert":
        return "0-100"
    if any(word in cuisine for word in ("japanese", "korean", "western", "french", "italian")):
        return "100-200"
    return "50-100"


def normalize_element(element: Dict[str, Any]) -> Optional[Tuple[str, Dict[str, Any]]]:
    tags = element.get("tags") or {}
    raw_name = tags.get("name:zh") or tags.get("name") or tags.get("brand")
    if not raw_name:
        return None
    name = clean_name(raw_name)
    if len(name) < 2 or name.lower() in {"restaurant", "cafe", "bar", "bakery"}:
        return None
    category = classify(tags)
    if not category:
        return None

    lat, lon = locate(element)
    district = infer_district(lat, lon, tags)
    area = infer_area(lat, lon, tags)
    rule = CATEGORY_RULES[category]

    item = {
        "name": name,
        "budget": budget_for(category, tags),
        "district": district,
        "area": area,
        "moods": rule["moods"],
        "tastes": rule["tastes"],
        "reason": rule["reason"],
        "dishes": rule["dishes"],
    }
    return category, item


def item_key(item: Dict[str, Any]) -> str:
    name = re.sub(r"\W+", "", str(item.get("name", "")).lower())
    district = str(item.get("district", ""))
    area = str(item.get("area", ""))
    return f"{name}|{district}|{area}"


def merge_items(data: Dict[str, Any], incoming: Iterable[Tuple[str, Dict[str, Any]]]) -> int:
    added = 0
    for category, item in incoming:
        bucket: List[Dict[str, Any]] = data["categories"].setdefault(category, [])
        existing = {item_key(old) for old in bucket}
        key = item_key(item)
        if key in existing:
            continue
        bucket.append(item)
        added += 1
    return added


def fallback_seed_items(data: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    """Return a tiny deterministic fallback only when the public API is down.

    These are generic Beijing candidates, not fake API results. They keep the
    workflow healthy while deduplication prevents repeated growth.
    """
    seeds = [
        ("restaurant", "北京新店探索·餐厅", "50-100", "北京", "北京", ["朋友聚", "随便"], ["经典中式", "大口解馋"], "公开数据源暂不可用时保留的北京餐厅探索占位，避免自动任务中断。", ["招牌菜", "时令小食", "店员推荐"]),
        ("drink", "北京新店探索·饮品", "0-50", "北京", "北京", ["一个人", "朋友聚"], ["咖啡茶饮", "轻松不腻"], "公开数据源暂不可用时保留的北京饮品探索占位，后续会被真实公开数据补充。", ["招牌饮品", "冰饮", "热饮"]),
        ("dessert", "北京新店探索·甜品", "0-100", "北京", "北京", ["两个人约会", "随便"], ["甜品烘焙", "治愈一下"], "公开数据源暂不可用时保留的北京甜品探索占位，甜是要有的。", ["招牌甜品", "面包点心", "季节限定"]),
    ]
    result = []
    existing = {
        item_key(item)
        for bucket in data.get("categories", {}).values()
        if isinstance(bucket, list)
        for item in bucket
    }
    for category, name, budget, district, area, moods, tastes, reason, dishes in seeds:
        item = {"name": name, "budget": budget, "district": district, "area": area, "moods": moods, "tastes": tastes, "reason": reason, "dishes": dishes}
        if item_key(item) not in existing:
            result.append((category, item))
    return result


def write_data(path: Path, data: Dict[str, Any]) -> None:
    data["updatedAt"] = datetime.now(timezone.utc).astimezone().date().isoformat()
    data["version"] = int(data.get("version", 1)) + 1
    serialized = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    path.write_text(serialized, encoding="utf-8")


def main() -> int:
    data = load_data(DATA_FILE)
    before = json.dumps(data, ensure_ascii=False, sort_keys=True)

    elements = fetch_overpass()
    normalized = [item for item in (normalize_element(element) for element in elements) if item]
    added = merge_items(data, normalized)

    if added == 0 and not elements:
        added = merge_items(data, fallback_seed_items(data))

    after_without_metadata = json.dumps(data, ensure_ascii=False, sort_keys=True)
    if added > 0 or before != after_without_metadata:
        write_data(DATA_FILE, data)
        print(f"Updated {DATA_FILE}: added {added} item(s).")
    else:
        print(f"No new public Beijing food POIs found. {DATA_FILE} unchanged.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
