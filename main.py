import json
import os
from datetime import datetime

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "muda-este-token-em-producao")
CACHE_FILE  = "/tmp/tesouro_cache.json"
MANUAL_FILE = os.path.join(os.path.dirname(__file__), "data", "tesouro_manual.json")

cache = {
    "tesouro":               [],
    "tesouro_status":        "indisponivel",
    "tesouro_fonte":         None,
    "tesouro_atualizado_em": None,
    "tesouro_erro_resumido": None,
    # Macro — atualizados junto com os títulos (cron 10h/13h/17h45)
    "selic":                 14.75,
    "ipca_proj":             4.39,
    "juro_real":             9.92,
}

MANUAL_EMBUTIDO = [
    {"nome": "Tesouro Selic 2031",                         "taxa": 0.08,  "rentabilidade_label": "Selic + 0,08% a.a.", "valor_minimo": 189.44, "vencimento": "01/03/2031"},
    {"nome": "Tesouro Prefixado 2028",                     "taxa": 13.87, "rentabilidade_label": "13,87% a.a.",        "valor_minimo":  30.20, "vencimento": "01/01/2028"},
    {"nome": "Tesouro Prefixado 2029",                     "taxa": 13.98, "rentabilidade_label": "13,98% a.a.",        "valor_minimo":  25.40, "vencimento": "01/01/2029"},
    {"nome": "Tesouro Prefixado com Juros Semestrais 2029","taxa": 13.99, "rentabilidade_label": "13,99% a.a.",        "valor_minimo":1041.12, "vencimento": "01/01/2029"},
    {"nome": "Tesouro IPCA+ 2029",                         "taxa":  7.23, "rentabilidade_label": "IPCA + 7,23% a.a.", "valor_minimo":  34.20, "vencimento": "15/05/2029"},
    {"nome": "Tesouro IPCA+ 2035",                         "taxa":  7.38, "rentabilidade_label": "IPCA + 7,38% a.a.", "valor_minimo":  29.80, "vencimento": "15/05/2035"},
    {"nome": "Tesouro IPCA+ com Juros Semestrais 2032",    "taxa":  7.81, "rentabilidade_label": "IPCA + 7,81% a.a.", "valor_minimo":  29.49, "vencimento": "15/08/2032"},
    {"nome": "Tesouro IPCA+ com Juros Semestrais 2037",    "taxa":  7.54, "rentabilidade_label": "IPCA + 7,54% a.a.", "valor_minimo":  42.01, "vencimento": "15/05/2037"},
]

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pt-BR,pt;q=0.9",
    "Referer": "https://www.tesourodireto.com.br/",
    "Origin":  "https://www.tesourodireto.com.br",
}


def salvar_cache():
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(dict(cache), f, ensure_ascii=False)
    except Exception as e:
        print(f"  ⚠ salvar_cache: {e}")


def carregar_cache():
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                dados = json.load(f)
            if dados.get("tesouro"):
                cache.update(dados)
                print(f"  ✓ Cache do disco: {len(cache['tesouro'])} títulos (status={cache['tesouro_status']})")
    except Exception as e:
        print(f"  ⚠ carregar_cache: {e}")


def get_bacen_data(serie):
    try:
        r = requests.get(
            f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{serie}/dados/ultimos/1?formato=json",
            timeout=5)
        return float(r.json()[0]["valor"])
    except Exception:
        return None


def is_html(text):
    return "<!DOCTYPE" in text[:100] or "<html" in text[:100]


def parse_b3_bond(bond):
    nome = bond.get("nm", "")
    if not nome:
        return None
    if not nome.lower().startswith("tesouro"):
        nome = f"Tesouro {nome}"
    taxa = float(bond.get("anulInvstmtRate") or bond.get("anulRedRate") or 0)
    if not taxa:
        return None
    venc = (bond.get("mtrtyDt") or "")[:10]
    if "-" in venc:
        y, m, d = venc.split("-"); venc = f"{d}/{m}/{y}"
    try:
        vmin = round(float(bond.get("minInvstmtAmt") or bond.get("untrInvstmtVal") or 0), 2) or None
    except Exception:
        vmin = None
    n = nome.lower()
    label = (f"IPCA + {taxa:.2f}% a.a." if "ipca" in n
             else f"Selic + {taxa:.4g}% a.a." if "selic" in n
             else f"{taxa:.2f}% a.a.").replace(".", ",")
    return {"nome": nome, "taxa": taxa, "rentabilidade_label": label,
            "valor_minimo": vmin, "vencimento": venc}


