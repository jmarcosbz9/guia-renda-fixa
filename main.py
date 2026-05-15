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
#  Headers que simulam browser real
#  Necessário: tesourodireto.com.br usa
#  Cloudflare Bot Management que bloqueia
#  requests sem headers de browser.
# ─────────────────────────────────────────
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer":         "https://www.tesourodireto.com.br/",
    "Origin":          "https://www.tesourodireto.com.br",
    "Connection":      "keep-alive",
    "sec-ch-ua":       '"Chromium";v="124", "Google Chrome";v="124"',
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest":  "empty",
    "Sec-Fetch-Mode":  "cors",
    "Sec-Fetch-Site":  "same-origin",
}


def parse_b3_bond(bond: dict) -> dict | None:
    """Converte um item da API JSON da B3 para o formato interno."""
    nome = bond.get("nm", "")
    if not nome:
        return None
    if not nome.lower().startswith("tesouro"):
        nome = f"Tesouro {nome}"

    taxa = bond.get("anulInvstmtRate") or bond.get("anulRedRate") or 0
    venc = (bond.get("mtrtyDt") or "")[:10]
    vmin = bond.get("minInvstmtAmt") or bond.get("untrInvstmtVal")

    if not taxa:
        return None
    if "-" in venc:
        y, m, d = venc.split("-")
        venc = f"{d}/{m}/{y}"

    taxa_f = float(taxa)
    n = nome.lower()
    if "ipca" in n:
        label = f"IPCA + {taxa_f:.2f}% a.a.".replace(".", ",")
    elif "selic" in n:
        label = f"Selic + {taxa_f:.4g}% a.a.".replace(".", ",")
    else:
        label = f"{taxa_f:.2f}% a.a.".replace(".", ",")

    try:
        vmin_f = round(float(vmin), 2) if vmin else None
    except Exception:
        vmin_f = None

    return {
        "nome":                nome,
        "taxa":                taxa_f,
        "rentabilidade_label": label,
        "valor_minimo":        vmin_f,
        "vencimento":          venc,
    }


