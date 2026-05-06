#!/usr/bin/env python3
"""
NYC Awning Lead Finder
----------------------
Pulls DOB awning permits for a given street, geocodes addresses, then enriches
with OSM POIs and (optionally) Google Places to identify the current occupant
storefront. The DOB awning permit is the primary lead signal; everything else
is just to figure out who is at that address today.

Filtering philosophy:
  - We only care about street-level retail / F&B / consumer-services storefronts
    that plausibly have an awning.
  - Google place types are noisy (one place can have ['point_of_interest',
    'establishment', 'lawyer'] etc.), so we use an allowlist + denylist with a
    confidence label rather than a hard yes/no.
"""

import argparse
import os
import re
import sys
import time
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

NYC_DOB_PERMITS_ENDPOINT = "https://data.cityofnewyork.us/resource/ipu4-2q9a.json"
NOMINATIM_ENDPOINT = "https://nominatim.openstreetmap.org/search"
GOOGLE_GEOCODE_ENDPOINT = "https://maps.googleapis.com/maps/api/geocode/json"
GOOGLE_PLACES_NEARBY_ENDPOINT = (
    "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
)
GOOGLE_PLACE_DETAILS_ENDPOINT = (
    "https://maps.googleapis.com/maps/api/place/details/json"
)
OVERPASS_ENDPOINT = "https://overpass-api.de/api/interpreter"

USER_AGENT = "awning-lead-finder/0.2 (https://github.com/cekbrooks/awning-lead-finder)"

# Google Places types we treat as a likely street-level storefront with an awning.
STOREFRONT_TYPES = {
    # Food & beverage
    "restaurant", "cafe", "bakery", "bar", "meal_takeaway", "meal_delivery",
    "ice_cream_shop",
    # Retail
    "clothing_store", "shoe_store", "jewelry_store", "book_store", "florist",
    "furniture_store", "home_goods_store", "hardware_store", "bicycle_store",
    "pet_store", "liquor_store", "convenience_store", "grocery_or_supermarket",
    "supermarket", "store",
    # Personal services with storefronts
    "hair_care", "beauty_salon", "spa", "nail_salon", "barber_shop",
    "laundry", "dry_cleaning", "tailor",
    # Health-adjacent retail (per user: opticians/optometrists IN)
    "optician", "optometrist", "pharmacy", "drugstore",
}

# Possibly a storefront — keep but flag as "Maybe". Per user: doctors stay in.
MAYBE_STOREFRONT_TYPES = {
    "doctor", "dentist", "physiotherapist", "veterinary_care",
    "bank", "atm",
    "travel_agency", "real_estate_agency", "insurance_agency",
    "art_gallery", "movie_theater", "gym",
}

# If the place is ONLY these types (no storefront/maybe types), drop it.
EXCLUDE_TYPES = {
    "lawyer", "accounting", "school", "primary_school", "secondary_school",
    "university", "church", "place_of_worship", "mosque", "synagogue",
    "hindu_temple", "lodging", "premise", "subpremise", "route", "locality",
    "political", "neighborhood", "park", "parking", "transit_station",
    "bus_station", "subway_station", "train_station", "embassy", "city_hall",
    "courthouse", "post_office", "fire_station", "police", "cemetery",
    "funeral_home", "storage", "moving_company",
}


def classify_place(types: List[str]) -> Optional[str]:
    """Return a confidence label or None to drop the place entirely.

    Priority:
      1. If any type is in STOREFRONT_TYPES -> 'Likely storefront'
      2. Else if any type is in MAYBE_STOREFRONT_TYPES -> 'Maybe storefront'
      3. Else if every type is in EXCLUDE_TYPES (or generic) -> drop (None)
      4. Otherwise -> 'Unknown' (keep, low confidence)
    """
    if not types:
        return "Unknown"
    tset = set(types)
    if tset & STOREFRONT_TYPES:
        return "Likely storefront"
    if tset & MAYBE_STOREFRONT_TYPES:
        return "Maybe storefront"
    # Strip out generic noise tags before deciding to exclude
    generic = {"point_of_interest", "establishment", "food", "health", "finance"}
    meaningful = tset - generic
    if meaningful and meaningful.issubset(EXCLUDE_TYPES):
        return None
    return "Unknown"


# ---------------------------------------------------------------------------
# DOB permits
# ---------------------------------------------------------------------------