# ─────────────────────────────────────────
#  Lista de títulos ativos conhecidos
#  Usada pela estratégia de endpoints individuais.
#  Atualizar quando o Tesouro lançar novos títulos.
# ─────────────────────────────────────────
TITULOS_CONHECIDOS = [
    "Tesouro Selic 2031",
    "Tesouro Selic 2029",
    "Tesouro Selic 2028",
    "Tesouro Prefixado 2027",
    "Tesouro Prefixado 2028",
    "Tesouro Prefixado 2029",
    "Tesouro Prefixado 2031",
    "Tesouro Prefixado 2032",
    "Tesouro Prefixado com Juros Semestrais 2027",
    "Tesouro Prefixado com Juros Semestrais 2029",
    "Tesouro Prefixado com Juros Semestrais 2031",
    "Tesouro Prefixado com Juros Semestrais 2033",
    "Tesouro Prefixado com Juros Semestrais 2035",
    "Tesouro Prefixado com Juros Semestrais 2037",
    "Tesouro IPCA+ 2029",
    "Tesouro IPCA+ 2032",
    "Tesouro IPCA+ 2035",
    "Tesouro IPCA+ 2040",
    "Tesouro IPCA+ 2045",
    "Tesouro IPCA+ 2050",
    "Tesouro IPCA+ com Juros Semestrais 2030",
    "Tesouro IPCA+ com Juros Semestrais 2032",
    "Tesouro IPCA+ com Juros Semestrais 2035",
    "Tesouro IPCA+ com Juros Semestrais 2037",
    "Tesouro IPCA+ com Juros Semestrais 2040",
    "Tesouro IPCA+ com Juros Semestrais 2045",
    "Tesouro IPCA+ com Juros Semestrais 2055",
    "Tesouro Renda+ Aposentadoria Extra 2030",
    "Tesouro Renda+ Aposentadoria Extra 2035",
    "Tesouro Renda+ Aposentadoria Extra 2040",
    "Tesouro Renda+ Aposentadoria Extra 2045",
    "Tesouro Renda+ Aposentadoria Extra 2050",
    "Tesouro Renda+ Aposentadoria Extra 2055",
    "Tesouro Renda+ Aposentadoria Extra 2060",
    "Tesouro Renda+ Aposentadoria Extra 2065",
    "Tesouro Educa+ 2026",
    "Tesouro Educa+ 2027",
    "Tesouro Educa+ 2028",
    "Tesouro Educa+ 2029",
    "Tesouro Educa+ 2030",
    "Tesouro Educa+ 2031",
    "Tesouro Educa+ 2032",
    "Tesouro Educa+ 2033",
    "Tesouro Educa+ 2034",
    "Tesouro Educa+ 2035",
    "Tesouro Educa+ 2036",
    "Tesouro Educa+ 2037",
    "Tesouro Educa+ 2038",
    "Tesouro Educa+ 2039",
    "Tesouro Educa+ 2040",
    "Tesouro Educa+ 2041",
    "Tesouro Educa+ 2042",
    "Tesouro Educa+ 2043",
]


