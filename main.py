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
#  Atualizado 1x/dia pelo cron às 10:30 BRT
# ─────────────────────────────────────────
cache = {
    "tesouro":       [],
    "atualizado_em": None,
}

# Sem fallback hardcoded — dados inventados são piores que nenhum dado.
# Se todas as fontes falharem, o frontend exibe aviso de indisponibilidade.
TESOURO_FALLBACK = []

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
#  Parser do CSV "rendimento-investir"
#  Estrutura típica do CSV do Tesouro:
#  Nome;Rentabilidade;Valor Mínimo;Vencimento
#  (pode ter colunas extras no meio)
# ─────────────────────────────────────────
def parse_rentabilidade(rent_str):
    """
    Recebe strings como:
      'IPCA + 7,23%'  → tipo='ipca',  taxa=7.23,  label='IPCA + 7,23% a.a.'
      'Selic + 0,05%' → tipo='selic', taxa=0.05,  label='Selic + 0,05% a.a.'
      '13,92%'        → tipo='pre',   taxa=13.92, label='13,92% a.a.'
    """
    s = rent_str.strip().strip('"')
    num_str = s.replace('%', '').split('+')[-1].strip().replace(',', '.')
    try:
        taxa = float(num_str)
    except Exception:
        return None, None, s

    s_lower = s.lower()
    if 'ipca' in s_lower:
        label = f"IPCA + {taxa:.2f}% a.a.".replace('.', ',')
    elif 'selic' in s_lower:
        label = f"Selic + {taxa:.4g}% a.a.".replace('.', ',')
    else:
        label = f"{taxa:.2f}% a.a.".replace('.', ',')

    return taxa, label


def parse_valor_minimo(val_str):
    """'R$189,44' ou '189.44' ou '189,44' → float"""
    try:
        s = val_str.strip().strip('"').replace('R$', '').replace(' ', '')
        # Formato brasileiro: ponto = milhar, vírgula = decimal
        if '.' in s and ',' in s:
            s = s.replace('.', '').replace(',', '.')
        elif ',' in s:
            s = s.replace(',', '.')
        return round(float(s), 2)
    except Exception:
        return None


