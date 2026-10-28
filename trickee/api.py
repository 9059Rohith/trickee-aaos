"""Authenticated Trickee cloud API and fleet console."""
from __future__ import annotations

import asyncio
import hmac
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from .domain import MockChargerProvider, Route, SignalFrame, VehicleProfile, predict_range, rank_chargers
from .store import CloudStore, ConsentRequiredError
from .daily_plans import calculate_plan
from .dispatch import DispatchConflict, DispatchNotFound, DispatchStore
from .orders import OrderConflict, OrderNotFound, OrderStore
from .partner_orders import PartnerOrderStore
from .service_cases import ServiceCaseConflict, ServiceCaseNotFound, ServiceCaseStore
from .operator_tokens import OperatorTokenInvalid, OperatorTokenVerifier
from .ocpi import OcpiBadResponse, OcpiClient, OcpiUnavailable
from .observability import RequestMetrics, elapsed_ms, new_request_id, request_log
from .replay import trip_timeline
from .ota_rollout import RolloutConflict, RolloutNotFound, RolloutStore
from .charging_sessions import ChargingSessionStore
from .route_context import RouteContext
from .runtime_config import FILE_SECRETS, prepared_environment, validate_deployment
from .providers import (ChargerFeed, ChargerProviderBadResponse, ChargerProviderStale,
                        ChargerProviderUnavailable, Geocoder, PlaceProviderBadResponse,
                        PlaceProviderRateLimited, PlaceProviderUnavailable, RoadProviderBadResponse,
                        RoadProviderUnavailable, RoadRouter, map_provider)


class TelemetryBatch(BaseModel):
    frames: list[SignalFrame] = Field(min_length=1, max_length=100)


class Registration(BaseModel):
    profile: VehicleProfile
    driver_id: str | None = None


class CopilotRequest(BaseModel):
    vehicle_id: str
    question: str = Field(min_length=1, max_length=300)
    route: Route | None = None


class PlanRequest(BaseModel):
    route: Route
    policy: str = "BALANCED"


class CampaignRequest(BaseModel):
    campaign_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    vehicle_id: str
    manifest: dict


class RolloutRequest(BaseModel):
    rollout_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    manifest: dict
    rings: dict[str, list[str]]


class CampaignResult(BaseModel):
    result: Literal["INSTALLED", "ROLLED_BACK", "BLOCKED_VEHICLE_STATE", "REJECTED_SIGNATURE"]
    version_after: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")


class RouteLookup(BaseModel):
    origin: str = Field(min_length=1, max_length=100)
    destination: str = Field(min_length=1, max_length=100)


