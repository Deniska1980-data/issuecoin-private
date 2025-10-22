# app.py — IssueCoin Private (OpenShift + n8n + Google Sheets* optional)
# UI: Streamlit (pôvodný denník výdajov + NOVÝ nákupný zoznam a zásoby)
# Externé integrácie: CNB (kurzy), Calendarific (sviatky – voliteľné)
# Autor: DenyP

import os, io, json, time, base64
from datetime import datetime, date
from typing import Dict, List, Optional

import streamlit as st
import pandas as pd
import requests

# ---------- Pomocné: bezpečné čítanie secretov ----------
def _get_secret(key: str, default: str = "") -> str:
    try:
        if "secrets" in dir(st):
            return st.secrets.get(key, default)  # type: ignore
    except Exception:
        pass
    return os.getenv(key, default)

CALENDARIFIC_KEY = _get_secret("CALENDARIFIC_KEY", "")
# napr. "CZ" / "SK" / "PL" – pre sviatky; ak prázdne, modul sa skryje
HOLIDAYS_COUNTRY = _get_secret("HOLIDAYS_COUNTRY", "CZ")

# ---------- Lokálne súbory (CSV) ----------
DATA_DIR = "data"
PURCHASES_CSV = os.path.join(DATA_DIR, "purchases_log.csv")   # denník nákupov
STOCK_CSV     = os.path.join(DATA_DIR, "stock.csv")           # aktuálne zásoby
GROCERIES_CSV = os.path.join(DATA_DIR, "seznam_potravin_appka.csv")  # katalóg
SETTINGS_JSON = os.path.join(DATA_DIR, "settings.json")

os.makedirs(DATA_DIR, exist_ok=True)

def _ensure_csv(path: str, columns: List[str]):
    if not os.path.exists(path):
        pd.DataFrame(columns=columns).to_csv(path, index=False, encoding="utf-8")

_ensure_csv(PURCHASES_CSV, ["date","item","qty","unit","price_total","currency","store","category","note"])
_ensure_csv(STOCK_CSV,     ["item","qty","unit","category","last_update"])
if not os.path.exists(GROCERIES_CSV):
    # ak ešte nemáš CSV, vytvoríme pár položiek – neskôr si ich nahradíš
    demo = pd.DataFrame([
        ["chléb","pečivo","čerstvé potraviny","ks"],
        ["mléko","mléčné výrobky","čerstvé potraviny","l"],
        ["vajíčka","ostatní","čerstvé potraviny","ks"],
        ["tvaroh jemný","mléčné výrobky","čerstvé potraviny","ks"],
        ["cibule","zelenina","trvanlivé - zelenina","ks"],
        ["rýže","ostatní","trvanlivé","kg"],
        ["kuřecí prsa","maso","mrazák","kg"],
    ], columns=["nazev_tovaru","druh","kategorie","jednotka"])
    demo.to_csv(GROCERIES_CSV, index=False, encoding="utf-8")

if not os.path.exists(SETTINGS_JSON):
    with open(SETTINGS_JSON, "w", encoding="utf-8") as f:
        json.dump({
            "preferred_stores": ["Albert","Penny","Lidl","Tesco","DM","Rossmann"],
            "default_currency": "CZK",
            "budget_month_czk": 7000
        }, f, ensure_ascii=False, indent=2)

def load_settings() -> Dict:
    with open(SETTINGS_JSON, "r", encoding="utf-8") as f:
        return json.load(f)

