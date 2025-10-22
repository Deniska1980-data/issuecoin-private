import os
import io
import json
import time
import datetime as dt
from typing import List, Dict, Optional

import streamlit as st
import pandas as pd
import requests
import altair as alt

# -------------------------
# CONFIG / PATHS
# -------------------------
DATA_DIR = "data"
PRODUCTS_XLSX = os.path.join(DATA_DIR, "seznam_potravin_app.xlsx")
PLANS_DIR = os.path.join(DATA_DIR, "shopping_plans")
INBOX_DIR = os.path.join(DATA_DIR, "inbox")  # sem uložíme uploadnuté účtenky/audio

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(PLANS_DIR, exist_ok=True)
os.makedirs(INBOX_DIR, exist_ok=True)

st.set_page_config(page_title="IssueCoin — Private", page_icon="🧠", layout="wide")

# -------------------------
# SIDEBAR — SECRETS / INTEGRATIONS
# -------------------------
st.sidebar.title("⚙️ Integrations")

# N8N webhook (voliteľné). Ak vyplníš, pošleme sem plány aj prílohy.
N8N_WEBHOOK = st.sidebar.text_input(
    "N8N Webhook URL (optional)",
    value=st.secrets.get("N8N_WEBHOOK_URL", ""),
    help="Ak vyplníš, nákupné plány/účtenky sa odošlú do n8n workflow."
)

# Calendarific (voliteľné)
CALENDARIFIC_KEY = st.secrets.get("CALENDARIFIC_API_KEY", "")
CALENDARIFIC_COUNTRY = st.sidebar.selectbox(
    "Calendarific country",
    options=["", "CZ", "SK"],
    index=0,
    help="Nechaj prázdne, ak nechceš volať Calendarific."
)

# -------------------------
# HELPERY: DÁTUMY / SVIATKY / ČNB
# -------------------------
@st.cache_data(ttl=600)
def get_cnb_rate(date: dt.date, currency: str) -> Optional[float]:
    """
    Vráti kurz (CZK / currency) podľa dátumu z ČNB. Ak nie je dostupný presný dátum (víkend/sviatok),
    skúsi posledný dostupný deň späť max 7 dní.
    CZK sa berie ako 1:1.
    """
    currency = currency.upper()
    if currency == "CZK":
        return 1.0

    # CNB textová tabuľka: https://www.cnb.cz/en/financial-markets/foreign-exchange-market/central-bank-exchange-rate-fixing/
    # Programmatic endpoint (historical day): https://www.cnb.cz/en/financial-markets/foreign-exchange-market/central-bank-exchange-rate-fixing/
    # Prakticky: https://www.cnb.cz/cs/financni-trhy/devizovy-trh/kurzy-devizoveho-trhu/denni_kurz.txt?date=DD.MM.YYYY
    for back in range(0, 7):
        d = date - dt.timedelta(days=back)
        url = f"https://www.cnb.cz/cs/financni-trhy/devizovy-trh/kurzy-devizoveho-trhu/denni_kurz.txt?date={d.strftime('%d.%m.%Y')}"
        try:
            resp = requests.get(url, timeout=10)
            if resp.ok and "země|měna|množství|kód|kurz" in resp.text:
                lines = resp.text.splitlines()
                for line in lines:
                    parts = line.split("|")
                    if len(parts) == 5 and parts[3].upper() == currency:
                        mnozstvi = float(parts[2].strip().replace(",", "."))
                        kurz = float(parts[4].strip().replace(",", "."))
                        return kurz / mnozstvi  # kurz za 1 jednotku meny
        except Exception:
            pass
    return None

