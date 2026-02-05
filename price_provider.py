from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, date
from typing import Optional, Tuple, List
import xml.etree.ElementTree as ET
import requests


# ---------------- REN SOAP ----------------
REN_ENDPOINT = "https://ws-mercado.ren.pt/MarketInfoService.asmx"
SOAP_ACTION = "https://ws-mercado.ren.pt/GetInfoForTimeFrameByInfoType"


def _soap_envelope(start_day: str, end_day: str, info_type: str) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope" xmlns:ws="https://ws-mercado.ren.pt">
  <soap:Header/>
  <soap:Body>
    <ws:GetInfoForTimeFrameByInfoType>
      <ws:StartDay>{start_day}</ws:StartDay>
      <ws:EndDay>{end_day}</ws:EndDay>
      <ws:InfoType>{info_type}</ws:InfoType>
    </ws:GetInfoForTimeFrameByInfoType>
  </soap:Body>
</soap:Envelope>
"""


def _post_ren(start_day: str, end_day: str, info_type: str, timeout_s: int = 20) -> str:
    headers = {
        "Content-Type": f'application/soap+xml; charset=utf-8; action="{SOAP_ACTION}"',
    }
    body = _soap_envelope(start_day, end_day, info_type)
    r = requests.post(REN_ENDPOINT, data=body.encode("utf-8"), headers=headers, timeout=timeout_s)
    r.raise_for_status()
    return r.text


def _extract_return_xml_from_soap(soap_text: str) -> str:
    root = ET.fromstring(soap_text)
    for el in root.iter():
        if el.tag.endswith("GetInfoForTimeFrameByInfoTypeResult"):
            return (el.text or "").strip()
    return ""


def _strip_namespaces(elem: ET.Element) -> None:
    for e in elem.iter():
        if "}" in e.tag:
            e.tag = e.tag.split("}", 1)[1]


def _parse_root_xml(root_xml: str) -> Tuple[Optional[str], Optional[str], List[Tuple[datetime, float]]]:
    """
    devolve: (error_code, error_message, items[(utc_dt, price_eur_mwh)])
    """
    if not root_xml:
        return ("GEN03", "XML interno vazio (SOAP result sem conteúdo)", [])

    try:
        doc = ET.fromstring(root_xml)
    except ET.ParseError:
        return ("GEN03", "XML interno inválido devolvido pela REN", [])

    _strip_namespaces(doc)

    err = doc.find(".//Error")
    if err is not None:
        code = (err.findtext("Code") or "").strip() or "GEN03"
        msg = (err.findtext("Message") or "").strip() or "Erro REN"
        return (code, msg, [])

    items: List[Tuple[datetime, float]] = []
    for it in doc.findall(".//Item"):
        utc_s = (it.findtext("UTCDate") or "").strip()
        price_s = (it.findtext("Price") or "").strip()  # €/MWh
        if not utc_s or not price_s:
            continue
        try:
            utc_dt = datetime.fromisoformat(utc_s.replace("Z", "+00:00"))
            price = float(price_s)
            items.append((utc_dt, price))
        except Exception:
            continue

    if not items:
        return ("GEN02", "No Data Available", [])

    return (None, None, items)


def _closest_price_eur_kwh(items: List[Tuple[datetime, float]], now_utc: datetime) -> Optional[float]:
    if not items:
        return None
    _, best_price_mwh = min(items, key=lambda t: abs((t[0] - now_utc).total_seconds()))
    return best_price_mwh / 1000.0


# ---------------- OMIE (fallback robusto por URL direto) ----------------
# Padrão de download (estável):
# https://www.omie.es/pt/file-download?parents%5B0%5D=marginalpdbcpt&filename=marginalpdbcpt_YYYYMMDD.1
# (em alguns dias pode ser .2, etc.)  :contentReference[oaicite:2]{index=2}

OMIE_BASE = "https://www.omie.es/pt/file-download"
OMIE_PARENTS = "parents%5B0%5D=marginalpdbcpt"


def _omie_download_url(day: date, variant: int) -> str:
    ymd = day.strftime("%Y%m%d")
    return f"{OMIE_BASE}?{OMIE_PARENTS}&filename=marginalpdbcpt_{ymd}.{variant}"


def _omie_fetch_csv(day: date, timeout_s: int = 20) -> Optional[str]:
    # tenta .1 até .5 (na prática quase sempre .1)
    for variant in range(1, 6):
        url = _omie_download_url(day, variant)
        r = requests.get(url, timeout=timeout_s)
        if r.status_code == 200 and r.text and "MARGINALPDBCPT" in r.text:
            return r.text
    return None


def _omie_parse_prices(csv_text: str) -> List[Tuple[datetime, float]]:
    """
    Formato típico (exemplo):
    MARGINALPDBCPT;
    2024;01;16;1;86.59;86.59;
    year;month;day;hour;price_pt_eur_mwh;price_es_eur_mwh;
    Vamos usar o 5º campo (PT) e criar timestamps UTC "naive" por hora.
    """
    lines = [ln.strip() for ln in csv_text.splitlines() if ln.strip()]
    out: List[Tuple[datetime, float]] = []

    for ln in lines:
        if ln.upper().startswith("MARGINALPDBCPT"):
            continue
        parts = [p for p in ln.split(";") if p != ""]
        if len(parts) < 5:
            continue

        try:
            y = int(parts[0]); m = int(parts[1]); d = int(parts[2])
            hour = int(parts[3])  # 1..24
            price_pt = float(parts[4].replace(",", "."))  # €/MWh

            # OMIE usa hora 1..24; converter para 0..23
            hh = max(0, min(23, hour - 1))
            ts = datetime(y, m, d, hh, 0, 0)  # naive
            out.append((ts, price_pt))
        except Exception:
            continue

    # ordenar
    out.sort(key=lambda t: t[0])
    return out


def _omie_prices_today(now_utc: datetime) -> List[Tuple[datetime, float]]:
    csv_text = _omie_fetch_csv(now_utc.date())
    if not csv_text:
        return []
    return _omie_parse_prices(csv_text)


def _omie_current_price_eur_kwh(now_utc: datetime) -> Optional[float]:
    pts = _omie_prices_today(now_utc)
    if not pts:
        return None
    # escolher o ponto horário mais próximo
    best_ts, best_mwh = min(pts, key=lambda t: abs((t[0] - now_utc.replace(tzinfo=None)).total_seconds()))
    return best_mwh / 1000.0


# ---------------- Public API ----------------
@dataclass
class PriceSnapshot:
    ok: bool
    ts_utc: str
    eur_per_kwh: Optional[float]
    source: str
    error: Optional[str] = None
    error_code: Optional[str] = None


class PriceCache:
    """
    Tenta:
      1) REN GetMarketPrice15M
      2) REN GetMarketPrice
      3) OMIE day-ahead Portugal (marginalpdbcpt_YYYYMMDD.N)
    """

    def get_current_price(self, now: Optional[datetime] = None) -> PriceSnapshot:
        now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        day = now_utc.date().isoformat()

        last_err: Tuple[str, str] = ("GEN03", "Erro desconhecido")

        # 1) REN 15M
        try:
            soap = _post_ren(day, day, "GetMarketPrice15M")
            root_xml = _extract_return_xml_from_soap(soap)
            code, msg, items = _parse_root_xml(root_xml)

            if items:
                p = _closest_price_eur_kwh(items, now_utc)
                return PriceSnapshot(
                    ok=True,
                    ts_utc=now_utc.isoformat(),
                    eur_per_kwh=p,
                    source="REN:GetMarketPrice15M",
                )

            last_err = (code or "GEN02", msg or "No Data Available")
        except Exception as e:
            last_err = ("GEN03", f"Falha REN (15M): {e}")

        # 2) REN hora
        try:
            soap = _post_ren(day, day, "GetMarketPrice")
            root_xml = _extract_return_xml_from_soap(soap)
            code, msg, items = _parse_root_xml(root_xml)

            if items:
                p = _closest_price_eur_kwh(items, now_utc)
                return PriceSnapshot(
                    ok=True,
                    ts_utc=now_utc.isoformat(),
                    eur_per_kwh=p,
                    source="REN:GetMarketPrice",
                )

            last_err = (code or last_err[0], msg or last_err[1])
        except Exception as e:
            last_err = ("GEN03", f"Falha REN (hora): {e}")

        # 3) OMIE fallback
        try:
            p = _omie_current_price_eur_kwh(now_utc)
            if p is not None:
                return PriceSnapshot(
                    ok=True,
                    ts_utc=now_utc.isoformat(),
                    eur_per_kwh=p,
                    source="OMIE:marginalpdbcpt (fallback)",
                )
        except Exception as e:
            last_err = ("GEN03", f"{last_err[0]}: {last_err[1]} | OMIE falhou: {e}")

        c, m = last_err
        return PriceSnapshot(
            ok=False,
            ts_utc=now_utc.isoformat(),
            eur_per_kwh=None,
            source="REN/OMIE",
            error=f"Sem preço disponível ({c}: {m})",
            error_code=c,
        )

    def get_prices_today(self, now: Optional[datetime] = None) -> List[Tuple[datetime, float]]:
        """
        Devolve lista de (ts_naive, eur_per_kwh) para o dia.
        Usa OMIE (horário) — estável para alimentar o /plan sem input manual.
        """
        now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        pts_mwh = _omie_prices_today(now_utc)
        out: List[Tuple[datetime, float]] = []
        for ts, eur_mwh in pts_mwh:
            out.append((ts, eur_mwh / 1000.0))
        return out
