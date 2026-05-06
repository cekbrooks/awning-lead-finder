import streamlit as st
import pandas as pd
from io import BytesIO
from find_awning_leads import (
    fetch_awning_permits,
    fetch_sidewalk_cafes,
    geocode_addresses,
    fetch_osm_pois,
    fetch_google_places,
    build_final_table,
)

st.set_page_config(page_title="NYC Awning Lead Finder", page_icon="🏙️", layout="wide")

st.title("🏙️ NYC Awning Lead Finder")
st.write("Find businesses with awning permits on any NYC street.")

col1, col2 = st.columns([3, 1])
with col1:
    street = st.text_input("Street name", value="BLEECKER STREET",
                           help="e.g. BLEECKER STREET, MULBERRY STREET, BEDFORD AVE")
with col2:
    borough = st.selectbox("Borough",
                           ["MANHATTAN", "BROOKLYN", "QUEENS", "BRONX", "STATEN ISLAND"])

skip_osm = st.checkbox("Skip OpenStreetMap lookup (faster, less complete)", value=False)

if st.button("🔍 Find Leads", type="primary"):
    street_clean = street.upper().strip()

    with st.spinner(f"Pulling DOB awning permits for {street_clean}..."):
        permits = fetch_awning_permits(street_clean, borough)

    if permits.empty:
        st.error("No awning permits found for that street.")
        st.stop()

    st.success(f"Found {len(permits)} addresses with awning permits on file.")

    with st.spinner("Looking up sidewalk cafe licenses..."):
        cafes = fetch_sidewalk_cafes(street_clean)

    addresses = [(int(r["house_num"]), street_clean) for _, r in permits.iterrows()]

    with st.spinner(f"Geocoding {len(addresses)} addresses (this takes ~1 second per address)..."):
        geocoded = geocode_addresses(addresses, borough)

    osm = {}
    if not skip_osm:
        with st.spinner("Looking up businesses via OpenStreetMap..."):
            osm = fetch_osm_pois(geocoded)

    google = fetch_google_places(geocoded)

    final = build_final_table(permits, geocoded, osm, google, cafes)

    total_addrs = permits["house_num"].nunique()
    found_addrs = final[final["Business"].notna()]["Address #"].nunique()
    leads = final["Business"].notna().sum()

    st.markdown("### Results")
    c1, c2, c3 = st.columns(3)
    c1.metric("Permit addresses", total_addrs)
    c2.metric("Addresses identified", f"{found_addrs} ({found_addrs/total_addrs*100:.0f}%)")
    c3.metric("Total business leads", leads)

    st.dataframe(final, use_container_width=True)

    # Excel download
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        final.to_excel(writer, index=False, sheet_name="Leads")
    st.download_button(
        "⬇️ Download Excel",
        data=buf.getvalue(),
        file_name=f"{street_clean.replace(' ', '_')}_{borough}_AWNING_LEADS.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    csv_data = final.to_csv(index=False).encode("utf-8")
    st.download_button(
        "⬇️ Download CSV",
        data=csv_data,
        file_name=f"{street_clean.replace(' ', '_')}_{borough}_AWNING_LEADS.csv",
        mime="text/csv",
    )