@st.cache_data(ttl=3600)
def get_holidays(year: int, country: str) -> pd.DataFrame:
    """
    Vráti sviatky z Calendarific. Ak chýba API key alebo country, vráti prázdny DF.
    """
    if not CALENDARIFIC_KEY or not country:
        return pd.DataFrame(columns=["date", "name", "type"])

    url = "https://calendarific.com/api/v2/holidays"
    params = {"api_key": CALENDARIFIC_KEY, "country": country, "year": year}
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        rows = []
        for h in data.get("response", {}).get("holidays", []):
            date = h["date"]["iso"][:10]
            name = h["name"]
            types = ",".join(h.get("type", []))
            rows.append({"date": date, "name": name, "type": types})
        return pd.DataFrame(rows)
    except Exception:
        return pd.DataFrame(columns=["date", "name", "type"])

def is_holiday(date: dt.date, country: str) -> Optional[str]:
    df = get_holidays(date.year, country)
    if df.empty:
        return None
    rec = df[df["date"] == date.strftime("%Y-%m-%d")]
    if rec.empty:
        return None
    return f"{rec.iloc[0]['name']} ({rec.iloc[0]['type']})"

# -------------------------
# LOAD PRODUCTS
# -------------------------
@st.cache_data
def load_products() -> pd.DataFrame:
    """
    Očakáva XLSX so stĺpcami aspoň: 'kategoria', 'nazov' (alebo 'produkt').
    Môžeš mať aj 'jednotka' (ks, kg, l) a 'preferovany_obchod' (Albert/Lidl/...)
    """
    if not os.path.exists(PRODUCTS_XLSX):
        # ak nemáš súbor, vytvoríme demo
        demo = pd.DataFrame({
            "kategoria": ["Mliečne", "Mäso", "Trvanlivé", "Pečivo", "Drogeria"],
            "nazov": ["Mlieko 1.5%", "Kuracie prsia", "Cestoviny vretienka", "Chlieb pšeničný", "Prací prášok"],
            "jednotka": ["l", "kg", "kg", "ks", "ks"],
            "preferovany_obchod": ["Albert", "Tesco", "Lidl", "Albert", "DM"]
        })
        return demo
    df = pd.read_excel(PRODUCTS_XLSX)
    # normalizácia názvov stĺpcov
    cols = {c.lower(): c for c in df.columns}
    # Ošetrenie povinných
    need = []
    if "kategoria" not in cols:
        need.append("kategoria")
    if "nazov" not in cols and "produkt" not in cols:
        need.append("nazov (alebo produkt)")
    if need:
        st.warning(f"V XLSX chýbajú stĺpce: {', '.join(need)}. Vytvorím dočasné demo dáta.")
        return load_products.__wrapped__()  # demo fallback

    # Premenuj 'produkt'->'nazov' ak treba
    if "nazov" not in df.columns and "produkt" in df.columns:
        df = df.rename(columns={"produkt": "nazov"})
    # Doplň jednotku/obchod ak chýbajú
    if "jednotka" not in df.columns:
        df["jednotka"] = ""
    if "preferovany_obchod" not in df.columns:
        df["preferovany_obchod"] = ""
    # Drop duplikáty, reset index
    df = df.dropna(subset=["nazov"]).copy()
    df["kategoria"] = df["kategoria"].fillna("Nezaradené")
    df["nazov"] = df["nazov"].astype(str)
    df = df.reset_index(drop=True)
    return df

products_df = load_products()

# -------------------------
# UI — HEADER / BADGES
# -------------------------
colA, colB = st.columns([0.75, 0.25])
with colA:
    st.markdown("## 🧠 IssueCoin — Private (OpenShift + N8N + Sheets-ready)")
    st.caption("Bilingválne, GDPR-friendly, bez Microsoft/Power Automate. Základ ostáva z verejnej appky — toto je nadstavba.")
with colB:
    st.metric("Položiek v katalógu", len(products_df))

st.divider()

# -------------------------
# FILTER / CHECKLIST
# -------------------------
st.subheader("🛒 Nákupný checklist z tvojho XLSX")