def save_settings(cfg: Dict):
    with open(SETTINGS_JSON, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

# ---------- CNB kurzy (jednoduchý parser denného feedu) ----------
def fetch_cnb_rates() -> pd.DataFrame:
    """
    Zdroj: Denný textový súbor CNB (aktuálny deň). Vráti tabuľku s kódom a kurzom voči CZK.
    """
    url = "https://www.cnb.cz/en/financial_markets/foreign_exchange_market/exchange_rate_fixing/daily.txt"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    lines = r.text.strip().splitlines()
    # prvé dva riadky sú hlavičky
    rows = []
    for ln in lines[2:]:
        parts = ln.split("|")
        if len(parts) >= 5:
            country, currency, amount, code, rate = parts[:5]
            try:
                rows.append([code, float(rate.replace(",", ".")), int(amount)])
            except Exception:
                pass
    df = pd.DataFrame(rows, columns=["code","rate_czk","amount"])
    return df

def convert_to_czk(df_rates: pd.DataFrame, amount: float, code: str) -> float:
    if code.upper() == "CZK":
        return round(amount, 2)
    row = df_rates[df_rates["code"] == code.upper()]
    if row.empty:  # keď nepoznáme menu, necháme pôvodnú sumu
        return round(amount, 2)
    rate = float(row.iloc[0]["rate_czk"])
    nominal = float(row.iloc[0]["amount"])
    return round(amount * (rate / nominal), 2)

# ---------- Calendarific (voliteľné) ----------
def fetch_holidays(country_code: str, year: int) -> pd.DataFrame:
    if not CALENDARIFIC_KEY:
        return pd.DataFrame(columns=["date","name","type"])
    url = "https://calendarific.com/api/v2/holidays"
    params = {"api_key": CALENDARIFIC_KEY, "country": country_code, "year": year}
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()["response"]["holidays"]
    rows = []
    for h in data:
        rows.append([h["date"]["iso"], h["name"], ", ".join(h.get("type", []))])
    return pd.DataFrame(rows, columns=["date","name","type"])

# ---------- I/O helpers ----------
def read_csv(path: str) -> pd.DataFrame:
    try:
        return pd.read_csv(path, encoding="utf-8")
    except UnicodeDecodeError:
        return pd.read_csv(path, encoding="utf-8-sig")

def write_csv(df: pd.DataFrame, path: str):
    df.to_csv(path, index=False, encoding="utf-8")

# ---------- Streamlit UI ----------
st.set_page_config(page_title="IssueCoin – Private", page_icon="💰", layout="wide")
cfg = load_settings()

st.title("💰 Výdavkový denník / IssueCoin – Private")
st.caption("CZK = vždy 1:1. Ostatné meny sa prepočítajú podľa denných kurzov ČNB. Údaje sa ukladajú **lokálne** do CSV (GDPR friendly).")

tabs = st.tabs(["🧾 Denník", "🛒 Nákupný zoznam", "📦 Zásoby", "📅 Sviatky & FX", "⚙️ Nastavenia"])

# ---------- TAB 1: Denník výdajov ----------
with tabs[0]:
    col1, col2, col3 = st.columns([1,1,2])
    with col1:
        d = st.date_input("📅 Dátum nákupu", value=date.today())
    with col2:
        currency = st.selectbox("Mena", ["CZK","EUR","USD","PLN","GBP"], index=["CZK","EUR","USD","PLN","GBP"].index(cfg.get("default_currency","CZK")))
    with col3:
        store = st.text_input("Obchod / miesto", value="")

    item = st.text_input("Položka", value="")
    qty = st.number_input("Množstvo", min_value=0.0, value=1.0, step=1.0)
    unit = st.text_input("Jednotka", value="ks")
    price = st.number_input("Suma (v zvolenej mene)", min_value=0.0, value=0.0, step=1.0)
    category = st.text_input("Kategória", value="")
    note = st.text_area("Poznámka", height=60, value="")

    if st.button("➕ Pridať do denníka", type="primary"):
        # prepočet na CZK
        try:
            rates = fetch_cnb_rates()
        except Exception:
            rates = pd.DataFrame(columns=["code","rate_czk","amount"])
        czk = convert_to_czk(rates, price, currency)
        log = read_csv(PURCHASES_CSV)
        new_row = {
            "date": d.isoformat(), "item": item, "qty": qty, "unit": unit,
            "price_total": price, "currency": currency, "store": store,
            "category": category, "note": note
        }
        log = pd.concat([log, pd.DataFrame([new_row])], ignore_index=True)
        write_csv(log, PURCHASES_CSV)
        st.success(f"Pridané: {item} • {qty} {unit} za {price} {currency} (~{czk} CZK).")
        # aktualizácia zásob (light)
        stock = read_csv(STOCK_CSV)
        if item.strip():
            idx = stock.index[stock["item"].str.lower()==item.strip().lower()]
            if len(idx):
                stock.loc[idx, "qty"] = stock.loc[idx, "qty"].astype(float) + qty
                stock.loc[idx, "last_update"] = datetime.now().isoformat(timespec="seconds")
            else:
                stock = pd.concat([stock, pd.DataFrame([{
                    "item": item, "qty": qty, "unit": unit or "ks",
                    "category": category, "last_update": datetime.now().isoformat(timespec="seconds")
                }])], ignore_index=True)
            write_csv(stock, STOCK_CSV)

    st.divider()
    st.subheader("Posledné nákupy")
    log = read_csv(PURCHASES_CSV)
    st.dataframe(log.tail(20), use_container_width=True)

# ---------- TAB 2: Nákupný zoznam (editor) ----------
with tabs[1]:
    st.write("Vyber položky z katalógu a zadaj množstvo. Klikni **Uložiť**.")
    catalog = read_csv(GROCERIES_CSV).rename(columns={
        "nazev_tovaru":"item", "jednotka":"unit", "kategorie":"category"
    })
    if "selected_rows" not in st.session_state:
        catalog["add"] = False
        catalog["qty"] = 0.0
        st.session_state.selected_rows = catalog
    # ak by CSV medzičasom pribudol, zosúladíme schému
    cat = read_csv(GROCERIES_CSV).rename(columns={
        "nazev_tovaru":"item", "jednotka":"unit", "kategorie":"category"
    })
    for col in ["add","qty"]:
        if col not in cat.columns:
            cat[col] = False if col=="add" else 0.0
    st.session_state.selected_rows = cat

    edited = st.data_editor(
        st.session_state.selected_rows,
        key="catalog_editor",
        use_container_width=True,
        num_rows="dynamic",
        column_config={
            "add": st.column_config.CheckboxColumn("Vybrať", help="Zaškrtni pre nákup"),
            "qty": st.column_config.NumberColumn("Množstvo", min_value=0.0, step=1.0),
            "item": "Položka", "unit": "Jednotka", "category": "Kategória", "druh": "Druh"
        }
    )

    colA, colB = st.columns([1,1])
    with colA:
        chosen_store = st.selectbox("Obchod", cfg["preferred_stores"], index=0)
    with colB:
        price_est = st.number_input("Odhad ceny spolu (CZK, voliteľné)", min_value=0.0, value=0.0, step=1.0)

    if st.button("💾 Uložiť výber → Denník + Zásoby", type="primary"):
        to_buy = edited[(edited["add"]==True) & (edited["qty"]>0)]
        if to_buy.empty:
            st.warning("Nezvolila si žiadne položky.")
        else:
            # zápis do denníka
            log = read_csv(PURCHASES_CSV)
            rows = []
            for _, r in to_buy.iterrows():
                rows.append({
                    "date": date.today().isoformat(),
                    "item": r["item"], "qty": float(r["qty"]),
                    "unit": r.get("unit","ks"), "price_total": 0.0,
                    "currency": "CZK", "store": chosen_store,
                    "category": r.get("category",""), "note": "shopping-list"
                })
            log = pd.concat([log, pd.DataFrame(rows)], ignore_index=True)
            write_csv(log, PURCHASES_CSV)

            # aktualizácia zásob
            stock = read_csv(STOCK_CSV)
            for _, r in to_buy.iterrows():
                name = str(r["item"]).strip()
                idx = stock.index[stock["item"].str.lower()==name.lower()]
                if len(idx):
                    stock.loc[idx, "qty"] = stock.loc[idx, "qty"].astype(float) + float(r["qty"])
                    stock.loc[idx, "last_update"] = datetime.now().isoformat(timespec="seconds")
                else:
                    stock = pd.concat([stock, pd.DataFrame([{
                        "item": name, "qty": float(r["qty"]), "unit": r.get("unit","ks"),
                        "category": r.get("category",""),
                        "last_update": datetime.now().isoformat(timespec="seconds")
                    }])], ignore_index=True)
            write_csv(stock, STOCK_CSV)
            st.success(f"Uložené {len(to_buy)} položiek do denníka a zásob. ✅")

    st.caption("Tip: Katalóg upravíš priamo v súbore `data/seznam_potravin_appka.csv`.")

# ---------- TAB 3: Zásoby ----------
with tabs[2]:
    st.write("Uprav zásoby a stlač **Uložiť**.")
    stock = read_csv(STOCK_CSV)
    stock["qty"] = stock.get("qty", 0).astype(float)
    edited_stock = st.data_editor(
        stock, num_rows="dynamic", use_container_width=True,
        column_config={"qty": st.column_config.NumberColumn("Množstvo", step=1.0, min_value=0.0)}
    )
    col1, col2 = st.columns([1,1])
    if col1.button("💾 Uložiť zásoby"):
        edited_stock["last_update"] = datetime.now().isoformat(timespec="seconds")
        write_csv(edited_stock, STOCK_CSV)
        st.success("Zásoby uložené.")
    if col2.button("🧹 Vyčistiť nulové položky"):
        cleaned = edited_stock[edited_stock["qty"]>0]
        write_csv(cleaned, STOCK_CSV)
        st.success("Odstránené položky s množstvom 0.")

    st.divider()
    st.subheader("Denník (posledných 50)")
    st.dataframe(read_csv(PURCHASES_CSV).tail(50), use_container_width=True)

# ---------- TAB 4: Sviatky & FX ----------
with tabs[3]:
    left, right = st.columns(2)
    with left:
        st.subheader("💱 Kurzy ČNB (dnes)")
        try:
            rates = fetch_cnb_rates()
            st.dataframe(rates, use_container_width=True, height=320)
        except Exception as e:
            st.error(f"Nepodarilo sa načítať kurzy ČNB: {e}")

    with right:
        st.subheader("📅 Štátne sviatky")
        if CALENDARIFIC_KEY:
            cc = st.text_input("Krajina (ISO2)", value=HOLIDAYS_COUNTRY)
            yr = st.number_input("Rok", min_value=2000, max_value=2100, value=date.today().year, step=1)
            if st.button("Načítať sviatky"):
                try:
                    hol = fetch_holidays(cc, int(yr))
                    st.dataframe(hol, use_container_width=True, height=320)
                except Exception as e:
                    st.error(f"Chyba pri načítaní sviatkov: {e}")
        else:
            st.info("Calendarific API kľúč nie je nastavený. (voliteľné)")

# ---------- TAB 5: Nastavenia ----------
with tabs[4]:
    st.subheader("Preferované obchody")
    stores_txt = st.text_input(
        "Zoznam (čiarkami):",
        value=", ".join(cfg.get("preferred_stores", ["Albert","Penny","Lidl","Tesco","DM","Rossmann"]))
    )
    st.subheader("Mesačný rozpočet (CZK)")
    budget = st.number_input("Limit na potraviny + drogériu", min_value=0.0,
                             value=float(cfg.get("budget_month_czk", 7000)), step=100.0)
    st.subheader("Predvolená mena")
    def_curr = st.selectbox("Mena", ["CZK","EUR","USD","PLN","GBP"],
                            index=["CZK","EUR","USD","PLN","GBP"].index(cfg.get("default_currency","CZK")))

    if st.button("💾 Uložiť nastavenia"):
        cfg["preferred_stores"] = [s.strip() for s in stores_txt.split(",") if s.strip()]
        cfg["budget_month_czk"] = float(budget)
        cfg["default_currency"] = def_curr
        save_settings(cfg)
        st.success("Nastavenia uložené.")

# ---------- Footer: IssueCoin agent (pôvodné „hlášky“) ----------
st.markdown(
    """
    <div style='margin-top:2rem; color:#555'>
    🧠 <b>IssueCoin Agent:</b> držím limit <b>{budget} CZK/mes.</b>. 
    Nakupuj chytro, sledujem akcie (Albert, Penny, Lidl, Tesco, DM, Rossmann) a ukladám denník aj zásoby. 
    </div>
    """.format(budget=int(cfg.get("budget_month_czk",7000))), unsafe_allow_html=True
)

