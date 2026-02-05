from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Literal

VehicleState = Literal["DISPONIVEL", "EM_ROTA", "MANUTENCAO"]

@dataclass
class Vehicle:
    id: str
    battery_kwh: float
    soc: float
    soh: float
    temp_c: float
    state: VehicleState
    charger_id: Optional[str] = None

@dataclass
class Charger:
    id: str
    max_kw: float
    enabled: bool = True
    efficiency: float = 0.92

@dataclass
class RoutePlan:
    id: str
    vehicle_id: str
    start_time: datetime
    end_time: datetime
    distance_km: float
    eta_minutes: float
    consumption_kwh: float
    required_soc_min: float

@dataclass
class EnergyPricePoint:
    ts: datetime
    eur_per_kwh: float

@dataclass
class SiteConstraints:
    site_max_kw: float
