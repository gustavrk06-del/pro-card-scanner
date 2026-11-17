import io
import json
import re
import requests
import streamlit as st
from google import genai
from google.genai import types
from PIL import Image, ImageEnhance

# INDSÆT DIN API-NØGLE FRA GOOGLE AI STUDIO HERUNDER:
GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY", "")   # eneste ændring: nøglen læses fra Secrets

# Modeller fra Google AI Studio
IMAGE_MODEL_ID = "gemini-3.1-flash-image-preview"
VISION_MODEL_ID = "gemini-3.8-flash"

st.set_page_config(page_title="Pro Card Scanner", page_icon="🃏", layout="wide")
st.title("🃏 Pro Card Scanner (Med Automatisk Database-Match)")
st.write("Fjerner skygger og genskin – bevarer ægte centering, farver og skader uden falsk whitening.")

if "front_res" not in st.session_state:
    st.session_state.front_res = None
if "back_res" not in st.session_state:
    st.session_state.back_res = None
if "blueprint_info" not in st.session_state:
    st.session_state.blueprint_info = None

def fetch_card_blueprint(client, front_img):
    """Aflæser kortets navn og nummer og finder det i TCGdex."""
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        id_prompt = (
            "Look at this Pokémon card. Return ONLY a JSON object with 'name' (just the Pokémon name, e.g. 'Venusaur') "
            "and 'number' (just the card number before the slash, e.g. '67' or 'H30')."
        )
        res = client.models.generate_content(
            model=VISION_MODEL_ID,
            contents=[id_prompt, front_img],
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        data = json.loads((res.text or "{}").strip())
        if isinstance(data, list) and data:
            data = data.pop(0)
        name = str(data.get("name", "")).strip()
        number = str(data.get("number", "")).strip()
        clean_num = number.lstrip("0") or number
        st.info(f"🔎 Aflæste kortet som: **{name}** (Nr. **{number}**)")

        base_url = "https://" + "api.tcgdex.net" + "/v2/en/cards"
        resp = requests.get(base_url, params={"name": name}, headers=headers, timeout=8)
        if resp.status_code == 200:
            cards = resp.json()
            if isinstance(cards, list) and cards:
                chosen = None
                for c in cards:
                    c_id = str(c.get("localId", "")).lstrip("0")
                    if clean_num and (c_id == clean_num or c_id.upper() == clean_num.upper()) and c.get("image"):
                        chosen = c
                        break
                if not chosen:
                    for c in cards:
                        if c.get("image"):
                            chosen = c
                            break
                if chosen and chosen.get("image"):
                    img_resp = requests.get(f"{chosen['image']}/high.webp", headers=headers, timeout=8)
                    if img_resp.status_code == 200:
                        bp_img = Image.open(io.BytesIO(img_resp.content)).convert("RGB")
                        return bp_img, f"{chosen.get('name', name)} #{chosen.get('localId', number)}"
    except Exception:
        pass
    return None, None

def get_prompt(bg_choice):
    bg_desc = "solid dark neutral gray background (#2B2B2B)" if "Mørkegrå" in bg_choice else "pure solid white background (#FFFFFF)"

    return f"""You are a forensic trading card scanner. Transform the uploaded smartphone photos into a single flat, studio-grade flatbed scanner image centered on a {bg_desc}.

STRICT IMAGE ROLES (CRITICAL):
- IMAGE 1 (PRIMARY MASTER REFERENCE): The ONLY source of truth for border thicknesses (centering), corner shapes, and raw color tones.
- EXTRA USER PHOTOS (GLARE & HOLO REFERENCE ONLY): Use these strictly to verify holo patterns and remove camera glare from Image 1.
- 3D PAPER THICKNESS WARNING: On angled reference photos (Image 2+), the white middle layer of the cardstock thickness is visible from the side. Do NOT copy this 3D side-edge thickness onto the flat 2D face of the card as edge whitening.

PROCESSING RULES:
1. EXACT BORDER CENTERING (DO NOT BALANCE OR SYMMETRIZE): Look closely at the four blue/yellow outer border widths in Image 1. If the left border is wider than the right border (left-heavy), or the bottom border is wider than the top border (bottom-heavy), you MUST keep that exact visual difference in pixel width. Do NOT equalize opposite borders—leaving uneven, off-center borders is mandatory so buyers can accurately grade centering.
2. CLEAN CORNERS & SILHOUETTE (NO FAKE CORNER WHITENING): Flatten Image 1 into a 2D vertical rectangle (63x88mm aspect ratio), removing the table background completely. Preserve the exact corner shapes of Image 1. If a corner in Image 1 is clean and solid dark-blue or silver, keep the curved edge 100% solid dark-blue or silver all the way to the gray background—NEVER add white speckles, anti-aliasing specks, or fake corner whitening onto clean rounded corners.
3. ZERO FAKE EDGE HALO OR WHITENING: Do NOT draw a light outline or white border fringe where the card meets the gray background. Remove camera glare from glossy borders so clean borders stay solid dark blue or silver.
4. RAW COLOR & HOLO FIDELITY: Preserve the exact holo pattern, muted tones, and raw color temperature of Image 1 without boosting saturation or contrast."""

def call_gemini(client, bg_choice, labeled_files):
    valid_items = [(label, Image.open(f)) for label, f in labeled_files if f is not None]
    if not valid_items:
        return None

    prompt = get_prompt(bg_choice)
    contents = [prompt]
    for label, img in valid_items:
        contents.append(label)
        contents.append(img)

    response = client.models.generate_content(
        model=IMAGE_MODEL_ID,
        contents=contents,
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE"],
            temperature=0.1,
            image_config=types.ImageConfig(
                aspect_ratio="3:4"
            ),
        ),
    )
    first_candidate = response.candidates.pop(0)
    for part in first_candidate.content.parts:
        if part.inline_data is not None:
            img = Image.open(io.BytesIO(part.inline_data.data)).convert("RGB")
            img = ImageEnhance.Color(img).enhance(0.94)
            return img
    st.error("Modellen returnerede intet billede. Tjek din API-nøgle.")
    return None

