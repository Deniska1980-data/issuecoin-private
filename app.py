# app.py — IssueCoin Private (OpenShift + n8n + Google Sheets) — by DenyP
# Streamlit UI pre nákupný zoznam, účtenky (OCR), zásoby a prehľad s rozpočtom.
# Bez OpenAI. Integrácie: n8n (OCR, deals, MCP), Google Sheets (pamäť) — voliteľné.

import os, io, json, time, base64, datetime as dt
from typing import List, Dict, Any, Optional

import streamlit as st
import pandas as pd
import altair as alt
import requests

# --- Konfigurácia z secrets / env ---
SECRETS = st.secrets if "secrets" in dir(st) else {}
N8N_WEBHOOK_OCR      = SECRETS.get("N8N_WEBHOOK_OCR",      os.getenv("N8N_WEBHOOK_OCR", ""))        # napr. https://n8n.../webhook/ocr
N8N_WEBHOOK_DEALS    = SECRETS.get("N8N_WEBHOOK_DEALS",    os.getenv("N8N_WEBHOOK_DEALS", ""))      # napr. https://n8n.../webhook/deals
N8N_WEBHOOK_MCP      = SECRETS.get("N8N_WEBHOOK_MCP",      os.getenv("N8N_WEBHOOK_MCP", ""))        # napr. https://n8n.../webhook/mcp
GSPREAD_ENABLED      = SECRETS.get("GSPREAD_ENABLED",      os.getenv("GSPREAD_ENABLED", "false")).lower() == "true"
GSHEETS_SPREADSHEET  = SECRETS.get("GSHEETS_SPREADSHEET",  os.getenv("GSHEETS_SPREADSHEET", ""))    # Spreadsheet ID
GDRIVE_FOLDER_ID     = SECRETS.get("GDRIVE_FOLDER_ID",     os.getenv("GDRIVE_FOLDER_ID", ""))       # voliteľné, ak chceš ukladať prílohy
LOCATION_ADDRESS     = SECRETS.get("LOCATION_ADDRESS",     os.getenv("LOCATION_ADDRESS", "Pod Terebkou 15/4, Praha"))
PREFERRED_STORES     = json.loads(SECRETS.get("PREFERRED_STORES", os.getenv("PREFERRED_STORES", '["Albert","Penny","Lidl","Tesco","DM","Rossmann"]')))

# Lokálne cesty (fallback / offline)
DATA_DIR         = os.getenv("DATA_DIR", "data")
INVENTORY_CSV    = os.path.join(DATA_DIR, "inventory.csv")     # zoznam položiek (kategória, položka)
LOG_CSV          = os.path.join(DATA_DIR, "purchases_log.csv") # denník nákupov
STOCK_CSV        = os.path.join(DATA_DIR, "stock.csv")         # aktuálne zásoby
BUDGET_DEFAULT   = 7000

# --- Pomocné: zaisti priečinky a súbory ---
os.makedirs(DATA_DIR, exist_ok=True)

def _init_csv(path: str, columns: List[str]):
    if not os.path.exists(path):
        pd.DataFrame(columns=columns).to_csv(path, index=False)

_init_csv(LOG_CSV,   ["date","store","item","qty","unit","price_total","category","note"])
_init_csv(STOCK_CSV, ["item","category","qty","unit","last_update"])
if not os.path.exists(INVENTORY_CSV):
    # ukážkový inventár (môžeš neskôr nahradiť vlastným z Excelu)
    demo = pd.DataFrame([
        ["Mlieko 1,5%", "Mlieko", "ks"],
        ["Vajcia L", "Vajcia", "ks"],
        ["Mrkva", "Zelenina", "ks"],
        ["Pór", "Zelenina", "ks"],
        ["Tvaroh jemný", "Mliečne", "ks"],
        ["Cestoviny", "Suché", "ks"],
        ["Mleté mäso", "Mäso", "kg"],
        ["Chlieb", "Pečivo", "ks"],
        ["Toaletný papier", "Drogéria", "bal"],
        ["Prací gél", "Drogéria", "ks"],
    ], columns=["item","category","unit"])
    demo.to_csv(INVENTORY_CSV, index=False)

