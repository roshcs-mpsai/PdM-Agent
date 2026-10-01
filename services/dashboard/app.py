"""PdM-Agent status board -- Streamlit Version Zero (R0.5, HIL-01 stub).

    pip install -r services/dashboard/requirements.txt
    streamlit run services/dashboard/app.py

Shows each asset's NE 107 marker, last window time (stream time) and score,
read from its retained status message on every refresh. Topics come from the
asset profiles under profiles/, never from code. Broker and refresh period
come from PDM_BROKER (localhost), PDM_BROKER_PORT (1883) and PDM_REFRESH_S
(2). R2b replaces this page with the four-page dashboard reading the API.
"""
import os
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))
from status_view import board, discover_profiles, mqtt_reader  # noqa: E402

from pdm_common.profile import load_profile  # noqa: E402

COLOURS = {"green": "#2e7d32", "amber": "#f9a825", "red": "#c62828", "grey": "#9e9e9e"}
BROKER = os.environ.get("PDM_BROKER", "localhost")
PORT = int(os.environ.get("PDM_BROKER_PORT", "1883"))
REFRESH_S = float(os.environ.get("PDM_REFRESH_S", "2"))

st.set_page_config(page_title="PdM-Agent status", layout="wide")
st.title("PdM-Agent · asset status")
st.caption(f"Retained status from {BROKER}:{PORT}, refreshed every {REFRESH_S:g} s. "
           "Walking skeleton: z-score baseline with a placeholder threshold and no "
           "persistence gate yet, so a single window over the threshold turns the marker amber.")


@st.cache_resource
def profiles():
    return [load_profile(path) for path in discover_profiles()]


def render():
    try:
        rows = board(profiles(), mqtt_reader(BROKER, PORT))
    except OSError as exc:
        st.error(f"Cannot reach the broker at {BROKER}:{PORT} ({exc}). "
                 "Start it with `docker compose up -d`.")
        return
    for row in rows:
        dot, text = st.columns([1, 12])
        dot.markdown(f"<div style='font-size:2.6rem;line-height:1;color:{COLOURS[row['marker']]}'>"
                     "&#9679;</div>", unsafe_allow_html=True)
        if row["ne107"] is None:
            text.markdown(f"**{row['asset']}**  \n{row['note']} on `{row['topic']}`")
            continue
        text.markdown(
            f"**{row['asset']}** · NE 107 **{row['ne107'].replace('_', ' ')}**  \n"
            f"last window {row['ts']} · score {row['score']:.2f} / threshold "
            f"{row['threshold']:.2f} · run `{row['detector_run']}`"
        )


fragment = getattr(st, "fragment", None)      # Streamlit >= 1.37
if fragment is not None:
    fragment(run_every=REFRESH_S)(render)()
else:
    render()
    st.button("Refresh")
