from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List, Dict, Tuple, Any, Optional

from models import Vehicle, Charger, RoutePlan, EnergyPricePoint, SiteConstraints


@dataclass
class ChargingCommand:
    vehicle_id: str
    charger_id: str
    set_kw: float
    reason: str


@dataclass
class PlanResult:
    ts: datetime
    total_kw: float
    commands: List[ChargingCommand]
    alerts: List[str]
    explanations: Dict[str, Any]


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# --------- Price helpers (EnergyPricePoint) ---------
def price_at(prices: List[EnergyPricePoint], ts: datetime) -> Optional[float]:
    """Preço mais próximo do timestamp."""
    if not prices:
        return None
    best = min(prices, key=lambda p: abs((p.ts - ts).total_seconds()))
    return float(best.eur_per_kwh)


def min_price_until(prices: List[EnergyPricePoint], start: datetime, end: datetime) -> Optional[float]:
    """Melhor preço (mínimo) entre start e end (inclusive)."""
    if not prices:
        return None
    window = [float(p.eur_per_kwh) for p in prices if start <= p.ts <= end]
    if not window:
        return None
    return float(min(window))


def should_delay_charging(
    price_now: Optional[float],
    best_future_price: Optional[float],
    minutes_to_start: float,
    deficit_soc: float,
    delay_margin: float = 0.15,
    urgency_minutes: float = 60.0,
) -> bool:
    """
    Decide se compensa adiar (por custo) sem comprometer a rota.
    - Se faltar pouco tempo (< urgency_minutes) e há défice -> NÃO adia.
    - Se não houver preços -> NÃO adia.
    - Se agora está > (best_future_price * (1 + delay_margin)) -> adia.
    """
    if deficit_soc <= 0:
        return False
    if minutes_to_start <= urgency_minutes:
        return False
    if price_now is None or best_future_price is None:
        return False
    return price_now > best_future_price * (1.0 + delay_margin)


def battery_friendly_kw(vehicle: Vehicle, charger: Charger, requested_kw: float) -> float:
    kw = requested_kw
    if vehicle.soc >= 0.80:
        kw *= 0.5
    if vehicle.soc >= 0.92:
        kw *= 0.3
    if vehicle.temp_c >= 40:
        kw *= 0.5
    return clamp(kw, 0.0, charger.max_kw)


def compute_urgency(vehicle: Vehicle, route: RoutePlan, now: datetime) -> float:
    time_to_start = (route.start_time - now).total_seconds() / 60.0
    deficit_soc = max(0.0, route.required_soc_min - vehicle.soc)
    return deficit_soc * 1000.0 + max(0.0, 180.0 - time_to_start)