# --- Cache načítania dát ---
@st.cache_data(ttl=120)
def load_inventory() -> pd.DataFrame:
    df = pd.read_csv(INVENTORY_CSV)
    return df

@st.cache_data(ttl=60)
def load_log() -> pd.DataFrame:
    df = pd.read_csv(LOG_CSV)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
    return df

@st.cache_data(ttl=60)
def load_stock() -> pd.DataFrame:
    df = pd.read_csv(STOCK_CSV)
    return df

def save_log_row(row: Dict[str, Any]):
    df = load_log()
    df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    df.to_csv(LOG_CSV, index=False)
    load_log.clear()  # invalidate cache

def upsert_stock(item: str, category: str, qty: float, unit: str):
    df = load_stock()
    mask = df["item"].astype(str).str.lower() == item.lower()
    now = dt.datetime.now().isoformat(timespec="seconds")
    if mask.any():
        idx = df[mask].index[0]
        try:
            base_qty = float(df.at[idx,"qty"]) if pd.notna(df.at[idx,"qty"]) else 0.0
        except:
            base_qty = 0.0
        df.at[idx,"qty"] = base_qty + qty
        df.at[idx,"last_update"] = now
    else:
        df = pd.concat([df, pd.DataFrame([{
            "item": item, "category": category, "qty": qty, "unit": unit, "last_update": now
        }])], ignore_index=True)
    df.to_csv(STOCK_CSV, index=False)
    load_stock.clear()

# --- Integrácie: n8n webhooky (OCR, deals, MCP) ---
def n8n_post(url: str, payload: Dict[str, Any], files: Optional[Dict[str, Any]]=None, timeout=60) -> Dict[str,Any]:
    if not url:
        return {"ok": False, "error": "N8N webhook not configured"}
    try:
        if files:
            r = requests.post(url, data=payload, files=files, timeout=timeout)
        else:
            r = requests.post(url, json=payload, timeout=timeout)
        r.raise_for_status()
        return {"ok": True, "data": r.json() if "application/json" in r.headers.get("Content-Type","") else r.text}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def send_receipt_to_ocr(file_bytes: bytes, filename: str, store_hint: str="") -> Dict[str,Any]:
    files = {"file": (filename, file_bytes)}
    payload = {"store_hint": store_hint}
    return n8n_post(N8N_WEBHOOK_OCR, payload, files=files)

def ask_deals(query_items: List[Dict[str,Any]]) -> Dict[str,Any]:
    # query_items: [{"item":"Mlieko 1,5%","qty":2,"unit":"ks","category":"Mlieko"}]
    payload = {
        "address": LOCATION_ADDRESS,
        "stores": PREFERRED_STORES,
        "items": query_items
    }
    return n8n_post(N8N_WEBHOOK_DEALS, payload)

def ask_mcp_plan(context: Dict[str,Any]) -> Dict[str,Any]:
    # MCP reasoning: „čo navrhuješ uvariť / čo nakúpiť podľa zásob, limitu, sezóny?“
    return n8n_post(N8N_WEBHOOK_MCP, context)

# --- UI ---
st.set_page_config(page_title="IssueCoin – Private", page_icon="💰", layout="wide")
st.title("💰 IssueCoin — Private (OpenShift + n8n + Google)")

with st.sidebar:
    st.markdown("### Nastavenia")
    month_budget = st.number_input("Mesačný limit (potraviny + drogéria) [CZK]", min_value=1000, max_value=20000, value=BUDGET_DEFAULT, step=100)
    daily_target = st.slider("Denný cieľ (raňajky+večera) [CZK]", 60, 180, 120, 5)
    st.write("Preferované obchody:")
    _ = st.multiselect("Obchody", options=["Albert","Penny","Lidl","Tesco","DM","Rossmann","Billa","Kaufland"], default=PREFERRED_STORES, key="stores_sel")

    st.info(f"📍 Lokalita: {LOCATION_ADDRESS}")
    st.caption("Integrácie: n8n (OCR, Deals, MCP), Google Sheets (voliteľné)")

tabs = st.tabs(["🛒 Nákupný zoznam", "🧾 Účtenka (OCR)", "📦 Zásoby", "📈 Prehľad", "🧠 Plán (MCP)"])