def fetch_awning_permits(street: str, borough: str, since_year: int = 2015) -> pd.DataFrame:
    """Pull DOB job filings tagged as awning work for the given street."""
    where = (
        f"upper(street_name) = upper('{street}') "
        f"AND upper(borough) = upper('{borough}') "
        f"AND (upper(job_description) like '%AWNING%' "
        f"OR upper(job_description) like '%CANOPY%') "
        f"AND filing_date >= '{since_year}-01-01T00:00:00.000'"
    )
    params = {"$where": where, "$limit": 5000, "$order": "filing_date DESC"}
    r = requests.get(NYC_DOB_PERMITS_ENDPOINT, params=params, timeout=30)
    r.raise_for_status()
    rows = r.json()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    keep = [
        "job__", "house__", "street_name", "borough", "job_description",
        "filing_date", "owner_s_business_name", "owner_s_first_name",
        "owner_s_last_name", "owner_s_phone__",
    ]
    keep = [c for c in keep if c in df.columns]
    df = df[keep].copy()
    df = df.rename(columns={
        "job__": "job_number",
        "house__": "house_num",
        "owner_s_business_name": "permit_owner_business",
        "owner_s_first_name": "permit_owner_first",
        "owner_s_last_name": "permit_owner_last",
        "owner_s_phone__": "permit_owner_phone",
    })
    return df


# ---------------------------------------------------------------------------
# Geocoding
# ---------------------------------------------------------------------------

def geocode_addresses(
    addresses: List[Tuple[str, str, str]],
) -> Dict[str, Tuple[float, float]]:
    """Geocode (house_num, street, borough) tuples -> {address_key: (lat, lng)}.

    Uses Google Geocoding API if GOOGLE_PLACES_API_KEY is set (Streamlit IPs are
    blocked by Nominatim). Falls back to Nominatim otherwise.
    """
    api_key = os.environ.get("GOOGLE_PLACES_API_KEY")
    out: Dict[str, Tuple[float, float]] = {}

    for house_num, street, borough in addresses:
        key = f"{house_num} {street}, {borough}, NY"
        if api_key:
            try:
                r = requests.get(
                    GOOGLE_GEOCODE_ENDPOINT,
                    params={"address": key, "key": api_key},
                    timeout=20,
                )
                data = r.json()
                if data.get("status") == "OK" and data.get("results"):
                    loc = data["results"][0]["geometry"]["location"]
                    out[key] = (loc["lat"], loc["lng"])
                    continue
            except Exception:
                pass
        # Fallback: Nominatim
        try:
            r = requests.get(
                NOMINATIM_ENDPOINT,
                params={"q": key, "format": "json", "limit": 1},
                headers={"User-Agent": USER_AGENT},
                timeout=20,
            )
            arr = r.json()
            if arr:
                out[key] = (float(arr[0]["lat"]), float(arr[0]["lon"]))
            time.sleep(1.0)  # Nominatim politeness
        except Exception:
            pass
    return out


# ---------------------------------------------------------------------------
# OSM (Overpass)
# ---------------------------------------------------------------------------

def fetch_osm_pois(geocoded: Dict[str, Tuple[float, float]]) -> Dict[str, List[dict]]:
    """For each geocoded address, find nearby OSM POIs (shops/amenities)."""
    out: Dict[str, List[dict]] = {}
    for addr, (lat, lng) in geocoded.items():
        q = f"""
        [out:json][timeout:25];
        (
          node(around:30,{lat},{lng})[shop];
          node(around:30,{lat},{lng})[amenity~"^(restaurant|cafe|bar|pub|fast_food|bakery|pharmacy|optician|hairdresser|beauty|dry_cleaning)$"];
          way(around:30,{lat},{lng})[shop];
        );
        out center tags;
        """
        try:
            r = requests.post(OVERPASS_ENDPOINT, data={"data": q}, timeout=40)
            r.raise_for_status()
            elements = r.json().get("elements", [])
            out[addr] = elements
        except Exception:
            out[addr] = []
        time.sleep(0.5)
    return out


# ---------------------------------------------------------------------------
# Google Places
# ---------------------------------------------------------------------------