bg_choice = st.radio(
    "Vælg scanner-baggrund:",
    ["Mørkegrå (#2B2B2B) - Bedst til at vise whitening og skader", "Kridhvid (#FFFFFF) - Klassisk scanner"]
)

col1, col2 = st.columns(2)
with col1:
    st.subheader("1. Forside")
    f1 = st.file_uploader("Billede 1: Direkte oppefra (Obligatorisk - Låser centering)", type=["jpg", "jpeg", "png", "webp"], key="f1")
    f2 = st.file_uploader("Billede 2: Let skævt uden genskin (Anbefalet - Detaljer)", type=["jpg", "jpeg", "png", "webp"], key="f2")
    f3 = st.file_uploader("Billede 3: Nærbillede af mikrodetaljer (Valgfrit)", type=["jpg", "jpeg", "png", "webp"], key="f3")
    f4 = st.file_uploader("Billede 4: Nærbillede af skader/ridser (Valgfrit)", type=["jpg", "jpeg", "png", "webp"], key="f4")

with col2:
    st.subheader("2. Bagside")
    b1 = st.file_uploader("Billede 1: Direkte oppefra (Valgfrit - Låser centering)", type=["jpg", "jpeg", "png", "webp"], key="b1")
    b2 = st.file_uploader("Billede 2: Let skævt uden genskin (Anbefalet - Whitening)", type=["jpg", "jpeg", "png", "webp"], key="b2")
    b3 = st.file_uploader("Billede 3: Nærbillede af hjørner/kanter (Valgfrit)", type=["jpg", "jpeg", "png", "webp"], key="b3")
    b4 = st.file_uploader("Billede 4: Nærbillede af overfladeskader (Valgfrit)", type=["jpg", "jpeg", "png", "webp"], key="b4")

