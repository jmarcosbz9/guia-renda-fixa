from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime
import pandas as pd
import requests
import io

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────
#  CACHE em memória
#  Atualizado 1x/dia pelo cron às 10:30
#  (horário de Brasília = UTC-3 → 13:30 UTC)
# ─────────────────────────────────────────
cache = {
    "tesouro":      [],
    "atualizado_em": None,
}

TESOURO_FALLBACK = [
    {"nome": "Tesouro Selic 2027",     "taxa": 14.56, "vencimento": "01/03/2027"},
    {"nome": "Tesouro Selic 2029",     "taxa": 14.57, "vencimento": "01/03/2029"},
    {"nome": "Tesouro Prefixado 2027", "taxa": 13.42, "vencimento": "01/01/2027"},
    {"nome": "Tesouro Prefixado 2029", "taxa": 13.61, "vencimento": "01/01/2029"},
    {"nome": "Tesouro IPCA+ 2029",     "taxa":  7.23, "vencimento": "15/05/2029"},
    {"nome": "Tesouro IPCA+ 2035",     "taxa":  7.38, "vencimento": "15/05/2035"},
    {"nome": "Tesouro IPCA+ 2045",     "taxa":  7.42, "vencimento": "15/05/2045"},
]

# ─────────────────────────────────────────
#  Busca Selic / IPCA no SGS do Banco Central
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
#  Busca CSV do Tesouro e atualiza o cache
#  Chamado na inicialização e pelo cron
# ─────────────────────────────────────────
def atualizar_tesouro():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Atualizando dados do Tesouro...")
    try:
        url_csv = (
            "https://www.tesourotransparente.gov.br/ckan/dataset/"
            "df56aa42-4150-4748-a26e-7e3748025e6f/resource/"
            "796d2059-14e9-40e3-8041-f8e2f24bc927/download/"
            "PrecosTaxasTesouroDireto.csv"
        )
        resp = requests.get(url_csv, timeout=15, verify=False)
        resp.raise_for_status()

        df = pd.read_csv(io.StringIO(resp.text), sep=';', decimal=',')
        data_max = df['Data Base'].max()
        df_hoje  = df[df['Data Base'] == data_max].copy()

        lista = []
        for _, row in df_hoje.iterrows():
            taxa = row['Taxa Compra Manha']
            if pd.isna(taxa) or float(taxa) == 0:
                continue
            lista.append({
                "nome":       row['Tipo Titulo'],
                "taxa":       float(taxa),
                "vencimento": row['Data Vencimento'],
            })

        if lista:
            cache["tesouro"]      = lista
            cache["atualizado_em"] = datetime.now().strftime("%d/%m/%Y %H:%M")
            print(f"  ✓ {len(lista)} títulos carregados. Base: {data_max}")
        else:
            print("  ✗ CSV vazio — mantendo cache anterior.")

    except Exception as e:
        print(f"  ✗ Erro ao buscar Tesouro: {e} — mantendo cache anterior.")
        if not cache["tesouro"]:
            cache["tesouro"] = TESOURO_FALLBACK
            cache["atualizado_em"] = "fallback"

# ─────────────────────────────────────────
#  CRON — roda todo dia útil às 10:30 BRT
#  Railway roda em UTC → 13:30 UTC
# ─────────────────────────────────────────
scheduler = BackgroundScheduler(timezone="America/Sao_Paulo")
scheduler.add_job(
    atualizar_tesouro,
    CronTrigger(day_of_week="mon-fri", hour=10, minute=30,
                timezone="America/Sao_Paulo"),
)
scheduler.start()

# Busca imediata na inicialização do servidor
atualizar_tesouro()

# ─────────────────────────────────────────
#  Endpoint principal — resposta instantânea
# ─────────────────────────────────────────
@app.get("/api/mercado")
async def get_market_data():

    # Selic e IPCA: leves, buscados ao vivo (< 200ms cada)
    selic     = get_bacen_data(432)   or 14.75
    ipca      = get_bacen_data(13522) or 4.39
    juro_real = round(((1 + selic / 100) / (1 + ipca / 100) - 1) * 100, 2)

    return {
        "macro": {
            "selic":        selic,
            "ipca_proj":    ipca,
            "juro_real":    juro_real,
        },
        "tesouro":       cache["tesouro"],
        "cache_em":      cache["atualizado_em"],
    }

# ─────────────────────────────────────────
#  Entrypoint
# ─────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
