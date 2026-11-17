# -*- coding: utf-8 -*-
"""
Pro Card Scanner – Streamlit-app til Streamlit Community Cloud.

Opsætning
  1. Læg app.py, requirements.txt og .streamlit/config.toml i dit GitHub-repo.
     Læg før/efter-billeder i mappen examples/ (se EXAMPLE_PAIRS nedenfor).
  2. Streamlit Community Cloud -> din app -> Settings -> Secrets:
         GEMINI_API_KEY = "din-nøgle-fra-Google-AI-Studio"
     Lokalt bruger du .streamlit/secrets.toml (tilføj den fil til .gitignore).
  3. Valgfrit i Secrets, hvis Google udfaser en model (ingen kodeændring nødvendig):
         IMAGE_MODEL_ID  = "gemini-3.1-flash-image"
         VISION_MODEL_ID = "gemini-3.8-flash"
"""

import io
import json
import logging
import os
from pathlib import Path
from urllib.parse import quote

import requests
import streamlit as st
from google import genai
from google.genai import types
from PIL import Image, ImageEnhance, ImageOps

# Skal være det første Streamlit-kald i scriptet.
st.set_page_config(page_title="Pro Card Scanner", page_icon="🃏", layout="wide")

logger = logging.getLogger("pro_card_scanner")

# ───────────────────────────── Indstillinger ─────────────────────────────
MAX_SCANS = 5                 # gratis scanninger pr. besøgende (pr. browser-session)
NORMALIZE_UPLOADS = True      # fotos sendes uændret til Gemini, medmindre de skal rettes (EXIF-rotation, enorme filer)
MAX_IMAGE_SIDE = 4096         # fotos med en længere side skaleres ned (almindelige mobilfotos på op til 12 MP røres ikke)
REQUEST_TIMEOUT_MS = 180_000  # timeout pr. kald til Gemini (millisekunder)
UPLOAD_TYPES = ["jpg", "jpeg", "png", "webp"]

# Ekstra prompt-afsnit (regel 5), der altid sendes med efter dine fire gennemtestede regler: kortet skal gengives 1:1.
APP_VERSION = "2026-11-17 d"   # vises nederst på siden, så man kan se hvilken kode der kører online
PRESERVE_WHITENING_RULE = """

5. FAITHFUL REPRODUCTION OF REAL WEAR (WHITENING, SCRATCHES, DISCOLORATION, EDGE DAMAGE): Judge wear only from the flat top-down view in Image 1 and the straight-on close-up photos, never from the visible paper side-edge in angled photos. If white chipping, whitening specks, nicks, scratches, print lines, dents, stains or discoloration are clearly visible on the card in those photos, reproduce each one faithfully: same location, same shape, similar size and intensity. Tiny white flecks on corners and edges must stay as tiny white flecks. Do NOT clean up, smooth over, retouch or remove real wear, and do NOT invent wear that is not visible in the photos. A clean corner stays 100% clean; a worn corner stays worn."""

EXAMPLES_DIR = Path(__file__).resolve().parent / "examples"
EXAMPLE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
EXAMPLE_PAIRS = (("foer1", "efter1"), ("foer2", "efter2"))   # (før, efter) uden filendelse


def read_secret(name, default=""):
    """Henter en værdi fra st.secrets (eller en miljøvariabel). Fejler aldrig, heller ikke uden secrets-fil."""
    try:
        value = st.secrets.get(name, "")
    except Exception:
        value = ""
    value = str(value or "").strip()
    return value or os.environ.get(name, "").strip() or default


# API-nøglen ligger kun i Secrets – aldrig i koden.
GEMINI_API_KEY = read_secret("GEMINI_API_KEY")

# Modeller fra Google AI Studio (kan overstyres i Secrets uden kodeændring)
IMAGE_MODEL_ID = read_secret("IMAGE_MODEL_ID", "gemini-3.1-flash-image-preview")
VISION_MODEL_ID = read_secret("VISION_MODEL_ID", "gemini-3.8-flash")

for _key, _default in (("front_res", None), ("back_res", None), ("blueprint_info", None), ("scans_used", 0)):
    if _key not in st.session_state:
        st.session_state[_key] = _default