# ─────────────────────────────────────────
#  Atualiza cache — tentativa em cascata
# ─────────────────────────────────────────
def atualizar_tesouro():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Atualizando dados do Tesouro...")

    # ── Fonte 1: CSV oficial "disponível para investir hoje" ──
    # Este CSV lista EXCLUSIVAMENTE os títulos em negociação.
    # Colunas: Nome ; Rentabilidade ; Valor Mínimo ; Vencimento
    # (pode haver colunas extras entre Rentabilidade e Valor Mínimo)
    try:
        url_investir = (
            "https://www.tesourodireto.com.br/documents/d/guest/"
            "rendimento-investir-csv?download=true"
        )
        resp = requests.get(url_investir, timeout=10,
                            headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()

        linhas = resp.text.strip().split('\n')
        # Detecta cabeçalho para mapear índices das colunas
        header = [h.strip().strip('"').lower() for h in linhas[0].split(';')]
        print(f"  Header CSV investir: {header}")

        # Tenta localizar colunas por nome; fallback para índices fixos
        try:
            idx_nome  = next(i for i, h in enumerate(header) if 'nome' in h or 'título' in h or 'titulo' in h)
            idx_rent  = next(i for i, h in enumerate(header) if 'rentab' in h)
            idx_vmin  = next(i for i, h in enumerate(header) if 'valor' in h and 'mín' in h.replace('i','í') or 'minimo' in h or 'mínimo' in h)
            idx_venc  = next(i for i, h in enumerate(header) if 'venc' in h)
        except StopIteration:
            # Fallback: assume ordem Nome;Rent;ValMin;Venc
            idx_nome, idx_rent, idx_vmin, idx_venc = 0, 1, 2, 3

        lista = []
        for linha in linhas[1:]:
            partes = linha.strip().split(';')
            if len(partes) < 3:
                continue
            def get(i):
                return partes[i].strip().strip('"') if i < len(partes) else ''

            nome = get(idx_nome)
            if not nome:
                continue
            if not nome.lower().startswith('tesouro'):
                nome = f"Tesouro {nome}"

            taxa, label = parse_rentabilidade(get(idx_rent))
            if taxa is None:
                continue

            vmin = parse_valor_minimo(get(idx_vmin))
            venc = get(idx_venc).strip()

            lista.append({
                "nome":                 nome,
                "taxa":                 taxa,
                "rentabilidade_label":  label or get(idx_rent),
                "valor_minimo":         vmin,
                "vencimento":           venc,
            })

        if lista:
            cache["tesouro"]       = lista
            cache["atualizado_em"] = datetime.now().strftime("%d/%m/%Y %H:%M")
            print(f"  ✓ {len(lista)} títulos disponíveis para investir (Fonte 1).")
            return
        print("  ✗ CSV investir vazio — tentando mirror.")

    except Exception as e:
        print(f"  ✗ Erro no CSV investir: {e} — tentando mirror.")

    # ── Fonte 2: Mirror radaropcoes ──
    # Espelha a API oficial B3/TD; retorna apenas títulos ativos.
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
            vmin = bond.get("minInvstmtAmt") or bond.get("untrInvstmtVal")
            if not nome or not taxa:
                continue
            if "-" in venc:
                y, m, d = venc.split("-")
                venc = f"{d}/{m}/{y}"
            try:
                vmin = round(float(vmin), 2) if vmin else None
            except Exception:
                vmin = None

            taxa_f = float(taxa)
            nome_f = f"Tesouro {nome}" if not nome.lower().startswith('tesouro') else nome
            n_lower = nome_f.lower()
            if 'ipca' in n_lower:
                label = f"IPCA + {taxa_f:.2f}% a.a.".replace('.', ',')
            elif 'selic' in n_lower:
                label = f"Selic + {taxa_f:.4g}% a.a.".replace('.', ',')
            else:
                label = f"{taxa_f:.2f}% a.a.".replace('.', ',')

            lista.append({
                "nome":                nome_f,
                "taxa":                taxa_f,
                "rentabilidade_label": label,
                "valor_minimo":        vmin,
                "vencimento":          venc,
            })

        if lista:
            cache["tesouro"]       = lista
            cache["atualizado_em"] = datetime.now().strftime("%d/%m/%Y %H:%M")
            print(f"  ✓ {len(lista)} títulos via mirror (Fonte 2).")
            return
        print("  ✗ Mirror vazio.")

    except Exception as e:
        print(f"  ✗ Erro no mirror: {e}")

    # ── Todas as fontes falharam ──
    # Cache permanece vazio → frontend exibe aviso de indisponibilidade.
    # Melhor mostrar nada do que dados desatualizados ou inventados.
    print("  ✗ Todas as fontes falharam. Cache de títulos vazio.")


# ─────────────────────────────────────────
#  CRON — todo dia útil às 10:30 BRT
# ─────────────────────────────────────────
scheduler = BackgroundScheduler(timezone="America/Sao_Paulo")
scheduler.add_job(
    atualizar_tesouro,
    CronTrigger(day_of_week="mon-fri", hour=10, minute=30,
                timezone="America/Sao_Paulo"),
)
scheduler.start()

# Busca imediata na inicialização
atualizar_tesouro()


# ─────────────────────────────────────────
#  Endpoint principal
# ─────────────────────────────────────────
@app.get("/api/mercado")
async def get_market_data():
    selic     = get_bacen_data(432)   or 14.75
    ipca      = get_bacen_data(13522) or 4.39
    juro_real = round(((1 + selic / 100) / (1 + ipca / 100) - 1) * 100, 2)

    return {
        "macro": {
            "selic":     selic,
            "ipca_proj": ipca,
            "juro_real": juro_real,
        },
        "tesouro":  cache["tesouro"],
        "cache_em": cache["atualizado_em"],
    }


# ─────────────────────────────────────────
#  Entrypoint
# ─────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