# --- Tab: Nákupný zoznam ---
with tabs[0]:
    st.subheader("🛒 Nákupný zoznam (checklist + množstvá)")
    inv = load_inventory().copy()
    categories = ["Všetko"] + sorted(inv["category"].dropna().unique().tolist())
    cat = st.selectbox("Filtrovať kategóriu", categories, index=0)
    if cat != "Všetko":
        inv = inv[inv["category"] == cat]

    # Checklist s množstvami
    inv["pick"] = False
    picked = []
    st.write("Zaškrtni položky a nastav množstvá:")
    for i, row in inv.iterrows():
        cols = st.columns([0.06, 0.64, 0.15, 0.15])
        with cols[0]:
            chk = st.checkbox("", key=f"chk_{i}")
        with cols[1]:
            st.write(f"**{row['item']}** · {row['category']}")
        with cols[2]:
            qty = st.number_input("Množ.", min_value=0.0, value=0.0, step=1.0, key=f"qty_{i}")
        with cols[3]:
            unit = row.get("unit", "ks")
            st.write(unit)

        if chk and qty > 0:
            picked.append({"item": row["item"], "qty": qty, "unit": row.get("unit","ks"), "category": row["category"]})

    st.divider()
    colA, colB, colC = st.columns([0.4,0.3,0.3])
    with colA:
        if st.button("🔎 Nájsť akcie v preferovaných obchodoch (n8n → kupi.cz)"):
            if not picked:
                st.warning("Najprv zaškrtni aspoň jednu položku s množstvom.")
            else:
                resp = ask_deals(picked)
                if resp.get("ok"):
                    st.success("Hotovo. Nižšie je návrh nákupného koša v akciách:")
                    st.json(resp["data"])
                else:
                    st.error(f"Deals API chyba: {resp.get('error')}")
    with colB:
        if st.button("📝 Exportovať zoznam do CSV (lokálne)"):
            if not picked:
                st.warning("Najprv vyber položky.")
            else:
                out = pd.DataFrame(picked)
                csv = out.to_csv(index=False).encode("utf-8")
                st.download_button("⬇️ Stiahnuť nákupný zoznam CSV", data=csv, file_name=f"nakupny_zoznam_{dt.date.today()}.csv", mime="text/csv")
    with colC:
        st.caption("Tip: Microsoft Forms môžeš použiť na rýchly prototyp checklistu. Po overení presuň vstup do Streamlit.")

# --- Tab: Účtenka (OCR) ---
with tabs[1]:
    st.subheader("🧾 Spracovanie účtenky (foto/PDF → OCR → denník + zásoby)")
    store_hint = st.text_input("Názov obchodu (pomôcka pre OCR)", value="")
    up = st.file_uploader("Nahraj fotku/PDF účtenky", type=["png","jpg","jpeg","pdf"])
    if up is not None:
        b = up.read()
        st.image(b, caption="Nahrané", use_column_width=True) if up.type.startswith("image/") else st.info("Súbor nahraný (PDF).")
        if st.button("🔠 Spustiť OCR v n8n"):
            resp = send_receipt_to_ocr(b, up.name, store_hint=store_hint)
            if resp.get("ok"):
                st.success("OCR hotové. Návrh položiek:")
                o = resp["data"]
                st.json(o)
                # Očakávaný formát z n8n: {"items":[{"item":"Mlieko","qty":2,"unit":"ks","price_total":39.8,"category":"Mlieko"}], "date":"2025-10-25","store":"Albert"}
                items = o.get("items", [])
                store = o.get("store", store_hint or "Neznámy")
                rdate = o.get("date", dt.date.today().isoformat())
                if items:
                    if st.button("✅ Zapísať do denníka a zásob"):
                        for it in items:
                            save_log_row({
                                "date": rdate,
                                "store": store,
                                "item": it.get("item",""),
                                "qty": it.get("qty",0),
                                "unit": it.get("unit","ks"),
                                "price_total": it.get("price_total",0),
                                "category": it.get("category",""),
                                "note": "OCR"
                            })
                            upsert_stock(
                                item=it.get("item",""),
                                category=it.get("category",""),
                                qty=float(it.get("qty",0)),
                                unit=it.get("unit","ks")
                            )
                        st.success("Zapísané 👍 (denník + zásoby)")
                else:
                    st.warning("OCR nevrátilo položky. Skús iný záber/kontrast.")
            else:
                st.error(f"OCR chyba: {resp.get('error')}")