# ═════════════════════ AI-MOTOR – UÆNDRET FRA PROTOTYPEN ═════════════════════
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
    is_dark = "Mørkegrå" in bg_choice
    bg_desc = "solid dark neutral gray background (#2B2B2B)" if is_dark else "pure solid white background (#FFFFFF)"
    bg_word = "gray" if is_dark else "white"

    prompt = f"""You are a forensic trading card scanner. Transform the uploaded smartphone photos into a single flat, studio-grade flatbed scanner image centered on a {bg_desc}.

STRICT IMAGE ROLES (CRITICAL):
- IMAGE 1 (PRIMARY MASTER REFERENCE): The ONLY source of truth for border thicknesses (centering), corner shapes, and raw color tones.
- EXTRA USER PHOTOS (GLARE & HOLO REFERENCE ONLY): Use these strictly to verify holo patterns and remove camera glare from Image 1.
- 3D PAPER THICKNESS WARNING: On angled reference photos (Image 2+), the white middle layer of the cardstock thickness is visible from the side. Do NOT copy this 3D side-edge thickness onto the flat 2D face of the card as edge whitening.

PROCESSING RULES:
1. EXACT BORDER CENTERING (DO NOT BALANCE OR SYMMETRIZE): Look closely at the four blue/yellow outer border widths in Image 1. If the left border is wider than the right border (left-heavy), or the bottom border is wider than the top border (bottom-heavy), you MUST keep that exact visual difference in pixel width. Do NOT equalize opposite borders—leaving uneven, off-center borders is mandatory so buyers can accurately grade centering.
2. CLEAN CORNERS & SILHOUETTE (NO FAKE CORNER WHITENING): Flatten Image 1 into a 2D vertical rectangle (63x88mm aspect ratio), removing the table background completely. Preserve the exact corner shapes of Image 1. If a corner in Image 1 is clean and solid dark-blue or silver, keep the curved edge 100% solid dark-blue or silver all the way to the {bg_word} background—NEVER add white speckles, anti-aliasing specks, or fake corner whitening onto clean rounded corners.
3. ZERO FAKE EDGE HALO OR WHITENING: Do NOT draw a light outline or white border fringe where the card meets the {bg_word} background. Remove camera glare from glossy borders so clean borders stay solid dark blue or silver.
4. RAW COLOR & HOLO FIDELITY: Preserve the exact holo pattern, muted tones, and raw color temperature of Image 1 without boosting saturation or contrast."""
    return prompt + PRESERVE_WHITENING_RULE

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
# ═════════════════════════ SLUT PÅ AI-MOTOREN ═════════════════════════════════


# ───────────────────────────── Hjælpefunktioner ─────────────────────────────
def prepare_upload(uploaded_file):
    """Giver Gemini fotoet præcis, som det er uploadet, medmindre det skal rettes (EXIF-rotation, enormt stort eller
    usædvanligt farveformat). Returnerer fotoets bytes, eller None hvis filen ikke kan læses som billede."""
    raw = uploaded_file.getvalue()
    if not NORMALIZE_UPLOADS:
        return raw
    try:
        with Image.open(io.BytesIO(raw)) as probe:
            probe.load()   # afslører ødelagte filer med det samme
            needs_rotation = probe.getexif().get(0x0112, 1) not in (0, 1)
            too_big = max(probe.size) > MAX_IMAGE_SIDE
            plain_mode = probe.mode in ("RGB", "RGBA")
        if plain_mode and not needs_rotation and not too_big:
            return raw
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
        if max(img.size) > MAX_IMAGE_SIDE:
            img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=95, subsampling=0)
        return buf.getvalue()
    except Exception:
        logger.exception("Kunne ikke læse uploadet billede")
        return None


def make_client(api_key):
    """Opretter Gemini-klienten med timeout, så et hængende kald ikke blokerer appen."""
    try:
        return genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS))
    except Exception:
        return genai.Client(api_key=api_key)


def friendly_error(exc):
    """Oversætter tekniske fejl til en forståelig besked. Detaljerne logges kun på serveren."""
    code = getattr(exc, "code", None)
    text = f"{type(exc).__name__} {exc}".lower()
    if code in (401, 403) or "api key" in text or "api_key" in text or "permission_denied" in text:
        return "Google afviste API-nøglen. Ejer: tjek GEMINI_API_KEY i Secrets."
    if code == 404 or "not_found" in text or "not found" in text:
        return ("Billedmodellen findes ikke længere hos Google. "
                "Ejer: sæt IMAGE_MODEL_ID i Secrets til en aktiv model, fx gemini-3.1-flash-image.")
    if code == 429 or "resource_exhausted" in text or "quota" in text:
        return "Google-API'et har nået sin kvote lige nu. Vent et par minutter og prøv igen."
    if (code in (500, 502, 503, 504) or "unavailable" in text or "overloaded" in text
            or "timeout" in text or "timed out" in text or "deadline" in text):
        return "Google-modellen svarer ikke lige nu. Prøv igen om lidt."
    if isinstance(exc, (AttributeError, IndexError, KeyError, TypeError)):
        return ("Modellen gav intet brugbart svar, måske fordi Googles sikkerhedsfilter blokerede billedet. "
                "Prøv igen eller med et andet foto.")
    return "Der opstod en uventet fejl under scanningen. Prøv igen om lidt."


def jpeg_bytes(img):
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=95, dpi=(300, 300))
    return buf.getvalue()


def pdf_bytes(images):
    """Samler siderne i én PDF uden at ændre billedernes proportioner."""
    pages = [im.convert("RGB") for im in images]
    buf = io.BytesIO()
    pages[0].save(buf, format="PDF", resolution=300.0, save_all=True, append_images=pages[1:])
    return buf.getvalue()