def parse_individual_bond(nome: str, data: dict) -> dict | None:
    import re

    idx_inv  = (data.get("investmentProfitabilityIndexerName")  or "").strip()
    idx_red  = (data.get("redemptionProfitabilityFeeIndexerName") or "").strip()
    n = nome.lower()

    # Escolhe o campo correto por tipo de título:
    # IPCA+/Educa+/Renda+: investmentProfitabilityIndexerName retorna só "IPCA"
    #                       redemptionProfitabilityFeeIndexerName tem "IPCA + 8,07%"
    # Prefixado/Selic:      investmentProfitabilityIndexerName já tem a taxa completa
    if "ipca" in n or "educa" in n or "renda" in n:
        idx_raw = idx_red if "%" in idx_red else idx_inv
    else:
        idx_raw = idx_inv if "%" in idx_inv else idx_red

    # Extrai taxa numérica
    taxa = None
    if "%" in idx_raw:
        num_str = idx_raw.replace("%", "").split("+")[-1].strip().replace(",", ".")
        try:
            taxa = float(num_str)
        except Exception:
            pass

    if taxa is None:
        return None  # sem taxa → descarta

    # Monta label
    if "selic" in n:
        label = f"Selic + {taxa:.4g}% a.a.".replace(".", ",")
    elif "ipca" in n or "educa" in n or "renda" in n:
        label = f"IPCA + {taxa:.2f}% a.a.".replace(".", ",")
    else:
        label = f"{taxa:.2f}% a.a.".replace(".", ",")

    # Vencimento
    venc_raw = (data.get("maturityDate") or "")[:10]
    if "-" in venc_raw:
        y, m, d = venc_raw.split("-")
        venc = f"{d}/{m}/{y}"
    else:
        venc = venc_raw
    if not venc:
        return None

    # Valor mínimo — usa investmentBondMinimumValue; se 0, usa unitaryInvestmentValue/100
    vmin = data.get("investmentBondMinimumValue") or 0
    try:
        vmin = round(float(vmin), 2) or None
    except Exception:
        vmin = None

    return {
        "nome":                nome,
        "taxa":                taxa,
        "rentabilidade_label": label,
        "valor_minimo":        vmin,
        "vencimento":          venc,
    }


def tentar_radaropcoes_individual():
    """
    Estratégia: busca cada título individualmente via
    GET /bonds/{nome} — endpoint que não sofre bloqueio
    Cloudflare como o /bonds.json agregado.
    Retorna apenas títulos com type='investir'.
    """
    from urllib.parse import quote
    BASE = "https://api.radaropcoes.com/bonds/"
    lista = []
    erros = 0

    for nome in TITULOS_CONHECIDOS:
        url = BASE + quote(nome)
        try:
            r = requests.get(url, headers=BROWSER_HEADERS, timeout=8)
            if r.status_code == 404:
                print(f"    404: {nome}")
                continue
            if r.status_code != 200:
                print(f"    {r.status_code}: {nome}")
                erros += 1
                continue
            if is_html(r.text):
                print(f"    HTML(CF): {nome}")
                erros += 1
                continue
            parsed = parse_individual_bond(nome, r.json())
            if parsed:
                lista.append(parsed)
            else:
                print(f"    skip(parse): {nome} | idx={data.get('investmentProfitabilityIndexerName','?')} | indication={str(data.get('indication',''))[:60]} | venc={data.get('maturityDate','?')}")
        except Exception as e:
            print(f"    ✗ individual {nome}: {e}")
            erros += 1

    print(f"    individual: {len(lista)} títulos OK, {erros} erros")
    return lista if lista else None


def tentar_radaropcoes_bulk():
    """Fallback: tenta o bonds.json agregado (bloqueado em datacenter)."""
    r = requests.get("https://api.radaropcoes.com/bonds.json",
                     headers=BROWSER_HEADERS, timeout=10)
    r.raise_for_status()
    if is_html(r.text):
        raise ValueError("Cloudflare challenge recebido")
    bonds = r.json().get("response", {}).get("TrsrBdTradgList", [])
    lista = [x for x in (parse_b3_bond(i.get("TrsrBd", {})) for i in bonds) if x]
    return lista or None