def make_plan(
    now: datetime,
    vehicles: List[Vehicle],
    chargers: List[Charger],
    routes: List[RoutePlan],
    prices: List[EnergyPricePoint],
    site: SiteConstraints,
) -> PlanResult:
    charger_by_id = {c.id: c for c in chargers}
    route_by_vehicle: Dict[str, RoutePlan] = {r.vehicle_id: r for r in routes}

    alerts: List[str] = []
    commands: List[ChargingCommand] = []
    explanations: Dict[str, Any] = {}

    eligible: List[Tuple[Vehicle, Charger, RoutePlan]] = []

    # --- Filtragem ---
    for v in vehicles:
        if v.state != "DISPONIVEL":
            explanations[v.id] = {"status": "ignorado", "motivo": f"estado={v.state}"}
            continue
        if not v.charger_id:
            explanations[v.id] = {"status": "ignorado", "motivo": "não está ligado a carregador"}
            continue
        ch = charger_by_id.get(v.charger_id)
        if not ch:
            explanations[v.id] = {"status": "ignorado", "motivo": f"carregador {v.charger_id} não existe"}
            continue
        if not ch.enabled:
            explanations[v.id] = {"status": "ignorado", "motivo": f"carregador {ch.id} desativado"}
            continue
        rt = route_by_vehicle.get(v.id)
        if not rt:
            explanations[v.id] = {"status": "ignorado", "motivo": "sem rota atribuída"}
            continue

        eligible.append((v, ch, rt))

    eligible.sort(key=lambda t: compute_urgency(t[0], t[2], now), reverse=True)

    remaining_kw = float(site.site_max_kw)

    # --- Planeamento ---
    for v, ch, rt in eligible:
        deficit_soc = max(0.0, rt.required_soc_min - v.soc)
        minutes_to_start = (rt.start_time - now).total_seconds() / 60.0

        # Preços (agora e melhor até à rota)
        price_now = price_at(prices, now)
        best_price = min_price_until(prices, now, rt.start_time)
        delay_applied = should_delay_charging(price_now, best_price, minutes_to_start, deficit_soc)

        base_expl = {
            "vehicle_id": v.id,
            "charger_id": ch.id,
            "soc_atual": v.soc,
            "soc_min_rota": rt.required_soc_min,
            "defice_soc": deficit_soc,
            "bateria_kwh": v.battery_kwh,
            "minutos_ate_rota": round(minutes_to_start, 1),
            "limite_site_kw": site.site_max_kw,
            "restante_site_kw": round(remaining_kw, 2),
            "limite_carregador_kw": ch.max_kw,
            "route_start_time": rt.start_time.isoformat(),
            "route_end_time": rt.end_time.isoformat(),
            "price_now_eur_kwh": price_now,
            "best_price_until_route_eur_kwh": best_price,
            "price_delay_applied": delay_applied,
        }

        if deficit_soc <= 0:
            explanations[v.id] = {**base_expl, "status": "ok", "motivo": "já tem SoC suficiente para a rota"}
            continue

        if minutes_to_start <= 0:
            msg = "rota já devia ter começado"
            alerts.append(f"⚠️ Veículo {v.id}: {msg}.")
            explanations[v.id] = {**base_expl, "status": "erro", "motivo": msg}
            continue

        need_kwh = deficit_soc * v.battery_kwh
        hours = minutes_to_start / 60.0
        avg_kw_needed = need_kwh / max(hours, 1e-6)

        # potência necessária para cumprir deadline
        requested_kw = min(avg_kw_needed, ch.max_kw, remaining_kw)
        requested_kw = clamp(requested_kw, 0.0, ch.max_kw)

        # --- custo: se está caro e há tempo, adiar (MVP) ---
        if delay_applied:
            requested_kw = 0.0

        final_kw = battery_friendly_kw(v, ch, requested_kw)

        explanations[v.id] = {
            **base_expl,
            "need_kwh": round(need_kwh, 2),
            "kw_medio_necessario": round(avg_kw_needed, 2),
            "kw_pedido": round(requested_kw, 2),
            "kw_final": round(final_kw, 2),
            "status": "planeado" if final_kw > 0.01 else "não_planeado",
            "motivo": (
                "Potência escolhida para cumprir SoC mínimo e minimizar custo "
                "(adiando quando está caro), respeitando limites e proteção de bateria."
            ),
        }

        if final_kw <= 0.01:
            # adiou por custo ou não havia margem -> não cria comando
            continue

        remaining_kw -= final_kw

        commands.append(
            ChargingCommand(
                vehicle_id=v.id,
                charger_id=ch.id,
                set_kw=final_kw,
                reason="Urgência/rota + limites do site + proteção da bateria + custo energia",
            )
        )

        if v.soc < rt.required_soc_min and minutes_to_start < 60:
            alerts.append(f"🚨 Veículo {v.id} crítico: rota em <60min, a carregar a {final_kw:.1f} kW.")

        if remaining_kw <= 0.01:
            break

    total_kw = site.site_max_kw - remaining_kw
    return PlanResult(ts=now, total_kw=total_kw, commands=commands, alerts=alerts, explanations=explanations)