if st.button("🚀 Scan Kort (For- og Bagside)", type="primary", width="stretch"):
    if GEMINI_API_KEY == "INDSAET_DIN_NOEGLE_HER" or not GEMINI_API_KEY.strip():
        st.error("Husk at indsætte din Google AI API-nøgle i filen først!")
    elif f1 is None and b1 is None:
        st.error("Upload som minimum Billede 1 for forsiden eller bagsiden.")
    else:
        with st.spinner("Identificerer kort og scanner... Vent et øjeblik"):
            client = genai.Client(api_key=GEMINI_API_KEY.strip())
            
            blueprint_img, blueprint_label = (None, None)
            if f1 is not None:
                blueprint_img, blueprint_label = fetch_card_blueprint(client, Image.open(f1))
            st.session_state.blueprint_info = (blueprint_img, blueprint_label)

            front_labeled = [
                ("IMAGE 1 (PRIMARY MASTER REFERENCE - GEOMETRY, CENTERING & RAW COLORS):", f1),
                ("IMAGE 2 (GLARE & SHADOW REFERENCE ONLY):", f2),
                ("IMAGE 3 (MICRO-DETAIL EVIDENCE ONLY):", f3),
                ("IMAGE 4 (SURFACE DAMAGE & SCRATCHES EVIDENCE ONLY):", f4),
            ]
            back_labeled = [
                ("IMAGE 1 (PRIMARY MASTER REFERENCE - GEOMETRY, CENTERING & RAW COLORS):", b1),
                ("IMAGE 2 (GLARE & SHADOW REFERENCE ONLY):", b2),
                ("IMAGE 3 (CORNER & EDGE WHITENING EVIDENCE ONLY):", b3),
                ("IMAGE 4 (SURFACE DAMAGE & SCRATCHES EVIDENCE ONLY):", b4),
            ]
            
            st.session_state.front_res = call_gemini(client, bg_choice, front_labeled) if f1 is not None else None
            st.session_state.back_res = call_gemini(client, bg_choice, back_labeled) if b1 is not None else None

if st.session_state.front_res is not None or st.session_state.back_res is not None:
    st.divider()
    info = st.session_state.blueprint_info
    if info and info.pop(1) if isinstance(info, list) else (info and info):
        label_txt = info if isinstance(info, tuple) else str(info)
        st.success(f"✅ Kort genkendt i databasen: **{label_txt}**")
    
    res_col1, res_col2 = st.columns(2)
    pdf_pages = []

    if st.session_state.front_res is not None:
        with res_col1:
            st.image(st.session_state.front_res, caption="Færdig Forside", width="stretch")
            buf_f = io.BytesIO()
            st.session_state.front_res.save(buf_f, format="JPEG", quality=95, dpi=(300, 300))
            st.download_button("⬇️ Download Forside (JPG)", buf_f.getvalue(), "forside_scan.jpg", "image/jpeg", key="dl_f")
        pdf_pages.append(st.session_state.front_res.resize((744, 1039), Image.Resampling.LANCZOS))

    if st.session_state.back_res is not None:
        with res_col2:
            st.image(st.session_state.back_res, caption="Færdig Bagside", width="stretch")
            buf_b = io.BytesIO()
            st.session_state.back_res.save(buf_b, format="JPEG", quality=95, dpi=(300, 300))
            st.download_button("⬇️ Download Bagside (JPG)", buf_b.getvalue(), "bagside_scan.jpg", "image/jpeg", key="dl_b")
        pdf_pages.append(st.session_state.back_res.resize((744, 1039), Image.Resampling.LANCZOS))

    if pdf_pages:
        buf_pdf = io.BytesIO()
        first_page = pdf_pages.pop(0)
        first_page.save(buf_pdf, format="PDF", resolution=300.0, save_all=True, append_images=pdf_pages)
        st.download_button("📄 Download Samlet PDF (63 x 88 mm)", buf_pdf.getvalue(), "samlet_kort_scan.pdf", "application/pdf", width="stretch", key="dl_pdf")