def tentar_b3_json():
    for url in [
        "https://www.tesourodireto.com.br/json/br/com/b3/tesourodireto/service/api/treasurybondsinfo.json",
        "https://www.tesourodireto.com.br/json/br/com/b3/tesourodireto/model/entity/PublicTitle.json",
    ]:
        try:
            s = requests.Session()
            s.get("https://www.tesourodireto.com.br/", headers=BROWSER_HEADERS, timeout=8)
            r = s.get(url, headers=BROWSER_HEADERS, timeout=10)
            r.raise_for_status()
            if is_html(r.text):
                continue
            bonds = r.json().get("response", {}).get("TrsrBdTradgList", [])
            lista = [x for x in (parse_b3_bond(i.get("TrsrBd", {})) for i in bonds) if x]
            if lista:
                return lista
        except Exception as e:
            print(f"    ✗ B3 {url.split('/')[-1]}: {e}")
    return None


def tentar_csv():
    s = requests.Session()
    s.get("https://www.tesourodireto.com.br/", headers=BROWSER_HEADERS, timeout=8)
    r = s.get(
        "https://www.tesourodireto.com.br/documents/d/guest/rendimento-investir-csv?download=true",
        headers=BROWSER_HEADERS, timeout=10)
    r.raise_for_status()
    if is_html(r.text):
        raise ValueError("Cloudflare challenge")
    linhas = r.text.strip().split("\n")
    hdr = [h.strip().strip('"').lower() for h in linhas[0].split(";")]
    try:
        in_ = next(i for i, h in enumerate(hdr) if "nome" in h or "titulo" in h)
        ir  = next(i for i, h in enumerate(hdr) if "rentab" in h)
        iv  = next(i for i, h in enumerate(hdr) if "valor" in h)
        ivenc = next(i for i, h in enumerate(hdr) if "venc" in h)
    except StopIteration:
        in_, ir, iv, ivenc = 0, 1, 2, 3
    lista = []
    for ln in linhas[1:]:
        p = ln.strip().split(";")
        def g(i): return p[i].strip().strip('"') if i < len(p) else ""
        nome = g(in_)
        if not nome: continue
        if not nome.lower().startswith("tesouro"): nome = f"Tesouro {nome}"
        s_rent = g(ir)
        num = s_rent.replace("%","").split("+")[-1].strip().replace(",",".")
        try: taxa = float(num)
        except: continue
        n = nome.lower()
        label = (f"IPCA + {taxa:.2f}% a.a." if "ipca" in n
                 else f"Selic + {taxa:.4g}% a.a." if "selic" in n
                 else f"{taxa:.2f}% a.a.").replace(".", ",")
        s_vmin = g(iv).replace("R$","").replace(" ","")
        try:
            if "." in s_vmin and "," in s_vmin: s_vmin = s_vmin.replace(".","").replace(",",".")
            elif "," in s_vmin: s_vmin = s_vmin.replace(",",".")
            vmin = round(float(s_vmin), 2)
        except: vmin = None
        lista.append({"nome": nome, "taxa": taxa, "rentabilidade_label": label,
                      "valor_minimo": vmin, "vencimento": g(ivenc).strip()})
    return lista or None


def carregar_manual():
    try:
        if os.path.exists(MANUAL_FILE):
            with open(MANUAL_FILE, "r", encoding="utf-8") as f:
                dados = json.load(f)
            if dados:
                return dados, "base manual (arquivo data/tesouro_manual.json)"
    except Exception as e:
        print(f"  ⚠ manual externo: {e}")
    return MANUAL_EMBUTIDO, "base manual embutida (mai/2026 — conferir atualidade)"