def find_example(stem):
    """Finder et eksempelbillede uanset store/små bogstaver (Linux skelner, Windows gør ikke)."""
    try:
        for path in sorted(EXAMPLES_DIR.iterdir()):
            if path.is_file() and path.stem.lower() == stem.lower() and path.suffix.lower() in EXAMPLE_EXTS:
                return path
    except OSError:
        pass
    return None


def load_example(path):
    """Læser et eksempelbillede sikkert (korrupte filer må aldrig vælte siden)."""
    try:
        with Image.open(path) as im:
            img = ImageOps.exif_transpose(im).convert("RGB")
        img.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
        return img
    except Exception:
        logger.exception("Kunne ikke læse eksempelbillede %s", path)
        return None


# ──────────────────────── Scanning (bruger de låste funktioner) ────────────────────────
FRONT_KEYS = ("f1", "f2", "f3", "f4")
BACK_KEYS = ("b1", "b2", "b3", "b4")
FRONT_LABELS = (
    "IMAGE 1 (PRIMARY MASTER REFERENCE - GEOMETRY, CENTERING & RAW COLORS):",
    "IMAGE 2 (GLARE & SHADOW REFERENCE ONLY):",
    "IMAGE 3 (MICRO-DETAIL EVIDENCE ONLY):",
    "IMAGE 4 (SURFACE DAMAGE & SCRATCHES EVIDENCE ONLY):",
)
BACK_LABELS = (
    "IMAGE 1 (PRIMARY MASTER REFERENCE - GEOMETRY, CENTERING & RAW COLORS):",
    "IMAGE 2 (GLARE & SHADOW REFERENCE ONLY):",
    "IMAGE 3 (CORNER & EDGE WHITENING EVIDENCE ONLY):",
    "IMAGE 4 (SURFACE DAMAGE & SCRATCHES EVIDENCE ONLY):",
)


def labeled(prepared, labels, keys):
    """Bygger [(label, fil eller None)] i det format call_gemini() forventer."""
    return [(label, io.BytesIO(prepared[key]) if key in prepared else None) for label, key in zip(labels, keys)]


def scan_side(client, bg_choice, labeled_files, name, problems):
    """Kører call_gemini() for én side. Fejl bliver til en pæn besked i stedet for et nedbrud."""
    try:
        result = call_gemini(client, bg_choice, labeled_files)
    except Exception as exc:
        logger.exception("Scanning af %s fejlede", name.lower())
        problems.append(f"{name}: {friendly_error(exc)}")
        return None
    if result is None:
        problems.append(f"{name}: Modellen returnerede intet billede. Prøv igen eller med et andet foto.")
    return result


def run_scan(api_key, bg_choice, uploads):
    """Kører hele scanningen. Returnerer True, hvis mindst én side blev scannet."""
    prepared = {}
    for key, upload in uploads.items():
        if upload is None:
            continue
        data = prepare_upload(upload)
        if data is None:
            st.error(f"Kunne ikke læse filen «{upload.name}». Upload et gyldigt JPG-, PNG- eller WEBP-billede i normal telefonopløsning.")
            return False
        prepared[key] = data

    if "f1" not in prepared and any(k in prepared for k in ("f2", "f3", "f4")):
        st.warning("Forsiden springes over, fordi Billede 1 (direkte oppefra) mangler.")
    if "b1" not in prepared and any(k in prepared for k in ("b2", "b3", "b4")):
        st.warning("Bagsiden springes over, fordi Billede 1 (direkte oppefra) mangler.")

    try:
        client = make_client(api_key)
    except Exception:
        logger.exception("Kunne ikke oprette Gemini-klient")
        st.error("Scanneren kunne ikke startes. Prøv igen om lidt.")
        return False

    blueprint = (None, None)
    if "f1" in prepared:
        with st.spinner("Identificerer kortet …"):
            try:
                blueprint = fetch_card_blueprint(client, Image.open(io.BytesIO(prepared["f1"])))
            except Exception:
                logger.exception("Database-opslag fejlede")

    problems = []
    front_img = back_img = None
    with st.status("Scanner dit kort … vent et øjeblik", expanded=True) as status:
        if "f1" in prepared:
            st.write("Scanner forsiden …")
            front_img = scan_side(client, bg_choice, labeled(prepared, FRONT_LABELS, FRONT_KEYS), "Forsiden", problems)
        if "b1" in prepared:
            st.write("Scanner bagsiden …")
            back_img = scan_side(client, bg_choice, labeled(prepared, BACK_LABELS, BACK_KEYS), "Bagsiden", problems)
        ok = front_img is not None or back_img is not None
        status.update(label="Scanning færdig" if ok else "Scanningen mislykkedes",
                      state="complete" if ok else "error", expanded=False)

    st.session_state.blueprint_info = blueprint
    st.session_state.front_res = front_img
    st.session_state.back_res = back_img
    for message in problems:
        st.error(message)
    return ok


