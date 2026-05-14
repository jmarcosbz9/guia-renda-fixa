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

    # ── Fonte 1: CSV de títulos disponíveis para INVESTIR hoje ──
    # Contém APENAS os títulos em negociação neste momento
    try:
        url_investir = (
            "https://www.tesourodireto.com.br/documents/d/guest/"
            "rendimento-investir-csv?download=true"
        )
        resp = requests.get(url_investir, timeout=10,
                            headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()

        linhas = resp.text.strip().split('\n')
        lista = []
        for linha in linhas[1:]:
            partes = linha.strip().split(';')
            if len(partes) < 3:
                continue
            nome = partes[0].strip().strip('"')
            rent = partes[1].strip().strip('"')
            venc = partes[-1].strip().strip('"').strip()
            # Extrai só o número da rentabilidade (ex: "IPCA + 7,23%" → 7.23)
            try:
                num = rent.replace('%', '').split('+')[-1].strip().replace(',', '.')
                taxa = float(num)
            except Exception:
                continue
            if not nome or taxa == 0:
                continue
            if not nome.lower().startswith('tesouro'):
                nome = f"Tesouro {nome}"
            lista.append({"nome": nome, "taxa": taxa, "vencimento": venc})

        if lista:
            cache["tesouro"]       = lista
            cache["atualizado_em"] = datetime.now().strftime("%d/%m/%Y %H:%M")
            print(f"  ✓ {len(lista)} títulos disponíveis para investir.")
            return
        else:
            print("  ✗ CSV investir vazio — tentando mirror.")

    except Exception as e:
        print(f"  ✗ Erro no CSV investir: {e} — tentando mirror.")

    # ── Fonte 2: Mirror radaropcoes (espelha API oficial B3/TD) ──
    try:
        resp = requests.get("https://api.radaropcoes.com/bonds.json",
                            timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        bonds = resp.json().get("response", {}).get("TrsrBdTradgList", [])
        lista = []
        for item in bonds:
            bond = item.get("TrsrBd", {})
            nome = bond.get("nm", "")
            taxa = bond.get("anulInvstmtRate", 0)
            venc = bond.get("mtrtyDt", "")[:10]
            if not nome or not taxa:
                continue
            if "-" in venc:
                y, m, d = venc.split("-")
                venc = f"{d}/{m}/{y}"
            lista.append({"nome": f"Tesouro {nome}", "taxa": float(taxa),
                          "vencimento": venc})
        if lista:
            cache["tesouro"]       = lista
            cache["atualizado_em"] = datetime.now().strftime("%d/%m/%Y %H:%M")
            print(f"  ✓ {len(lista)} títulos carregados via mirror.")
            return
        print("  ✗ Mirror vazio — tentando CSV histórico.")
    except Exception as e:
        print(f"  ✗ Erro no mirror: {e} — tentando CSV histórico.")

    # ── Fonte 3: CSV histórico filtrado (última data, vencimento futuro) ──
    try:
        url_csv = (
            "https://www.tesourotransparente.gov.br/ckan/dataset/"
            "df56aa42-484a-4a59-8184-7676580c81e3/resource/"
            "796d2059-14e9-44e3-80c9-2d9e30b405c1/download/"
            "precotaxatesourodireto.csv"
        )
        resp = requests.get(url_csv, timeout=15, verify=False,
                            headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text), sep=';', decimal=',')
        df['dt_base'] = pd.to_datetime(df['Data Base'],       format='%d/%m/%Y', errors='coerce')
        df['dt_venc'] = pd.to_datetime(df['Data Vencimento'], format='%d/%m/%Y', errors='coerce')
        df_hoje = df[df['dt_base'] == df['dt_base'].max()].copy()
        df_hoje = df_hoje[df_hoje['dt_venc'] > pd.Timestamp.now()]
        lista = []
        for _, row in df_hoje.iterrows():
            taxa = row['Taxa Compra Manha']
            if pd.isna(taxa) or float(taxa) == 0:
                continue
            lista.append({"nome": row['Tipo Titulo'], "taxa": float(taxa),
                          "vencimento": row['Data Vencimento']})
        if lista:
            cache["tesouro"]       = lista
            cache["atualizado_em"] = datetime.now().strftime("%d/%m/%Y %H:%M")
            print(f"  ✓ {len(lista)} títulos via CSV histórico.")
            return
    except Exception as e:
        print(f"  ✗ Erro CSV histórico: {e}")

    # ── Fallback ──
    if not cache["tesouro"]:
        cache["tesouro"]       = TESOURO_FALLBACK
        cache["atualizado_em"] = "fallback"
        print("  ✗ Usando fallback hardcoded.")

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