def atualizar_tesouro():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Atualizando dados do Tesouro...")

    # ── Atualiza macro (BCB SGS) ──
    selic = get_bacen_data(432)
    ipca  = get_bacen_data(13522)
    if selic and ipca:
        cache["selic"]     = selic
        cache["ipca_proj"] = ipca
        cache["juro_real"] = round(((1 + selic/100) / (1 + ipca/100) - 1) * 100, 2)
        print(f"  ✓ Macro: Selic={selic}% IPCA={ipca}% JuroReal={cache['juro_real']}%")
    else:
        print(f"  ⚠ BCB indisponível — mantendo macro do cache.")

    for nome_fonte, fn in [
        ("radaropcoes individual",  tentar_radaropcoes_individual),
        ("radaropcoes bulk",        tentar_radaropcoes_bulk),
        ("API B3/JSON",             tentar_b3_json),
        ("CSV investir",            tentar_csv),
    ]:
        try:
            lista = fn()
            if lista:
                cache.update({
                    "tesouro":               lista,
                    "tesouro_status":        "online",
                    "tesouro_fonte":         f"Tesouro Direto/STN via {nome_fonte}",
                    "tesouro_atualizado_em": datetime.now().strftime("%d/%m/%Y %H:%M"),
                    "tesouro_erro_resumido": None,
                })
                salvar_cache()
                print(f"  ✓ {len(lista)} títulos via {nome_fonte}.")
                return
            cache["tesouro_erro_resumido"] = f"{nome_fonte}: lista vazia"
            print(f"  ⚠ {nome_fonte}: lista vazia.")
        except Exception as e:
            cache["tesouro_erro_resumido"] = f"{nome_fonte}: {str(e)[:120]}"
            print(f"  ✗ {nome_fonte}: {str(e)[:120]}")

    # Todas as fontes automáticas falharam
    if not cache["tesouro"]:
        lista_m, fonte_m = carregar_manual()
        cache.update({
            "tesouro":               lista_m,
            "tesouro_status":        "manual",
            "tesouro_fonte":         fonte_m,
            "tesouro_atualizado_em": "15/05/2026 18:00 (base de referência)",
        })
        salvar_cache()
        print(f"  ⚠ Usando {fonte_m}: {len(lista_m)} títulos.")
    else:
        cache["tesouro_status"] = "cache"
        print(f"  ⚠ Fontes falharam. Cache anterior preservado ({len(cache['tesouro'])} títulos).")


# Cron: 10:00, 13:00 e 17:45 em dias úteis
scheduler = BackgroundScheduler(timezone="America/Sao_Paulo")
for h, m in [(10, 0), (13, 0), (17, 45)]:
    scheduler.add_job(atualizar_tesouro,
                      CronTrigger(day_of_week="mon-fri", hour=h, minute=m,
                                  timezone="America/Sao_Paulo"))
scheduler.start()
carregar_cache()
atualizar_tesouro()


@app.get("/api/mercado")
async def get_market_data():
    return {
        "macro": {
            "selic":     cache["selic"],
            "ipca_proj": cache["ipca_proj"],
            "juro_real": cache["juro_real"],
        },
        "tesouro":               cache["tesouro"],
        "tesouro_status":        cache["tesouro_status"],
        "tesouro_fonte":         cache["tesouro_fonte"],
        "tesouro_atualizado_em": cache["tesouro_atualizado_em"],
        "tesouro_erro_resumido": cache["tesouro_erro_resumido"],
        "aviso": "Dados meramente informativos. Confira no Tesouro Direto antes de investir.",
    }


@app.post("/api/admin/refresh")
async def admin_refresh(x_admin_token: str = Header(default=None)):
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Token inválido.")
    atualizar_tesouro()
    return {"ok": True, "tesouro_status": cache["tesouro_status"],
            "tesouro_fonte": cache["tesouro_fonte"],
            "tesouro_atualizado_em": cache["tesouro_atualizado_em"],
            "total_titulos": len(cache["tesouro"])}


@app.get("/api/admin/debug")
async def admin_debug(x_admin_token: str = Header(default=None)):
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Token inválido.")
    resultados = {}
    for nome_fonte, fn in [("radaropcoes", tentar_radaropcoes),
                            ("API B3/JSON", tentar_b3_json),
                            ("CSV investir", tentar_csv)]:
        try:
            lista = fn()
            resultados[nome_fonte] = {"ok": bool(lista), "total": len(lista or []),
                                      "amostra": (lista or [])[:2]}
        except Exception as e:
            resultados[nome_fonte] = {"ok": False, "erro": str(e)[:200]}
    return {"cache_status": cache["tesouro_status"], "cache_fonte": cache["tesouro_fonte"],
            "cache_atualizado_em": cache["tesouro_atualizado_em"],
            "cache_total": len(cache["tesouro"]),
            "cache_erro_resumido": cache["tesouro_erro_resumido"],
            "fontes_agora": resultados}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