# ───────────────────────────── Design: HTML og CSS ─────────────────────────────
def clean_markup(markup):
    """Fjerner indrykning og tomme linjer, så markdown ikke tolker HTML som kodeblok."""
    return "\n".join(line.strip() for line in markup.strip().splitlines() if line.strip())


def render_html(markup):
    st.markdown(clean_markup(markup), unsafe_allow_html=True)


def section_header(title, sub=""):
    sub_html = f'<div class="sec-sub">{sub}</div>' if sub else ""
    render_html(f'<div class="sec"><div class="sec-title" role="heading" aria-level="2">{title}</div>{sub_html}</div>')


def svg_uri(svg):
    """Gør en SVG til en data-URI, der kan bruges som CSS-baggrund (ingen eksterne filer)."""
    return "data:image/svg+xml;charset=utf-8," + quote(clean_markup(svg), safe="")


# Tre små tegninger (set fra siden) til foto-guiden.
FIG_LIGHT = """
<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 240 132'>
<rect x='12' y='100' width='216' height='20' rx='3' fill='#2B2B2B'/>
<rect x='94' y='96' width='52' height='4' rx='1' fill='#F2C230'/>
<rect x='18' y='14' width='60' height='42' rx='3' fill='#fff' stroke='#2B2B2B' stroke-width='2'/>
<path d='M48 14V56M18 35H78' stroke='#2B2B2B' stroke-width='1.5'/>
<path d='M82 26L102 92M82 36L118 92M82 46L134 92' fill='none' stroke='#E3AE00' stroke-width='2.2' stroke-dasharray='4 4'/>
<path d='M172 14H210L203 32H179Z' fill='#fff' stroke='#2B2B2B' stroke-width='2' stroke-linejoin='round'/>
<path d='M191 4V14' stroke='#2B2B2B' stroke-width='2'/>
<circle cx='191' cy='39' r='4.5' fill='#F2C230' stroke='#2B2B2B' stroke-width='1.5'/>
<path d='M168 10L214 56M214 10L168 56' stroke='#2B2B2B' stroke-width='3.5' stroke-linecap='round'/>
</svg>
"""

FIG_FLAT = """
<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 240 132'>
<rect x='12' y='108' width='216' height='12' rx='3' fill='#2B2B2B'/>
<rect x='94' y='104' width='52' height='4' rx='1' fill='#F2C230'/>
<rect x='88' y='16' width='64' height='10' rx='4' fill='#fff' stroke='#2B2B2B' stroke-width='2'/>
<circle cx='120' cy='27' r='3.5' fill='#2B2B2B'/>
<path d='M120 33V100' fill='none' stroke='#E3AE00' stroke-width='2.2' stroke-dasharray='4 4'/>
<path d='M120 96H128V104' fill='none' stroke='#2B2B2B' stroke-width='1.5'/>
<text x='164' y='29' font-family='Arial,Helvetica,sans-serif' font-size='16' font-weight='700' fill='#2B2B2B'>2x</text>
<path d='M190 18V104M185 18H195M185 104H195' fill='none' stroke='#8A8F98' stroke-width='1.5'/>
</svg>
"""

FIG_TILT = """
<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 240 132'>
<rect x='12' y='108' width='216' height='12' rx='3' fill='#2B2B2B'/>
<rect x='94' y='104' width='52' height='4' rx='1' fill='#F2C230'/>
<g transform='rotate(-25 90 40)'>
<rect x='60' y='35' width='60' height='10' rx='4' fill='#fff' stroke='#2B2B2B' stroke-width='2'/>
<circle cx='90' cy='46' r='3.5' fill='#2B2B2B'/>
</g>
<path d='M94.6 50L119.8 104' fill='none' stroke='#E3AE00' stroke-width='2.2' stroke-dasharray='4 4'/>
<path d='M119.8 104V58' fill='none' stroke='#8A8F98' stroke-width='1.5' stroke-dasharray='3 3'/>
<path d='M119.8 78A26 26 0 0 0 108.8 80.4' fill='none' stroke='#2B2B2B' stroke-width='1.5'/>
<text x='125' y='86' font-family='Arial,Helvetica,sans-serif' font-size='13' font-weight='700' fill='#2B2B2B'>25°</text>
</svg>
"""

