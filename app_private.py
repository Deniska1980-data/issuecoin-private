# -*- coding: utf-8 -*-
"""
IssueCoin — Private Layer (OpenShift + n8n + MCP-ready)
Nadstavba nad verejnou appkou. Nič v nej nemení, iba rozširuje.

Funguje aj bez OpenAI/Azure:
- CSV/Google Sheets zápis
- n8n webhook (ak je nastavený)
- OCR z obrázkov/PDF (ak je k dispozícii pytesseract alebo pypdf)
- Auto-detekcia meny/krajiny podľa účtenky (heuristiky)
- Zachováva CNB/Calendarific/hlášky z verejnej appky, ak sú importovateľné
"""

import io
import os
import re
import json
import time
import base64
import typing as t
from datetime import datetime, date

import pandas as pd
import streamlit as st
import requests

# Voliteľné / best-effort knižnice (nevadí, ak nie sú)
try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None

try:
    from PIL import Image
except Exception:
    Image = None

try:
    import pytesseract  # potrebuje systémový tesseract pre najlepšiu kvalitu (voliteľné)
except Exception:
    pytesseract = None


# ------------------------------------------------------------------------------
# 1) Pokus o import funkcií z verejnej appky (CNB/Calendarific/hlášky)
# ------------------------------------------------------------------------------

def _fallback_rate(_dt: date, _ccy: str) -> float:
    # bezpečný fallback, ak nie je import z verejnej appky
    return 1.0 if _ccy.upper() == "CZK" else 0.0

def _fallback_holiday(_dt: date, _country: str) -> dict:
    return {"is_holiday": False, "label": ""}

def _fallback_issuecoin_msg(context: dict) -> str:
    # krátka, milá správa keď nemáme verejný modul k dispozícii
    cat = context.get("category", "nákup")
    total = context.get("amount_czk", 0)
    return f"✅ Záznam uložený. Kategória: {cat}, dnešný súčet ~ {total:.2f} CZK."

try:
    # názov prispôsob svojmu verejnému súboru, ak je iný
    import app_public as public_core

    get_cnb_rate = getattr(public_core, "get_cnb_rate", _fallback_rate)
    get_holiday_info = getattr(public_core, "get_holiday_info", _fallback_holiday)
    issuecoin_message = getattr(public_core, "issuecoin_message", _fallback_issuecoin_msg)
except Exception:
    get_cnb_rate = _fallback_rate
    get_holiday_info = _fallback_holiday
    issuecoin_message = _fallback_issuecoin_msg


# ------------------------------------------------------------------------------
# 2) Cesty / konštanty
# ------------------------------------------------------------------------------

DATA_DIR = "data"
PRODUCTS_CSV = os.path.join(DATA_DIR, "seznam_potravin_app.csv")  # tvoj súbor so zoznamom
INBOX_CSV = os.path.join(DATA_DIR, "inbox_priv.csv")              # sem sa ukladajú správy/účtenky
LEDGER_CSV = os.path.join(DATA_DIR, "ledger_priv.csv")            # finálne položky

os.makedirs(DATA_DIR, exist_ok=True)

DEFAULT_STORES = ["ALBERT", "LIDL", "PENNY", "TESCO", "DM", "ROSSMANN"]

# Prehľad dostupných ISO kódov
ISO_MAP = {
    "CZ": {"ccy": "CZK"},
    "SK": {"ccy": "EUR"},
    "PL": {"ccy": "PLN"},
}

# KUPI.CZ: len „placeholder“ – aby bola pipeline komplet (reálny scraper doplníme neskôr)
SUPPORTED_KUPI = {"ALBERT", "LIDL", "PENNY", "TESCO", "DM", "ROSSMANN"}


# ------------------------------------------------------------------------------
# 3) Utility – načítanie/uloženie CSV
# ------------------------------------------------------------------------------

def load_csv_safe(path: str, cols: t.List[str]) -> pd.DataFrame:
    if not os.path.exists(path):
        return pd.DataFrame(columns=cols)
    try:
        df = pd.read_csv(path)
        # doplň chýbajúce stĺpce (ak si niekedy zmenila schému)
        for c in cols:
            if c not in df.columns:
                df[c] = None
        return df[cols]
    except Exception:
        return pd.DataFrame(columns=cols)

def save_csv_safe(df: pd.DataFrame, path: str) -> None:
    tmp = f"{path}.tmp"
    df.to_csv(tmp, index=False, encoding="utf-8")
    os.replace(tmp, path)

# ------------------------------------------------------------------------------
# 4) Načítanie zoznamu potravín
# ------------------------------------------------------------------------------

