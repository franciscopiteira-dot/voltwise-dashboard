from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime, timezone
from pathlib import Path

from .models import Vehicle, Charger, RoutePlan, EnergyPricePoint, SiteConstraints
from .scheduler import make_plan
from .notifications import Notifier
from .price_provider import PriceCache
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Fleet AI")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

notifier = Notifier()
price_cache = PriceCache()

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- Servir UI ----------
BASE_DIR = Path(__file__).resolve().parent.parent  # .../fleet_ai
WEB_DIR = BASE_DIR / "web"

app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/")
def ui():
    return FileResponse(str(WEB_DIR / "index.html"))


@app.get("/health")
def health():
    return {"status": "ok"}


# ---------- Preço ----------
@app.get("/price/current")
def price_current():
    snap = price_cache.get_current_price(datetime.now(timezone.utc))
    return {
        "ok": snap.ok,
        "ts_utc": snap.ts_utc,
        "eur_per_kwh": snap.eur_per_kwh,
        "source": snap.source,
        "error": snap.error,
        "error_code": snap.error_code,
    }


@app.get("/prices/today")
def prices_today():
    """
    Curva do dia (MVP): devolve pontos horários (ts + €/kWh).
    Usa o provider (OMIE fallback).
    """
    try:
        now = datetime.now(timezone.utc)
        pts = price_cache.get_prices_today(now)
        return {
            "ok": True,
            "points": [{"ts": ts.isoformat(), "eur_per_kwh": round(v, 6)} for ts, v in pts],
            "source": "auto (OMIE fallback)",
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------- DTOs ----------
class VehicleDTO(BaseModel):
    id: str
    battery_kwh: float
    soc: float
    soh: float
    temp_c: float
    state: str
    charger_id: Optional[str] = None


class ChargerDTO(BaseModel):
    id: str
    max_kw: float
    enabled: bool = True
    efficiency: float = 0.92


class RouteDTO(BaseModel):
    id: str
    vehicle_id: str
    start_time: datetime
    end_time: datetime
    distance_km: float
    eta_minutes: float
    consumption_kwh: float
    required_soc_min: float


class PriceDTO(BaseModel):
    ts: datetime
    eur_per_kwh: float


class PlanRequest(BaseModel):
    now: datetime
    site_max_kw: float
    vehicles: List[VehicleDTO]
    chargers: List[ChargerDTO]
    routes: List[RouteDTO]
    prices: List[PriceDTO] = Field(default_factory=list)


# ---------- API ----------
@app.post("/plan")
async def plan(req: PlanRequest):
    vehicles = [Vehicle(**v.model_dump()) for v in req.vehicles]
    chargers = [Charger(**c.model_dump()) for c in req.chargers]
    routes = [RoutePlan(**r.model_dump()) for r in req.routes]

    prices: List[EnergyPricePoint] = []

    # 1) Se vierem preços do dashboard, usa-os
    if req.prices and len(req.prices) > 0:
        for p in req.prices:
            prices.append(
                EnergyPricePoint(
                    ts=p.ts.replace(tzinfo=None),
                    eur_per_kwh=float(p.eur_per_kwh),
                )
            )
    else:
        # 2) Caso contrário: usa curva do dia automaticamente
        pts = price_cache.get_prices_today(req.now)

        if pts and len(pts) > 0:
            for ts, eur_kwh in pts:
                prices.append(
                    EnergyPricePoint(
                        ts=ts.replace(tzinfo=None),
                        eur_per_kwh=float(eur_kwh),
                    )
                )
        else:
            # fallback: preço atual
            snap = price_cache.get_current_price(req.now)
            if snap.ok and snap.eur_per_kwh is not None:
                prices.append(
                    EnergyPricePoint(
                        ts=req.now.replace(tzinfo=None),
                        eur_per_kwh=float(snap.eur_per_kwh),
                    )
                )
            else:
                # fallback final
                prices.append(
                    EnergyPricePoint(
                        ts=req.now.replace(tzinfo=None),
                        eur_per_kwh=0.20,
                    )
                )

    site = SiteConstraints(site_max_kw=req.site_max_kw)

    # Nota: req.now pode vir com tz; o scheduler usa datetimes "naive"
    now_naive = req.now.replace(tzinfo=None)

    result = make_plan(now_naive, vehicles, chargers, routes, prices, site)

    payload = {
        "timestamp": result.ts.isoformat(),
        "total_kw": result.total_kw,
        "commands": [c.__dict__ for c in result.commands],
        "alerts": result.alerts,
        "explanations": result.explanations,
    }

    if result.alerts:
        await notifier.broadcast(
            {
                "type": "popups",
                "items": result.alerts,
                "timestamp": result.ts.isoformat(),
            }
        )

    return payload


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await notifier.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        notifier.disconnect(ws)