CSS = """
:root{--graphite:#2B2B2B;--yellow:#F2C230;--amber:#E3AE00;--line:rgba(127,127,127,.32);--cond:Bahnschrift,"Avenir Next Condensed","Arial Narrow","Roboto Condensed","sans-serif-condensed","DejaVu Sans Condensed",sans-serif;}
footer,[data-testid="stDecoration"]{display:none !important;}
.element-container:has(style),[data-testid="stElementContainer"]:has(style){display:none;}
[data-testid="stMainBlockContainer"],.block-container{max-width:1120px !important;padding-top:2.4rem;padding-bottom:4rem;}

.hero-title{font-family:var(--cond);font-stretch:condensed;font-weight:700;font-size:clamp(2.7rem,6.2vw,4.3rem);line-height:.96;margin:.1rem 0 1rem;}
.hero-sub{font-size:1.1rem;line-height:1.55;max-width:44ch;margin:0 0 1.3rem;opacity:.8;}

.bed{position:relative;overflow:hidden;background:var(--graphite);border-radius:8px;padding:2.1rem 1.4rem 1.6rem;display:flex;flex-direction:column;align-items:center;gap:1.4rem;color:#fff;}
.slab{position:relative;width:min(172px,46vw);aspect-ratio:63/88;min-height:200px;box-sizing:border-box;background:var(--yellow);padding:9px 8px 11px 12px;border-radius:7px;display:flex;flex-direction:column;gap:5px;}
.slab .nm{flex:8 1 0;background:rgba(43,43,43,.82);border-radius:2px;}
.slab .art{flex:50 1 0;border-radius:2px;position:relative;overflow:hidden;background:linear-gradient(130deg,#5F8FC9 0%,#8E7BE0 30%,#E58AC6 55%,#F2C96B 78%,#6FD3AE 100%);}
.slab .art::after{content:"";position:absolute;top:0;right:0;bottom:0;left:0;background:linear-gradient(115deg,rgba(255,255,255,0) 32%,rgba(255,255,255,.4) 50%,rgba(255,255,255,0) 68%);}
.slab .tx{flex:42 1 0;background:#FBF3D6;border-radius:2px;padding:6px;display:flex;flex-direction:column;gap:4px;}
.slab .tx i{display:block;height:3px;border-radius:2px;background:rgba(43,43,43,.3);}
.gl{position:absolute;background:rgba(255,255,255,.55);}
.gl.v{top:-16px;bottom:-16px;width:1px;}
.gl.h{left:-16px;right:-16px;height:1px;}
.gl.vl{left:12px;}
.gl.vr{right:8px;}
.gl.ht{top:9px;}
.gl.hb{bottom:11px;}
.readouts{display:flex;gap:2rem;justify-content:center;animation:bedfade .6s ease 2.4s 1 both;}
.readouts div{display:flex;flex-direction:column;align-items:center;gap:.1rem;}
.readouts b{font-family:var(--cond);font-stretch:condensed;font-weight:700;font-size:1.9rem;line-height:1;color:var(--yellow);}
.readouts span{font-size:.8rem;opacity:.72;}
.bed-note{font-size:.88rem;line-height:1.45;opacity:.8;text-align:center;max-width:30ch;}
.sweep{position:absolute;left:0;right:0;top:0;height:2px;background:var(--yellow);box-shadow:0 0 22px 5px rgba(242,194,48,.5);opacity:0;pointer-events:none;animation:bedscan 2.2s cubic-bezier(.45,0,.2,1) .5s 1 both;}
@keyframes bedscan{0%{top:0;opacity:0}10%{opacity:1}90%{opacity:1}100%{top:100%;opacity:0}}
@keyframes bedfade{from{opacity:0}to{opacity:1}}
@media (prefers-reduced-motion:reduce){.sweep{display:none}.readouts{animation:none}}

.quota{display:inline-flex;align-items:center;gap:.7rem;padding:.5rem 1rem .5rem .8rem;border:1px solid var(--line);border-radius:999px;font-size:.95rem;}
.quota .dots{display:inline-flex;gap:5px;}
.quota .dots i{width:10px;height:10px;border-radius:50%;box-sizing:border-box;border:1.5px solid currentColor;opacity:.55;}
.quota .dots i.on{background:var(--yellow);border-color:var(--graphite);opacity:1;}
.quota.empty{border-color:#B3261E;color:#B3261E;}

.sec{margin:3.2rem 0 1rem;}
.sec-title{font-family:var(--cond);font-stretch:condensed;font-weight:700;font-size:clamp(1.8rem,3.6vw,2.4rem);line-height:1.05;}
.sec-sub{margin-top:.4rem;max-width:62ch;font-size:1rem;line-height:1.5;opacity:.72;}

.steps{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));column-gap:2.4rem;border-top:2px solid currentColor;margin-top:.4rem;}
.step{position:relative;padding:1.1rem 0 1.2rem;}
.step + .step::before{content:"";position:absolute;left:-1.2rem;top:1.1rem;bottom:0;width:1px;background:var(--line);}
.fig{aspect-ratio:240/132;background-color:#F1F2F4;background-position:center;background-repeat:no-repeat;background-size:contain;border-radius:4px;margin-bottom:.95rem;}
.fig.f1{background-image:url("__FIG_LIGHT__");}
.fig.f2{background-image:url("__FIG_FLAT__");}
.fig.f3{background-image:url("__FIG_TILT__");}
.step-head{display:flex;align-items:center;gap:.6rem;margin-bottom:.4rem;}
.step-no{flex:none;width:1.8rem;height:1.8rem;display:flex;align-items:center;justify-content:center;background:var(--yellow);color:var(--graphite);font-family:var(--cond);font-stretch:condensed;font-weight:700;font-size:1.2rem;border-radius:3px;}
.step-title{font-family:var(--cond);font-stretch:condensed;font-weight:700;font-size:1.4rem;line-height:1.1;}
.step-text{font-size:.95rem;line-height:1.5;opacity:.8;}
@media (max-width:820px){.steps{grid-template-columns:minmax(0,1fr);column-gap:0;}.step{padding:1.1rem 0;}.step + .step{border-top:1px solid var(--line);}.step + .step::before{display:none;}}

.plate-title{font-family:var(--cond);font-stretch:condensed;font-weight:700;font-size:1.6rem;line-height:1;margin:.1rem 0 .3rem;}
.plate-sub{font-size:.92rem;line-height:1.45;opacity:.7;margin-bottom:.6rem;}

.legal{margin-top:3.5rem;padding-top:1rem;border-top:1px solid var(--line);font-size:.82rem;line-height:1.6;opacity:.68;max-width:92ch;}

[data-testid="stFileUploaderDropzone"]{border:1.5px dashed rgba(127,127,127,.65);border-radius:6px;background:rgba(127,127,127,.07);}
[data-testid="stFileUploaderDropzone"]:hover{border-color:var(--amber);background:rgba(242,194,48,.14);}
[data-testid="stButton"],[data-testid="stDownloadButton"]{width:100%;}
button[kind="primary"],[data-testid="stBaseButton-primary"]{width:100%;background:var(--yellow);color:var(--graphite);border:2px solid var(--graphite);border-radius:4px;padding:.75rem 1.25rem;font-weight:700;}
button[kind="primary"] p,[data-testid="stBaseButton-primary"] p{color:var(--graphite);font-size:1.05rem;font-weight:700;}
button[kind="primary"]:hover:not(:disabled),[data-testid="stBaseButton-primary"]:hover:not(:disabled){background:#FFD24D;border-color:var(--graphite);color:var(--graphite);}
button[kind="primary"]:disabled,[data-testid="stBaseButton-primary"]:disabled{background:rgba(127,127,127,.12);border-color:var(--line);}
button[kind="primary"]:disabled p,[data-testid="stBaseButton-primary"]:disabled p{color:inherit;opacity:.6;}
[data-testid="stDownloadButton"] button{width:100%;border:1.5px solid var(--graphite);border-radius:4px;}
[data-testid="stImage"] img{border-radius:6px;border:1px solid var(--line);}
"""
CSS = (CSS.replace("__FIG_LIGHT__", svg_uri(FIG_LIGHT))
          .replace("__FIG_FLAT__", svg_uri(FIG_FLAT))
          .replace("__FIG_TILT__", svg_uri(FIG_TILT)))