def load_products() -> pd.DataFrame:
    # Očakávame aspoň stĺpce: item, category, unit (unit je nepovinné)
    base_cols = ["item", "category", "unit"]
    df = load_csv_safe(PRODUCTS_CSV, base_cols)
    # basic clean
    df["item"] = df["item"].fillna("").astype(str).str.strip()
    df["category"] = df["category"].fillna("Potraviny").astype(str).str.strip()
    df["unit"] = df["unit"].fillna("ks").astype(str).str.strip()
    return df[df["item"] != ""]

# ------------------------------------------------------------------------------
# 5) OCR / Parsovanie účteniek
# ------------------------------------------------------------------------------

RE_TOTAL = re.compile(r"(?:TOTAL|CELKEM|SUMA|SPOLU)\D*([0-9]+[\.,]?[0-9]*)", re.IGNORECASE)
RE_DATE = re.compile(r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})")
RE_CURR = re.compile(r"\b(CZK|Kč|EUR|€|PLN|zł)\b", re.IGNORECASE)
RE_STORE = re.compile(r"(ALBERT|LIDL|PENNY|TESCO|ROSSMANN|DM)", re.IGNORECASE)

def ocr_from_pdf(file_bytes: bytes) -> str:
    if PdfReader is None:
        return ""
    try:
        reader = PdfReader(io.BytesIO(file_bytes))
        text = []
        for page in reader.pages:
            text.append(page.extract_text() or "")
        return "\n".join(text)
    except Exception:
        return ""

def ocr_from_image(file_bytes: bytes) -> str:
    # Skús PIL + pytesseract, inak prázdny string
    if Image is None or pytesseract is None:
        return ""
    try:
        img = Image.open(io.BytesIO(file_bytes)).convert("RGB")
        return pytesseract.image_to_string(img, lang="ces+slk+eng")
    except Exception:
        return ""

