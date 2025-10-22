import streamlit as st
import pandas as pd
import os
from datetime import datetime

# ----------------------------------------------------------
# 🟦 KONFIGURÁCIA
# ----------------------------------------------------------
st.set_page_config(
    page_title="IssueCoin Private – Nákupný formulár",
    page_icon="🛒",
    layout="centered"
)

# ----------------------------------------------------------
# 🟨 NAČÍTANIE DÁT
# ----------------------------------------------------------
DATA_PATH = "seznam_potravin_apkka.csv"
if not os.path.exists(DATA_PATH):
    st.error("❌ Súbor so zoznamom potravín nebol nájdený.")
else:
    df = pd.read_csv(DATA_PATH)

    st.title("🛍️ IssueCoin Private – Nákupný formulár")
    st.write("Vyber položky, ktoré chceš kúpiť, a zadaj množstvo. Dáta sa automaticky uložia do CSV súboru.")

    # ----------------------------------------------------------
    # 🟩 FORMULÁR
    # ----------------------------------------------------------
    st.subheader("Vyber potraviny na nákup:")

    # Výber kategórie pre lepšiu orientáciu
    categories = sorted(df["kategorie"].dropna().unique())
    selected_category = st.selectbox("Vyber kategóriu:", ["Všetky"] + categories)

    # Filtrovanie podľa kategórie
    if selected_category != "Všetky":
        filtered_df = df[df["kategorie"] == selected_category]
    else:
        filtered_df = df

    selected_items = []
    quantities = {}

    # Zobrazenie položiek s checkboxom a množstvom
    for _, row in filtered_df.iterrows():
        col1, col2 = st.columns([3, 1])
        with col1:
            checkbox = st.checkbox(f"{row['nazev_tovaru']} ({row['druh']})", key=row['nazev_tovaru'])
        with col2:
            qty = st.text_input("Množstvo", value="", key=row['nazev_tovaru'] + "_qty")

        if checkbox:
            selected_items.append(row['nazev_tovaru'])
            quantities[row['nazev_tovaru']] = qty

    # ----------------------------------------------------------
    # 🟦 ULOŽENIE DÁT
    # ----------------------------------------------------------
    if st.button("💾 Uložiť nákupný zoznam"):
        if not selected_items:
            st.warning("⚠️ Nevybral/a si žiadne položky.")
        else:
            save_path = "data"
            os.makedirs(save_path, exist_ok=True)

            file_name = f"data/shopping_list_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

            shopping_data = []
            for item in selected_items:
                row = df[df["nazev_tovaru"] == item].iloc[0].to_dict()
                row["množstvo_zadané"] = quantities.get(item, "")
                shopping_data.append(row)

            pd.DataFrame(shopping_data).to_csv(file_name, index=False, encoding="utf-8-sig")

            st.success(f"✅ Zoznam uložený do súboru: `{file_name}`")
            st.balloons()

    # ----------------------------------------------------------
    # 🟧 PREHĽAD POSLEDNÉHO ULOŽENIA
    # ----------------------------------------------------------
    st.divider()
    st.subheader("🧾 Posledné uložené zoznamy:")

    data_files = sorted(
        [f for f in os.listdir("data") if f.startswith("shopping_list_") and f.endswith(".csv")],
        reverse=True
    )

    if data_files:
        latest_file = os.path.join("data", data_files[0])
        recent_df = pd.read_csv(latest_file)
        st.dataframe(recent_df)
    else:
        st.info("Zatiaľ neexistujú žiadne uložené zoznamy.")