# Et kort på en scannerplade: kantene er med vilje skæve, og målingerne viser det.
# Hjælpelinjerne ligger præcis ved de indre kanter: venstre 12 px / højre 8 px = 60/40, top 9 px / bund 11 px = 45/55.
BED_HTML = """
<div class="bed" role="img" aria-label="Illustration: et kort på en scannerplade, hvor skæv centering vises som 60/40 og 45/55">
  <div class="slab">
    <i class="gl v vl"></i><i class="gl v vr"></i><i class="gl h ht"></i><i class="gl h hb"></i>
    <div class="nm"></div>
    <div class="art"></div>
    <div class="tx"><i></i><i></i><i></i></div>
  </div>
  <div class="readouts">
    <div><b>60 / 40</b><span>venstre / højre</span></div>
    <div><b>45 / 55</b><span>top / bund</span></div>
  </div>
  <div class="bed-note">Centeringen bevares, som den er. Kanterne rettes aldrig til.</div>
  <span class="sweep"></span>
</div>
"""

STEPS = (
    ("f1", "1", "Lys og underlag",
     "Læg kortet på et mørkt underlag i blødt, indirekte lys. Undgå direkte loftlampe og genskin.",
     "Tegning: lys fra et vindue rammer kortet fra siden, mens en loftlampe er streget over"),
    ("f2", "2", "Fladt oppefra",
     "Hold telefonen fladt oppefra for præcis centering. Brug evt. 2x zoom på afstand.",
     "Tegning: telefonen holdes vandret lige over kortet med god afstand"),
    ("f3", "3", "Let skråt vinkel",
     "Tilføj evt. et let skråt billede af holo-mønstret eller ridser.",
     "Tegning: telefonen holdes let på skrå over kortet"),
)