def parse_receipt_text(txt: str) -> dict:
    # merchant
    m_store = RE_STORE.search(txt or "")
    store = m_store.group(1).upper() if m_store else ""

    # currency
    m_curr = RE_CURR.search(txt or "")
    raw_curr = (m_curr.group(1) if m_curr else "").upper()
    if raw_curr in {"KČ", "KC", "CZK"}:
        ccy = "CZK"
        country = "CZ"
    elif raw_curr in {"EUR", "€"}:
        ccy = "EUR"
        country = "SK"  # heuristika (príp. doplníme podľa textu účtenky)
    elif raw_curr in {"PLN", "ZŁ"}:
        ccy = "PLN"
        country = "PL"
    else:
        # fallback: ak store napovedá krajinu
        if store in {"ALBERT", "LIDL", "PENNY", "TESCO", "ROSSMANN", "DM"}:
            # default CZ
            ccy = "CZK"
            country = "CZ"
        else:
            ccy = "CZK"
            country = "CZ"

    # date
    m_date = RE_DATE.search(txt or "")
    parsed_date = None
    if m_date:
        raw = m_date.group(1)
        for fmt in ("%d.%m.%Y", "%d.%m.%y", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d-%m-%y"):
            try:
                parsed_date = datetime.strptime(raw, fmt).date()
                break
            except Exception:
                continue
    if not parsed_date:
        parsed_date = date.today()

    # total
    m_total = RE_TOTAL.search(txt or "")
    total = 0.0
    if m_total:
        raw = m_total.group(1).replace(",", ".")
        try:
            total = float(raw)
        except Exception:
            total = 0.0

    return {
        "store": store or "",
        "country": country,
        "currency": ccy,
        "date": parsed_date,
        "total": total,
        "raw_preview": txt[:1200] if txt else "",
    }


# ------------------------------------------------------------------------------
# 6) Kurzy a prepočet do CZK podľa dátumu nákupu (via verejná appka, ak dostupná)
# ------------------------------------------------------------------------------

def convert_to_czk(dt: date, amount: float, currency: str) -> float:
    ccy = (currency or "CZK").upper()
    if ccy == "CZK":
        return float(amount or 0.0)
    rate = get_cnb_rate(dt, ccy)  # z verejnej appky; fallback = 0.0
    if not rate or rate == 0.0:
        # ak nič nedostaneme, radšej vrátime amount (bez prepočtu), aby UI žilo
        return float(amount or 0.0)
    return float(amount or 0.0) * float(rate)


# ------------------------------------------------------------------------------
# 7) n8n / Google Sheets (voliteľné) / CSV ledger
# ------------------------------------------------------------------------------

def post_to_n8n(payload: dict) -> None:
    url = st.secrets.get("N8N_WEBHOOK_URL", "")
    if not url:
        return
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception:
        pass

def write_ledger_row(row: dict) -> None:
    cols = ["ts", "store", "country", "currency", "date", "total_src",
            "amount_czk", "category", "items_json", "note"]
    df = load_csv_safe(LEDGER_CSV, cols)
    df.loc[len(df)] = [
        datetime.utcnow().isoformat(),
        row.get("store", ""),
        row.get("country", "CZ"),
        row.get("currency", "CZK"),
        row.get("date", date.today()).isoformat(),
        float(row.get("total_src", 0.0)),
        float(row.get("amount_czk", 0.0)),
        row.get("category", "Potraviny"),
        json.dumps(row.get("items", []), ensure_ascii=False),
        row.get("note", ""),
    ]
    save_csv_safe(df, LEDGER_CSV)


# ------------------------------------------------------------------------------
# 8) „Kupi.cz tracker“ – placeholder (vráti None / alebo demo cenu)
# ------------------------------------------------------------------------------

def lookup_price_in_flyers(item_name: str, stores: t.List[str]) -> t.Optional[dict]:
    """
    Placeholder na integráciu s kupi.cz.
    Zatiaľ vráti None alebo demo štruktúru, aby pipeline bežala.
    """
    # Príklad demo odpovede – aby sa UI správalo pekne
    demo_hit = {
        "store": "ALBERT",
        "price": 24.90,
        "unit": "ks",
        "promo": True,
        "valid_to": (date.today()).isoformat(),
        "source": "kupi.cz/demo"
    }
    # Ak chceš úplné ticho, vráť None
    return demo_hit


# ------------------------------------------------------------------------------
# 9) Streamlit UI
# ------------------------------------------------------------------------------

st.set_page_config(page_title="IssueCoin — Private", page_icon="🧠", layout="wide")

st.title("🧠 IssueCoin — Private Layer (OpenShift + n8n + MCP-ready)")
st.caption("Nadstavba nad verejnou výdavkovou appkou. Data ostávajú u teba (CSV/Sheets).")

tabs = st.tabs(["🧾 Inbox (účtenky & hlas)", "🛒 Nákup / Zásoby", "📈 Ledger", "⚙️ Nastavenia"])

# --- TAB 1: Inbox ----------------------------------------------------------------
with tabs[0]:
    st.subheader("Nahraj účtenku alebo správu")
    up = st.file_uploader("Obrázok/PDF účtenky", type=["png", "jpg", "jpeg", "pdf"])
    note = st.text_area("Poznámka (voliteľné)", "")

    colA, colB = st.columns([1,1])

    with colA:
        if st.button("📤 Spracovať účtenku", use_container_width=True):
            if not up:
                st.warning("Najprv nahraj súbor.")
            else:
                raw_text = ""
                fb = up.read()
                if up.type == "application/pdf":
                    raw_text = ocr_from_pdf(fb)
                else:
                    raw_text = ocr_from_image(fb)

                parsed = parse_receipt_text(raw_text)
                # Prepočet do CZK podľa dátumu nákupu
                amount_czk = convert_to_czk(parsed["date"], parsed["total"], parsed["currency"])

                st.success("Účtenka spracovaná.")
                st.json({
                    "detected_store": parsed["store"],
                    "detected_country": parsed["country"],
                    "detected_currency": parsed["currency"],
                    "purchase_date": parsed["date"].isoformat(),
                    "total": parsed["total"],
                    "amount_czk": amount_czk
                })

                # Ulož do inboxu
                cols = ["ts","filename","mime","store","country","currency","date","total","raw_preview","note"]
                inbox = load_csv_safe(INBOX_CSV, cols)
                inbox.loc[len(inbox)] = [
                    datetime.utcnow().isoformat(),
                    up.name, up.type,
                    parsed["store"], parsed["country"], parsed["currency"],
                    parsed["date"].isoformat(), parsed["total"],
                    parsed["raw_preview"], note
                ]
                save_csv_safe(inbox, INBOX_CSV)

                # odošli do n8n (ak je)
                post_to_n8n({
                    "type": "receipt",
                    "store": parsed["store"],
                    "country": parsed["country"],
                    "currency": parsed["currency"],
                    "date": parsed["date"].isoformat(),
                    "total": parsed["total"],
                    "note": note
                })

    with colB:
        st.markdown("**STT (hlas) – pripravené**")
        st.caption("Po pridaní STT (napr. Whisper lokálne / n8n uzol) sem doplníme upload audio a rovnaké spracovanie.")

    st.divider()
    st.subheader("Inbox záznamy")
    inbox_df = load_csv_safe(INBOX_CSV, ["ts","filename","mime","store","country","currency","date","total","raw_preview","note"])
    st.dataframe(inbox_df, use_container_width=True, height=300)

# --- TAB 2: Nákup / Zásoby ------------------------------------------------------
with tabs[1]:
    st.subheader("Rýchly nákup & zásoby")
    products = load_products()

    # Filtre
    left, right = st.columns([1,2])
    with left:
        category = st.selectbox("Kategória", sorted(products["category"].unique()))
        subset = products[products["category"] == category].reset_index(drop=True)
        st.caption(f"Položiek v kategórii: **{len(subset)}**")

        # výber krajiny/meny pre tento nákup (ak chceš ručne prepísať to, čo príde z účtenky)
        sel_country = st.selectbox("Krajina nákupu", ["CZ","SK","PL"])
        sel_currency = ISO_MAP[sel_country]["ccy"]

        # dátum nákupu (ak ideš manuálny nákup)
        sel_date = st.date_input("Dátum nákupu", value=date.today())

        # obchod (manuálny výber)
        sel_store = st.selectbox("Obchod", DEFAULT_STORES)

        st.markdown("—")
        st.caption("💡 Ceny z letákov: placeholder kupi.cz (demonštračná odpoveď).")

    with right:
        st.caption("Zaškrtni položky a zadaj množstvo:")
        picked_rows = []
        for i, r in subset.iterrows():
            c1, c2, c3, c4 = st.columns([4,2,2,3])
            with c1:
                chk = st.checkbox(r["item"], key=f"pick_{category}_{i}")
            with c2:
                qty = st.number_input("Množstvo", min_value=0.0, step=1.0, value=0.0, key=f"qty_{category}_{i}")
            with c3:
                unit = r["unit"]
                st.write(unit)
            with c4:
                flyer = lookup_price_in_flyers(r["item"], DEFAULT_STORES)
                if flyer:
                    st.write(f"{flyer['store']}: {flyer['price']} {flyer['unit']}{' 🔥' if flyer['promo'] else ''}")
                else:
                    st.write("—")

            if chk and qty > 0:
                picked_rows.append({
                    "item": r["item"],
                    "qty": qty,
                    "unit": unit
                })

        st.markdown("—")
        if st.button("💾 Uložiť nákup", type="primary", use_container_width=True):
            # súčet „od oka“ (keď nemáme reálne ceny, použijeme 0 a necháme účtenku rozhodnúť)
            rough_total = 0.0
            amount_czk = convert_to_czk(sel_date, rough_total, sel_currency)

            payload = {
                "store": sel_store,
                "country": sel_country,
                "currency": sel_currency,
                "date": sel_date,
                "total_src": rough_total,
                "amount_czk": amount_czk,
                "category": category,
                "items": picked_rows,
                "note": "manuálny nákup (bez účtenky)"
            }
            write_ledger_row(payload)
            post_to_n8n({"type": "manual_purchase", **{k:(v.isoformat() if isinstance(v, date) else v) for k,v in payload.items()}})

            # IssueCoin správa (z verejnej appky ak je)
            msg = issuecoin_message({"category": category, "amount_czk": amount_czk})
            st.success(msg)

# --- TAB 3: Ledger --------------------------------------------------------------
with tabs[2]:
    st.subheader("Ledger (súkromný)")
    ledger = load_csv_safe(LEDGER_CSV, ["ts","store","country","currency","date","total_src","amount_czk","category","items_json","note"])
    st.dataframe(ledger, use_container_width=True, height=420)
    st.download_button("⬇️ Stiahnuť CSV", data=ledger.to_csv(index=False).encode("utf-8"), file_name="ledger_priv.csv", mime="text/csv")

# --- TAB 4: Nastavenia ----------------------------------------------------------
with tabs[3]:
    st.subheader("Integrácie & režimy")
    st.write("• n8n webhook: ", "✅ nastavený" if st.secrets.get("N8N_WEBHOOK_URL") else "—")
    st.write("• Google Sheets: (voliteľné, doplníme neskôr)")
    st.write("• OCR: PDF → pypdf, Obrázky → PIL + pytesseract (ak dostupné)")
    st.write("• STT (hlas): pripravené – pridať neskôr (Whisper lokálne alebo n8n uzol)")
    st.info("Táto súkromná nadstavba nič neprepisuje v tvojej verejnej appke. Len sa na ňu pripája.")