with st.expander("🔎 Filter"):
    c1, c2, c3 = st.columns(3)
    kategorie = ["(všetky)"] + sorted(products_df["kategoria"].unique().tolist())
    kat = c1.selectbox("Kategória", options=kategorie, index=0)
    hledej = c2.text_input("Hľadať názov", "")
    obchod = c3.text_input("Preferovaný obchod (filter)", "")

filtered = products_df.copy()
if kat != "(všetky)":
    filtered = filtered[filtered["kategoria"] == kat]
if hledej.strip():
    filtered = filtered[filtered["nazov"].str.contains(hledej, case=False, na=False)]
if obchod.strip():
    filtered = filtered[filtered["preferovany_obchod"].str.contains(obchod, case=False, na=False)]

if filtered.empty:
    st.info("Žiadne položky pre aktuálny filter.")
else:
    st.caption("Zaškrtni, doplň množstvo (ks/kg/l) a voliteľne cenu/obchod. Potom ulož alebo odošli do n8n.")
    # Formulár s dynamickými položkami
    with st.form("shopping_form", clear_on_submit=False):
        rows = []
        for i, row in filtered.reset_index(drop=True).iterrows():
            with st.container(border=True):
                c1, c2, c3, c4, c5 = st.columns([0.4, 0.15, 0.15, 0.15, 0.15])
                chk = c1.checkbox(f"{row['nazov']}", key=f"chk_{row['nazov']}")
                qty = c2.number_input("Množstvo", min_value=0.0, value=0.0, step=1.0, key=f"qty_{row['nazov']}")
                unit = c3.text_input("Jednotka", value=str(row.get("jednotka","")), key=f"unit_{row['nazov']}")
                price = c4.number_input("Cena (CZK)", min_value=0.0, value=0.0, step=1.0, key=f"price_{row['nazov']}")
                shop = c5.text_input("Obchod", value=str(row.get("preferovany_obchod","")), key=f"shop_{row['nazov']}")
                rows.append({"nazov": row["nazov"], "kategoria": row["kategoria"],
                             "vybrate": chk, "mnozstvo": qty, "jednotka": unit,
                             "cena": price, "obchod": shop})
        submitted = st.form_submit_button("💾 Uložiť plán (CSV)")

    plan_df = pd.DataFrame(rows)
    plan_df = plan_df[(plan_df["vybrate"]) & (plan_df["mnozstvo"] > 0)]

    if submitted:
        if plan_df.empty:
            st.warning("Nezaškrtla si nič s množstvom > 0.")
        else:
            now = dt.datetime.now()
            fname = f"plan_{now.strftime('%Y-%m-%d_%H%M%S')}.csv"
            out_path = os.path.join(PLANS_DIR, fname)
            # dopln dátum, sviatok info, kurz CZK->CZK=1, prípadne kalkulácie
            plan_df = plan_df.copy()
            plan_df["datum"] = now.date().isoformat()
            if CALENDARIFIC_COUNTRY:
                hol = is_holiday(now.date(), CALENDARIFIC_COUNTRY)
            else:
                hol = None
            plan_df["sviatok"] = hol if hol else ""

            plan_df.to_csv(out_path, index=False, encoding="utf-8")
            st.success(f"Plán uložený: `{out_path}`")

            if N8N_WEBHOOK:
                try:
                    payload = plan_df.to_dict(orient="records")
                    r = requests.post(N8N_WEBHOOK, json={"type": "shopping_plan", "data": payload}, timeout=20)
                    if r.ok:
                        st.success("✅ Odošlé do n8n.")
                    else:
                        st.warning(f"n8n odpoveď {r.status_code}: {r.text[:200]}")
                except Exception as e:
                    st.warning(f"n8n webhook zlyhal: {e}")

    if not plan_df.empty:
        st.markdown("#### Náhľad uloženého plánu")
        st.dataframe(plan_df[["kategoria","nazov","mnozstvo","jednotka","cena","obchod"]], use_container_width=True)

        # rýchly súčet podľa kategórie/obchodu
        if "cena" in plan_df.columns:
            plan_df["medzisucet"] = plan_df["cena"]
            chart = alt.Chart(plan_df).mark_bar().encode(
                x="kategoria:N",
                y="sum(medzisucet):Q",
                tooltip=["kategoria","sum(medzisucet)"]
            ).properties(height=280)
            st.altair_chart(chart, use_container_width=True)