GUIDE_DO = """
**Gør**

- Brug dagslys fra et vindue eller to bløde lamper fra siden.
- Læg kortet fladt på et mørkt, mat underlag.
- Hold telefonen parallelt med kortet, så kortet fylder det meste af billedet.
- Vent, til fokus er låst, før du tager billedet.
"""
GUIDE_DONT = """
**Undgå**

- Direkte loftlampe, blitz og skarpt lys fra siden.
- Skygger fra din hånd eller telefon.
- Sleeve eller toploader, hvis du kan tage kortet ud. De giver ekstra genskin.
- HDR, filtre og skønhedsfunktioner i kamera-appen.
"""
GUIDE_ROLES = """
**Hvad bruges billederne til?**

- **Billede 1, oppefra:** låser centering, hjørner og farver.
- **Billede 2, let skråt:** fjerner genskin og viser holo-mønstret.
- **Billede 3 og 4, nærbilleder:** ridser, whitening og mikrodetaljer.
"""

LEGAL_HTML = """
<div class="legal">Pro Card Scanner er et uofficielt fanværktøj og er ikke tilknyttet, godkendt eller sponsoreret af The Pokémon Company, Nintendo, Creatures eller GAME FREAK. Pokémon og Pokémon-karakterer er varemærker tilhørende deres respektive ejere. Dine fotos sendes til Google Gemini til behandling. Resultatet er en AI-genereret visuel gengivelse og erstatter ikke en professionel gradering.</div>
"""


def quota_html(left):
    dots = "".join(f'<i class="{"on" if i < left else ""}"></i>' for i in range(MAX_SCANS))
    if left > 0:
        cls, text = "quota", f"{left} af {MAX_SCANS} gratis test-scans tilbage"
    else:
        cls, text = "quota empty", "Ingen gratis test-scans tilbage"
    return f'<div class="{cls}"><span class="dots" aria-hidden="true">{dots}</span><span>{text}</span></div>'


def steps_html():
    items = "".join(
        f'<div class="step" role="listitem">'
        f'<div class="fig {key}" role="img" aria-label="{alt}"></div>'
        f'<div class="step-head"><span class="step-no" aria-hidden="true">{number}</span>'
        f'<span class="step-title">{title}</span></div>'
        f'<div class="step-text">{text}</div></div>'
        for key, number, title, text, alt in STEPS
    )
    return f'<div class="steps" role="list">{items}</div>'


def render_quota():
    left = max(MAX_SCANS - st.session_state.scans_used, 0)
    quota_slot.markdown(clean_markup(quota_html(left)), unsafe_allow_html=True)


def render_results():
    front_res = st.session_state.front_res
    back_res = st.session_state.back_res
    if front_res is None and back_res is None:
        return
    section_header("Dit scan", "Download hver side som JPG, eller begge sider samlet som PDF.")
    info = st.session_state.blueprint_info
    label = info[1] if isinstance(info, (tuple, list)) and len(info) > 1 else None
    if label:
        st.success(f"✅ Kort genkendt i databasen: **{label}**")
    col_front, col_back = st.columns(2, gap="large")
    pages = []
    if front_res is not None:
        with col_front:
            st.image(front_res, caption="Færdig forside")
            st.download_button("⬇️ Download Forside (JPG)", jpeg_bytes(front_res), "forside_scan.jpg", "image/jpeg", key="dl_f")
        pages.append(front_res)
    if back_res is not None:
        with col_back:
            st.image(back_res, caption="Færdig bagside")
            st.download_button("⬇️ Download Bagside (JPG)", jpeg_bytes(back_res), "bagside_scan.jpg", "image/jpeg", key="dl_b")
        pages.append(back_res)
    if pages:
        st.download_button("📄 Download samlet PDF (forside og bagside)", pdf_bytes(pages),
                           "samlet_kort_scan.pdf", "application/pdf", key="dl_pdf")


def render_gallery():
    """Viser før/efter-par, så snart begge filer i et par findes i examples/. Ellers vises ingenting."""
    pairs = []
    for before_stem, after_stem in EXAMPLE_PAIRS:
        before, after = find_example(before_stem), find_example(after_stem)
        before_img = load_example(before) if before else None
        after_img = load_example(after) if after else None
        if before_img is not None and after_img is not None:
            pairs.append((before_img, after_img))
    if not pairs:
        return
    section_header("Se forskellen: Mobilfoto vs. Vores Scanner", "Samme kort før og efter scanning.")
    for before_img, after_img in pairs:
        col_before, col_after = st.columns(2, gap="large")
        with col_before:
            st.image(before_img, caption="Før: mobilfoto")
        with col_after:
            st.image(after_img, caption="Efter: Pro Card Scanner")


# ═════════════════════════════════ SIDEN ═════════════════════════════════
render_html(f"<style>{CSS}</style>")

# Hero
hero_left, hero_right = st.columns([1.1, 1], gap="large")
with hero_left:
    render_html("""
        <div class="hero-title" role="heading" aria-level="1">Pro Card Scanner</div>
        <div class="hero-sub">Upload dine mobilfotos af kortet. Du får et fladt scan uden skygger og genskin, hvor centering, farver og skader bevares, som de er på det rigtige kort.</div>
    """)
    quota_slot = st.empty()
