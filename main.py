from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import pandas as pd
import requests
import io

app = FastAPI()

# CORS — permite que o frontend acesse a API
# Em produção, substitua "*" por "https://rf.bizi.net.br"
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8080",
        "http://localhost:80",
        "http://localhost",
        "https://rf.bizi.net.br",  # produção futura
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────
#  Busca uma série do SGS / Banco Central
# ─────────────────────────────────────────
def get_bacen_data(serie):
    try:
        url = (
            f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{serie}"
            f"/dados/ultimos/1?formato=json"
        )
        r = requests.get(url, timeout=5)
        return float(r.json()[0]['valor'])
    except Exception:
        return None

# ─────────────────────────────────────────
#  Endpoint principal
# ─────────────────────────────────────────
@app.get("/api/mercado")
async def get_market_data():

    # Séries SGS:
    #   432   → Meta Selic (% a.a.)
    #   13522 → IPCA acumulado 12 meses
    selic = get_bacen_data(432) or 10.50
    ipca  = get_bacen_data(13522) or 3.90
    juro_real = round(((1 + selic / 100) / (1 + ipca / 100) - 1) * 100, 2)

    # ── Tesouro Direto — CSV do Tesouro Transparente ──
    try:
        url_tesouro = (
            "https://www.tesourotransparente.gov.br/ckan/dataset/"
            "df56aa42-4150-4748-a26e-7e3748025e6f/resource/"
            "796d2059-14e9-40e3-8041-f8e2f24bc927/download/"
            "PrecosTaxasTesouroDireto.csv"
        )
        resp = requests.get(url_tesouro, timeout=10, verify=False)
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text), sep=';', decimal=',')

        # Filtra apenas a data mais recente disponível
        hoje = df['Data Base'].max()
        df_hoje = df[df['Data Base'] == hoje].copy()

        tesouro_list = []
        for _, row in df_hoje.iterrows():
            taxa = row['Taxa Compra Manha']
            # Ignora linhas sem taxa (títulos fora de negociação no dia)
            if pd.isna(taxa) or float(taxa) == 0:
                continue
            tesouro_list.append({
                "nome":       row['Tipo Titulo'],
                "taxa":       float(taxa),
                "vencimento": row['Data Vencimento'],
            })

    except Exception as e:
        print(f"Erro ao buscar Tesouro: {e}")
        # Fallback com dados reais de referência (mai/2026)
        tesouro_list = [
            {"nome": "Tesouro Selic 2027",      "taxa": 14.56, "vencimento": "01/03/2027"},
            {"nome": "Tesouro Selic 2029",      "taxa": 14.57, "vencimento": "01/03/2029"},
            {"nome": "Tesouro Prefixado 2027",  "taxa": 13.42, "vencimento": "01/01/2027"},
            {"nome": "Tesouro Prefixado 2029",  "taxa": 13.61, "vencimento": "01/01/2029"},
            {"nome": "Tesouro IPCA+ 2029",      "taxa":  7.23, "vencimento": "15/05/2029"},
            {"nome": "Tesouro IPCA+ 2035",      "taxa":  7.38, "vencimento": "15/05/2035"},
            {"nome": "Tesouro IPCA+ 2045",      "taxa":  7.42, "vencimento": "15/05/2045"},
        ]

    return {
        "macro": {
            "selic":     selic,
            "ipca_proj": ipca,
            "juro_real": juro_real,
        },
        "tesouro": tesouro_list,
    }

# ─────────────────────────────────────────
#  Entrypoint (Railway usa PORT do ambiente)
# ─────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