class GeoPoint(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class RoadRouteRequest(BaseModel):
    origin: GeoPoint
    destination: GeoPoint


class DecisionPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pickup: GeoPoint
    destination: GeoPoint
    preparation_min: float = Field(ge=0, le=240)


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TripStart(StrictBody):
    trip_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    starting_soc_pct: float | None = Field(default=None, ge=0, le=100)
    origin: GeoPoint | None = None


class TripComplete(StrictBody):
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    ending_soc_pct: float | None = Field(default=None, ge=0, le=100)
    destination: GeoPoint | None = None


class TripStopStart(StrictBody):
    stop_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    kind: Literal["WAITING", "CHARGING"]


class TripStopComplete(StrictBody):
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")


class NudgeCreate(StrictBody):
    nudge_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    kind: Literal["DEPARTURE", "CHARGING", "ROUTE_REVIEW"]
    message: str = Field(min_length=1, max_length=280)
    plan_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")

    @model_validator(mode="after")
    def meaningful_message(self) -> "NudgeCreate":
        if not self.message.strip():
            raise ValueError("nudge message cannot be blank")
        return self


class NudgeRespond(StrictBody):
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    outcome: Literal["ACKNOWLEDGED", "DECLINED"]


class DispatchOfferCreate(StrictBody):
    offer_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    pickup: GeoPoint
    destination: GeoPoint
    pickup_label: str = Field(min_length=1, max_length=160)
    destination_label: str = Field(min_length=1, max_length=160)
    message: str = Field(min_length=1, max_length=500)
    external_reference: str | None = Field(default=None, max_length=100)
    expires_in_minutes: int = Field(default=30, ge=5, le=1440)

    @model_validator(mode="after")
    def meaningful_proposal(self) -> "DispatchOfferCreate":
        if not all(value.strip() for value in
                   (self.pickup_label, self.destination_label, self.message)):
            raise ValueError("proposal labels and message cannot be blank")
        if self.pickup == self.destination:
            raise ValueError("pickup and destination must differ")
        return self


class DispatchOfferRespond(StrictBody):
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    outcome: Literal["ACCEPTED", "DECLINED"]


class DispatchOfferCancel(StrictBody):
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    note: str = Field(default="", max_length=280)


class InternalOrderAssign(StrictBody):
    order_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")


class InternalOrderCommand(StrictBody):
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")


class InternalOrderNote(InternalOrderCommand):
    note: str = Field(min_length=3, max_length=280)

    @model_validator(mode="after")
    def meaningful_note(self) -> "InternalOrderNote":
        self.note = self.note.strip()
        if len(self.note) < 3:
            raise ValueError("an operator note is required")
        return self


class ServiceCaseCreate(StrictBody):
    case_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    category: Literal["VEHICLE_FAULT", "CHARGER_PROBLEM", "ROUTE_PROBLEM", "TRIP_ASSISTANCE", "OTHER"]
    priority: Literal["ROUTINE", "URGENT"]
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def meaningful_case(self) -> "ServiceCaseCreate":
        if not self.title.strip() or not self.description.strip():
            raise ValueError("case title and description cannot be blank")
        return self


class ServiceCaseMessage(StrictBody):
    message_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    body: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def meaningful_message(self) -> "ServiceCaseMessage":
        if not self.body.strip():
            raise ValueError("message cannot be blank")
        return self


class ServiceCaseAcknowledge(StrictBody):
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")


class ServiceCaseResolve(StrictBody):
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    resolution_code: Literal["ADVICE_GIVEN", "SERVICE_SCHEDULED", "ISSUE_CLEARED", "NO_ACTION", "OTHER"]
    note: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def meaningful_resolution(self) -> "ServiceCaseResolve":
        if not self.note.strip():
            raise ValueError("resolution note cannot be blank")
        return self


class OsmRef(StrictBody):
    osm_type: Literal["N", "W", "R"]
    osm_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    category: str = Field(pattern=r"^[a-z][a-z0-9_:-]{0,79}$")


class DailyStop(StrictBody):
    label: str = Field(min_length=1, max_length=160)
    requested_arrival_local: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    coordinates: GeoPoint
    osm_ref: OsmRef | None = None


class DailyPlanContent(StrictBody):
    service_date: date
    timezone: str = Field(min_length=3, max_length=64)
    starting_soc_pct: float = Field(ge=0, le=100)
    origin: GeoPoint
    origin_osm_ref: OsmRef | None = None
    stops: list[DailyStop] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def valid_schedule(self) -> "DailyPlanContent":
        try:
            zone = ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must be an IANA timezone") from exc
        times = [stop.requested_arrival_local for stop in self.stops]
        if times != sorted(set(times)):
            raise ValueError("stop arrival times must increase within one service day")
        for arrival in times:
            local = datetime.combine(self.service_date, datetime.strptime(arrival, "%H:%M").time())
            first = local.replace(tzinfo=zone, fold=0)
            second = local.replace(tzinfo=zone, fold=1)
            if first.utcoffset() != second.utcoffset() or first.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != local:
                raise ValueError("stop arrival time is ambiguous or absent in the selected timezone")
        points = [self.origin, *(stop.coordinates for stop in self.stops)]
        if any(a == b for a, b in zip(points, points[1:])):
            raise ValueError("adjacent plan locations must differ")
        return self


class DailyPlanDraft(DailyPlanContent):
    plan_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")


class DailyPlanEdit(DailyPlanContent):
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    expected_revision: int = Field(ge=1)


class PlanConfirm(StrictBody):
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")


class AlertAction(StrictBody):
    note: str | None = Field(default=None, max_length=500)


class PhoneImuWindow(StrictBody):
    source: Literal["PHONE_IMU"] = "PHONE_IMU"
    window_ms: int = Field(ge=1, le=60000)
    accelerometer_present: bool
    gyroscope_present: bool
    accelerometer_samples: int = Field(ge=0, le=5000)
    gyroscope_samples: int = Field(ge=0, le=5000)
    raw_acceleration_rms_mps2: float | None = Field(default=None, ge=0, le=200)
    angular_rate_rms_radps: float | None = Field(default=None, ge=0, le=100)
    accelerometer_accuracy: int = Field(ge=0, le=3)
    gyroscope_accuracy: int = Field(ge=0, le=3)

    @model_validator(mode="after")
    def consistent_samples(self) -> "PhoneImuWindow":
        if self.accelerometer_samples > 0 and (not self.accelerometer_present or self.raw_acceleration_rms_mps2 is None):
            raise ValueError("accelerometer samples require a present sensor and RMS")
        if self.gyroscope_samples > 0 and (not self.gyroscope_present or self.angular_rate_rms_radps is None):
            raise ValueError("gyroscope samples require a present sensor and RMS")
        if self.accelerometer_samples == 0 and self.raw_acceleration_rms_mps2 is not None:
            raise ValueError("accelerometer RMS requires samples")
        if self.gyroscope_samples == 0 and self.angular_rate_rms_radps is not None:
            raise ValueError("gyroscope RMS requires samples")
        return self


class GPSPoint(StrictBody):
    sequence: int = Field(ge=1)
    timestamp: datetime
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    accuracy: float | None = Field(default=None, ge=0, le=1000)
    speed: float | None = Field(default=None, ge=0, le=100)
    heading: float | None = Field(default=None, ge=0, lt=360)
    altitude: float | None = Field(default=None, ge=-500, le=10000)
    mocked: bool = False
    provider: str | None = Field(default=None, max_length=40)
    app_version: str | None = Field(default=None, max_length=40)
    device_model: str | None = Field(default=None, max_length=80)
    imu: PhoneImuWindow | None = None

    @model_validator(mode="after")
    def timestamp_has_timezone(self) -> "GPSPoint":
        if self.timestamp.tzinfo is None:
            raise ValueError("GPS timestamp requires timezone")
        return self


class GPSBatch(StrictBody):
    batch_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    points: list[GPSPoint] = Field(min_length=1, max_length=200)


class PhoneTripConsentChange(StrictBody):
    granted: StrictBool


class RoadMatchRequest(StrictBody):
    points: list[GPSPoint] = Field(min_length=2, max_length=50)

    @model_validator(mode="after")
    def usable_trace(self) -> "RoadMatchRequest":
        if any(point.mocked for point in self.points):
            raise ValueError("mocked positions cannot be road matched")
        if any(current.sequence <= previous.sequence or current.timestamp <= previous.timestamp
               for previous, current in zip(self.points, self.points[1:])):
            raise ValueError("trace points must increase by sequence and timestamp")
        return self


@dataclass(frozen=True)
class OperatorPrincipal:
    tenant_id: str
    role: Literal["admin", "fleet_operator", "viewer"]
    actor_id: str = "legacy-operator-key"


def configured_operator_accounts() -> list[tuple[str, OperatorPrincipal]] | None:
    raw = os.getenv("TRICKEE_OPERATOR_ACCOUNTS_JSON")
    if raw is None:
        return None
    try:
        accounts = json.loads(raw)
        if not isinstance(accounts, list) or not accounts:
            raise ValueError("operator account list must be nonempty")
        parsed = []
        hashes = set()
        actor_ids = set()
        for account in accounts:
            if (not isinstance(account, dict) or not {"tenant_id", "role", "key_sha256"} <= set(account)
                    or set(account) - {"tenant_id", "role", "key_sha256", "actor_id"}):
                raise ValueError("operator account fields are invalid")
            tenant = account["tenant_id"]
            role = account["role"]
            digest = account["key_sha256"]
            if not isinstance(tenant, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tenant) or tenant == "*":
                raise ValueError("operator account tenant is invalid")
            if role not in ("admin", "fleet_operator", "viewer") or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
                raise ValueError("operator account role or key hash is invalid")
            if digest in hashes:
                raise ValueError("operator account key hash is duplicated")
            actor_id = account.get("actor_id", "operator-key-" + digest[:16])
            if (not isinstance(actor_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", actor_id)
                    or (tenant, actor_id) in actor_ids):
                raise ValueError("operator account actor ID is invalid or duplicated")
            hashes.add(digest)
            actor_ids.add((tenant, actor_id))
            parsed.append((digest, OperatorPrincipal(tenant, role, actor_id)))
        return parsed
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("operator account configuration is invalid") from exc


def create_app(db_path: str, operator_key: str, vehicle_key: str,
               allow_legacy_operator: bool = True) -> FastAPI:
    if len(operator_key) < 12 or len(vehicle_key) < 12:
        raise ValueError("keys must be at least 12 characters")
    if hmac.compare_digest(operator_key, vehicle_key):
        raise ValueError("operator and vehicle keys must be distinct")
    scoped_accounts = configured_operator_accounts()
    token_config = [os.getenv(name) for name in (
        "TRICKEE_OIDC_ISSUER", "TRICKEE_OIDC_AUDIENCE", "TRICKEE_OIDC_JWKS_URL",
        "TRICKEE_OIDC_SUBJECTS_JSON")]
    if any(value is not None for value in token_config) and not all(token_config):
        raise ValueError("OIDC configuration requires issuer, audience, JWKS URL and subject allowlist")
    token_verifier = OperatorTokenVerifier(*token_config) if all(token_config) else None
    account_links = {}
    for kind in ('LOGIN', 'REGISTRATION', 'RECOVERY', 'SECURITY'):
        url = os.getenv('TRICKEE_ACCOUNT_' + kind + '_URL', '').strip()
        if url:
            parsed = urlsplit(url)
            if (len(url) > 2048 or parsed.scheme != 'https' or not parsed.hostname
                    or parsed.username or parsed.password or parsed.fragment
                    or any(character.isspace() for character in url)):
                raise ValueError('account access URL must be an HTTPS provider URL without credentials or fragments')
        account_links[kind.lower() + '_url'] = url or None
    tile_url = os.getenv("TRICKEE_MAP_TILE_URL", "").strip()
    tile_attribution = os.getenv("TRICKEE_MAP_ATTRIBUTION", "").strip()
    tile_quality = "UNAVAILABLE"
    if tile_url:
        parsed_tile = urlsplit(tile_url)
        if parsed_tile.scheme != "https" or not parsed_tile.netloc or not all(
            token in tile_url for token in ("{z}", "{x}", "{y}")
        ):
            raise ValueError("map tile URL must be HTTPS with {z}, {x}, {y} placeholders")
        if not tile_attribution:
            raise ValueError("map tile attribution is required")
        if parsed_tile.username or parsed_tile.password:
            raise ValueError("map tile URL must not contain credentials")
        tile_quality = "CONFIGURED"
    elif os.getenv("TRICKEE_DEMO_MODE") == "1":
        tile_url = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
        tile_attribution = '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a>'
        tile_quality = "DEMO"
    reminder_interval = int(os.getenv("TRICKEE_WORKSPACE_REMINDER_SECONDS", "30"))
    if not 1 <= reminder_interval <= 3600:
        raise ValueError("Workspace reminder interval must be between 1 and 3600 seconds")

    @asynccontextmanager
    async def lifespan(application):
        from .workspace_reminders import ReminderWorker
        worker = ReminderWorker(application.state.workspace, reminder_interval).start()
        application.state.workspace_reminder_worker = worker
        try:
            yield
        finally:
            worker.stop()

    api = FastAPI(title="Trickee Vehicle OS", version="0.1.0", lifespan=lifespan)
    api.mount("/assets", StaticFiles(directory=Path(__file__).parent.parent / "fleet-console" / "assets"), name="assets")
    store = CloudStore(db_path)
    api.state.store = store
    dispatch = DispatchStore(store)
    api.state.dispatch = dispatch
    orders = OrderStore(store)
    api.state.orders = orders
    partner_orders = PartnerOrderStore(store)
    api.state.partner_orders = partner_orders
    charging_records = ChargingSessionStore(store)
    api.state.charging_records = charging_records
    service_cases = ServiceCaseStore(store)
    api.state.service_cases = service_cases
    rollouts = RolloutStore(store)
    api.state.rollouts = rollouts
    api.state.request_metrics = RequestMetrics()
    api.state.token_verifier = token_verifier
    road_url = os.getenv("TRICKEE_OSRM_URL", "").strip()
    api.state.road_router = RoadRouter(road_url) if road_url else None
    api.state.route_context = RouteContext(
        os.getenv("TRICKEE_OPENWEATHER_API_KEY"),
        os.getenv("TRICKEE_ELEVATION_URL"),
        elevation_token=os.getenv("TRICKEE_ELEVATION_TOKEN"),
        daily_limit=int(os.getenv("TRICKEE_CONTEXT_DAILY_LIMIT", "100")),
        elevation_cache_seconds=int(os.getenv("TRICKEE_ELEVATION_CACHE_SECONDS", "0")),
    )
    geocoder_url = os.getenv("TRICKEE_GEOCODER_URL", "").strip()
    api.state.geocoder = Geocoder(geocoder_url) if geocoder_url else None
    charger_url = os.getenv("TRICKEE_CHARGER_FEED_URL", "").strip()
    api.state.charger_feed = ChargerFeed(charger_url, os.getenv("TRICKEE_CHARGER_FEED_TOKEN")) if charger_url else None
    ocpi_url = os.getenv("TRICKEE_OCPI_LOCATIONS_URL", "").strip()
    ocpi_token = os.getenv("TRICKEE_OCPI_TOKEN", "").strip()
    if ocpi_url and not ocpi_token:
        raise ValueError("OCPI locations endpoint requires TRICKEE_OCPI_TOKEN")
    api.state.ocpi_client = OcpiClient(ocpi_url, ocpi_token) if ocpi_url else None
    demo_mode = os.getenv("TRICKEE_DEMO_MODE") == "1"

    @api.middleware("http")
    async def response_policy(request, call_next):
        started = time.monotonic()
        request_id = new_request_id()
        try:
            response = await call_next(request)
            status_code = response.status_code
        except Exception:
            status_code = 500
            raise
        finally:
            route = request.scope.get("route")
            template = getattr(route, "path", "UNMATCHED")
            latency = elapsed_ms(started)
            api.state.request_metrics.record(request.method, template, status_code,
                                             latency)
            request_log(request.method, template, status_code, latency, request_id)
        response.headers["X-Request-ID"] = request_id
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers['X-Frame-Options'] = 'DENY'
        route_policy = response.headers.get('Content-Security-Policy', '')
        response.headers['Content-Security-Policy'] = (
            (route_policy.rstrip('; ') + '; ' if route_policy else '')
            + "frame-ancestors 'none'; object-src 'none'; base-uri 'self'"
        )
        response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=(self), accelerometer=(self), gyroscope=(self)'
        # Tile providers require the site's origin, while authenticated API calls
        # should not disclose their request URL to third parties.
        response.headers["Referrer-Policy"] = (
            "no-referrer" if request.url.path.startswith("/api/") else "strict-origin-when-cross-origin"
        )
        return response

    def operator(x_operator_key: str | None = Header(default=None),
                 authorization: str | None = Header(default=None)) -> OperatorPrincipal:
        if authorization is not None:
            if x_operator_key is not None:
                raise HTTPException(status_code=400, detail="use one operator authentication method")
            if api.state.token_verifier is None or not authorization.startswith("Bearer "):
                raise HTTPException(status_code=401, detail="operator authentication required")
            try:
                subject, tenant, role = api.state.token_verifier.verify(authorization[7:])
                return OperatorPrincipal(tenant, role, "oidc:" + subject)
            except OperatorTokenInvalid as exc:
                raise HTTPException(status_code=401, detail="operator authentication required",
                                    headers={"WWW-Authenticate": 'Bearer error="invalid_token"'}) from exc
        if not x_operator_key:
            raise HTTPException(status_code=401, detail="operator authentication required")
        if scoped_accounts is not None:
            digest = hashlib.sha256(x_operator_key.encode()).hexdigest()
            for expected, principal in scoped_accounts:
                if hmac.compare_digest(digest, expected):
                    return principal
        elif allow_legacy_operator and hmac.compare_digest(x_operator_key, operator_key):
            return OperatorPrincipal("*", "admin")
        raise HTTPException(status_code=401, detail="operator authentication required")

    def operator_write(principal: OperatorPrincipal = Depends(operator)) -> OperatorPrincipal:
        if principal.role == "viewer":
            raise HTTPException(status_code=403, detail="operator write access required")
        return principal

    @api.get("/api/v1/ops/metrics")
    def operations_metrics(principal: OperatorPrincipal = Depends(operator)) -> dict:
        if principal.role != "admin" or principal.tenant_id != "*":
            raise HTTPException(status_code=403, detail="global administrator required")
        return {**api.state.request_metrics.snapshot(),
                "telemetry": store.telemetry_metrics()}

    @api.get("/api/v1/ops/tenant-metrics")
    def tenant_operations_metrics(principal: OperatorPrincipal = Depends(operator)) -> dict:
        if principal.role != "admin":
            raise HTTPException(status_code=403, detail="tenant administrator required")
        return {"tenant_id": principal.tenant_id,
                "telemetry": store.telemetry_metrics(tenant_id=principal.tenant_id)}

    def operator_admin(principal: OperatorPrincipal = Depends(operator)) -> OperatorPrincipal:
        if principal.role != "admin":
            raise HTTPException(status_code=403, detail="operator admin access required")
        return principal

    @api.get("/api/v1/partner-orders")
    def partner_order_mirror(principal: OperatorPrincipal = Depends(operator)) -> list[dict]:
        return partner_orders.list_for_operator(principal.tenant_id)

    def vehicle_operator(vehicle_id: str, principal: OperatorPrincipal = Depends(operator)) -> OperatorPrincipal:
        tenant = store.vehicle_tenant(vehicle_id)
        if tenant is None or (principal.tenant_id != "*" and principal.tenant_id != tenant):
            raise HTTPException(status_code=404, detail="vehicle not found")
        return principal

    def vehicle_writer(vehicle_id: str, principal: OperatorPrincipal = Depends(operator_write)) -> OperatorPrincipal:
        tenant = store.vehicle_tenant(vehicle_id)
        if tenant is None or (principal.tenant_id != "*" and principal.tenant_id != tenant):
            raise HTTPException(status_code=404, detail="vehicle not found")
        return principal

    def twin_or_404(vehicle_id: str) -> dict:
        twin = store.twin(vehicle_id)
        if twin is None:
            raise HTTPException(status_code=404, detail="vehicle not found")
        return twin

    def nearby_chargers(latitude: float | None, longitude: float | None) -> dict:
        if latitude is None or longitude is None:
            return {"quality": "UNAVAILABLE", "observed_at": None, "chargers": [], "reason": "LOCATION_UNAVAILABLE"}
        provider = api.state.charger_feed
        if provider is None:
            if demo_mode:
                return {"quality": "SIMULATED", "observed_at": None,
                        "chargers": MockChargerProvider().nearby(latitude, longitude), "reason": None}
            return {"quality": "UNAVAILABLE", "observed_at": None, "chargers": [], "reason": "CHARGER_FEED_NOT_CONFIGURED"}
        try:
            return {**provider.nearby(latitude, longitude), "reason": None}
        except ChargerProviderStale:
            return {"quality": "STALE", "observed_at": None, "chargers": [], "reason": "CHARGER_FEED_STALE"}
        except (ChargerProviderUnavailable, ChargerProviderBadResponse):
            return {"quality": "UNAVAILABLE", "observed_at": None, "chargers": [], "reason": "CHARGER_FEED_UNAVAILABLE"}

    def nearby_ocpi(latitude: float, longitude: float, radius_km: float) -> dict:
        client = api.state.ocpi_client
        if client is None:
            return {"quality": "UNAVAILABLE", "stations": [], "reason": "OCPI_NOT_CONFIGURED"}
        try:
            return {"quality": "PROVIDER", "stations": client.nearby(latitude, longitude, radius_km),
                    "reason": None}
        except (OcpiUnavailable, OcpiBadResponse):
            return {"quality": "UNAVAILABLE", "stations": [], "reason": "OCPI_UNAVAILABLE"}

    def trip_data(vehicle_id: str, trip_id: str, *, include_trace: bool = False) -> dict:
        session = store.trip_session(vehicle_id, trip_id)
        stops = store.trip_stops(vehicle_id, trip_id) if session else []
        frames = store.trip_frames(vehicle_id, trip_id)
        phone = store.gps_trip_summary(trip_id)
        if session is None and not frames:
            raise HTTPException(status_code=404, detail="trip not found")
        first, last = store.trip_endpoints(vehicle_id, trip_id)
        frame_count = store.trip_frame_count(vehicle_id, trip_id)
        reported_distance = None
        if 2 <= len(frames) == frame_count:
            odometers = [frame.get("odometer_km") for frame in frames]
            if all(value is not None for value in odometers) and all(
                later >= earlier for earlier, later in zip(odometers, odometers[1:])
            ):
                reported_distance = round(odometers[-1] - odometers[0], 2)
        def point(frame: dict | None) -> dict | None:
            return {"latitude": frame["latitude"], "longitude": frame["longitude"]} if frame and frame.get("latitude") is not None else None
        origin = ({"latitude": session["origin_lat"], "longitude": session["origin_lon"]}
                  if session and session["origin_lat"] is not None else point(first) or
                  ({"latitude": phone["trace"][0]["latitude"], "longitude": phone["trace"][0]["longitude"]} if phone["trace"] else None))
        destination = ({"latitude": session["destination_lat"], "longitude": session["destination_lon"]}
                       if session and session["destination_lat"] is not None else point(last) or
                       ({"latitude": phone["trace"][-1]["latitude"], "longitude": phone["trace"][-1]["longitude"]} if phone["trace"] else None))
        data = {
            "trip_id": trip_id, "vehicle_id": vehicle_id,
            "driver_id": session["driver_id"] if session else None,
            "status": session["status"] if session else "UNMANAGED",
            "source": session["source"] if session else "TELEMETRY_ONLY",
            "started_at": session["started_at"] if session else first["observed_at"],
            "ended_at": session["ended_at"] if session else None,
            "starting_soc_pct": session["starting_soc_pct"] if session and session["starting_soc_pct"] is not None else first.get("soc_pct") if first else None,
            "starting_soc_source": "MANUAL_DASHBOARD" if session and session["starting_soc_pct"] is not None else "TELEMETRY" if first and first.get("soc_pct") is not None else None,
            "ending_soc_pct": session["ending_soc_pct"] if session and session["ending_soc_pct"] is not None else last.get("soc_pct") if last else None,
            "ending_soc_source": "MANUAL_DASHBOARD" if session and session["ending_soc_pct"] is not None else "TELEMETRY" if last and last.get("soc_pct") is not None else None,
            "telemetry_start_soc_pct": first.get("soc_pct") if first else None,
            "telemetry_end_soc_pct": last.get("soc_pct") if last else None,
            "origin": origin, "destination": destination,
            "reported_distance_km": reported_distance,
            "telemetry_frame_count": frame_count,
            "phone_gps_sample_count": phone["sample_count"],
            "phone_gps_usable_count": phone["usable_count"],
            "phone_imu_window_count": phone["imu_window_count"],
            "phone_imu_accelerometer_samples": phone["imu_accelerometer_samples"],
            "phone_imu_gyroscope_samples": phone["imu_gyroscope_samples"],
            "stops": stops,
            "active_stop": next((stop for stop in stops if stop["status"] == "OPEN"), None),
            "quality": "PARTIAL" if frame_count > len(frames) else "SIMULATED" if frames and all(frame["source"] == "SIMULATOR" for frame in frames) else "REPORTED" if frames else "PHONE_GPS_ONLY" if phone["usable_count"] else "MISSING",
        }
        if include_trace:
            vehicle_trace = [
                {"sequence": frame["sequence"], "observed_at": frame["observed_at"],
                 "latitude": frame["latitude"], "longitude": frame["longitude"], "source": frame["source"]}
                for frame in frames if frame.get("latitude") is not None
            ]
            data["trace"] = sorted(vehicle_trace + phone["trace"], key=lambda item: item["observed_at"])
            data["trace_truncated"] = frame_count > len(frames) or phone["trace_truncated"]
        return data

    def require_vehicle(vehicle_id: str, supplied: str | None) -> None:
        expected = hmac.new(vehicle_key.encode(), vehicle_id.encode(), hashlib.sha256).hexdigest()
        if not supplied or not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="vehicle authentication required")

    @api.get("/health")
    def healthcheck() -> dict:
        return {"status": "ok", "service": "trickee-cloud",
                "mode": "DEMO" if demo_mode else "CONFIGURED"}

    @api.get("/ready")
    def readiness() -> dict:
        try:
            store.db.execute("SELECT 1 FROM vehicles LIMIT 1").fetchone()
        except sqlite3.Error as exc:
            raise HTTPException(status_code=503, detail="database is not ready") from exc
        worker = getattr(api.state, "workspace_reminder_worker", None)
        if worker is not None and (not worker.thread.is_alive() or worker.last_error):
            raise HTTPException(status_code=503, detail="workspace reminders are not ready")
        return {"status": "ready", "service": "trickee-cloud"}

    @api.get("/")
    def console() -> FileResponse:
        return FileResponse(Path(__file__).parent.parent / "fleet-console" / "index.html")

    @api.get("/app.js")
    def console_script() -> FileResponse:
        return FileResponse(Path(__file__).parent.parent / "fleet-console" / "app.js", media_type="text/javascript")

    @api.get("/map.js")
    def map_script() -> FileResponse:
        return FileResponse(Path(__file__).parent.parent / "fleet-console" / "map.js", media_type="text/javascript")

    @api.get("/styles.css")
    def console_style() -> FileResponse:
        return FileResponse(Path(__file__).parent.parent / "fleet-console" / "styles.css", media_type="text/css")

    @api.get("/driver")
    def driver_console() -> FileResponse:
        return FileResponse(Path(__file__).parent.parent / "driver-console" / "index.html")

    @api.get("/driver/app.js")
    def driver_console_script() -> FileResponse:
        return FileResponse(Path(__file__).parent.parent / "driver-console" / "app.js", media_type="text/javascript")

    @api.get("/driver/styles.css")
    def driver_console_style() -> FileResponse:
        return FileResponse(Path(__file__).parent.parent / "driver-console" / "styles.css", media_type="text/css")

    @api.get("/driver/manifest.webmanifest")
    def driver_manifest() -> FileResponse:
        return FileResponse(Path(__file__).parent.parent / "driver-console" / "manifest.webmanifest", media_type="application/manifest+json")

    @api.get("/driver/sw.js")
    def driver_service_worker() -> FileResponse:
        return FileResponse(Path(__file__).parent.parent / "driver-console" / "sw.js", media_type="text/javascript",
                            headers={"Service-Worker-Allowed": "/driver"})

    @api.get("/api/v1/map/config", dependencies=[Depends(operator)])
    def map_config() -> dict:
        return {
            "tile_url": tile_url or None,
            "attribution": tile_attribution or None,
            "quality": tile_quality,
        }

    @api.get("/api/v1/operator/me")
    def operator_identity(principal: OperatorPrincipal = Depends(operator)) -> dict:
        return {"tenant_id": principal.tenant_id, "role": principal.role,
                "scoped": scoped_accounts is not None}

    @api.get('/api/v1/account-access')
    def account_access() -> dict:
        return {**account_links, 'passwords_managed_by': 'IDENTITY_PROVIDER',
                'email_delivery_verified': False,
                'operator_help': 'Use your configured identity provider for password reset, registration, email verification and password changes. Operator access keys are provisioned by your administrator; they are not passwords.',
                'driver_help': 'Contact your fleet administrator to replace a lost driver credential and revoke the old one. Do not send credentials in chat or support messages.'}

    @api.get('/account-access', include_in_schema=False)
    def account_access_page():
        return FileResponse(Path(__file__).parent.parent / 'fleet-console' / 'account-access.html')

    @api.get("/api/v1/places/search", dependencies=[Depends(operator)])
    def search_places(q: str = Query(min_length=3, max_length=160)) -> list[dict]:
        if len(q.strip()) < 3:
            raise HTTPException(status_code=422, detail="place query must contain at least three characters")
        geocoder = api.state.geocoder
        if geocoder is None:
            raise HTTPException(status_code=503, detail="place provider is not configured")
        try:
            return geocoder.search(q.strip())
        except PlaceProviderRateLimited as exc:
            raise HTTPException(status_code=503, detail=str(exc),
                                headers={"Retry-After": str(exc.retry_seconds)}) from exc
        except PlaceProviderUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except PlaceProviderBadResponse as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @api.get("/api/v1/places/reverse", dependencies=[Depends(operator)])
    def reverse_place(latitude: float = Query(ge=-90, le=90), longitude: float = Query(ge=-180, le=180)) -> dict:
        geocoder = api.state.geocoder
        if geocoder is None:
            raise HTTPException(status_code=503, detail="place provider is not configured")
        try:
            result = geocoder.reverse(latitude, longitude)
        except PlaceProviderRateLimited as exc:
            raise HTTPException(status_code=503, detail=str(exc),
                                headers={"Retry-After": str(exc.retry_seconds)}) from exc
        except PlaceProviderUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except PlaceProviderBadResponse as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        if result is None:
            raise HTTPException(status_code=404, detail="place not found")
        return result

    @api.post("/api/v1/vehicles")
    def register(body: Registration, principal: OperatorPrincipal = Depends(operator_admin)) -> dict:
        try:
            store.register(body.profile, body.driver_id, principal.tenant_id if principal.tenant_id != "*" else "default")
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        store.audit("vehicle_registered", body.profile.vehicle_id, "simulator/API", principal.actor_id)
        ingest_key = hmac.new(vehicle_key.encode(), body.profile.vehicle_id.encode(), hashlib.sha256).hexdigest()
        return {"vehicle_id": body.profile.vehicle_id, "ingest_key": ingest_key}

    def driver(x_driver_key: str | None = Header(default=None)) -> dict:
        if not x_driver_key or len(x_driver_key) < 32:
            raise HTTPException(status_code=401, detail="driver credential required")
        identity = store.driver_by_hash(hashlib.sha256(x_driver_key.encode()).hexdigest())
        if identity is None:
            raise HTTPException(status_code=401, detail="driver credential required")
        return identity

    from .personal_workspace import install_workspace
    workspace = install_workspace(api, store, operator, driver)
    from .workspace_files import install_workspace_files
    from .workspace_navigation import install_workspace_navigation
    install_workspace_files(api, store, workspace["operator"], workspace["driver"])
    install_workspace_navigation(api, store, workspace["operator"], workspace["driver"])

    @api.post("/api/v1/vehicles/{vehicle_id}/driver-credentials")
    def issue_driver_credential(vehicle_id: str, principal: OperatorPrincipal = Depends(operator_admin)) -> dict:
        tenant = store.vehicle_tenant(vehicle_id)
        if tenant is None or (principal.tenant_id != "*" and tenant != principal.tenant_id):
            raise HTTPException(status_code=404, detail="vehicle not found")
        token = secrets.token_urlsafe(32)
        try:
            credential = store.issue_driver_credential(vehicle_id, hashlib.sha256(token.encode()).hexdigest())
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        store.audit("driver_credential_issued", vehicle_id, credential["credential_id"], principal.actor_id)
        return {**credential, "token": token}

    @api.get("/api/v1/vehicles/{vehicle_id}/driver-credentials")
    def list_driver_credentials(vehicle_id: str, principal: OperatorPrincipal = Depends(operator_admin)) -> list[dict]:
        tenant = store.vehicle_tenant(vehicle_id)
        if tenant is None or (principal.tenant_id != "*" and tenant != principal.tenant_id):
            raise HTTPException(status_code=404, detail="vehicle not found")
        credentials = store.driver_credentials(vehicle_id)
        store.audit("driver_credentials_read", vehicle_id, f"count:{len(credentials)}", principal.actor_id)
        return credentials

    @api.post("/api/v1/driver-credentials/{credential_id}/revoke")
    def revoke_driver_credential(credential_id: str, principal: OperatorPrincipal = Depends(operator_admin)) -> dict:
        match = re.fullmatch(r"D-([1-9][0-9]*)", credential_id)
        if not match or not store.revoke_driver_credential(int(match.group(1)), principal.tenant_id):
            raise HTTPException(status_code=404, detail="driver credential not found")
        store.audit("driver_credential_revoked", None, credential_id, principal.actor_id)
        return {"credential_id": credential_id, "revoked": True}

    @api.get("/api/v1/driver/me")
    def driver_me(identity: dict = Depends(driver)) -> dict:
        return {"driver_id": identity["driver_id"], "vehicle_id": identity["vehicle_id"],
                "tenant_id": identity["tenant_id"], "twin": twin_or_404(identity["vehicle_id"])}

    @api.get("/api/v1/driver/consent/phone-trip")
    def driver_phone_trip_consent(identity: dict = Depends(driver)) -> dict:
        return store.phone_trip_consent(identity["tenant_id"], identity["driver_id"])

    @api.post("/api/v1/driver/consent/phone-trip")
    def driver_change_phone_trip_consent(body: PhoneTripConsentChange,
                                         identity: dict = Depends(driver)) -> dict:
        return store.set_phone_trip_consent(identity["tenant_id"], identity["driver_id"],
                                            identity["vehicle_id"], body.granted)

    @api.get("/api/v1/driver/phone-data/export")
    def driver_phone_data_export(limit: int = Query(default=1000, ge=1, le=1000),
                                 offset: int = Query(default=0, ge=0),
                                 identity: dict = Depends(driver)) -> dict:
        return store.export_driver_phone_data(identity["tenant_id"], identity["driver_id"], limit, offset)

    @api.delete("/api/v1/driver/phone-data")
    def driver_phone_data_erase(identity: dict = Depends(driver)) -> dict:
        try:
            return store.erase_driver_phone_data(identity["tenant_id"], identity["driver_id"],
                                                 identity["vehicle_id"])
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.get("/api/v1/driver/trips")
    def driver_trips(identity: dict = Depends(driver)) -> list[dict]:
        vehicle_id = identity["vehicle_id"]
        sessions = [store.trip_session(vehicle_id, trip_id) for trip_id in store.trip_ids(vehicle_id)]
        return sorted((trip_data(vehicle_id, session["trip_id"]) for session in sessions
                       if session and session["driver_id"] == identity["driver_id"]),
                      key=lambda trip: trip["started_at"], reverse=True)

    @api.get("/api/v1/driver/daily-plans")
    def driver_daily_plans(identity: dict = Depends(driver)) -> list[dict]:
        return [plan for plan in store.daily_plans(identity["vehicle_id"])
                if plan["driver_id"] == identity["driver_id"] and plan["status"] == "CONFIRMED"]

    def nudge_view(row: dict) -> dict:
        return {key: row[key] for key in ("nudge_id", "vehicle_id", "driver_id", "plan_id", "kind",
                                          "message", "status", "created_at", "responded_at")}

    @api.get("/api/v1/driver/nudges")
    def driver_nudges(identity: dict = Depends(driver)) -> list[dict]:
        return store.nudges(identity["vehicle_id"], identity["driver_id"])

    @api.get("/api/v1/driver/dispatch-proposals")
    def driver_dispatch_proposals(identity: dict = Depends(driver)) -> list[dict]:
        return dispatch.list_for_driver(identity["tenant_id"], identity["vehicle_id"], identity["driver_id"])

    @api.get("/api/v1/driver/internal-orders")
    def driver_internal_orders(identity: dict = Depends(driver)) -> list[dict]:
        return orders.list_for_driver(identity["tenant_id"], identity["vehicle_id"], identity["driver_id"])

    @api.post("/api/v1/driver/internal-orders/{order_id}/pickup")
    def driver_order_pickup(order_id: str, body: InternalOrderCommand,
                            identity: dict = Depends(driver)) -> dict:
        try:
            row, _ = orders.driver_pickup(identity["tenant_id"], identity["vehicle_id"],
                                          identity["driver_id"], order_id, body.idempotency_key)
            return row
        except OrderNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except OrderConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.post("/api/v1/driver/internal-orders/{order_id}/report-delivery")
    def driver_order_report_delivery(order_id: str, body: InternalOrderCommand,
                                     identity: dict = Depends(driver)) -> dict:
        try:
            row, _ = orders.driver_report_delivery(identity["tenant_id"], identity["vehicle_id"],
                                                   identity["driver_id"], order_id, body.idempotency_key)
            return row
        except OrderNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except OrderConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.get("/api/v1/driver/service-cases")
    def driver_service_cases(identity: dict = Depends(driver)) -> list[dict]:
        return service_cases.list_for_driver(identity["tenant_id"], identity["vehicle_id"], identity["driver_id"])

    @api.get("/api/v1/driver/updates")
    async def driver_updates(request: Request, after: int = Query(default=0, ge=0),
                             wait_seconds: int = Query(default=15, ge=0, le=20),
                             x_driver_key: str | None = Header(default=None),
                             identity: dict = Depends(driver)) -> dict:
        deadline = asyncio.get_running_loop().time() + wait_seconds
        credential_hash = hashlib.sha256(x_driver_key.encode()).hexdigest()
        while True:
            current = store.driver_by_hash(credential_hash)
            if current != identity:
                raise HTTPException(status_code=401, detail="driver credential required")
            result = store.changes("driver", identity["tenant_id"], after,
                                   identity["vehicle_id"], identity["driver_id"])
            if result["events"] or result["reset_required"] or asyncio.get_running_loop().time() >= deadline:
                return result
            if await request.is_disconnected():
                return result
            await asyncio.sleep(0.4)

    @api.post("/api/v1/driver/service-cases")
    def driver_create_service_case(body: ServiceCaseCreate, identity: dict = Depends(driver)) -> dict:
        content = body.model_dump(exclude={"case_id", "idempotency_key"})
        content["title"] = content["title"].strip()
        content["description"] = content["description"].strip()
        try:
            row, _ = service_cases.create(identity["tenant_id"], identity["vehicle_id"],
                                          identity["driver_id"], body.case_id,
                                          body.idempotency_key, content)
            return row
        except ServiceCaseNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ServiceCaseConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.get("/api/v1/driver/service-cases/{case_id}/messages")
    def driver_service_case_messages(case_id: str, identity: dict = Depends(driver)) -> list[dict]:
        try:
            return service_cases.messages_for_driver(identity["tenant_id"], identity["vehicle_id"],
                                                     identity["driver_id"], case_id)
        except ServiceCaseNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @api.post("/api/v1/driver/service-cases/{case_id}/messages")
    def driver_add_service_case_message(case_id: str, body: ServiceCaseMessage,
                                        identity: dict = Depends(driver)) -> dict:
        try:
            row, _ = service_cases.add_driver_message(identity["tenant_id"], identity["vehicle_id"],
                                                       identity["driver_id"], case_id, body.message_id,
                                                       body.idempotency_key, body.body.strip())
            return row
        except ServiceCaseNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ServiceCaseConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.post("/api/v1/driver/dispatch-proposals/{offer_id}/seen")
    def driver_seen_dispatch_proposal(offer_id: str, identity: dict = Depends(driver)) -> dict:
        try:
            row, _ = dispatch.mark_seen(identity["tenant_id"], identity["vehicle_id"],
                                        identity["driver_id"], offer_id)
            return row
        except DispatchNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @api.post("/api/v1/driver/dispatch-proposals/{offer_id}/respond")
    def driver_respond_dispatch_proposal(offer_id: str, body: DispatchOfferRespond,
                                         identity: dict = Depends(driver)) -> dict:
        try:
            row, _ = dispatch.respond(identity["tenant_id"], identity["vehicle_id"],
                                      identity["driver_id"], offer_id,
                                      body.idempotency_key, body.outcome)
            return row
        except DispatchNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except DispatchConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.post("/api/v1/driver/nudges/{nudge_id}/respond")
    def driver_respond_nudge(nudge_id: str, body: NudgeRespond, identity: dict = Depends(driver)) -> dict:
        try:
            row, changed = store.respond_nudge(identity["vehicle_id"], identity["driver_id"],
                                               nudge_id, body.idempotency_key, body.outcome)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if changed:
            store.audit("driver_nudge_responded", identity["vehicle_id"], f"{nudge_id}:{body.outcome}", f"driver:{identity['driver_id']}")
        return nudge_view(row)

    @api.get("/api/v1/driver/chargers/nearby")
    def driver_nearby_chargers(latitude: float = Query(ge=-90, le=90),
                               longitude: float = Query(ge=-180, le=180),
                               identity: dict = Depends(driver)) -> dict:
        return nearby_chargers(latitude, longitude)

    @api.get("/api/v1/driver/chargers/ocpi/nearby")
    def driver_nearby_ocpi(latitude: float = Query(ge=-90, le=90),
                           longitude: float = Query(ge=-180, le=180),
                           radius_km: float = Query(default=25, gt=0, le=100),
                           identity: dict = Depends(driver)) -> dict:
        return nearby_ocpi(latitude, longitude, radius_km)

    @api.get("/api/v1/driver/map/config")
    def driver_map_config(identity: dict = Depends(driver)) -> dict:
        return {"tile_url": tile_url or None, "attribution": tile_attribution or None,
                "quality": tile_quality}

    @api.post("/api/v1/driver/routes/road")
    def driver_road_route(body: RoadRouteRequest, identity: dict = Depends(driver)) -> dict:
        return configured_road_route(body)

    @api.post("/api/v1/driver/routes/match")
    def driver_road_match(body: RoadMatchRequest, identity: dict = Depends(driver)) -> dict:
        router = api.state.road_router
        if router is None:
            raise HTTPException(status_code=503, detail="road router is not configured")
        try:
            return router.match_trace([(point.lat, point.lng, int(point.timestamp.timestamp()),
                                        point.accuracy if point.accuracy is not None else 5)
                                       for point in body.points])
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RoadProviderUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except RoadProviderBadResponse as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @api.post("/api/v1/driver/trips")
    def driver_start_trip(body: TripStart, identity: dict = Depends(driver)) -> dict:
        vehicle_id = identity["vehicle_id"]
        try:
            _, created = store.start_trip(vehicle_id, body.trip_id, body.idempotency_key,
                                          body.starting_soc_pct, body.origin.model_dump() if body.origin else None,
                                          source="DRIVER_REPORTED")
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if created:
            store.audit("driver_trip_started", vehicle_id, body.trip_id, f"driver:{identity['driver_id']}")
        return trip_data(vehicle_id, body.trip_id)

    @api.post("/api/v1/driver/trips/{trip_id}/complete")
    def driver_complete_trip(trip_id: str, body: TripComplete, identity: dict = Depends(driver)) -> dict:
        vehicle_id = identity["vehicle_id"]
        session = store.trip_session(vehicle_id, trip_id)
        if session is None or session["driver_id"] != identity["driver_id"]:
            raise HTTPException(status_code=404, detail="driver trip not found")
        try:
            _, changed = store.complete_trip(vehicle_id, trip_id, body.idempotency_key,
                                             body.ending_soc_pct, body.destination.model_dump() if body.destination else None)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if changed:
            store.audit("driver_trip_completed", vehicle_id, trip_id, f"driver:{identity['driver_id']}")
        return trip_data(vehicle_id, trip_id)

    @api.post("/api/v1/driver/trips/{trip_id}/stops")
    def driver_start_stop(trip_id: str, body: TripStopStart, identity: dict = Depends(driver)) -> dict:
        try:
            _, created = store.start_trip_stop(identity["vehicle_id"], identity["driver_id"], trip_id,
                                               body.stop_id, body.idempotency_key, body.kind)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if created:
            store.audit("driver_trip_stop_started", identity["vehicle_id"], f"{trip_id}:{body.stop_id}:{body.kind}", f"driver:{identity['driver_id']}")
        return trip_data(identity["vehicle_id"], trip_id)

    @api.post("/api/v1/driver/trips/{trip_id}/stops/{stop_id}/complete")
    def driver_complete_stop(trip_id: str, stop_id: str, body: TripStopComplete,
                             identity: dict = Depends(driver)) -> dict:
        try:
            _, changed = store.complete_trip_stop(identity["vehicle_id"], identity["driver_id"], trip_id,
                                                  stop_id, body.idempotency_key)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if changed:
            store.audit("driver_trip_stop_completed", identity["vehicle_id"], f"{trip_id}:{stop_id}", f"driver:{identity['driver_id']}")
        return trip_data(identity["vehicle_id"], trip_id)

    @api.post("/api/v1/driver/trips/{trip_id}/gps-batches")
    def driver_gps_batch(trip_id: str, body: GPSBatch, identity: dict = Depends(driver)) -> dict:
        try:
            result = store.ingest_gps_batch(identity["vehicle_id"], identity["driver_id"], trip_id,
                                            body.batch_id, [point.model_dump(mode="json", exclude={"imu"}
                                                if point.imu is None else set()) for point in body.points])
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ConsentRequiredError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return result

    @api.post("/api/v1/telemetry/batches")
    def ingest(body: TelemetryBatch, x_vehicle_key: str | None = Header(default=None)) -> dict:
        if len({frame.vehicle_id for frame in body.frames}) != 1:
            raise HTTPException(status_code=400, detail="batch must belong to one vehicle")
        require_vehicle(body.frames[0].vehicle_id, x_vehicle_key)
        try:
            return store.ingest(body.frames)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    def available_twins(principal: OperatorPrincipal) -> list[dict]:
        # Keep fleet reads total if a row disappears between ID and detail reads.
        return [twin for vehicle_id in store.vehicle_ids(principal.tenant_id) if (twin := store.twin(vehicle_id)) is not None]

    @api.get("/api/v1/vehicles")
    def vehicles(principal: OperatorPrincipal = Depends(operator)) -> list[dict]:
        return available_twins(principal)

    @api.get("/api/v1/vehicles/{vehicle_id}/twin", dependencies=[Depends(vehicle_operator)])
    def twin(vehicle_id: str) -> dict:
        return twin_or_404(vehicle_id)

    @api.get("/api/v1/vehicles/{vehicle_id}/state", dependencies=[Depends(vehicle_operator)])
    def state(vehicle_id: str) -> dict:
        data = twin_or_404(vehicle_id)
        return {k: data[k] for k in ("vehicle_id", "state", "quality", "observed_at", "received_at")}

    @api.get("/api/v1/vehicles/{vehicle_id}/battery", dependencies=[Depends(vehicle_operator)])
    def battery(vehicle_id: str) -> dict:
        data = twin_or_404(vehicle_id)
        return {"vehicle_id": vehicle_id, "battery": data["battery"], "quality": data["quality"], "observed_at": data["observed_at"]}

    @api.get("/api/v1/vehicles/{vehicle_id}/health", dependencies=[Depends(vehicle_operator)])
    def vehicle_health(vehicle_id: str) -> dict:
        return twin_or_404(vehicle_id)["health"]

    @api.get("/api/v1/vehicles/{vehicle_id}/events")
    def events(vehicle_id: str, limit: int = 50,
               principal: OperatorPrincipal = Depends(vehicle_operator)) -> list[dict]:
        twin_or_404(vehicle_id)
        bounded_limit = min(max(limit, 1), 200)
        result = store.events(vehicle_id, bounded_limit)
        store.audit("vehicle_events_read", vehicle_id, f"limit:{bounded_limit}", principal.actor_id)
        return result

    @api.get("/api/v1/vehicles/{vehicle_id}/audit")
    def vehicle_audit_events(vehicle_id: str,
                             before_id: int | None = Query(default=None, ge=1),
                             limit: int = Query(default=50, ge=1, le=100),
                             principal: OperatorPrincipal = Depends(vehicle_operator)) -> dict:
        if principal.role != "admin":
            raise HTTPException(status_code=403, detail="operator admin access required")
        return store.audit_events(vehicle_id, before_id, limit)

    @api.get("/api/v1/vehicles/{vehicle_id}/trips", dependencies=[Depends(vehicle_operator)])
    def trips(vehicle_id: str) -> list[dict]:
        twin_or_404(vehicle_id)
        return sorted((trip_data(vehicle_id, trip_id) for trip_id in store.trip_ids(vehicle_id)),
                      key=lambda trip: trip["started_at"], reverse=True)

    @api.post("/api/v1/vehicles/{vehicle_id}/trips")
    def start_trip(vehicle_id: str, body: TripStart, principal: OperatorPrincipal = Depends(vehicle_writer)) -> dict:
        try:
            _, created = store.start_trip(vehicle_id, body.trip_id, body.idempotency_key, body.starting_soc_pct,
                                          body.origin.model_dump() if body.origin else None)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if created:
            store.audit("trip_started", vehicle_id, body.trip_id, principal.actor_id)
        return trip_data(vehicle_id, body.trip_id)

    @api.get("/api/v1/vehicles/{vehicle_id}/trips/{trip_id}")
    def trip_detail(vehicle_id: str, trip_id: str,
                    principal: OperatorPrincipal = Depends(vehicle_operator)) -> dict:
        twin_or_404(vehicle_id)
        result = trip_data(vehicle_id, trip_id, include_trace=True)
        store.audit("trip_trace_read", vehicle_id, trip_id, principal.actor_id)
        return result

    @api.get("/api/v1/vehicles/{vehicle_id}/trips/{trip_id}/replay")
    def replay_trip(vehicle_id: str, trip_id: str,
                    principal: OperatorPrincipal = Depends(vehicle_operator)) -> dict:
        twin_or_404(vehicle_id)
        if trip_id not in store.trip_ids(vehicle_id):
            raise HTTPException(status_code=404, detail="trip not found")
        frames = store.trip_frames(vehicle_id, trip_id)
        result = trip_timeline(vehicle_id, trip_id, frames,
                               store.trip_frame_count(vehicle_id, trip_id))
        store.audit("trip_replay_read", vehicle_id, trip_id, principal.actor_id)
        return result

    @api.post("/api/v1/vehicles/{vehicle_id}/trips/{trip_id}/complete")
    def complete_trip(vehicle_id: str, trip_id: str, body: TripComplete,
                      principal: OperatorPrincipal = Depends(vehicle_writer)) -> dict:
        try:
            _, completed = store.complete_trip(vehicle_id, trip_id, body.idempotency_key, body.ending_soc_pct,
                                               body.destination.model_dump() if body.destination else None)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if completed:
            store.audit("trip_completed", vehicle_id, trip_id, principal.actor_id)
        return trip_data(vehicle_id, trip_id)

    @api.get("/api/v1/vehicles/{vehicle_id}/daily-plans", dependencies=[Depends(vehicle_operator)])
    def daily_plans(vehicle_id: str) -> list[dict]:
        twin_or_404(vehicle_id)
        return store.daily_plans(vehicle_id)

    @api.get("/api/v1/vehicles/{vehicle_id}/nudges", dependencies=[Depends(vehicle_operator)])
    def vehicle_nudges(vehicle_id: str) -> list[dict]:
        return store.nudges(vehicle_id)

    @api.get("/api/v1/vehicles/{vehicle_id}/dispatch-proposals")
    def vehicle_dispatch_proposals(vehicle_id: str,
                                   principal: OperatorPrincipal = Depends(vehicle_operator)) -> list[dict]:
        try:
            return dispatch.list_for_operator(principal.tenant_id, vehicle_id)
        except DispatchNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @api.get("/api/v1/vehicles/{vehicle_id}/internal-orders")
    def vehicle_internal_orders(vehicle_id: str,
                                principal: OperatorPrincipal = Depends(vehicle_operator)) -> list[dict]:
        try:
            return orders.list_for_operator(principal.tenant_id, vehicle_id)
        except OrderNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @api.post("/api/v1/vehicles/{vehicle_id}/dispatch-proposals/{offer_id}/assign")
    def assign_internal_order(vehicle_id: str, offer_id: str, body: InternalOrderAssign,
                              principal: OperatorPrincipal = Depends(vehicle_writer)) -> dict:
        try:
            row, _ = orders.create_from_accepted_offer(principal.tenant_id, vehicle_id, offer_id,
                                                        body.order_id, body.idempotency_key,
                                                        principal.actor_id)
            return row
        except OrderNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except OrderConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.get("/api/v1/vehicles/{vehicle_id}/internal-orders/{order_id}/events")
    def internal_order_events(vehicle_id: str, order_id: str,
                              principal: OperatorPrincipal = Depends(vehicle_operator)) -> list[dict]:
        try:
            return orders.events_for_operator(principal.tenant_id, vehicle_id, order_id)
        except OrderNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @api.post("/api/v1/vehicles/{vehicle_id}/internal-orders/{order_id}/reconcile")
    def reconcile_internal_order(vehicle_id: str, order_id: str, body: InternalOrderNote,
                                 principal: OperatorPrincipal = Depends(vehicle_writer)) -> dict:
        try:
            row, _ = orders.reconcile(principal.tenant_id, vehicle_id, order_id,
                                      body.idempotency_key, body.note, principal.actor_id)
            return row
        except OrderNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except OrderConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.post("/api/v1/vehicles/{vehicle_id}/internal-orders/{order_id}/cancel")
    def cancel_internal_order(vehicle_id: str, order_id: str, body: InternalOrderNote,
                              principal: OperatorPrincipal = Depends(vehicle_writer)) -> dict:
        try:
            row, _ = orders.cancel(principal.tenant_id, vehicle_id, order_id,
                                   body.idempotency_key, body.note, principal.actor_id)
            return row
        except OrderNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except OrderConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.get("/api/v1/vehicles/{vehicle_id}/service-cases")
    def vehicle_service_cases(vehicle_id: str,
                              principal: OperatorPrincipal = Depends(vehicle_operator)) -> list[dict]:
        try:
            return service_cases.list_for_operator(principal.tenant_id, vehicle_id)
        except ServiceCaseNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @api.get("/api/v1/vehicles/{vehicle_id}/service-cases/{case_id}/messages")
    def vehicle_service_case_messages(vehicle_id: str, case_id: str,
                                      principal: OperatorPrincipal = Depends(vehicle_operator)) -> list[dict]:
        try:
            return service_cases.messages_for_operator(principal.tenant_id, vehicle_id, case_id)
        except ServiceCaseNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @api.get("/api/v1/vehicles/{vehicle_id}/service-cases/{case_id}/events")
    def vehicle_service_case_events(vehicle_id: str, case_id: str,
                                    principal: OperatorPrincipal = Depends(vehicle_operator)) -> list[dict]:
        try:
            return service_cases.events_for_operator(principal.tenant_id, vehicle_id, case_id)
        except ServiceCaseNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @api.post("/api/v1/vehicles/{vehicle_id}/service-cases/{case_id}/messages")
    def vehicle_add_service_case_message(vehicle_id: str, case_id: str, body: ServiceCaseMessage,
                                         principal: OperatorPrincipal = Depends(vehicle_writer)) -> dict:
        try:
            row, _ = service_cases.add_operator_message(principal.tenant_id, vehicle_id,
                                                         principal.actor_id, case_id, body.message_id,
                                                         body.idempotency_key, body.body.strip())
            return row
        except ServiceCaseNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ServiceCaseConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.post("/api/v1/vehicles/{vehicle_id}/service-cases/{case_id}/acknowledge")
    def vehicle_ack_service_case(vehicle_id: str, case_id: str, body: ServiceCaseAcknowledge,
                                 principal: OperatorPrincipal = Depends(vehicle_writer)) -> dict:
        try:
            row, _ = service_cases.acknowledge(principal.tenant_id, vehicle_id, case_id,
                                               body.idempotency_key, principal.actor_id)
            return row
        except ServiceCaseNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ServiceCaseConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.post("/api/v1/vehicles/{vehicle_id}/service-cases/{case_id}/resolve")
    def vehicle_resolve_service_case(vehicle_id: str, case_id: str, body: ServiceCaseResolve,
                                     principal: OperatorPrincipal = Depends(vehicle_writer)) -> dict:
        try:
            row, _ = service_cases.resolve(principal.tenant_id, vehicle_id, case_id,
                                           body.idempotency_key, principal.actor_id,
                                           body.resolution_code, body.note.strip())
            return row
        except ServiceCaseNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ServiceCaseConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.post("/api/v1/vehicles/{vehicle_id}/dispatch-proposals")
    def create_dispatch_proposal(vehicle_id: str, body: DispatchOfferCreate,
                                 principal: OperatorPrincipal = Depends(vehicle_writer)) -> dict:
        payload = body.model_dump(mode="json", exclude={"offer_id", "idempotency_key"})
        try:
            row, _ = dispatch.create(principal.tenant_id, vehicle_id, body.offer_id,
                                     body.idempotency_key, payload, principal.actor_id)
            return row
        except DispatchNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except DispatchConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.get("/api/v1/vehicles/{vehicle_id}/dispatch-proposals/{offer_id}/events")
    def dispatch_proposal_events(vehicle_id: str, offer_id: str,
                                 principal: OperatorPrincipal = Depends(vehicle_operator)) -> list[dict]:
        try:
            return dispatch.events_for_operator(principal.tenant_id, vehicle_id, offer_id)
        except DispatchNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @api.post("/api/v1/vehicles/{vehicle_id}/dispatch-proposals/{offer_id}/cancel")
    def cancel_dispatch_proposal(vehicle_id: str, offer_id: str, body: DispatchOfferCancel,
                                 principal: OperatorPrincipal = Depends(vehicle_writer)) -> dict:
        try:
            row, _ = dispatch.cancel(principal.tenant_id, vehicle_id, offer_id,
                                     body.idempotency_key, body.note.strip(), principal.actor_id)
            return row
        except DispatchNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except DispatchConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.post("/api/v1/vehicles/{vehicle_id}/nudges")
    def create_vehicle_nudge(vehicle_id: str, body: NudgeCreate,
                             principal: OperatorPrincipal = Depends(vehicle_writer)) -> dict:
        try:
            row, created = store.create_nudge(vehicle_id, body.nudge_id, body.idempotency_key,
                                              body.kind, body.message.strip(), body.plan_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if created:
            store.audit("driver_nudge_created", vehicle_id, body.nudge_id, principal.actor_id)
        return nudge_view(row)

    @api.post("/api/v1/vehicles/{vehicle_id}/daily-plans")
    def draft_daily_plan(vehicle_id: str, body: DailyPlanDraft,
                         principal: OperatorPrincipal = Depends(vehicle_writer)) -> dict:
        payload = body.model_dump(mode="json", exclude={"plan_id", "idempotency_key"})
        payload["starting_soc_source"] = "MANUAL_DASHBOARD"
        try:
            plan, created = store.create_daily_plan(vehicle_id, body.plan_id, body.idempotency_key, payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if created:
            store.audit("daily_plan_drafted", vehicle_id, body.plan_id, principal.actor_id)
        return plan

    @api.get("/api/v1/vehicles/{vehicle_id}/daily-plans/{plan_id}", dependencies=[Depends(vehicle_operator)])
    def daily_plan_detail(vehicle_id: str, plan_id: str) -> dict:
        twin_or_404(vehicle_id)
        plan = store.daily_plan(vehicle_id, plan_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="daily plan not found")
        return plan

    @api.put("/api/v1/vehicles/{vehicle_id}/daily-plans/{plan_id}")
    def edit_daily_plan(vehicle_id: str, plan_id: str, body: DailyPlanEdit,
                        principal: OperatorPrincipal = Depends(vehicle_writer)) -> dict:
        payload = body.model_dump(mode="json", exclude={"idempotency_key", "expected_revision"})
        payload["starting_soc_source"] = "MANUAL_DASHBOARD"
        try:
            plan, changed = store.edit_daily_plan(vehicle_id, plan_id, body.idempotency_key,
                                                  body.expected_revision, payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if changed:
            store.audit("daily_plan_edited", vehicle_id, f"{plan_id}:revision:{plan['revision']}", principal.actor_id)
        return plan

    @api.post("/api/v1/vehicles/{vehicle_id}/daily-plans/{plan_id}/confirm")
    def confirm_daily_plan(vehicle_id: str, plan_id: str, body: PlanConfirm,
                           principal: OperatorPrincipal = Depends(vehicle_writer)) -> dict:
        twin = twin_or_404(vehicle_id)
        plan = store.daily_plan(vehicle_id, plan_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="daily plan not found")
        if plan["status"] == "CONFIRMED":
            try:
                confirmed, _ = store.confirm_daily_plan(vehicle_id, plan_id, body.idempotency_key, plan["result"])
                return confirmed
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        result = calculate_plan(vehicle_id, twin["profile"], plan, api.state.road_router)
        try:
            confirmed, created = store.confirm_daily_plan(vehicle_id, plan_id, body.idempotency_key, result)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if created:
            store.audit("daily_plan_confirmed", vehicle_id, plan_id, principal.actor_id)
        return confirmed

    @api.get("/api/v1/vehicles/{vehicle_id}/charging", dependencies=[Depends(vehicle_operator)])
    def charging(vehicle_id: str) -> list[dict]:
        twin_or_404(vehicle_id)
        return [e for e in store.events(vehicle_id, 200) if e["frame"].get("charging")]

    @api.get("/api/v1/fleet/updates")
    async def fleet_updates(request: Request, after: int = Query(default=0, ge=0),
                            wait_seconds: int = Query(default=15, ge=0, le=20),
                            principal: OperatorPrincipal = Depends(operator)) -> dict:
        deadline = asyncio.get_running_loop().time() + wait_seconds
        while True:
            result = store.changes("operator", principal.tenant_id, after)
            if result["events"] or result["reset_required"] or asyncio.get_running_loop().time() >= deadline:
                return result
            if await request.is_disconnected():
                return result
            await asyncio.sleep(0.4)

    @api.get("/api/v1/fleet/summary")
    def fleet_summary(principal: OperatorPrincipal = Depends(operator)) -> dict:
        twins = available_twins(principal)
        case_summary = service_cases.summary(principal.tenant_id)
        return {
            "vehicles": len(twins),
            "online": sum(t["quality"] in ("OK", "SIMULATED") for t in twins),
            "offline": sum(t["quality"] not in ("OK", "SIMULATED") for t in twins),
            "low_battery": sum(t["quality"] != "INVALID" and t["battery"] is not None and t["battery"]["soc_pct"] is not None and t["battery"]["soc_pct"] < 20 for t in twins),
            "charging": sum(t["state"] == "CHARGING" for t in twins),
            "alerts": sum(t["health"]["status"] in ("WARNING", "CRITICAL") for t in twins),
            "service_cases_open": case_summary["open"],
            "service_cases_urgent": case_summary["urgent"],
            "data_quality": "MISSING" if not twins or all(t["quality"] == "MISSING" for t in twins)
                            else "SIMULATED" if all(t["quality"] == "SIMULATED" for t in twins)
                            else "REPORTED" if all(t["quality"] == "OK" for t in twins)
                            else "INVALID" if all(t["quality"] == "INVALID" for t in twins)
                            else "STALE" if all(t["quality"] == "STALE" for t in twins) else "MIXED",
        }

    @api.get("/api/v1/fleet/report")
    def fleet_report(days: int = Query(default=7, ge=1, le=30), principal: OperatorPrincipal = Depends(operator)) -> dict:
        return store.fleet_report(days, principal.tenant_id)

    @api.get("/api/v1/fleet/daily-operations")
    def fleet_daily_operations(report_date: date | None = Query(default=None, alias="date"),
                               timezone_name: str = Query(default="Asia/Kolkata", alias="timezone",
                                                          min_length=3, max_length=64),
                               principal: OperatorPrincipal = Depends(operator)) -> dict:
        try:
            zone = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="timezone must be an IANA timezone") from exc
        selected = report_date or datetime.now(zone).date()
        start = datetime.combine(selected, datetime.min.time(), tzinfo=zone)
        end = datetime.combine(selected + timedelta(days=1), datetime.min.time(), tzinfo=zone)
        start_utc, end_utc = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
        report = store.daily_operations(start_utc, end_utc, principal.tenant_id)
        assignment_report = orders.daily_summary(start_utc, end_utc, principal.tenant_id)
        has_assignment_activity = any(assignment_report.values())
        return {"period": {"date": selected.isoformat(), "timezone": timezone_name,
                           "start": start.isoformat(), "end": end.isoformat()},
                "quality": "PARTIAL" if report["truncated"] else "AGGREGATED" if report["trips_started"] or has_assignment_activity else "MISSING",
                "provenance": "TRIP_SESSION_DRIVER_REPORTED_STOPS_AND_MANUAL_ASSIGNMENT_EVENTS",
                **report, **assignment_report}

    @api.post("/api/v1/fleet/decisions/preview")
    def fleet_decision_preview(body: DecisionPreviewRequest,
                               principal: OperatorPrincipal = Depends(operator)) -> dict:
        def unavailable(reason: str, considered: int = 0, truncated: bool = False) -> dict:
            return {"quality": "UNAVAILABLE", "reason": reason,
                    "recommended_vehicle_id": None, "candidates": [],
                    "vehicles_considered": considered, "vehicles_truncated": truncated,
                    "charger_quality": "NOT_QUERIED", "charging_decision": "NO_VERIFIED_CHARGING_OPPORTUNITY",
                    "traffic_quality": "UNAVAILABLE", "automatic_assignment": False}
        router = api.state.road_router
        if router is None:
            return unavailable("ROAD_ROUTER_NOT_CONFIGURED")
        twins = available_twins(principal)
        if len(twins) > 20:
            return unavailable("FLEET_TOO_LARGE_FOR_PREVIEW", truncated=True)
        try:
            delivery = Route.model_validate(router.route(
                (body.pickup.latitude, body.pickup.longitude),
                (body.destination.latitude, body.destination.longitude))["routes"][0]["route"])
        except (RoadProviderUnavailable, RoadProviderBadResponse, ValueError, KeyError, IndexError) as exc:
            return unavailable(type(exc).__name__.upper(), len(twins))
        candidates = []
        contexts = []
        for twin in twins:
            if not twin.get("driver_id") or twin["quality"] not in ("OK", "SIMULATED"):
                continue
            location = twin.get("location")
            latest = store.events(twin["vehicle_id"], 1)
            if not location or not latest:
                continue
            frame = SignalFrame.model_validate(latest[0]["frame"])
            if frame.quality().value not in ("OK", "SIMULATED") or frame.soc_pct is None:
                continue
            contexts.append((twin, frame, VehicleProfile.model_validate(twin["profile"])))
        pickup = (body.pickup.latitude, body.pickup.longitude)
        origins = [(twin["location"]["latitude"], twin["location"]["longitude"])
                   for twin, _, _ in contexts
                   if (twin["location"]["latitude"], twin["location"]["longitude"]) != pickup]
        try:
            matrix = iter(router.table_metrics_to_destination(origins, pickup)) if origins else iter(())
        except (RoadProviderUnavailable, RoadProviderBadResponse, ValueError) as exc:
            return unavailable(type(exc).__name__.upper(), len(twins))
        for twin, frame, profile in contexts:
            try:
                location = twin["location"]
                at_pickup = (location["latitude"], location["longitude"]) == pickup
                if at_pickup:
                    pickup_distance = pickup_eta = 0.0
                    pickup_soc_low = frame.soc_pct
                else:
                    pickup_route = next(matrix)
                    if pickup_route is None:
                        continue
                    if pickup_route.distance_km == 0:
                        pickup_distance = pickup_eta = 0.0
                        pickup_soc_low = frame.soc_pct
                    else:
                        pickup_leg = Route(distance_km=pickup_route.distance_km,
                                           baseline_eta_min=pickup_route.duration_min,
                                           average_speed_kmh=pickup_route.distance_km / (pickup_route.duration_min / 60),
                                           source="PROVIDER")
                        pickup_estimate = predict_range(frame, profile, pickup_leg)
                        if pickup_estimate is None:
                            continue
                        pickup_distance = pickup_route.distance_km
                        pickup_eta = pickup_route.duration_min
                        pickup_soc_low = pickup_estimate.arrival_soc_interval_pct[0]
                conservative_frame = frame.model_copy(update={"soc_pct": pickup_soc_low})
                delivery_estimate = predict_range(conservative_frame, profile, delivery)
                if delivery_estimate is None:
                    continue
            except (ValueError, KeyError, IndexError, StopIteration):
                continue
            wait_min = max(0.0, body.preparation_min - pickup_eta)
            safe = delivery_estimate.arrival_soc_interval_pct[0] >= profile.reserve_soc_pct
            candidates.append({
                "vehicle_id": twin["vehicle_id"], "driver_id": twin["driver_id"],
                "vehicle_quality": twin["quality"], "road_source": "PROVIDER",
                "pickup_distance_km": round(pickup_distance, 2),
                "pickup_eta_min": round(pickup_eta, 1),
                "delivery_distance_km": round(delivery.distance_km, 2),
                "estimated_wait_min": round(wait_min, 1),
                "conservative_arrival_soc_pct": delivery_estimate.arrival_soc_interval_pct[0],
                "reserve_soc_pct": profile.reserve_soc_pct, "range_safe": safe,
                "risk": delivery_estimate.risk,
            })
        candidates.sort(key=lambda item: (not item["range_safe"], item["pickup_eta_min"],
                                          -item["conservative_arrival_soc_pct"], item["vehicle_id"]))
        recommended = next((item for item in candidates if item["range_safe"]), None)
        charger = (nearby_chargers(body.pickup.latitude, body.pickup.longitude)
                   if recommended and recommended["estimated_wait_min"] >= 10
                   else {"quality": "NOT_QUERIED", "chargers": []})
        charging = ("REVIEW_AT_PICKUP" if recommended and recommended["estimated_wait_min"] >= 10
                    and charger["quality"] == "PROVIDER"
                    and any(c.available and c.distance_km <= 0.5 for c in charger["chargers"])
                    else "NO_VERIFIED_CHARGING_OPPORTUNITY")
        return {"quality": "SIMULATED" if recommended and recommended["vehicle_quality"] == "SIMULATED" else "ESTIMATED" if recommended else "UNAVAILABLE",
                "reason": "ADVISORY_ONLY" if recommended else "NO_RANGE_SAFE_CANDIDATE",
                "recommended_vehicle_id": recommended["vehicle_id"] if recommended else None,
                "preparation_min": body.preparation_min, "delivery_distance_km": round(delivery.distance_km, 2),
                "charger_quality": charger["quality"], "charging_decision": charging,
                "candidates": candidates, "vehicles_considered": len(twins),
                "vehicles_truncated": False,
                "traffic_quality": "UNAVAILABLE", "automatic_assignment": False}

    @api.get("/api/v1/fleet/alerts")
    def fleet_alerts(principal: OperatorPrincipal = Depends(operator)) -> list[dict]:
        alerts = []
        for twin in available_twins(principal):
            vehicle_id = twin["vehicle_id"]
            observed = twin["observed_at"]
            quality = twin["quality"]
            sequence = twin["last_sequence"]
            def add(kind: str, severity: str, message: str, evidence: list[str]) -> None:
                alerts.append({
                    "id": f"{vehicle_id}:{kind}:{sequence or 0}", "vehicle_id": vehicle_id,
                    "type": kind, "severity": severity, "message": message,
                    "observed_at": observed, "quality": quality, "evidence": evidence,
                    "source": "RULE_BASED", "status": "ACTIVE",
                })
            if quality == "MISSING":
                add("telemetry_missing", "WARNING", "No telemetry has been reported for this vehicle.", [])
            elif quality == "STALE":
                add("telemetry_stale", "WARNING", "The latest telemetry frame is stale.", [])
            elif quality == "INVALID":
                add("telemetry_invalid", "WARNING", "The latest telemetry timestamp is invalid.", [])
            battery = twin["battery"]
            if quality in ("OK", "SIMULATED") and battery and battery["soc_pct"] is not None and battery["soc_pct"] < 20:
                add("low_soc", "CRITICAL" if battery["soc_pct"] < 10 else "WARNING",
                    f"Latest reported battery charge: {battery['soc_pct']}%.", ["soc_below_20_pct"])
            health = twin["health"]
            if quality in ("OK", "SIMULATED") and health["status"] in ("WARNING", "CRITICAL"):
                add("health_" + health["status"].lower(), health["status"],
                    "Vehicle health rule requires review.", health["evidence"])
        return store.sync_alerts(alerts, principal.tenant_id)

    @api.get("/api/v1/fleet/notifications")
    def fleet_notifications(limit: int = Query(default=50, ge=1, le=100),
                            principal: OperatorPrincipal = Depends(operator)) -> list[dict]:
        return store.notifications.list_for_tenant(principal.tenant_id, limit)

    @api.post("/api/v1/fleet/notifications/{notice_id}/acknowledge")
    def acknowledge_notification(notice_id: int,
                                 principal: OperatorPrincipal = Depends(operator_write)) -> dict:
        try:
            return store.notifications.acknowledge(notice_id, principal.tenant_id,
                                                   principal.actor_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @api.get("/api/v1/fleet/alerts/history")
    def fleet_alert_history(limit: int = Query(default=100, ge=1, le=200),
                            principal: OperatorPrincipal = Depends(operator)) -> list[dict]:
        fleet_alerts(principal)
        return store.alert_history(principal.tenant_id, limit)

    @api.get("/api/v1/chargers/nearby", dependencies=[Depends(operator)])
    def operator_nearby_chargers(latitude: float = Query(ge=-90, le=90),
                                 longitude: float = Query(ge=-180, le=180)) -> dict:
        return nearby_chargers(latitude, longitude)

    @api.get("/api/v1/chargers/ocpi/nearby", dependencies=[Depends(operator)])
    def operator_nearby_ocpi(latitude: float = Query(ge=-90, le=90),
                             longitude: float = Query(ge=-180, le=180),
                             radius_km: float = Query(default=25, gt=0, le=100)) -> dict:
        return nearby_ocpi(latitude, longitude, radius_km)

    @api.get("/api/v1/charging/records")
    def operator_charging_records(limit: int = Query(default=50, ge=1, le=100),
                                  principal: OperatorPrincipal = Depends(operator_admin)) -> dict:
        return charging_records.list_for_tenant(principal.tenant_id, limit)

    def change_alert(alert_id: str, action: str, body: AlertAction, principal: OperatorPrincipal) -> dict:
        match = re.fullmatch(r"A-([1-9][0-9]*)", alert_id)
        if match is None:
            raise HTTPException(status_code=404, detail="alert not found")
        try:
            updated = store.update_alert(int(match.group(1)), principal.tenant_id, action, body.note, principal.role)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        store.audit("alert_" + action, updated["vehicle_id"], f"{alert_id}:{principal.tenant_id}:{principal.role}", principal.actor_id)
        return updated

    @api.post("/api/v1/fleet/alerts/{alert_id}/acknowledge")
    def acknowledge_alert(alert_id: str, body: AlertAction,
                          principal: OperatorPrincipal = Depends(operator_write)) -> dict:
        return change_alert(alert_id, "acknowledge", body, principal)

    @api.post("/api/v1/fleet/alerts/{alert_id}/resolve")
    def resolve_alert(alert_id: str, body: AlertAction,
                      principal: OperatorPrincipal = Depends(operator_write)) -> dict:
        return change_alert(alert_id, "resolve", body, principal)

    @api.post("/api/v1/vehicles/{vehicle_id}/plan", dependencies=[Depends(vehicle_operator)])
    def plan(vehicle_id: str, body: PlanRequest) -> dict:
        data = twin_or_404(vehicle_id)
        latest = store.events(vehicle_id, 1)
        if not latest:
            return {"quality": "MISSING", "range": None, "eta": None, "chargers": [],
                    "charger_quality": "UNAVAILABLE", "charger_reason": "VEHICLE_TELEMETRY_UNAVAILABLE"}
        frame = SignalFrame.model_validate(latest[0]["frame"])
        profile = VehicleProfile.model_validate(data["profile"])
        prediction = predict_range(frame, profile, body.route)
        inventory = nearby_chargers(frame.latitude, frame.longitude)
        chargers = rank_chargers(inventory["chargers"], frame, profile, body.policy)
        return {"quality": "ESTIMATED", "range": prediction.model_dump(mode="json") if prediction else None, "eta": {"baseline_min": body.route.baseline_eta_min, "trickee_eta_min": round(body.route.baseline_eta_min * body.route.traffic_factor, 1), "quality": "ESTIMATED"},
                "chargers": chargers, "charger_quality": inventory["quality"],
                "charger_observed_at": inventory["observed_at"], "charger_reason": inventory["reason"]}

    @api.post("/api/v1/routes", dependencies=[Depends(operator)])
    def mock_route(body: RouteLookup) -> dict:
        try:
            provider = map_provider(os.getenv("TRICKEE_MAP_PROVIDER", "mock"))
            route = provider.route(body.origin, body.destination)
            return {"route": route.model_dump(), "traffic": provider.traffic(body.origin, body.destination), "quality": "SIMULATED"}
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def configured_road_route(body: RoadRouteRequest) -> dict:
        router = api.state.road_router
        if router is None:
            raise HTTPException(status_code=503, detail="road router is not configured")
        try:
            return router.route(
                (body.origin.latitude, body.origin.longitude),
                (body.destination.latitude, body.destination.longitude),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RoadProviderUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except RoadProviderBadResponse as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @api.post("/api/v1/routes/road", dependencies=[Depends(operator)])
    def road_route(body: RoadRouteRequest) -> dict:
        return configured_road_route(body)

    @api.post("/api/v1/routes/context", dependencies=[Depends(operator)])
    def route_context(body: RoadRouteRequest) -> dict:
        return api.state.route_context.for_route(body.origin.model_dump(), body.destination.model_dump())

    @api.post("/api/v1/driver/routes/context")
    def driver_route_context(body: RoadRouteRequest, identity: dict = Depends(driver)) -> dict:
        return api.state.route_context.for_route(body.origin.model_dump(), body.destination.model_dump())

    @api.post("/api/v1/copilot")
    def copilot(body: CopilotRequest, principal: OperatorPrincipal = Depends(operator)) -> dict:
        if store.vehicle_tenant(body.vehicle_id) not in (principal.tenant_id,) and principal.tenant_id != "*":
            raise HTTPException(status_code=404, detail="vehicle not found")
        data = twin_or_404(body.vehicle_id)
        question = body.question.lower()
        if any(word in question for word in ("brake", "steer", "accelerate", "unlock", "start charging", "set charge")):
            store.audit("copilot_denied", body.vehicle_id, "control intent", principal.actor_id)
            return {"tier": "DENIED", "answer": "Vehicle control is unavailable to the copilot.", "tool": None}
        latest = store.events(body.vehicle_id, 1)
        frame = SignalFrame.model_validate(latest[0]["frame"]) if latest else None
        if any(word in question for word in ("reach", "range", "arrive")) and body.route:
            prediction = predict_range(frame, VehicleProfile.model_validate(data["profile"]), body.route) if frame else None
            return {"tier": "T0", "answer": f"Estimated arrival charge: {prediction.arrival_soc_pct}% ({prediction.arrival_soc_interval_pct[0]}–{prediction.arrival_soc_interval_pct[1]}%)." if prediction else "Battery charge is unavailable.", "tool": "get_range_prediction", "data": prediction.model_dump(mode="json") if prediction else None}
        if "charger" in question and frame:
            inventory = nearby_chargers(frame.latitude, frame.longitude)
            ranked = rank_chargers(inventory["chargers"], frame, VehicleProfile.model_validate(data["profile"]))
            if inventory["quality"] in ("UNAVAILABLE", "STALE"):
                return {"tier": "T0", "answer": "Current charger availability is unavailable.", "tool": None,
                        "charger_quality": inventory["quality"]}
            label = "simulated" if inventory["quality"] == "SIMULATED" else "provider-reported"
            return {"tier": "T1", "answer": f"Best {label} charger: {ranked[0]['charger']['name']}." if ranked else f"No reachable charger in current {label} data.", "tool": "find_chargers", "data": ranked, "charger_quality": inventory["quality"]}
        if "battery" in question or "soc" in question:
            soc = data["battery"]["soc_pct"] if data["battery"] else None
            return {"tier": "T0", "answer": f"Battery charge: {soc}%." if soc is not None else "Battery charge is unavailable.", "tool": "get_battery_state"}
        if "health" in question or "fault" in question:
            return {"tier": "T0", "answer": f"Vehicle health: {data['health']['status']}.", "tool": "get_vehicle_health", "data": data["health"]}
        return {"tier": "T0", "answer": "Ask about battery, range, chargers or health.", "tool": None}

    @api.post("/api/v1/ota/campaigns")
    def create_campaign(body: CampaignRequest, principal: OperatorPrincipal = Depends(operator_admin)) -> dict:
        if store.vehicle_tenant(body.vehicle_id) not in (principal.tenant_id,) and principal.tenant_id != "*":
            raise HTTPException(status_code=404, detail="vehicle not found")
        try:
            created = store.create_campaign(body.campaign_id, body.vehicle_id, body.manifest)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if created:
            store.audit("ota_campaign_created", body.vehicle_id, body.campaign_id, principal.actor_id)
        return {"campaign_id": body.campaign_id, "status": "PENDING"}

    @api.post("/api/v1/ota/rollouts")
    def create_rollout(body: RolloutRequest,
                       principal: OperatorPrincipal = Depends(operator_admin)) -> dict:
        try:
            return rollouts.create(body.rollout_id, principal.tenant_id,
                                   body.manifest, body.rings, principal.actor_id)
        except RolloutNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (RolloutConflict, ValueError, sqlite3.IntegrityError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.get("/api/v1/ota/rollouts/{rollout_id}")
    def rollout_status(rollout_id: str,
                       principal: OperatorPrincipal = Depends(operator_admin)) -> dict:
        try:
            return rollouts.status(rollout_id, principal.tenant_id)
        except RolloutNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @api.post("/api/v1/ota/rollouts/{rollout_id}/advance")
    def advance_rollout(rollout_id: str,
                        principal: OperatorPrincipal = Depends(operator_admin)) -> dict:
        try:
            return rollouts.advance(rollout_id, principal.tenant_id, principal.actor_id)
        except RolloutNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (RolloutConflict, ValueError, sqlite3.IntegrityError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.get("/api/v1/vehicles/{vehicle_id}/ota")
    def vehicle_campaigns(vehicle_id: str, x_vehicle_key: str | None = Header(default=None)) -> list[dict]:
        require_vehicle(vehicle_id, x_vehicle_key)
        twin_or_404(vehicle_id)
        return store.campaigns(vehicle_id)

    @api.post("/api/v1/vehicles/{vehicle_id}/ota/{campaign_id}/result")
    def report_campaign(vehicle_id: str, campaign_id: str, body: CampaignResult, x_vehicle_key: str | None = Header(default=None)) -> dict:
        require_vehicle(vehicle_id, x_vehicle_key)
        try:
            changed = store.report_campaign(campaign_id, vehicle_id, body.result, body.version_after)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if changed:
            store.audit("ota_result", vehicle_id, f"{campaign_id}:{body.result}", f"vehicle:{vehicle_id}")
        return {"campaign_id": campaign_id, "result": body.result}

    return api


def app_from_environment() -> FastAPI:
    prepared = prepared_environment(os.environ)
    validate_deployment(prepared)
    injected = {name: prepared[name] for name in FILE_SECRETS
                if name in prepared and name + "_FILE" in os.environ}
    try:
        os.environ.update(injected)
        demo = os.getenv("TRICKEE_DEMO_MODE") == "1"
        scoped = os.getenv("TRICKEE_OPERATOR_ACCOUNTS_JSON") is not None
        token_auth = os.getenv("TRICKEE_OIDC_ISSUER") is not None
        operator_key = os.getenv("TRICKEE_OPERATOR_KEY", "local-demo-operator-key" if demo else "scoped-operator-disabled" if scoped or token_auth else "")
        vehicle_key = os.getenv("TRICKEE_VEHICLE_KEY", "local-demo-vehicle-key" if demo else "")
        if not demo and (operator_key == "local-demo-operator-key" or vehicle_key == "local-demo-vehicle-key"):
            raise ValueError("demo keys are forbidden outside demo mode")
        if not demo and len(vehicle_key) < 32:
            raise ValueError("non-demo vehicle master key must have at least 32 characters")
        if not demo and os.getenv("TRICKEE_OPERATOR_KEY") is not None and len(operator_key) < 32:
            raise ValueError("non-demo legacy operator key must have at least 32 characters")
        return create_app(os.getenv("TRICKEE_DB", "data/cloud.db"), operator_key, vehicle_key,
                          allow_legacy_operator=demo or os.getenv("TRICKEE_OPERATOR_KEY") is not None)
    finally:
        for name in injected:
            os.environ.pop(name, None)


app = app_from_environment() if (os.getenv("TRICKEE_DEPLOYMENT_MODE") in ("staging", "production")
    or os.getenv("TRICKEE_DEMO_MODE") == "1"
    or ((os.getenv("TRICKEE_VEHICLE_KEY") or os.getenv("TRICKEE_VEHICLE_KEY_FILE"))
        and (os.getenv("TRICKEE_OPERATOR_KEY") or os.getenv("TRICKEE_OPERATOR_KEY_FILE")
             or os.getenv("TRICKEE_OPERATOR_ACCOUNTS_JSON") is not None
             or os.getenv("TRICKEE_OPERATOR_ACCOUNTS_JSON_FILE")
             or os.getenv("TRICKEE_OIDC_ISSUER")))) else FastAPI(title="Trickee unconfigured")
