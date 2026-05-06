#!/usr/bin/env python3
"""
NYC Awning Lead Finder
======================

Given any NYC street name, this script produces a CSV of addresses with
filed awning permits, the building owners, and (where findable) the
current businesses at those addresses.

USAGE:
    python find_awning_leads.py "BLEECKER STREET" --borough MANHATTAN
    python find_awning_leads.py "MULBERRY STREET" --borough MANHATTAN
    python find_awning_leads.py "BEDFORD AVE" --borough BROOKLYN
    python find_awning_leads.py "STEINWAY STREET" --borough QUEENS

OPTIONS:
    --borough  MANHATTAN | BROOKLYN | QUEENS | BRONX | STATEN ISLAND
    --output   Path for the output .xlsx and .csv (default: ./<street>_leads)
    --skip-osm Skip the OpenStreetMap Overpass enrichment step (faster)

DATA SOURCES (all free, no API keys required):
    1. NYC DOB Job Applications (filtered for "AWNING" in description)
    2. NYC DCWP Sidewalk Cafe Licenses
    3. OpenStreetMap Nominatim (geocoding)
    4. OpenStreetMap Overpass API (business POI lookup)

OPTIONAL:
    Set GOOGLE_PLACES_API_KEY environment variable to enable Google Places
    enrichment (catches well-known chain businesses OSM misses). Without
    a key, the script will skip Google and rely on OSM only — still
    typically gets 50-60% coverage.

REQUIREMENTS:
    pip install requests pandas openpyxl
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests

# -------------------- CONFIG --------------------
NYC_OPEN_DATA_BASE = "https://data.cityofnewyork.us/resource"
DOB_JOBS_DATASET = "ic3t-wcy2"          # Job Application Filings (legacy BIS)
SIDEWALK_CAFE_DATASET = "qcdj-rwhu"     # Sidewalk Cafe Licenses
LEGAL_BIZ_DATASET = "w7w3-xahh"         # Legally Operating Businesses

NOMINATIM_BASE = "https://nominatim.openstreetmap.org/search"
OVERPASS_ENDPOINTS = [
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass-api.de/api/interpreter",
]

USER_AGENT = "AwningLeadResearch/1.0"
GOOGLE_KEY = os.environ.get("GOOGLE_PLACES_API_KEY")


# -------------------- STOREFRONT FILTER --------------------
# Likely street-level retail / F&B / consumer-services with awnings.
STOREFRONT_TYPES = {
    "restaurant", "cafe", "bakery", "bar", "meal_takeaway", "meal_delivery",
    "ice_cream_shop",
    "clothing_store", "shoe_store", "jewelry_store", "book_store", "florist",
    "furniture_store", "home_goods_store", "hardware_store", "bicycle_store",
    "pet_store", "liquor_store", "convenience_store", "grocery_or_supermarket",
    "supermarket", "store",
    "hair_care", "beauty_salon", "spa", "nail_salon", "barber_shop",
    "laundry", "dry_cleaning", "tailor",
    "optician", "optometrist", "pharmacy", "drugstore",
}
# Possibly a storefront — keep but flag as "Maybe". Doctors/dentists stay in.
MAYBE_STOREFRONT_TYPES = {
    "doctor", "dentist", "physiotherapist", "veterinary_care",
    "bank", "atm",
    "travel_agency", "real_estate_agency", "insurance_agency",
    "art_gallery", "movie_theater", "gym",
}
# If the place is ONLY these types, drop it.
EXCLUDE_TYPES = {
    "lawyer", "accounting", "school", "primary_school", "secondary_school",
    "university", "church", "place_of_worship", "mosque", "synagogue",
    "hindu_temple", "lodging", "premise", "subpremise", "route", "locality",
    "political", "neighborhood", "park", "parking", "transit_station",
    "bus_station", "subway_station", "train_station", "embassy", "city_hall",
    "courthouse", "post_office", "fire_station", "police", "cemetery",
    "funeral_home", "storage", "moving_company",
}


def classify_place(types):
    """Return 'Likely storefront' / 'Maybe storefront' / 'Unknown', or None to drop."""
    if not types:
        return "Unknown"
    tset = set(types)
    if tset & STOREFRONT_TYPES:
        return "Likely storefront"
    if tset & MAYBE_STOREFRONT_TYPES:
        return "Maybe storefront"
    generic = {"point_of_interest", "establishment", "food", "health", "finance"}
    meaningful = tset - generic
    if meaningful and meaningful.issubset(EXCLUDE_TYPES):
        return None
    if meaningful & EXCLUDE_TYPES:
        return None
    return "Unknown"


# -------------------- STEP 1: PERMITS --------------------
def fetch_awning_permits(street_name: str, borough: str) -> pd.DataFrame:
    """Pull DOB job applications mentioning AWNING for a given street."""
    print(f"\n[1/4] Pulling DOB awning permits for {street_name}, {borough}...")
    url = f"{NYC_OPEN_DATA_BASE}/{DOB_JOBS_DATASET}.json"
    params = {
        "$where": f"borough='{borough}' AND street_name='{street_name.upper()}'",
        "$select": "job__,house__,street_name,job_type,other,other_description,"
                   "owner_s_business_name,owner_sphone__,job_status_descrp,latest_action_date",
        "$limit": 50000,
    }
    r = requests.get(url, params=params, timeout=180)
    r.raise_for_status()
    df = pd.DataFrame(r.json())
    if df.empty:
        return df

    # Filter for awning mentions
    if "other_description" in df.columns:
        mask = df["other_description"].fillna("").str.upper().str.contains("AWNING")
        df = df[mask].copy()

    if df.empty:
        print(f"   No awning permits found.")
        return df

    df["latest_action_date"] = pd.to_datetime(df["latest_action_date"], errors="coerce")
    # Dedupe to most recent permit per address
    df = df.sort_values("latest_action_date", ascending=False)
    df = df.drop_duplicates(subset="house__", keep="first")
    df["house_num"] = pd.to_numeric(df["house__"], errors="coerce").astype("Int64")
    df = df.dropna(subset=["house_num"]).sort_values("house_num")
    print(f"   Found {len(df)} unique addresses with awning permits.")
    return df


def fetch_sidewalk_cafes(street_name: str) -> set:
    """Get addresses with active sidewalk cafe licenses (high awning probability)."""
    url = f"{NYC_OPEN_DATA_BASE}/{SIDEWALK_CAFE_DATASET}.json"
    params = {
        "$where": f"upper(street) like '%{street_name.upper()}%'",
        "$limit": 5000,
    }
    try:
        r = requests.get(url, params=params, timeout=60)
        if r.status_code == 200 and r.json():
            df = pd.DataFrame(r.json())
            return set(int(b) for b in pd.to_numeric(df["building"], errors="coerce").dropna())
    except Exception:
        pass
    return set()


# -------------------- STEP 2: GEOCODE --------------------
def geocode_addresses(addresses, borough: str) -> dict:
    """Geocode (number, street) via Google if GOOGLE_KEY is set, else Nominatim."""
    results = {}

    if GOOGLE_KEY:
        print(f"\n[2/4] Geocoding {len(addresses)} addresses via Google Geocoding API...")
        for num, street in addresses:
            addr = f"{num} {street}, {borough}, NY"
            try:
                r = requests.get(
                    "https://maps.googleapis.com/maps/api/geocode/json",
                    params={"address": addr, "key": GOOGLE_KEY},
                    timeout=15,
                )
                if r.status_code == 200:
                    data = r.json()
                    if data.get("status") == "OK" and data.get("results"):
                        loc = data["results"][0]["geometry"]["location"]
                        results[num] = (loc["lat"], loc["lng"])
                    elif data.get("status") not in ("OK", "ZERO_RESULTS"):
                        print(f"   Google geocode error for {addr}: {data.get('status')} {data.get('error_message','')}")
                else:
                    print(f"   Google geocode HTTP {r.status_code} for {addr}")
            except Exception as e:
                print(f"   Google geocode exception for {addr}: {e}")
        print(f"   Geocoded {len(results)}/{len(addresses)} via Google.")
        return results

    # Fallback: Nominatim
    print(f"\n[2/4] Geocoding {len(addresses)} addresses via OpenStreetMap (no Google key)...")
    headers = {"User-Agent": USER_AGENT}
    for num, street in addresses:
        addr = f"{num} {street} {borough} NY"
        try:
            r = requests.get(NOMINATIM_BASE, headers=headers,
                             params={"q": addr, "format": "json", "limit": 1}, timeout=15)
            if r.status_code == 200 and r.json():
                d = r.json()[0]
                results[num] = (float(d["lat"]), float(d["lon"]))
        except Exception as e:
            print(f"   Nominatim error for {addr}: {e}")
        time.sleep(1.1)
    print(f"   Geocoded {len(results)}/{len(addresses)} via Nominatim.")
    return results


# -------------------- STEP 3: OSM POI LOOKUP --------------------
def overpass_query(lat: float, lon: float, radius: int = 25) -> list:
    """Query Overpass API for businesses near a point. Tries mirrors."""
    query = f"""[out:json][timeout:20];