# --- Tab: Zásoby ---
with tabs[2]:
    st.subheader("📦 Aktuálne zásoby")
    stock = load_stock()
    st.dataframe(stock, use_container_width=True)
    st.caption("Tip: Sem sa pripisujú množstvá z účteniek (OCR) aj manuálnych úprav.")

    with st.expander("➕ Ručná úprava zásob"):
        col1, col2, col3, col4 = st.columns([0.35,0.25,0.2,0.2])
        with col1: item = st.text_input("Položka")
        with col2: category = st.text_input("Kategória")
        with col3: qty = st.number_input("Množstvo", min_value=-999.0, value=1.0, step=1.0)
        with col4: unit = st.text_input("Jednotka", value="ks")
        if st.button("💾 Uložiť do zásob"):
            if item.strip():
                upsert_stock(item=item.strip(), category=category.strip(), qty=qty, unit=unit.strip())
                st.success("Zásoby upravené.")
            else:
                st.warning("Zadaj názov položky.")

# --- Tab: Prehľad ---
with tabs[3]:
    st.subheader("📈 Prehľad výdavkov")
    df = load_log()
    if df.empty:
        st.info("Zatiaľ žiadne nákupy v denníku.")
    else:
        df["month"] = df["date"].dt.to_period("M").astype(str)
        month_sel = st.selectbox("Mesiac", sorted(df["month"].unique()), index=len(df["month"].unique())-1)
        dff = df[df["month"] == month_sel]
        total = dff["price_total"].fillna(0).sum()
        st.metric("Mesačný súčet [CZK]", f"{total:,.0f}".replace(",", " "))
        st.progress(min(total / month_budget, 1.0))

        by_cat = dff.groupby("category", dropna=False)["price_total"].sum().reset_index().fillna({"category":"(nezaradené)"})
        chart = alt.Chart(by_cat).mark_bar().encode(
            x=alt.X("price_total:Q", title="Suma CZK"),
            y=alt.Y("category:N", sort="-x", title="Kategória"),
            tooltip=["category","price_total"]
        )
        st.altair_chart(chart, use_container_width=True)

        st.download_button("⬇️ Export denníka (CSV)", data=dff.to_csv(index=False), file_name=f"dennik_{month_sel}.csv", mime="text/csv")

# --- Tab: MCP plánovanie ---
with tabs[4]:
    st.subheader("🧠 Plán od agenta (MCP)")
    st.caption("Agent vyhodnotí zásoby, rozpočet a preferované obchody a navrhne týždenný plán.")
    want_plan = st.button("🧠 Požiadať MCP agenta o plán")
    if want_plan:
        context = {
            "budget_month": month_budget,
            "budget_daily": daily_target,
            "stocks": load_stock().to_dict(orient="records"),
            "preferred_stores": st.session_state.get("stores_sel", PREFERRED_STORES),
            "address": LOCATION_ADDRESS,
            "season": {"month": dt.date.today().month},  # pre sezónne tipy
            "restrictions": {
                "no_sour_raw": True,          # kyslé veci surové – nie
                "mustard_cooked_only": True,  # horčica len v tepelne upravenom jedle
                "no_mayo_tartar": True,
                "mild_spicy_ok": True
            }
        }
        resp = ask_mcp_plan(context)
        if resp.get("ok"):
            st.success("Plán hotový ✔︎")
            st.json(resp["data"])
        else:
            st.error(f"MCP chyba: {resp.get('error')}")

# --- Voliteľná podpora Google Sheets (ak zapneš v secrets) ---
if GSPREAD_ENABLED and GSHEETS_SPREADSHEET:
    st.sidebar.markdown("---")
    st.sidebar.caption("Google Sheets sync je zapnutý (pozri n8n/cron na pravidelnú synchronizáciu).")
else:
    st.sidebar.markdown("---")
    st.sidebar.caption("Google Sheets sync je vypnutý. Používam lokálne CSV (GDPR-friendly).")

st.sidebar.success("Hotovo. Tento build je pripravený na Docker/OpenShift nasadenie.")