def fetch_google_places(
    geocoded: Dict[str, Tuple[float, float]],
) -> Dict[str, List[dict]]:
    """For each geocoded address, find nearby Google Places, filtered to likely
    storefronts via classify_place()."""
    api_key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not api_key:
        return {}

    out: Dict[str, List[dict]] = {}
    for addr, (lat, lng) in geocoded.items():
        try:
            r = requests.get(
                GOOGLE_PLACES_NEARBY_ENDPOINT,
                params={
                    "location": f"{lat},{lng}",
                    "radius": 30,
                    "key": api_key,
                },
                timeout=20,
            )
            results = r.json().get("results", []) or []
        except Exception:
            results = []

        kept: List[dict] = []
        for p in results:
            label = classify_place(p.get("types", []))
            if label is None:
                continue
            # Get details for phone + website
            place_id = p.get("place_id")
            phone, website = "", ""
            if place_id:
                try:
                    d = requests.get(
                        GOOGLE_PLACE_DETAILS_ENDPOINT,
                        params={
                            "place_id": place_id,
                            "fields": "formatted_phone_number,website,name,formatted_address",
                            "key": api_key,
                        },
                        timeout=20,
                    ).json()
                    res = d.get("result", {}) or {}
                    phone = res.get("formatted_phone_number", "") or ""
                    website = res.get("website", "") or ""
                except Exception:
                    pass
            kept.append({
                "name": p.get("name", ""),
                "types": p.get("types", []),
                "vicinity": p.get("vicinity", ""),
                "phone": phone,
                "website": website,
                "confidence": label,
            })
            time.sleep(0.05)

        # Sort: Likely > Maybe > Unknown
        order = {"Likely storefront": 0, "Maybe storefront": 1, "Unknown": 2}
        kept.sort(key=lambda x: order.get(x["confidence"], 9))
        out[addr] = kept
        time.sleep(0.1)
    return out


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def build_final_table(
    permits: pd.DataFrame,
    geocoded: Dict[str, Tuple[float, float]],
    osm: Dict[str, List[dict]],
    google: Dict[str, List[dict]],
    cafes: bool = False,
) -> pd.DataFrame:
    rows: List[dict] = []
    if permits.empty:
        return pd.DataFrame()

    grouped = permits.groupby(["house_num", "street_name", "borough"], dropna=False)
    for (house_num, street, borough), gdf in grouped:
        addr_key = f"{house_num} {street}, {borough}, NY"
        latlng = geocoded.get(addr_key)
        permit_owner = ""
        if "permit_owner_business" in gdf.columns:
            vals = [v for v in gdf["permit_owner_business"].dropna().tolist() if v]
            if vals:
                permit_owner = vals[0]

        google_hits = google.get(addr_key, []) or []
        osm_hits = osm.get(addr_key, []) or []

        if google_hits:
            for g in google_hits:
                rows.append({
                    "Address #": house_num,
                    "Street": street,
                    "Borough": borough,
                    "Lat": latlng[0] if latlng else None,
                    "Lng": latlng[1] if latlng else None,
                    "Permit Owner": permit_owner,
                    "Business": g.get("name", ""),
                    "Source": "Google",
                    "Confidence": g.get("confidence", ""),
                    "Phone": g.get("phone", ""),
                    "Website": g.get("website", ""),
                    "Types": ", ".join(g.get("types", []) or []),
                })
        elif osm_hits:
            for o in osm_hits:
                tags = o.get("tags", {}) or {}
                rows.append({
                    "Address #": house_num,
                    "Street": street,
                    "Borough": borough,
                    "Lat": latlng[0] if latlng else None,
                    "Lng": latlng[1] if latlng else None,
                    "Permit Owner": permit_owner,
                    "Business": tags.get("name", ""),
                    "Source": "OSM",
                    "Confidence": "OSM",
                    "Phone": tags.get("phone", "") or tags.get("contact:phone", ""),
                    "Website": tags.get("website", "") or tags.get("contact:website", ""),
                    "Types": tags.get("shop", "") or tags.get("amenity", ""),
                })
        else:
            rows.append({
                "Address #": house_num,
                "Street": street,
                "Borough": borough,
                "Lat": latlng[0] if latlng else None,
                "Lng": latlng[1] if latlng else None,
                "Permit Owner": permit_owner,
                "Business": None,
                "Source": "",
                "Confidence": "",
                "Phone": "",
                "Website": "",
                "Types": "",
            })

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--street", required=True)
    p.add_argument("--borough", default="MANHATTAN")
    p.add_argument("--since-year", type=int, default=2015)
    p.add_argument("--skip-osm", action="store_true")
    p.add_argument("--cafes", action="store_true",
                   help="(legacy flag, kept for compatibility)")
    p.add_argument("--out-prefix", default="awning_leads")
    args = p.parse_args()

    street = args.street.strip()
    borough = args.borough.strip().upper()
    out_prefix = args.out_prefix
    cafes = args.cafes

    # Step 1: Permits
    permits = fetch_awning_permits(street, borough, since_year=args.since_year)
    if permits.empty:
        print(f"No awning permits found for {street}, {borough}")
        return 0

    # Step 2: Geocode unique addresses
    uniq = (
        permits[["house_num", "street_name", "borough"]]
        .drop_duplicates()
        .itertuples(index=False, name=None)
    )
    geocoded = geocode_addresses(list(uniq))

    # Step 3: OSM
    osm = {} if args.skip_osm else fetch_osm_pois(geocoded)

    # Step 4: Google (optional)
    google = fetch_google_places(geocoded)

    # Assemble
    final = build_final_table(permits, geocoded, osm, google, cafes)

    # Stats
    total_addrs = permits["house_num"].nunique()
    found_addrs = final[final["Business"].notna()]["Address #"].nunique()
    print(f"\n{'='*60}")
    print(f"SUMMARY")
    print(f"  Street:                       {street}, {borough}")
    print(f"  Awning-permit addresses:      {total_addrs}")
    print(f"  Addresses w/ business found:  {found_addrs} ({found_addrs/total_addrs*100:.0f}%)")
    print(f"  Total business leads:         {final['Business'].notna().sum()}")
    print(f"{'='*60}")

    final.to_csv(f"{out_prefix}.csv", index=False)
    final.to_excel(f"{out_prefix}.xlsx", index=False)
    print(f"\nWrote: {out_prefix}.csv")
    print(f"Wrote: {out_prefix}.xlsx")
    return 0


if __name__ == "__main__":
    sys.exit(main())