st.divider()

# -------------------------
# UPLOAD — účtenky / audio (STT/OCR hooks)
# -------------------------
st.subheader("📤 Inbox: účtenky / hlasové poznámky")

cU1, cU2 = st.columns(2)

with cU1:
    st.markdown("**Účtenka (foto/PDF)** — uloží sa do `data/inbox/` a (voliteľne) pošle do n8n.")
    receipt = st.file_uploader("Nahraj účtenku", type=["png","jpg","jpeg","pdf"], key="receipt_up")
    if receipt is not None:
        ts = int(time.time())
        ext = os.path.splitext(receipt.name)[1].lower()
        safe = f"receipt_{ts}{ext}"
        path = os.path.join(INBOX_DIR, safe)
        with open(path, "wb") as f:
            f.write(receipt.read())
        st.success(f"Uložené: `{path}`")

        if N8N_WEBHOOK:
            try:
                files = {"file": (safe, open(path,"rb"))}
                data = {"type": "receipt_upload", "ts": ts}
                r = requests.post(N8N_WEBHOOK, data=data, files=files, timeout=30)
                if r.ok:
                    st.success("✅ Účtenka odoslaná do n8n (OCR/klasifikácia si spravíš vo workflow).")
                else:
                    st.warning(f"n8n odpoveď {r.status_code}: {r.text[:200]}")
            except Exception as e:
                st.warning(f"n8n upload zlyhal: {e}")

with cU2:
    st.markdown("**Hlasová poznámka (audio)** — uloží sa do `data/inbox/` a (voliteľne) pošle do n8n.")
    audio = st.file_uploader("Nahraj audio", type=["wav","mp3","m4a"], key="audio_up")
    if audio is not None:
        ts = int(time.time())
        ext = os.path.splitext(audio.name)[1].lower()
        safe = f"voice_{ts}{ext}"
        path = os.path.join(INBOX_DIR, safe)
        with open(path, "wb") as f:
            f.write(audio.read())
        st.success(f"Uložené: `{path}`")

        if N8N_WEBHOOK:
            try:
                files = {"file": (safe, open(path,"rb"))}
                data = {"type": "voice_upload", "ts": ts}
                r = requests.post(N8N_WEBHOOK, data=data, files=files, timeout=30)
                if r.ok:
                    st.success("✅ Audio odoslané do n8n (STT/transkripciu spravíš vo workflow).")
                else:
                    st.warning(f"n8n odpoveď {r.status_code}: {r.text[:200]}")
            except Exception as e:
                st.warning(f"n8n upload zlyhal: {e}")

st.divider()

# -------------------------
# OPTIONAL: CNB PREPOČET DEMO
# -------------------------
with st.expander("💱 (Voliteľné) Prepočet menou cez ČNB"):
    col1, col2, col3 = st.columns(3)
    dt_pick = col1.date_input("Dátum kurzu", value=dt.date.today())
    cur = col2.text_input("Mena (napr. EUR, USD)", value="EUR")
    amount = col3.number_input("Suma v mene", min_value=0.0, value=100.0, step=10.0)

    if st.button("Prepočítať do CZK"):
        rate = get_cnb_rate(dt_pick, cur)
        if rate:
            czk = amount * rate
            st.success(f"≈ {czk:,.2f} CZK (kurz {cur}/{dt_pick.strftime('%d.%m.%Y')} ~ {rate:,.4f})")
        else:
            st.warning("Kurz sa nepodarilo získať.")

# -------------------------
# FOOTER
# -------------------------
st.caption("© 2025 IssueCoin — Private Edition (OpenShift + N8N hooks). by DenyP")