with hero_right:
    render_html(BED_HTML)
render_quota()

if not GEMINI_API_KEY:
    st.error("**API-nøglen mangler.** Scanneren er ikke sat op endnu, så scanning er slået fra.")
    st.caption("Ejer: tilføj `GEMINI_API_KEY` under Settings → Secrets i Streamlit Community Cloud "
               "(lokalt i `.streamlit/secrets.toml`), og genstart appen.")

# Foto-guide
section_header("Sådan tager du fotos, der scanner godt", "Tre trin, så får scanneren det bedste at arbejde med.")
render_html(steps_html())
with st.expander("📖 Åbn den fulde foto-guide"):
    guide_do, guide_dont, guide_roles = st.columns(3, gap="large")
    guide_do.markdown(GUIDE_DO)
    guide_dont.markdown(GUIDE_DONT)
    guide_roles.markdown(GUIDE_ROLES)

# Upload
section_header("Upload dine fotos", "Kun billede 1 af forsiden er obligatorisk. Flere vinkler giver flere detaljer.")
up_front, up_back = st.columns(2, gap="large")
with up_front:
    with st.container(border=True):
        render_html('<div class="plate-title">Forside</div><div class="plate-sub">Billede 1 er obligatorisk.</div>')
        f1 = st.file_uploader("Billede 1: Direkte oppefra (Obligatorisk - Låser centering)", type=UPLOAD_TYPES, key="f1")
        f2 = st.file_uploader("Billede 2: Let skævt uden genskin (Anbefalet - Detaljer)", type=UPLOAD_TYPES, key="f2")
with up_back:
    with st.container(border=True):
        render_html('<div class="plate-title">Bagside</div><div class="plate-sub">Valgfri. Tilføj billede 1, hvis bagsiden også skal scannes.</div>')
        b1 = st.file_uploader("Billede 1: Direkte oppefra (Valgfrit - Låser centering)", type=UPLOAD_TYPES, key="b1")
        b2 = st.file_uploader("Billede 2: Let skævt uden genskin (Anbefalet - Whitening)", type=UPLOAD_TYPES, key="b2")

with st.expander("➕ Tilføj ekstra vinkler eller nærbilleder af ridser (Valgfrit)"):
    extra_front, extra_back = st.columns(2, gap="large")
    with extra_front:
        render_html('<div class="plate-title">Forside</div>')
        f3 = st.file_uploader("Billede 3: Nærbillede af mikrodetaljer (Valgfrit)", type=UPLOAD_TYPES, key="f3")
        f4 = st.file_uploader("Billede 4: Nærbillede af skader/ridser (Valgfrit)", type=UPLOAD_TYPES, key="f4")
    with extra_back:
        render_html('<div class="plate-title">Bagside</div>')
        b3 = st.file_uploader("Billede 3: Nærbillede af hjørner/kanter (Valgfrit)", type=UPLOAD_TYPES, key="b3")
        b4 = st.file_uploader("Billede 4: Nærbillede af overfladeskader (Valgfrit)", type=UPLOAD_TYPES, key="b4")

bg_choice = st.radio(
    "Vælg scanner-baggrund:",
    ["Mørkegrå (#2B2B2B) - Bedst til at vise whitening og skader", "Kridhvid (#FFFFFF) - Klassisk scanner"],
    horizontal=True,
)
scans_left = MAX_SCANS - st.session_state.scans_used
scan_clicked = st.button("🚀 Scan Kort (For- og Bagside)", type="primary", key="scan_btn",
                         disabled=(not GEMINI_API_KEY) or scans_left <= 0)
st.caption("Forside og bagside tæller samlet som én scanning. Dine fotos sendes til Google Gemini og gemmes ikke af appen.")
if GEMINI_API_KEY and scans_left <= 0:
    st.warning(f"Du har brugt alle {MAX_SCANS} gratis test-scans i denne session.")

if scan_clicked:
    if not GEMINI_API_KEY:
        st.error("Scanning er slået fra, fordi API-nøglen mangler.")
    elif st.session_state.scans_used >= MAX_SCANS:
        st.error(f"Du har brugt alle {MAX_SCANS} gratis test-scans i denne session.")
    elif f1 is None and b1 is None:
        st.error("Upload som minimum Billede 1 for forsiden eller bagsiden.")
    else:
        st.session_state.scans_used += 1
        scan_ok = run_scan(GEMINI_API_KEY, bg_choice,
                           {"f1": f1, "f2": f2, "f3": f3, "f4": f4, "b1": b1, "b2": b2, "b3": b3, "b4": b4})
        if not scan_ok:
            st.session_state.scans_used -= 1   # en scanning uden resultat tæller ikke
    render_quota()

render_results()
render_gallery()
render_html(LEGAL_HTML)
st.caption(f"Programversion {APP_VERSION}")