# ─────────────────────────────────────────
#  Atualiza cache — tentativa em cascata
# ─────────────────────────────────────────
def atualizar_tesouro():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Atualizando dados do Tesouro...")

    # ── Fonte 1: API JSON da B3 (backend real do portal Tesouro Direto) ──
    # Retorna APENAS títulos ativos para compra neste momento.
    # Usa session com cookies para passar pelo Cloudflare.
    try:
        session = requests.Session()
        # Primeiro acesso à homepage para obter cookies do Cloudflare
        session.get("https://www.tesourodireto.com.br/",
                    headers=BROWSER_HEADERS, timeout=10)
        # Agora busca o JSON com os cookies já setados
        resp = session.get(
            "https://www.tesourodireto.com.br/json/br/com/b3/tesourodireto"
            "/model/entity/PublicTitle.json",
            headers=BROWSER_HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        data  = resp.json()
        bonds = (data.get("response", {})
                     .get("TrsrBdTradgList", []))
        lista = []
        for item in bonds:
            bond = item.get("TrsrBd", {})
            parsed = parse_b3_bond(bond)
            if parsed:
                lista.append(parsed)

        if lista:
            cache["tesouro"]       = lista
            cache["atualizado_em"] = datetime.now().strftime("%d/%m/%Y %H:%M")
            print(f"  ✓ {len(lista)} títulos via API B3 (Fonte 1).")
            return
        print("  ✗ API B3 retornou lista vazia — tentando mirror.")

    except Exception as e:
        print(f"  ✗ Erro na API B3: {e} — tentando mirror.")

    # ── Fonte 2: Mirror radaropcoes ──
    # Espelha a mesma API da B3; retorna apenas títulos ativos.
    try:
        resp = requests.get(
            "https://api.radaropcoes.com/bonds.json",
            timeout=10,
            headers=BROWSER_HEADERS,
        )
        resp.raise_for_status()
        bonds = resp.json().get("response", {}).get("TrsrBdTradgList", [])
        lista = []
        for item in bonds:
            bond   = item.get("TrsrBd", {})
            parsed = parse_b3_bond(bond)
            if parsed:
                lista.append(parsed)

        if lista:
            cache["tesouro"]       = lista
            cache["atualizado_em"] = datetime.now().strftime("%d/%m/%Y %H:%M")
            print(f"  ✓ {len(lista)} títulos via mirror radaropcoes (Fonte 2).")
            return
        print("  ✗ Mirror vazio.")

    except Exception as e:
        print(f"  ✗ Erro no mirror: {e}")

    # ── Fonte 3: CSV do Tesouro Direto (rendimento-investir) ──
    # Terceira opção: mesmo CSV de antes, mas agora com session+cookies.
    try:
        session = requests.Session()
        session.get("https://www.tesourodireto.com.br/",
                    headers=BROWSER_HEADERS, timeout=10)
        resp = session.get(
            "https://www.tesourodireto.com.br/documents/d/guest/"
            "rendimento-investir-csv?download=true",
            headers=BROWSER_HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        if "<!DOCTYPE" in resp.text[:50]:
            raise ValueError("Retornou HTML (Cloudflare challenge), não CSV")

        linhas = resp.text.strip().split("\n")
        header = [h.strip().strip('"').lower() for h in linhas[0].split(";")]
        print(f"  Header CSV: {header}")

        try:
            idx_nome = next(i for i, h in enumerate(header) if "nome" in h or "título" in h or "titulo" in h)
            idx_rent = next(i for i, h in enumerate(header) if "rentab" in h)
            idx_vmin = next(i for i, h in enumerate(header) if "valor" in h)
            idx_venc = next(i for i, h in enumerate(header) if "venc" in h)
        except StopIteration:
            idx_nome, idx_rent, idx_vmin, idx_venc = 0, 1, 2, 3

        lista = []
        for linha in linhas[1:]:
            partes = linha.strip().split(";")
            if len(partes) < 3:
                continue
            def get(i):
                return partes[i].strip().strip('"') if i < len(partes) else ""
            nome = get(idx_nome)
            if not nome:
                continue
            if not nome.lower().startswith("tesouro"):
                nome = f"Tesouro {nome}"
            taxa, label = parse_rentabilidade(get(idx_rent))
            if taxa is None:
                continue
            vmin = parse_valor_minimo(get(idx_vmin))
            lista.append({
                "nome":                nome,
                "taxa":                taxa,
                "rentabilidade_label": label or get(idx_rent),
                "valor_minimo":        vmin,
                "vencimento":          get(idx_venc).strip(),
            })

        if lista:
            cache["tesouro"]       = lista
            cache["atualizado_em"] = datetime.now().strftime("%d/%m/%Y %H:%M")
            print(f"  ✓ {len(lista)} títulos via CSV (Fonte 3).")
            return

    except Exception as e:
        print(f"  ✗ Erro no CSV: {e}")

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
#  Endpoint de diagnóstico — só para debug
#  Acesse: /api/debug para ver o CSV bruto
#  e o resultado do parsing em tempo real.
#  Remova em produção após confirmar parsing.
# ─────────────────────────────────────────
@app.get("/api/debug")
async def debug():
    url = (
        "https://www.tesourodireto.com.br/documents/d/guest/"
        "rendimento-investir-csv?download=true"
    )
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        raw  = resp.text
        linhas = raw.strip().split('\n')
        return {
            "status_code": resp.status_code,
            "encoding":    resp.encoding,
            "num_linhas":  len(linhas),
            "header_raw":  linhas[0] if linhas else None,
            "linhas_raw":  linhas[:6],          # primeiras 6 linhas cruas
            "cache_atual": cache["tesouro"],
            "cache_em":    cache["atualizado_em"],
        }
    except Exception as e:
        return {"erro": str(e), "cache_atual": cache["tesouro"]}


# ─────────────────────────────────────────
#  Entrypoint
# ─────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