(nwr["shop"](around:{radius},{lat},{lon});
 nwr["amenity"~"^(restaurant|cafe|bar|pub|fast_food|ice_cream|bank|pharmacy|cinema|theatre|nightclub)$"](around:{radius},{lat},{lon});
 nwr["office"](around:{radius},{lat},{lon});
 nwr["craft"](around:{radius},{lat},{lon});
 nwr["tourism"~"^(hotel|gallery|museum)$"](around:{radius},{lat},{lon});
);
out tags center;"""
    headers = {"User-Agent": USER_AGENT}
    for ep in OVERPASS_ENDPOINTS:
        try:
            r = requests.post(ep, data={"data": query}, headers=headers, timeout=40)
            if r.status_code == 200:
                return r.json().get("elements", [])
        except Exception:
            continue
    return []


def fetch_osm_pois(geocoded: dict) -> dict:
    """For each geocoded address, fetch nearby POIs from OSM."""
    print(f"\n[3/4] Looking up OpenStreetMap POIs near each address...")
    results = {}

    def fetch_one(num, lat, lon):
        elements = overpass_query(lat, lon)
        hits = []
        for el in elements:
            tags = el.get("tags", {})
            name = tags.get("name") or tags.get("brand") or tags.get("operator")
            if not name:
                continue
            hits.append({
                "name": name,
                "kind": (tags.get("shop") or tags.get("amenity") or tags.get("office")
                         or tags.get("craft") or tags.get("tourism")),
                "phone": tags.get("phone") or tags.get("contact:phone"),
                "website": tags.get("website") or tags.get("contact:website"),
                "addr_num": tags.get("addr:housenumber", ""),
            })
        return num, hits

    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = [ex.submit(fetch_one, num, lat, lon) for num, (lat, lon) in geocoded.items()]
        for fu in as_completed(futs):
            num, hits = fu.result()
            results[num] = hits
    matched = sum(1 for v in results.values() if v)
    print(f"   Got POI data for {matched}/{len(geocoded)} addresses.")
    return results


# -------------------- STEP 4: GOOGLE PLACES (optional) --------------------
def fetch_google_places(geocoded: dict) -> dict:
    """If GOOGLE_PLACES_API_KEY is set, query Nearby Search at each lat/long."""
    if not GOOGLE_KEY:
        print(f"\n[4/4] Skipping Google Places (no GOOGLE_PLACES_API_KEY env var set).")
        return {}

    print(f"\n[4/4] Querying Google Places for {len(geocoded)} addresses...")
    results = {}
    for num, (lat, lon) in geocoded.items():
        url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
        params = {"location": f"{lat},{lon}", "radius": 25, "key": GOOGLE_KEY}
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 200:
                hits = []
                for p in r.json().get("results", []):
                    types = p.get("types", [])
                    # Storefront filter: drop pure address/premise + non-storefront places
                    label = classify_place(types)
                    if label is None:
                        continue
                    details = {"name": p.get("name"), "place_id": p.get("place_id"),
                               "types": types, "rating": p.get("rating"),
                               "vicinity": p.get("vicinity"),
                               "confidence": label}
                    # Get phone via Place Details
                    det_url = "https://maps.googleapis.com/maps/api/place/details/json"
                    det_params = {"place_id": p["place_id"],
                                  "fields": "formatted_phone_number,formatted_address,website",
                                  "key": GOOGLE_KEY}
                    d = requests.get(det_url, params=det_params, timeout=15)
                    if d.status_code == 200:
                        details.update(d.json().get("result", {}))
                    hits.append(details)
                # Sort: Likely > Maybe > Unknown
                order = {"Likely storefront": 0, "Maybe storefront": 1, "Unknown": 2}
                hits.sort(key=lambda x: order.get(x.get("confidence"), 9))
                results[num] = hits
        except Exception:
            pass
        time.sleep(0.1)
    print(f"   Google returned business listings for {sum(1 for v in results.values() if v)}/{len(geocoded)}.")
    return results


# -------------------- ASSEMBLE FINAL OUTPUT --------------------
def build_final_table(permits: pd.DataFrame, geocoded: dict, osm: dict,
                      google: dict, sidewalk_cafes: set) -> pd.DataFrame:
    rows = []
    for _, p in permits.iterrows():
        num = int(p["house_num"])
        base = {
            "Address #": num,
            "Street": p["street_name"],
            "Building Owner / LLC": p.get("owner_s_business_name"),
            "Owner Phone (on file)": p.get("owner_sphone__"),
            "Most Recent Awning Permit": p["latest_action_date"].strftime("%Y-%m-%d") if pd.notna(p["latest_action_date"]) else "",
            "Has Sidewalk Cafe": num in sidewalk_cafes,
            "Latitude": geocoded.get(num, (None, None))[0],
            "Longitude": geocoded.get(num, (None, None))[1],
        }

        # Google hits (highest priority)
        for h in google.get(num, []):
            rows.append({**base,
                         "Business": h["name"],
                         "Confidence": h.get("confidence", ""),
                         "Phone": h.get("formatted_phone_number"),
                         "Website": h.get("website"),
                         "Category": ", ".join(h.get("types", [])[:2]),
                         "Rating": h.get("rating"),
                         "Source": "Google Places"})

        # OSM exact-address matches
        exact_hits = []
        nearby_hits = []
        for h in osm.get(num, []):
            try:
                if h["addr_num"] and int(h["addr_num"]) == num:
                    exact_hits.append(h)
                else:
                    nearby_hits.append(h)
            except (ValueError, TypeError):
                nearby_hits.append(h)

        for h in exact_hits:
            rows.append({**base,
                         "Business": h["name"],
                         "Confidence": "OSM exact",
                         "Phone": h.get("phone"),
                         "Website": h.get("website"),
                         "Category": h.get("kind"),
                         "Rating": None,
                         "Source": "OpenStreetMap (exact address)"})

        # Only include OSM nearby hits if we have nothing else for this address
        if not google.get(num) and not exact_hits:
            for h in nearby_hits:
                label = f"{h['name']} [at {h['addr_num']}]" if h.get("addr_num") else f"{h['name']} [nearby]"
                rows.append({**base,
                             "Business": label,
                             "Confidence": "OSM nearby",
                             "Phone": h.get("phone"),
                             "Website": h.get("website"),
                             "Category": h.get("kind"),
                             "Rating": None,
                             "Source": "OpenStreetMap (nearby - verify)"})

        # If nothing at all, keep address row with permit info
        if not google.get(num) and not exact_hits and not nearby_hits:
            rows.append({**base,
                         "Business": None,
                         "Confidence": "",
                         "Phone": None,
                         "Website": None,
                         "Category": None,
                         "Rating": None,
                         "Source": "No business data"})

    return pd.DataFrame(rows)


# -------------------- MAIN --------------------
def main():
    ap = argparse.ArgumentParser(description="NYC Awning Lead Finder")
    ap.add_argument("street", help='Street name, e.g. "BLEECKER STREET"')
    ap.add_argument("--borough", default="MANHATTAN",
                    choices=["MANHATTAN", "BROOKLYN", "QUEENS", "BRONX", "STATEN ISLAND"])
    ap.add_argument("--output", default=None,
                    help="Output prefix for .xlsx and .csv (default: <street>_leads)")
    ap.add_argument("--skip-osm", action="store_true",
                    help="Skip OpenStreetMap Overpass lookup")
    args = ap.parse_args()

    street = args.street.strip().upper()
    borough = args.borough.upper()
    out_prefix = args.output or f"{street.replace(' ', '_')}_{borough}_AWNING_LEADS"

    # Step 1
    permits = fetch_awning_permits(street, borough)
    if permits.empty:
        print("\nNo awning permits found. Exiting.")
        return 0

    sidewalk = fetch_sidewalk_cafes(street)

    addresses = [(int(r["house_num"]), street) for _, r in permits.iterrows()]

    # Step 2
    geocoded = geocode_addresses(addresses, borough)

    # Step 3
    osm = {} if args.skip_osm else fetch_osm_pois(geocoded)

    # Step 4
    google = fetch_google_places(geocoded)

    # Build & save
    final = build_final_table(permits, geocoded, osm, google, sidewalk)
    print(f"\nWriting {out_prefix}.xlsx and {out_prefix}.csv...")
    final.to_excel(f"{out_prefix}.xlsx", index=False)
    final.to_csv(f"{out_prefix}.csv", index=False)
    print(f"Wrote: {out_prefix}.csv")
    print(f"Wrote: {out_prefix}.xlsx")
    return 0


if __name__ == "__main__":
    sys.exit(main())
