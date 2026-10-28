const $ = (id) => document.getElementById(id);
function syncTheme() {
  const light = document.documentElement.dataset.theme === 'light';
  $('themeToggle').setAttribute('aria-pressed', String(light));
  $('themeToggle').setAttribute('aria-label', light ? 'Switch to dark theme' : 'Switch to light theme');
  $('brandLogo').src = light ? '/assets/trickee-logo-light.png' : '/assets/trickee-logo.png';
}
$('themeToggle').addEventListener('click', () => {
  const light = document.documentElement.dataset.theme !== 'light';
  document.documentElement.dataset.theme = light ? 'light' : 'dark';
  try { localStorage.setItem('trickee-theme', light ? 'light' : 'dark'); } catch { /* Theme remains active for this page. */ }
  syncTheme();
});
syncTheme();
const todayLocal = new Date();
todayLocal.setMinutes(todayLocal.getMinutes() - todayLocal.getTimezoneOffset());
$('dailyDate').value = todayLocal.toISOString().slice(0, 10);
let key = sessionStorage.getItem('trickeeOperatorKey') || '';
let authMode = sessionStorage.getItem('trickeeOperatorAuthMode') === 'token' ? 'token' : 'key';
let selected = null;
let route = null;
let twins = [];
let mapConfigured = false;
let mapConfigLoading = false;
let routeSource = 'SIMULATED';
let routeContextGeneration = 0;
let trips = [];
let selectedTripId = null;
let tripGeneration = 0;
let insightGeneration = 0;
let operatorRole = 'viewer';
let refreshGeneration = 0;
let refreshRequestGeneration = 0;
window.trickeeWorkspaceContext = () => ({connected: !!key && $('connection').classList.contains('connected'), generation: refreshGeneration, driving: false});
let dailyPlanGeneration = 0;
let dailyPlans = [];
let selectedDailyPlanId = null;
let credentialGeneration = 0;
let editingDailyPlanId = null;
let editingDailyRevision = null;
let nudgeGeneration = 0;
let dispatchGeneration = 0;
let auditGeneration = 0;
let auditNextBefore = null;
let auditVehicleId = null;
let chargingRecordsGeneration = 0;
let serviceGeneration = 0;
let changeController = null;
let changeCursor = 0;
let dailyOriginReference = null;
let dailyStopReferences = new Map();

function planStopLine(stop) {
  return `${stop.requested_arrival_local} | ${stop.label} | ${stop.coordinates.latitude}, ${stop.coordinates.longitude}`;
}
function osmRefLabel(ref) {
  return ref ? `OSM ${ref.osm_type}${ref.osm_id} · ${ref.category}` : 'coordinate pin';
}

function leaveDailyPlanEdit() {
  editingDailyPlanId = null;
  editingDailyRevision = null;
  $('saveDailyPlan').textContent = 'Save draft';
  $('cancelDailyPlanEdit').hidden = true;
}
$('cancelDailyPlanEdit').addEventListener('click', () => {
  leaveDailyPlanEdit();
  text('dailyPlanStatus', 'Edit cancelled. Create a new draft or select a saved plan.');
});

function text(id, value) {
  const element = $(id);
  const next = value == null ? '—' : String(value);
  if (element.textContent === next) return;
  element.textContent = next;
  if (element.closest('.metrics')) {
    element.classList.remove('value-updated');
    void element.offsetWidth;
    element.classList.add('value-updated');
  }
}
function error(message) { text('error', message || ''); }
function clearProtectedData() {
  stopChangeWatch();
  dailyOriginReference = null;
  dailyStopReferences = new Map();
  refreshGeneration++; tripGeneration++; insightGeneration++; dailyPlanGeneration++; credentialGeneration++; nudgeGeneration++; dispatchGeneration++; auditGeneration++; chargingRecordsGeneration++; serviceGeneration++;
  for (const storageKey of Object.keys(sessionStorage)) if (storageKey.startsWith('trickee-nudge-') || storageKey.startsWith('trickee-dispatch-') || storageKey.startsWith('trickee-order-') || storageKey.startsWith('trickee-service-')) sessionStorage.removeItem(storageKey);
  twins = []; selected = null; trips = []; selectedTripId = null; dailyPlans = []; selectedDailyPlanId = null;
  leaveDailyPlanEdit();
  route = null; routeSource = 'SIMULATED';
  operatorRole = 'viewer';
  for (const id of ['mVehicles', 'mOnline', 'mLow', 'mCharging', 'mAlerts', 'mServiceCases', 'soc', 'state', 'health',
    'observed', 'soh', 'location', 'software', 'driver', 'reportFrames', 'reportSoc', 'reportSpeed', 'reportLow']) text(id, '—');
  text('vehicleTitle', 'Select a vehicle'); text('quality', 'NO DATA'); text('reportQuality', 'NO DATA');
  $('quality').dataset.quality = 'MISSING'; $('reportQuality').dataset.quality = 'MISSING';
  updateChargeMeter(null); $('vehicleList').replaceChildren();
  text('vehicleList', 'Connect to load vehicles.');
  text('vehicleFilterCount', '');
  $('tripList').replaceChildren(); text('tripDetail', 'Select a trip to review its provenance and trace.');
  $('dailyPlanList').replaceChildren(); text('dailyPlanDetail', 'Select a plan to review it and confirm.');
  $('nudgeList').replaceChildren(); text('nudgeStatus', 'Select a vehicle to review advisories.'); $('nudgeMessage').value = '';
  for (const field of $('nudgeForm').querySelectorAll('select, textarea, button')) field.disabled = true;
  $('dispatchList').replaceChildren(); text('dispatchStatus', 'Select a vehicle to review pickup proposals.'); $('dispatchForm').reset();
  $('partnerOrderList').replaceChildren(); text('partnerOrderStatus', 'Connect to review imported partner records.');
  $('orderList').replaceChildren(); text('orderStatus', 'Select a vehicle to review assignments.');
  $('auditList').replaceChildren(); text('auditStatus', 'Select a vehicle and load its audit trail.'); $('auditWorkspace').hidden = true; $('moreAudit').hidden = true; auditNextBefore = null; auditVehicleId = null;
  $('chargingRecordsList').replaceChildren(); text('chargingRecordsStatus', 'Load provider charging records after an operator sync.'); $('chargingRecordsWorkspace').hidden = true;
  for (const field of $('dispatchForm').querySelectorAll('input, select, textarea, button')) field.disabled = true;
  $('serviceList').replaceChildren(); text('serviceStatus', 'Select a vehicle to review driver service cases.');
  $('fleetAlerts').replaceChildren(); $('fleetAlertHistory').replaceChildren(); text('insightsStatus', 'Connect to load stored signal statistics.');
  text('tripStatus', 'Select a vehicle to view trips.'); text('dailyPlanStatus', 'Select a vehicle to review saved plans.');
  text('planResult', 'Select a vehicle and calculate a route.'); text('copilotResult', 'Responses are calculated from vehicle tools.');
  $('routeOptions').replaceChildren(); $('alert').hidden = true; $('drivingBanner').hidden = true;
  $('driverProvision').hidden = true; $('driverCredentialResult').replaceChildren(); $('driverCredentialList').replaceChildren();
  for (const id of ['question', 'startSoc', 'endSoc', 'dailySoc', 'dailyOrigin', 'dailyStops', 'dailyPlaceQuery', 'placeQuery', 'decisionPickup', 'decisionDestination']) $(id).value = '';
  $('dailyPlaceResults').replaceChildren();
  text('dailyPlaceStatus', 'Place search needs a configured provider. Select a result to set the origin or add a stop.');
  text('decisionResult', 'Enter confirmed pins to preview an advisory decision.');
  text('dailyOperationsResult', 'Choose a local day to review recorded operations.');
  $('origin').value = 'Bengaluru'; $('destination').value = 'Hosur';
  for (const [id, value] of [['distance', '91'], ['eta', '95'], ['speed', '58'], ['traffic', '1.1']]) $(id).value = value;
  text('placeResults', 'Place search requires a configured provider. Submit a query to search.');
  for (const field of $('dailyPlanForm').querySelectorAll('input, textarea, button')) field.disabled = true;
  drawTripControls();
  window.TrickeeMap.showRoute(null); window.TrickeeMap.showTripTrace(null);
  window.TrickeeMap.render([], null, selectVehicle);
}
function resultChanged(id) {
  const element = $(id);
  element.classList.remove('is-fresh');
  void element.offsetWidth;
  element.classList.add('is-fresh');
}
async function withBusy(button, action) {
  button.disabled = true;
  button.setAttribute('aria-busy', 'true');
  try { return await action(); }
  finally {
    button.disabled = (button.id === 'sendNudge' && (!key || operatorRole === 'viewer' || !selected)) ||
      (!($('drivingBanner').hidden) && (button.id === 'findRoute' || button.id === 'roadRouteBtn' || button.id === 'placeSearchBtn' || !!button.closest('#planForm')));
    button.removeAttribute('aria-busy');
  }
}
async function api(path, options = {}) {
  const { responseType = 'json', ...fetchOptions } = options;
  const credentialHeader = authMode === 'token' ? { Authorization: `Bearer ${key}` } : { 'X-Operator-Key': key };
  const response = await fetch(path, { ...fetchOptions, headers: { ...credentialHeader, 'Content-Type': 'application/json', ...(options.headers || {}) } });
  if (!response.ok) {
    const detail = await response.json().catch(() => null);
    const failure = new Error(response.status === 401 ? (authMode === 'token' ? 'Access token is invalid or expired.' : 'Operator key is invalid.') :
      typeof detail?.detail === 'string' ? detail.detail : `Request failed (${response.status}).`);
    const retryAfter = response.headers.get('Retry-After');
    if (response.status === 503 && retryAfter && /^[0-9]{1,3}$/.test(retryAfter))
      failure.message += `${failure.message.endsWith('.') ? '' : '.'} Try again in ${retryAfter} seconds.`;
    failure.status = response.status;
    throw failure;
  }
  if (responseType === 'response') return response;
  if (responseType === 'blob') return response.blob();
  return response.json();
}
function stopChangeWatch() {
  if (changeController) changeController.abort();
  changeController = null;
  changeCursor = 0;
}
function startChangeWatch() {
  if (changeController || !key) return;
  const controller = new AbortController();
  const watchedKey = key, watchedMode = authMode;
  changeController = controller;
  void (async () => {
    while (changeController === controller && key === watchedKey && authMode === watchedMode) {
      try {
        const batch = await api(`/api/v1/fleet/updates?after=${changeCursor}&wait_seconds=15`,
          { signal: controller.signal, cache: 'no-store' });
        if (changeController !== controller || key !== watchedKey || authMode !== watchedMode) break;
        if (batch.reset_required || batch.events.length) {
          if (!await refresh()) throw new Error('Fleet snapshot refresh failed');
          if (changeController !== controller || key !== watchedKey) break;
        }
        changeCursor = batch.next_cursor;
      } catch (failure) {
        if (controller.signal.aborted) break;
        if (failure.status === 401) { stopChangeWatch(); void refresh(); break; }
        await new Promise(resolve => setTimeout(resolve, 1500));
      }
    }
    if (changeController === controller) changeController = null;
  })();
}
function routeBody() {
  return { distance_km: Number($('distance').value), baseline_eta_min: Number($('eta').value), average_speed_kmh: Number($('speed').value), traffic_factor: Number($('traffic').value), source: routeSource };
}
function fmtTime(value) {
  if (!value) return '—';
  return new Date(value).toLocaleString();
}
function updateChargeMeter(soc) {
  const meter = document.querySelector('.charge-track');
  const known = Number.isFinite(soc);
  $('chargeBar').style.width = `${known ? Math.max(0, Math.min(100, soc)) : 0}%`;
  meter.setAttribute('role', known ? 'meter' : 'group');
  meter.setAttribute('aria-label', known ? 'Battery charge' : 'Battery charge unavailable');
  for (const [name, value] of Object.entries({'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': String(soc), 'aria-valuetext': `${soc}%`})) {
    if (known) meter.setAttribute(name, value); else meter.removeAttribute(name);
  }
}
function drawVehicleList() {
  const parent = $('vehicleList'); parent.replaceChildren();
  const query = $('vehicleSearch').value.trim().toLocaleLowerCase();
  const state = $('vehicleStateFilter').value;
  const visible = twins.filter(twin => (!state || twin.state === state) &&
    [twin.vehicle_id, twin.quality, twin.state].join(' ').toLocaleLowerCase().includes(query));
  text('vehicleFilterCount', key ? `${visible.length} of ${twins.length} vehicles shown` : '');
  if (!visible.length) { const p = document.createElement('p'); p.className = 'empty'; p.textContent = !key ? 'Connect to load vehicles.' : twins.length ? 'No vehicles match your filters.' : 'No vehicles registered. Run the simulator demo.'; parent.append(p); return; }
  for (const twin of visible) {
    const button = document.createElement('button'); button.className = `vehicle-row ${selected === twin.vehicle_id ? 'selected' : ''}`;
    button.type = 'button'; button.setAttribute('aria-pressed', String(selected === twin.vehicle_id));
    const left = document.createElement('span'); left.className = 'vehicle-name'; left.textContent = twin.vehicle_id;
    const sub = document.createElement('small'); sub.textContent = `${twin.state} · ${twin.quality}`; left.append(sub);
    const right = document.createElement('strong'); right.textContent = twin.battery?.soc_pct == null ? '—' : `${twin.battery.soc_pct}%`;
    button.append(left, right); button.addEventListener('click', () => selectVehicle(twin.vehicle_id)); parent.append(button);
  }
}
$('vehicleSearch').addEventListener('input', drawVehicleList);
$('vehicleStateFilter').addEventListener('change', drawVehicleList);
$('clearVehicleFilters').addEventListener('click', () => {
  $('vehicleSearch').value = ''; $('vehicleStateFilter').value = ''; drawVehicleList();
});
function selectVehicle(vehicleId) {
  const twin = twins.find(item => item.vehicle_id === vehicleId);
  if (!twin) return;
  if (selected !== vehicleId) {
    leaveDailyPlanEdit();
    dailyOriginReference = null;
    dailyStopReferences = new Map();
    $('driverCredentialResult').replaceChildren();
    $('driverCredentialList').replaceChildren();
    for (const id of ['dailySoc', 'dailyOrigin', 'dailyStops']) $(id).value = '';
  }
  selected = vehicleId;
  drawVehicleList();
  drawTwin(twin);
}
function drawTripControls() {
  $('startTrip').disabled = operatorRole === 'viewer' || !selected || trips.some(trip => trip.status === 'ACTIVE');
  $('endTrip').disabled = operatorRole === 'viewer' || !trips.some(trip => trip.trip_id === selectedTripId && trip.status === 'ACTIVE');
}
function drawTripList() {
  const parent = $('tripList'); parent.replaceChildren();
  if (!trips.length) {
    const empty = document.createElement('p'); empty.className = 'empty'; empty.textContent = 'No trips reported for this vehicle.'; parent.append(empty);
  }
  for (const trip of trips) {
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'trip-row';
    button.setAttribute('aria-pressed', String(trip.trip_id === selectedTripId));
    const label = document.createElement('span'); label.textContent = trip.trip_id;
    const sub = document.createElement('small'); sub.textContent = `${trip.status} · ${trip.quality}`; label.append(sub);
    const count = document.createElement('strong'); count.textContent = `${trip.telemetry_frame_count} frames`;
    button.append(label, count);
    button.addEventListener('click', () => {
      selectedTripId = trip.trip_id;
      drawTripList(); drawTripControls();
      loadTripDetail(trip.trip_id, tripGeneration);
    });
    parent.append(button);
  }
}
function showTripDetail(trip, generation) {
  const parent = $('tripDetail'); parent.replaceChildren();
  const heading = document.createElement('strong'); heading.textContent = `${trip.trip_id} · ${trip.status}`; parent.append(heading);
  const grid = document.createElement('div'); grid.className = 'trip-detail-grid';
  const entries = [
    ['Started', fmtTime(trip.started_at)], ['Ended', fmtTime(trip.ended_at)],
    ['Distance', trip.reported_distance_km == null ? 'Not reported' : `${trip.reported_distance_km} km odometer`],
    ['Start charge', trip.starting_soc_pct == null ? 'Unknown' : `${trip.starting_soc_pct}% · ${trip.starting_soc_source}`],
    ['End charge', trip.ending_soc_pct == null ? 'Unknown' : `${trip.ending_soc_pct}% · ${trip.ending_soc_source}`],
    ['GPS trace', `${trip.trace.length} points${trip.trace_truncated ? ' · truncated' : ''}`],
    ['Driver stops', `${trip.stops?.length || 0} reported${trip.active_stop ? ' · one open' : ''}`],
  ];
  for (const [label, value] of entries) {
    const item = document.createElement('p'); const name = document.createElement('span'); const data = document.createElement('strong');
    name.textContent = label; data.textContent = value; item.append(name, data); grid.append(item);
  }
  parent.append(grid);
  for (const stop of trip.stops || []) {
    const item = document.createElement('p'); item.className = 'route-hint';
    const signal = stop.kind === 'CHARGING'
      ? stop.reconciliation === 'CAR_API_CORROBORATED'
        ? ' · car charging signal observed (charger session unverified)'
        : ' · car charging signal unverified'
      : '';
    item.textContent = `${stop.kind} · ${stop.status} · driver reported${signal} · ${fmtTime(stop.started_at)} → ${fmtTime(stop.ended_at)}`;
    parent.append(item);
  }
  window.TrickeeMap.showTripTrace(trip.trace);
  loadTripReplay(trip.trip_id, generation);
}
async function loadTripReplay(tripId, generation) {
  const vehicleId = selected;
  const parent = $('tripDetail');
  const section = document.createElement('section'); section.className = 'replay-panel';
  const heading = document.createElement('strong'); heading.textContent = 'HISTORICAL SIGNAL REPLAY';
  const status = document.createElement('p'); status.className = 'route-hint';
  status.textContent = 'Loading recorded vehicle frames…';
  section.append(heading, status); parent.append(section);
  try {
    const replay = await api(`/api/v1/vehicles/${encodeURIComponent(vehicleId)}/trips/${encodeURIComponent(tripId)}/replay`);
    if (generation !== tripGeneration || tripId !== selectedTripId || vehicleId !== selected) return;
    if (!replay.timeline.length) {
      status.textContent = 'No vehicle telemetry frames were recorded for this trip.';
      window.TrickeeMap.showReplayPosition(null);
      return;
    }
    const label = document.createElement('label'); label.textContent = 'Recorded frame';
    label.htmlFor = 'replayFrameSlider';
    const slider = document.createElement('input'); slider.type = 'range';
    slider.id = 'replayFrameSlider'; slider.className = 'replay-slider';
    slider.min = '0'; slider.max = String(replay.timeline.length - 1);
    slider.value = String(replay.timeline.length - 1);
    slider.disabled = replay.timeline.length === 1;
    const readout = document.createElement('p'); readout.className = 'replay-readout';
    readout.setAttribute('aria-live', 'polite');
    function renderReplayFrame() {
      const frame = replay.timeline[Number(slider.value)];
      if (!frame) return;
      status.textContent = `${Number(slider.value) + 1}/${replay.timeline.length} recorded frames${replay.truncated ? ' · older frames truncated' : ''} · historical, not live`;
      readout.textContent = `${fmtTime(frame.observed_at)} · ${frame.state} · battery ${frame.soc_pct == null ? 'unknown' : `${frame.soc_pct}%`} · speed ${frame.speed_kmh == null ? 'unknown' : `${frame.speed_kmh} km/h`} · health ${frame.health.status} · source ${frame.original_source}${frame.clock_anomaly ? ' · clock anomaly' : ''}`;
      window.TrickeeMap.showReplayPosition(frame);
    }
    slider.addEventListener('input', renderReplayFrame);
    section.append(label, slider, readout);
    renderReplayFrame();
  } catch (error) {
    if (generation === tripGeneration && tripId === selectedTripId) {
      status.textContent = `Historical replay unavailable: ${error.message}`;
    }
  }
}
async function loadTripDetail(tripId, generation) {
  try {
    const detail = await api(`/api/v1/vehicles/${encodeURIComponent(selected)}/trips/${encodeURIComponent(tripId)}`);
    if (generation !== tripGeneration || tripId !== selectedTripId) return;
    showTripDetail(detail, generation);
  } catch (e) { if (generation === tripGeneration) text('tripDetail', e.message); }
}
async function loadTrips() {
  const generation = ++tripGeneration;
  const vehicleId = selected;
  if (!vehicleId) {
    trips = []; selectedTripId = null; drawTripList(); drawTripControls();
    text('tripStatus', 'Select a vehicle to view trips.');
    window.TrickeeMap.showTripTrace(null);
    return;
  }
  try {
    const next = await api(`/api/v1/vehicles/${encodeURIComponent(vehicleId)}/trips`);
    if (generation !== tripGeneration || vehicleId !== selected) return;
    trips = next;
    if (!trips.some(trip => trip.trip_id === selectedTripId)) selectedTripId = trips[0]?.trip_id || null;
    drawTripList(); drawTripControls();
    text('tripStatus', `${trips.length} trip${trips.length === 1 ? '' : 's'} for ${vehicleId} · explicit sessions and telemetry-only trips are labeled`);
    if (selectedTripId) loadTripDetail(selectedTripId, generation);
    else { text('tripDetail', 'No trip selected.'); window.TrickeeMap.showTripTrace(null); }
  } catch (e) { if (generation === tripGeneration) text('tripStatus', e.message); }
}
function drawDailyPlanDetail(plan) {
  const parent = $('dailyPlanDetail'); parent.replaceChildren();
  if (!plan) { parent.textContent = 'No daily plan selected.'; return; }
  const heading = document.createElement('strong'); heading.textContent = `${plan.service_date} · ${plan.status}`; parent.append(heading);
  const context = document.createElement('p');
  context.textContent = `${plan.stops.length} confirmed map pins · origin ${osmRefLabel(plan.origin_osm_ref)} · ${plan.starting_soc_pct}% ${plan.starting_soc_source} · ${plan.timezone}`;
  parent.append(context);
  if (plan.status === 'DRAFT' && operatorRole !== 'viewer') {
    const edit = document.createElement('button'); edit.type = 'button'; edit.className = 'secondary';
    edit.textContent = 'Edit draft';
    edit.addEventListener('click', () => {
      editingDailyPlanId = plan.plan_id;
      editingDailyRevision = plan.revision;
      $('dailyDate').value = plan.service_date;
      $('dailyTimezone').value = plan.timezone;
      $('dailySoc').value = plan.starting_soc_pct;
      $('dailyOrigin').value = `${plan.origin.latitude}, ${plan.origin.longitude}`;
      dailyOriginReference = plan.origin_osm_ref ? { value: $('dailyOrigin').value, ref: plan.origin_osm_ref } : null;
      dailyStopReferences = new Map(plan.stops.filter(stop => stop.osm_ref)
        .map(stop => [planStopLine(stop), stop.osm_ref]));
      $('dailyStops').value = plan.stops.map(planStopLine).join('\n');
      $('saveDailyPlan').textContent = 'Save changes';
      $('cancelDailyPlanEdit').hidden = false;
      $('dailyPlanForm').scrollIntoView({ behavior: 'auto', block: 'start' });
      text('dailyPlanStatus', `Editing ${plan.plan_id} · revision ${plan.revision}`);
    });
    const button = document.createElement('button'); button.type = 'button'; button.className = 'secondary';
    button.textContent = 'Confirm and calculate';
    button.addEventListener('click', () => confirmDailyPlan(plan.plan_id, button));
    parent.append(edit, button);
  }
  for (const [index, stop] of plan.stops.entries()) {
    const leg = plan.result?.legs?.[index];
    const card = document.createElement('div'); card.className = 'daily-plan-leg';
    const title = document.createElement('strong'); title.textContent = `${stop.requested_arrival_local} · ${stop.label}`;
    const detail = document.createElement('span');
    detail.textContent = !leg ? 'Awaiting confirmation' : leg.route_source === 'PROVIDER'
      ? `${leg.distance_km} km provider route · depart ${new Date(leg.planned_departure_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', timeZone: plan.timezone })} · ${leg.arrival_soc_pct == null ? 'charge prediction unavailable' : `${leg.arrival_soc_pct}% estimated arrival charge`}${leg.schedule_conflict ? ' · schedule conflict' : ''}`
      : `Road and energy prediction unavailable · ${leg.degraded_reason}`;
    detail.textContent += ` · ${osmRefLabel(stop.osm_ref)}`;
    card.append(title, detail); parent.append(card);
  }
  if (plan.result) {
    const evidence = document.createElement('p');
    evidence.textContent = `${plan.result.quality} · ${plan.result.energy_assumptions} Traffic and live charger status unavailable.`;
    parent.append(evidence);
  }
}
function drawDailyPlanList() {
  const parent = $('dailyPlanList'); parent.replaceChildren();
  if (!dailyPlans.length) { const p = document.createElement('p'); p.className = 'empty'; p.textContent = 'No saved plans for this vehicle.'; parent.append(p); }
  for (const plan of dailyPlans) {
    const button = document.createElement('button'); button.type = 'button'; button.className = 'trip-row';
    button.setAttribute('aria-pressed', String(plan.plan_id === selectedDailyPlanId));
    const name = document.createElement('span'); name.textContent = plan.service_date;
    const sub = document.createElement('small'); sub.textContent = `${plan.status} · ${plan.stops.length} stops`; name.append(sub);
    const count = document.createElement('strong'); count.textContent = plan.result?.quality || 'DRAFT';
    button.append(name, count);
    button.addEventListener('click', () => { selectedDailyPlanId = plan.plan_id; drawDailyPlanList(); drawDailyPlanDetail(plan); });
    parent.append(button);
  }
  drawDailyPlanDetail(dailyPlans.find(plan => plan.plan_id === selectedDailyPlanId));
}
async function loadDailyPlans() {
  const generation = ++dailyPlanGeneration;
  const vehicleId = selected;
  if (!vehicleId) { dailyPlans = []; selectedDailyPlanId = null; drawDailyPlanList(); text('dailyPlanStatus', 'Select a vehicle to review saved plans.'); return; }
  try {
    const next = await api(`/api/v1/vehicles/${encodeURIComponent(vehicleId)}/daily-plans`);
    if (generation !== dailyPlanGeneration || selected !== vehicleId) return;
    dailyPlans = next;
    if (!next.some(plan => plan.plan_id === selectedDailyPlanId)) selectedDailyPlanId = next[0]?.plan_id || null;
    drawDailyPlanList();
    text('dailyPlanStatus', `${next.length} saved daily plan${next.length === 1 ? '' : 's'} for ${vehicleId}`);
  } catch (e) { if (generation === dailyPlanGeneration) text('dailyPlanStatus', e.message); }
}
async function loadNudges() {
  const generation = ++nudgeGeneration;
  const vehicleId = selected;
  const parent = $('nudgeList'); parent.replaceChildren();
  if (!vehicleId) { text('nudgeStatus', 'Select a vehicle to review advisories.'); return; }
  try {
    const nudges = await api(`/api/v1/vehicles/${encodeURIComponent(vehicleId)}/nudges`);
    if (generation !== nudgeGeneration || selected !== vehicleId) return;
    for (const nudge of nudges) {
      const card = document.createElement('article'); card.className = 'nudge-card';
      const heading = document.createElement('strong'); heading.textContent = `${nudge.kind.replace('_', ' ')} · ${nudge.status}`;
      const message = document.createElement('p'); message.textContent = nudge.message;
      const meta = document.createElement('small'); meta.textContent = `${fmtTime(nudge.created_at)} · ${nudge.driver_id}${nudge.plan_id ? ` · plan ${nudge.plan_id}` : ''}${nudge.responded_at ? ` · responded ${fmtTime(nudge.responded_at)}` : ''}`;
      card.append(heading, message, meta); parent.append(card);
    }
    if (!nudges.length) parent.textContent = 'No advisories for this vehicle.';
    text('nudgeStatus', `${nudges.length} stored advisor${nudges.length === 1 ? 'y' : 'ies'} for ${vehicleId}. Pending means no driver outcome has been recorded.`);
  } catch (e) { if (generation === nudgeGeneration) text('nudgeStatus', e.message); }
}
function dispatchCoordinates(raw) {
  const parts = raw.split(',').map(value => Number(value.trim()));
  if (parts.length !== 2 || !parts.every(Number.isFinite) || parts[0] < -90 || parts[0] > 90 || parts[1] < -180 || parts[1] > 180)
    throw new Error('Enter coordinates as valid latitude, longitude.');
  return { latitude: parts[0], longitude: parts[1] };
}
async function loadDispatch() {
  const generation = ++dispatchGeneration, vehicleId = selected;
  const parent = $('dispatchList');
  const orderParent = $('orderList');
  const partnerParent = $('partnerOrderList');
  if (!vehicleId) { parent.replaceChildren(); orderParent.replaceChildren(); partnerParent.replaceChildren(); text('dispatchStatus', 'Select a vehicle to review pickup proposals.'); text('orderStatus', 'Select a vehicle to review assignments.'); text('partnerOrderStatus', 'Select a vehicle to review imported partner records.'); return; }
  try {
    const [offers, orders, partnerOrders] = await Promise.all([
      api(`/api/v1/vehicles/${encodeURIComponent(vehicleId)}/dispatch-proposals`),
      api(`/api/v1/vehicles/${encodeURIComponent(vehicleId)}/internal-orders`),
      api('/api/v1/partner-orders'),
    ]);
    if (generation !== dispatchGeneration || selected !== vehicleId) return;
    const assignedOffers = new Set(orders.map(order => order.offer_id));
    const openTimelines = new Set([...parent.querySelectorAll('.nudge-card[data-offer-id]')]
      .filter(card => card.querySelector('.route-hint')?.dataset.open === '1')
      .map(card => card.dataset.offerId));
    parent.replaceChildren();
    for (const offer of offers) {
      const card = document.createElement('article'); card.className = 'nudge-card'; card.dataset.offerId = offer.offer_id;
      const title = document.createElement('strong'); title.textContent = `${offer.pickup_label} → ${offer.destination_label} · ${offer.status}`;
      const message = document.createElement('p'); message.textContent = offer.message;
      const coordinates = document.createElement('small');
      coordinates.textContent = `${offer.pickup.latitude}, ${offer.pickup.longitude} → ${offer.destination.latitude}, ${offer.destination.longitude}`;
      const meta = document.createElement('small');
      meta.textContent = `${offer.driver_id} · sent ${fmtTime(offer.created_at)} · expires ${fmtTime(offer.expires_at)}${offer.seen_at ? ` · seen ${fmtTime(offer.seen_at)}` : ''}${offer.responded_at ? ` · responded ${fmtTime(offer.responded_at)}` : ''}`;
      card.append(title, message, coordinates, meta);
      const actions = document.createElement('div'); actions.className = 'actions';
      const timeline = document.createElement('button'); timeline.type = 'button'; timeline.className = 'quiet'; timeline.textContent = 'View timeline';
      const events = document.createElement('div'); events.className = 'route-hint';
      timeline.addEventListener('click', async () => {
        events.dataset.open = '1';
        timeline.disabled = true;
        try {
          const history = await api(`/api/v1/vehicles/${encodeURIComponent(vehicleId)}/dispatch-proposals/${encodeURIComponent(offer.offer_id)}/events`);
          if (selected === vehicleId) events.textContent = history.map(item => `${fmtTime(item.occurred_at)} · ${item.event_type} · ${item.actor_id}${item.detail ? ` · ${item.detail}` : ''}`).join(' | ');
        } catch (error) { events.textContent = error.message; }
        finally { timeline.disabled = false; }
      });
      actions.append(timeline);
      if (offer.status === 'ACCEPTED' && !assignedOffers.has(offer.offer_id) && operatorRole !== 'viewer') {
        const assign = document.createElement('button'); assign.type = 'button'; assign.className = 'secondary'; assign.textContent = 'Confirm internal assignment';
        assign.addEventListener('click', async () => {
          const storageKey = `trickee-order-assign-${vehicleId}-${offer.offer_id}`;
          let command = JSON.parse(sessionStorage.getItem(storageKey) || 'null');
          if (!command) { command = { order_id: commandId('order-'), idempotency_key: commandId('assign-') }; sessionStorage.setItem(storageKey, JSON.stringify(command)); }
          assign.disabled = true;
          try {
            await api(`/api/v1/vehicles/${encodeURIComponent(vehicleId)}/dispatch-proposals/${encodeURIComponent(offer.offer_id)}/assign`, { method: 'POST', body: JSON.stringify(command) });
            sessionStorage.removeItem(storageKey);
            if (selected === vehicleId) { await loadDispatch(); text('orderStatus', 'Internal assignment saved. The driver can report pickup while connected.'); }
          } catch (error) {
            if (error.status === 409 || error.status === 422) sessionStorage.removeItem(storageKey);
            if (selected === vehicleId) text('orderStatus', error.message);
          } finally { assign.disabled = false; }
        });
        actions.append(assign);
      }
      if (offer.status === 'PENDING' && operatorRole !== 'viewer') {
        const cancel = document.createElement('button'); cancel.type = 'button'; cancel.className = 'quiet'; cancel.textContent = 'Withdraw proposal';
        cancel.addEventListener('click', async () => {
          const storageKey = `trickee-dispatch-cancel-${vehicleId}-${offer.offer_id}`;
          let command = JSON.parse(sessionStorage.getItem(storageKey) || 'null');
          if (!command) { command = { idempotency_key: commandId('cancel-'), note: 'Withdrawn by fleet operator' }; sessionStorage.setItem(storageKey, JSON.stringify(command)); }
          cancel.disabled = true;
          try {
            await api(`/api/v1/vehicles/${encodeURIComponent(vehicleId)}/dispatch-proposals/${encodeURIComponent(offer.offer_id)}/cancel`, { method: 'POST', body: JSON.stringify(command) });
            sessionStorage.removeItem(storageKey);
            if (selected === vehicleId) { await loadDispatch(); text('dispatchStatus', 'Proposal withdrawn. The driver inbox updates while connected.'); }
          } catch (error) {
            if (error.status === 409 || error.status === 422) sessionStorage.removeItem(storageKey);
            if (selected === vehicleId) text('dispatchStatus', error.message);
          } finally { cancel.disabled = false; }
        });
        actions.append(cancel);
      }
      card.append(actions, events); parent.append(card);
      if (openTimelines.has(offer.offer_id)) timeline.click();
    }
    if (!offers.length) parent.textContent = 'No pickup proposals for this vehicle.';
    text('dispatchStatus', `${offers.length} proposal${offers.length === 1 ? '' : 's'} stored for ${vehicleId}. Driver decisions refresh after saved changes.`);
    const orderDrafts = new Map([...orderParent.querySelectorAll('textarea[data-order-draft]')].map(note =>
      [note.dataset.orderDraft, {value: note.value, start: note.selectionStart, end: note.selectionEnd,
        direction: note.selectionDirection, focused: document.activeElement === note}]));
    orderParent.replaceChildren();
    for (const order of orders) {
      const card = document.createElement('article'); card.className = 'nudge-card';
      const title = document.createElement('strong'); title.textContent = `${order.pickup_label} → ${order.destination_label} · ${order.status.replaceAll('_', ' ')}`;
      const details = document.createElement('p'); details.textContent = `${order.order_id} · ${order.driver_id} · ${order.fulfilment_quality.replaceAll('_', ' ').toLowerCase()}`;
      const meta = document.createElement('small'); meta.textContent = `Assigned ${fmtTime(order.created_at)}${order.delivery_reported_at ? ` · driver report ${fmtTime(order.delivery_reported_at)}` : ''}${order.reconciled_at ? ` · reconciled ${fmtTime(order.reconciled_at)}` : ''}`;
      card.append(title, details, meta);
      const actions = document.createElement('div'); actions.className = 'actions';
      const events = document.createElement('div'); events.className = 'route-hint';
      const timeline = document.createElement('button'); timeline.type = 'button'; timeline.className = 'quiet'; timeline.textContent = 'View assignment history';
      timeline.addEventListener('click', async () => {
        timeline.disabled = true;
        try {
          const history = await api(`/api/v1/vehicles/${encodeURIComponent(vehicleId)}/internal-orders/${encodeURIComponent(order.order_id)}/events`);
          if (selected === vehicleId) events.textContent = history.map(item => `${fmtTime(item.occurred_at)} · ${item.event_type.replaceAll('_', ' ')} · ${item.actor_id}${item.detail ? ` · ${item.detail}` : ''}`).join(' | ');
        } catch (error) { events.textContent = error.message; }
        finally { timeline.disabled = false; }
      });
      actions.append(timeline);
      if (operatorRole !== 'viewer' && (order.status === 'ASSIGNED' || order.status === 'DELIVERED_REPORTED')) {
        const action = order.status === 'ASSIGNED' ? 'cancel' : 'reconcile';
        const note = document.createElement('textarea'); note.maxLength = 280; note.rows = 2; note.placeholder = order.status === 'ASSIGNED' ? 'Reason for cancellation' : 'What did you verify manually?'; note.setAttribute('aria-label', note.placeholder);
        note.dataset.orderDraft = `${order.order_id}:${action}`;
        note.value = orderDrafts.get(note.dataset.orderDraft)?.value || '';
        const button = document.createElement('button'); button.type = 'button'; button.className = order.status === 'ASSIGNED' ? 'quiet' : 'secondary'; button.textContent = order.status === 'ASSIGNED' ? 'Cancel assignment' : 'Reconcile driver report';
        button.addEventListener('click', async () => {
          const value = note.value.trim();
          if (value.length < 3) { text('orderStatus', 'Enter a note of at least three characters.'); return; }
          const storageKey = `trickee-order-${action}-${vehicleId}-${order.order_id}`;
          let command = JSON.parse(sessionStorage.getItem(storageKey) || 'null');
          if (!command || command.note !== value) { command = { idempotency_key: commandId(`${action}-`), note: value }; sessionStorage.setItem(storageKey, JSON.stringify(command)); }
          button.disabled = true;
          try {
            await api(`/api/v1/vehicles/${encodeURIComponent(vehicleId)}/internal-orders/${encodeURIComponent(order.order_id)}/${action}`, { method: 'POST', body: JSON.stringify(command) });
            sessionStorage.removeItem(storageKey);
            if (selected === vehicleId) { await loadDispatch(); text('orderStatus', action === 'cancel' ? 'Assignment cancelled.' : 'Driver report manually reconciled.'); }
          } catch (error) {
            if (error.status === 409 || error.status === 422) sessionStorage.removeItem(storageKey);
            if (selected === vehicleId) text('orderStatus', error.message);
          } finally { button.disabled = false; }
        });
        card.append(note); actions.append(button);
      }
      card.append(actions, events); orderParent.append(card);
    }
    for (const note of orderParent.querySelectorAll('textarea[data-order-draft]')) {
      const draft = orderDrafts.get(note.dataset.orderDraft);
      if (draft?.focused) {
        note.focus({preventScroll: true});
        note.setSelectionRange(draft.start, draft.end, draft.direction);
      }
    }
    if (!orders.length) orderParent.textContent = 'No internal assignments for this vehicle.';
    text('orderStatus', `${orders.length} internal assignment${orders.length === 1 ? '' : 's'} stored for ${vehicleId}.`);
    partnerParent.replaceChildren();
    for (const partner of partnerOrders) {
      const card = document.createElement('article'); card.className = 'nudge-card';
      const title = document.createElement('strong'); title.textContent = `${partner.pickup_label} → ${partner.destination_label} · ${partner.status.replaceAll('_', ' ')}`;
      const source = document.createElement('p'); source.textContent = `${partner.source} / ${partner.external_id} · ${partner.vehicle_id || 'unassigned'} · ${partner.vehicle_match.toLowerCase()}`;
      const meta = document.createElement('small'); meta.textContent = `Provider update ${fmtTime(partner.updated_at)} · received ${fmtTime(partner.received_at)} · provider mirror unverified`;
      card.append(title, source, meta); partnerParent.append(card);
    }
    if (!partnerOrders.length) partnerParent.textContent = 'No partner records have been imported.';
    text('partnerOrderStatus', `${partnerOrders.length} imported provider record${partnerOrders.length === 1 ? '' : 's'} in your operator scope.`);
  } catch (error) { if (generation === dispatchGeneration) { text('dispatchStatus', error.message); text('partnerOrderStatus', error.message); } }
}
async function loadAudit(reset = true) {
  if (operatorRole !== 'admin' || !selected) return;
  const vehicleId = selected, generation = ++auditGeneration;
  if (reset || auditVehicleId !== vehicleId) {
    auditVehicleId = vehicleId; auditNextBefore = null; $('auditList').replaceChildren();
  }
  const query = new URLSearchParams({ limit: '30' });
  if (auditNextBefore != null) query.set('before_id', String(auditNextBefore));
  $('loadAudit').disabled = true; $('moreAudit').disabled = true;
  try {
    const result = await api(`/api/v1/vehicles/${encodeURIComponent(vehicleId)}/audit?${query}`);
    if (generation !== auditGeneration || selected !== vehicleId) return;
    for (const item of result.events) {
      const card = document.createElement('article'); card.className = 'nudge-card';
      const title = document.createElement('strong'); title.textContent = item.action.replaceAll('_', ' ');
      const details = document.createElement('small'); details.textContent = `${fmtTime(item.at)} · ${item.actor_id || 'unknown actor'}${item.detail ? ` · ${item.detail}` : ''}`;
      card.append(title, details); $('auditList').append(card);
    }
    auditNextBefore = result.next_before;
    $('moreAudit').hidden = auditNextBefore == null;
    if (!$('auditList').childElementCount) $('auditList').textContent = 'No audit events for this vehicle.';
    text('auditStatus', `${$('auditList').querySelectorAll('.nudge-card').length} events loaded for ${vehicleId}${auditNextBefore == null ? '.' : ' · older events available.'}`);
  } catch (error) { if (generation === auditGeneration) text('auditStatus', error.message); }
  finally { $('loadAudit').disabled = false; $('moreAudit').disabled = false; }
}
$('loadAudit').addEventListener('click', () => { void loadAudit(true); });
$('moreAudit').addEventListener('click', () => { void loadAudit(false); });
async function loadChargingRecords() {
  if (operatorRole !== 'admin') return;
  const generation = ++chargingRecordsGeneration;
  $('loadChargingRecords').disabled = true;
  try {
    const result = await api('/api/v1/charging/records?limit=50');
    if (generation !== chargingRecordsGeneration || operatorRole !== 'admin') return;
    const parent = $('chargingRecordsList'); parent.replaceChildren();
    for (const session of result.sessions) {
      const card = document.createElement('article'); card.className = 'nudge-card';
      const title = document.createElement('strong'); title.textContent = `Session ${session.country_code}/${session.party_id}/${session.session_id} · ${session.status}`;
      const detail = document.createElement('p'); detail.textContent = `${session.kwh} kWh · ${session.total_cost == null ? 'cost unavailable' : `${session.total_cost} ${session.currency}`} · ${session.reconciliation.replaceAll('_', ' ')} · ${session.freshness}`;
      const meta = document.createElement('small'); meta.textContent = `Provider update ${fmtTime(session.last_updated)} · tenant ${session.tenant_id}`;
      card.append(title, detail, meta); parent.append(card);
    }
    for (const cdr of result.cdrs) {
      const card = document.createElement('article'); card.className = 'nudge-card';
      const title = document.createElement('strong'); title.textContent = `CDR ${cdr.country_code}/${cdr.party_id}/${cdr.cdr_id}${cdr.credit ? ' · CREDIT' : ''}`;
      const detail = document.createElement('p'); detail.textContent = `${cdr.total_energy} kWh · ${cdr.total_cost} ${cdr.currency} · ${cdr.reconciliation.replaceAll('_', ' ')}`;
      const meta = document.createElement('small'); meta.textContent = `Ended ${fmtTime(cdr.end_date_time)} · tenant ${cdr.tenant_id}${cdr.session_id ? ` · session ${cdr.session_id}` : ' · no session link'}`;
      card.append(title, detail, meta); parent.append(card);
    }
    if (!parent.childElementCount) parent.textContent = 'No tenant-scoped provider charging records have been synced.';
    text('chargingRecordsStatus', `${result.sessions.length} sessions · ${result.cdrs.length} charge records shown, up to ${result.limited_to} of each. Vehicle assignment remains unavailable.`);
  } catch (error) { if (generation === chargingRecordsGeneration) text('chargingRecordsStatus', error.message); }
  finally { $('loadChargingRecords').disabled = false; }
}
$('loadChargingRecords').addEventListener('click', () => { void loadChargingRecords(); });
async function loadServiceCases() {
  const generation = ++serviceGeneration, vehicleId = selected;
  const parent = $('serviceList');
  if (!vehicleId) { parent.replaceChildren(); text('serviceStatus', 'Select a vehicle to review driver service cases.'); return; }
  try {
    const cases = await api(`/api/v1/vehicles/${encodeURIComponent(vehicleId)}/service-cases`);
    if (generation !== serviceGeneration || selected !== vehicleId) return;
    const drafts = new Map([...parent.querySelectorAll('.nudge-card[data-case-id]')].map(card => [
      card.dataset.caseId, {
        reply: card.querySelector('.service-case-form textarea')?.value || '',
        resolution: card.querySelector('.service-case-form select')?.value || '',
        note: card.querySelector('.service-case-form:last-child textarea')?.value || '',
        conversationOpen: card.querySelector('.route-hint')?.dataset.open === '1',
      },
    ]));
    parent.replaceChildren();
    for (const serviceCase of cases) {
      const caseId = serviceCase.case_id;
      const base = `/api/v1/vehicles/${encodeURIComponent(vehicleId)}/service-cases/${encodeURIComponent(caseId)}`;
      const card = document.createElement('article'); card.className = 'nudge-card'; card.dataset.caseId = caseId;
      const title = document.createElement('strong'); title.textContent = `${serviceCase.priority} · ${serviceCase.category.replaceAll('_', ' ')} · ${serviceCase.status}`;
      const heading = document.createElement('p'); heading.textContent = serviceCase.title;
      const description = document.createElement('p'); description.textContent = serviceCase.description;
      const meta = document.createElement('small'); meta.textContent = `${serviceCase.driver_id} · reported ${fmtTime(serviceCase.created_at)} · updated ${fmtTime(serviceCase.updated_at)}`;
      card.append(title, heading, description, meta);
      if (serviceCase.resolution_note) {
        const resolution = document.createElement('p'); resolution.textContent = `Resolution: ${serviceCase.resolution_code} · ${serviceCase.resolution_note}`;
        card.append(resolution);
      }
      const actions = document.createElement('div'); actions.className = 'actions';
      const history = document.createElement('div'); history.className = 'route-hint';
      const read = document.createElement('button'); read.type = 'button'; read.className = 'quiet'; read.textContent = 'View conversation';
      read.addEventListener('click', async () => {
        history.dataset.open = '1';
        read.disabled = true;
        try {
          const [messages, events] = await Promise.all([api(base + '/messages'), api(base + '/events')]);
          if (selected !== vehicleId) return;
          history.replaceChildren();
          const summary = document.createElement('p'); summary.textContent = events.map(item => `${fmtTime(item.occurred_at)} ${item.event_type}`).join(' · ');
          history.append(summary);
          for (const item of messages) {
            const entry = document.createElement('p'); entry.textContent = `${item.actor_kind} · ${fmtTime(item.created_at)} · ${item.body}`;
            history.append(entry);
          }
          if (!messages.length) { const empty = document.createElement('p'); empty.textContent = 'No follow-up messages yet.'; history.append(empty); }
        } catch (error) { history.textContent = error.message; }
        finally { read.disabled = false; }
      });
      actions.append(read);
      if (operatorRole !== 'viewer' && serviceCase.status === 'OPEN') {
        const acknowledge = document.createElement('button'); acknowledge.type = 'button'; acknowledge.className = 'secondary'; acknowledge.textContent = 'Acknowledge';
        acknowledge.addEventListener('click', async () => {
          const storageKey = `trickee-service-ack-${caseId}`;
          let body = JSON.parse(sessionStorage.getItem(storageKey) || 'null');
          if (!body) { body = { idempotency_key: commandId('ack-') }; sessionStorage.setItem(storageKey, JSON.stringify(body)); }
          acknowledge.disabled = true;
          try {
            await api(base + '/acknowledge', { method: 'POST', body: JSON.stringify(body) });
            sessionStorage.removeItem(storageKey);
            if (selected === vehicleId) await loadServiceCases();
          } catch (error) {
            if (error.status === 409 || error.status === 422) sessionStorage.removeItem(storageKey);
            if (selected === vehicleId) text('serviceStatus', error.message);
          } finally { acknowledge.disabled = false; }
        });
        actions.append(acknowledge);
      }
      card.append(actions, history);
      if (operatorRole !== 'viewer' && serviceCase.status !== 'RESOLVED') {
        const replyForm = document.createElement('form'); replyForm.className = 'service-case-form';
        const replyLabel = document.createElement('label'); replyLabel.textContent = 'Reply to driver';
        const replyText = document.createElement('textarea'); replyText.maxLength = 1000; replyText.required = true; replyText.rows = 2;
        replyText.value = drafts.get(caseId)?.reply || '';
        replyLabel.append(replyText);
        const send = document.createElement('button'); send.type = 'submit'; send.className = 'secondary'; send.textContent = 'Send reply';
        replyForm.append(replyLabel, send);
        replyForm.addEventListener('submit', event => {
          event.preventDefault();
          const message = replyText.value.trim();
          if (!message) { text('serviceStatus', 'Enter a reply.'); return; }
          const storageKey = `trickee-service-reply-${caseId}`;
          let pending = JSON.parse(sessionStorage.getItem(storageKey) || 'null');
          if (!pending || pending.body !== message) {
            pending = { message_id: commandId('MSG-'), idempotency_key: commandId('reply-'), body: message };
            sessionStorage.setItem(storageKey, JSON.stringify(pending));
          }
          send.disabled = true;
          void (async () => {
            try {
              await api(base + '/messages', { method: 'POST', body: JSON.stringify(pending) });
              sessionStorage.removeItem(storageKey);
              replyText.value = '';
              if (selected === vehicleId) { await loadServiceCases(); text('serviceStatus', 'Reply saved for the driver.'); }
            } catch (error) {
              if (error.status === 409 || error.status === 422) sessionStorage.removeItem(storageKey);
              if (selected === vehicleId) text('serviceStatus', error.message);
            } finally { send.disabled = false; }
          })();
        });
        const resolveForm = document.createElement('form'); resolveForm.className = 'service-case-form';
        const codeLabel = document.createElement('label'); codeLabel.textContent = 'Resolution';
        const code = document.createElement('select');
        for (const [value, label] of [['ADVICE_GIVEN', 'Advice given'], ['SERVICE_SCHEDULED', 'Service scheduled'], ['ISSUE_CLEARED', 'Issue cleared'], ['NO_ACTION', 'No action'], ['OTHER', 'Other']]) {
          const option = document.createElement('option'); option.value = value; option.textContent = label; code.append(option);
        }
        if (drafts.get(caseId)?.resolution) code.value = drafts.get(caseId).resolution;
        codeLabel.append(code);
        const noteLabel = document.createElement('label'); noteLabel.textContent = 'Resolution note';
        const note = document.createElement('textarea'); note.maxLength = 500; note.required = true; note.rows = 2; noteLabel.append(note);
        note.value = drafts.get(caseId)?.note || '';
        const resolve = document.createElement('button'); resolve.type = 'submit'; resolve.className = 'quiet'; resolve.textContent = 'Resolve case';
        resolveForm.append(codeLabel, noteLabel, resolve);
        resolveForm.addEventListener('submit', event => {
          event.preventDefault();
          const message = note.value.trim();
          if (!message) { text('serviceStatus', 'Enter a resolution note.'); return; }
          const storageKey = `trickee-service-resolve-${caseId}`;
          let pending = JSON.parse(sessionStorage.getItem(storageKey) || 'null');
          if (!pending || pending.note !== message || pending.resolution_code !== code.value) {
            pending = { idempotency_key: commandId('resolve-'), resolution_code: code.value, note: message };
            sessionStorage.setItem(storageKey, JSON.stringify(pending));
          }
          resolve.disabled = true;
          void (async () => {
            try {
              await api(base + '/resolve', { method: 'POST', body: JSON.stringify(pending) });
              sessionStorage.removeItem(storageKey);
              note.value = '';
              if (selected === vehicleId) { await loadServiceCases(); text('serviceStatus', 'Case resolution saved.'); }
            } catch (error) {
              if (error.status === 409 || error.status === 422) sessionStorage.removeItem(storageKey);
              if (selected === vehicleId) text('serviceStatus', error.message);
            } finally { resolve.disabled = false; }
          })();
        });
        card.append(replyForm, resolveForm);
      }
      parent.append(card);
      if (drafts.get(caseId)?.conversationOpen) read.click();
    }
    if (!cases.length) parent.textContent = 'No service cases reported for this vehicle.';
    text('serviceStatus', `${cases.length} driver case${cases.length === 1 ? '' : 's'} · ${cases.filter(item => item.status !== 'RESOLVED').length} awaiting resolution.`);
  } catch (error) { if (generation === serviceGeneration) text('serviceStatus', error.message); }
}
$('dispatchForm').addEventListener('submit', event => {
  event.preventDefault();
  if (!selected || operatorRole === 'viewer') return;
  const vehicleId = selected;
  let payload;
  try {
    payload = {
      pickup: dispatchCoordinates($('dispatchPickup').value), destination: dispatchCoordinates($('dispatchDestination').value),
      pickup_label: $('dispatchPickupLabel').value.trim(), destination_label: $('dispatchDestinationLabel').value.trim(),
      message: $('dispatchMessage').value.trim(), external_reference: $('dispatchReference').value.trim() || null,
      expires_in_minutes: Number($('dispatchExpiry').value),
    };
    if (!payload.pickup_label || !payload.destination_label || !payload.message) throw new Error('Enter both place names and a message.');
    if (payload.pickup.latitude === payload.destination.latitude && payload.pickup.longitude === payload.destination.longitude)
      throw new Error('Pickup and destination must differ.');
  } catch (error) { text('dispatchStatus', error.message); return; }
  const storageKey = `trickee-dispatch-create-${vehicleId}`;
  let pending = JSON.parse(sessionStorage.getItem(storageKey) || 'null');
  const fingerprint = JSON.stringify(payload);
  if (!pending || pending.fingerprint !== fingerprint) {
    pending = { fingerprint, body: { ...payload, offer_id: commandId('O-'), idempotency_key: commandId('dispatch-') } };
    sessionStorage.setItem(storageKey, JSON.stringify(pending));
  }
  const button = $('sendDispatch'); button.disabled = true;
  void (async () => {
    try {
      await api(`/api/v1/vehicles/${encodeURIComponent(vehicleId)}/dispatch-proposals`, { method: 'POST', body: JSON.stringify(pending.body) });
      sessionStorage.removeItem(storageKey);
      if (selected === vehicleId) { $('dispatchForm').reset(); await loadDispatch(); text('dispatchStatus', 'Proposal saved for the driver. Waiting for a response.'); }
    } catch (error) {
      if (error.status === 409 || error.status === 422) sessionStorage.removeItem(storageKey);
      if (selected === vehicleId) text('dispatchStatus', error.message);
    } finally { button.disabled = !key || operatorRole === 'viewer' || !selected; }
  })();
});
$('nudgeForm').addEventListener('submit', event => {
  event.preventDefault();
  if (!selected || operatorRole === 'viewer') return;
  const vehicleId = selected;
  const message = $('nudgeMessage').value.trim();
  if (!message) { text('nudgeStatus', 'Enter an advisory message.'); return; }
  const storageKey = `trickee-nudge-${vehicleId}`;
  let pending = JSON.parse(sessionStorage.getItem(storageKey) || 'null');
  if (!pending || pending.message !== message || pending.kind !== $('nudgeKind').value) {
    const plan = dailyPlans.find(item => item.plan_id === selectedDailyPlanId && item.status === 'CONFIRMED');
    pending = { nudge_id: commandId('N-'), idempotency_key: commandId('nudge-'),
      kind: $('nudgeKind').value, message, plan_id: plan?.plan_id || null };
    sessionStorage.setItem(storageKey, JSON.stringify(pending));
  }
  withBusy($('sendNudge'), async () => {
    try {
      await api(`/api/v1/vehicles/${encodeURIComponent(vehicleId)}/nudges`, { method: 'POST', body: JSON.stringify(pending) });
      sessionStorage.removeItem(storageKey);
      if (selected !== vehicleId) return;
      $('nudgeMessage').value = '';
      await loadNudges();
      text('nudgeStatus', 'Advisory saved to the driver inbox. Waiting for the driver to open it and respond.');
    } catch (e) {
      if (e.status === 409 || e.status === 422) sessionStorage.removeItem(storageKey);
      if (selected === vehicleId) text('nudgeStatus', e.message);
    }
  });
});
function parseDailyStops(raw) {
  const lines = raw.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
  if (!lines.length || lines.length > 10) throw new Error('Enter 1 to 10 stops.');
  return lines.map((line, index) => {
    const parts = line.split('|').map(part => part.trim());
    if (parts.length !== 3 || !/^([01]\d|2[0-3]):[0-5]\d$/.test(parts[0]) || !parts[1])
      throw new Error(`Stop ${index + 1}: use HH:MM | name | latitude, longitude.`);
    return { requested_arrival_local: parts[0], label: parts[1], coordinates: coordinateInput(parts[2]),
      osm_ref: dailyStopReferences.get(line) || null };
  });
}
$('dailyPlaceForm').addEventListener('submit', event => {
  event.preventDefault();
  withBusy($('dailyPlaceSearch'), async () => {
    const parent = $('dailyPlaceResults'); parent.replaceChildren();
    try {
      const query = $('dailyPlaceQuery').value.trim();
      if (query.length < 3) throw new Error('Enter at least three characters.');
      const places = await api(`/api/v1/places/search?q=${encodeURIComponent(query)}`);
      text('dailyPlaceStatus', places.length ? `${places.length} provider place${places.length === 1 ? '' : 's'} found. Confirm each pin before saving.` : 'No places found by the configured provider.');
      for (const place of places) {
        const card = document.createElement('div'); card.className = 'place-result';
        const name = document.createElement('strong'); name.textContent = place.name;
        const provenance = document.createElement('small');
        provenance.textContent = `${place.source} place · ${osmRefLabel(place.osm_ref)} · ${place.latitude}, ${place.longitude} · © OpenStreetMap contributors`;
        const actions = document.createElement('div');
        const originButton = document.createElement('button'); originButton.type = 'button'; originButton.textContent = 'Use as plan origin';
        originButton.addEventListener('click', () => {
          $('dailyOrigin').value = `${place.latitude}, ${place.longitude}`;
          dailyOriginReference = place.osm_ref ? { value: $('dailyOrigin').value, ref: place.osm_ref } : null;
          text('dailyPlaceStatus', `${place.name} selected as the plan origin. Confirm the pin before saving.`);
        });
        const stopButton = document.createElement('button'); stopButton.type = 'button'; stopButton.textContent = 'Add as plan stop';
        stopButton.addEventListener('click', () => {
          const arrival = $('dailyPlaceTime').value;
          if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(arrival)) { text('dailyPlaceStatus', 'Choose a valid arrival time first.'); return; }
          const label = place.name.replace(/[|\r\n]/g, ' ').slice(0, 160).trim();
          const line = `${arrival} | ${label} | ${place.latitude}, ${place.longitude}`;
          if (place.osm_ref) dailyStopReferences.set(line, place.osm_ref);
          $('dailyStops').value = [$('dailyStops').value.trim(), line].filter(Boolean).join('\n');
          text('dailyPlaceStatus', `${place.name} added at ${arrival}. Confirm stop order and pin before saving.`);
        });
        actions.append(originButton, stopButton); card.append(name, provenance, actions); parent.append(card);
      }
    } catch (error) { text('dailyPlaceStatus', error.message); }
  });
});
async function confirmDailyPlan(planId, button) {
  if (!selected) return;
  const vehicleId = selected;
  await withBusy(button, async () => {
    try {
      const storageKey = `trickee-plan-confirm-${planId}`;
      let pending = JSON.parse(sessionStorage.getItem(storageKey) || 'null');
      if (!pending) { pending = { idempotency_key: commandId('confirm-') }; sessionStorage.setItem(storageKey, JSON.stringify(pending)); }
      await api(`/api/v1/vehicles/${encodeURIComponent(vehicleId)}/daily-plans/${encodeURIComponent(planId)}/confirm`, { method: 'POST', body: JSON.stringify(pending) });
      sessionStorage.removeItem(storageKey);
      if (editingDailyPlanId === planId) leaveDailyPlanEdit();
      if (selected === vehicleId) { selectedDailyPlanId = planId; await loadDailyPlans(); }
    } catch (e) {
      if (e.status === 409 || e.status === 422) sessionStorage.removeItem(`trickee-plan-confirm-${planId}`);
      text('dailyPlanStatus', e.message);
    }
  });
}
$('dailyPlanForm').addEventListener('submit', event => {
  event.preventDefault();
  withBusy($('saveDailyPlan'), async () => {
    const vehicleId = selected;
    try {
      if (!vehicleId) throw new Error('Select a vehicle first.');
      const editingId = editingDailyPlanId;
      const storageKey = editingId
        ? `trickee-plan-edit-${vehicleId}-${editingId}-${editingDailyRevision}`
        : `trickee-plan-draft-${vehicleId}`;
      let pending = JSON.parse(sessionStorage.getItem(storageKey) || 'null');
      if (!pending) {
        const soc = Number($('dailySoc').value);
        if (!$('dailySoc').value.trim() || !Number.isFinite(soc) || soc < 0 || soc > 100) throw new Error('Enter confirmed charge between 0 and 100%.');
        pending = {
          idempotency_key: commandId(editingId ? 'edit-' : 'draft-'),
          service_date: $('dailyDate').value, timezone: $('dailyTimezone').value.trim(),
          starting_soc_pct: soc, origin: coordinateInput($('dailyOrigin').value),
          origin_osm_ref: dailyOriginReference?.value === $('dailyOrigin').value ? dailyOriginReference.ref : null,
          stops: parseDailyStops($('dailyStops').value),
        };
        if (editingId) pending.expected_revision = editingDailyRevision;
        else pending.plan_id = commandId('DAY-');
        sessionStorage.setItem(storageKey, JSON.stringify(pending));
      }
      const path = `/api/v1/vehicles/${encodeURIComponent(vehicleId)}/daily-plans${editingId ? `/${encodeURIComponent(editingId)}` : ''}`;
      const saved = await api(path, { method: editingId ? 'PUT' : 'POST', body: JSON.stringify(pending) });
      sessionStorage.removeItem(storageKey);
      leaveDailyPlanEdit();
      if (selected === vehicleId) { selectedDailyPlanId = saved.plan_id; await loadDailyPlans(); }
    } catch (e) {
      if (e.status === 409 || e.status === 422) {
        const storageKey = editingDailyPlanId
          ? `trickee-plan-edit-${vehicleId}-${editingDailyPlanId}-${editingDailyRevision}`
          : `trickee-plan-draft-${vehicleId}`;
        sessionStorage.removeItem(storageKey);
      }
      text('dailyPlanStatus', e.message);
    }
  });
});
async function loadInsights() {
  const generation = ++insightGeneration;
  try {
    const [report, alerts, history, notifications] = await Promise.all([
      api('/api/v1/fleet/report?days=7'), api('/api/v1/fleet/alerts'), api('/api/v1/fleet/alerts/history?limit=20'),
      api('/api/v1/fleet/notifications?limit=20'),
    ]);
    if (generation !== insightGeneration) return;
    text('reportFrames', report.telemetry_frames);
    text('reportSoc', report.average_soc_pct == null ? '—' : `${report.average_soc_pct}%`);
    text('reportSpeed', report.average_speed_kmh == null ? '—' : `${report.average_speed_kmh} km/h`);
    text('reportLow', report.low_soc_frames);
    text('reportQuality', report.quality);
    $('reportQuality').dataset.quality = report.quality;
    text('insightsStatus', `${report.telemetry_frames} stored frames over 7 days · ${report.quality} source · ${alerts.length} open rule alert${alerts.length === 1 ? '' : 's'}`);
    const parent = $('fleetAlerts'); parent.replaceChildren();
    if (!alerts.length) {
      const empty = document.createElement('p'); empty.className = 'empty'; empty.textContent = 'No active rule alerts.'; parent.append(empty);
    }
    for (const alert of alerts) {
      const item = document.createElement('article'); item.className = 'fleet-alert'; item.dataset.severity = alert.severity;
      const title = document.createElement('strong'); title.textContent = `${alert.vehicle_id} · ${alert.type.replaceAll('_', ' ')}`;
      const message = document.createElement('span'); message.textContent = alert.message;
      const meta = document.createElement('span'); meta.textContent = `${alert.status} · ${alert.quality} · ${fmtTime(alert.observed_at)} · ${alert.source}`;
      item.append(title, message, meta);
      if (operatorRole !== 'viewer') {
        const actions = document.createElement('div'); actions.className = 'fleet-alert-actions';
        for (const [action, label] of [['acknowledge', 'Acknowledge'], ['resolve', 'Resolve']]) {
          if (action === 'acknowledge' && alert.status !== 'ACTIVE') continue;
          const button = document.createElement('button'); button.type = 'button'; button.textContent = label;
          button.setAttribute('aria-label', `${label} ${alert.type.replaceAll('_', ' ')} for ${alert.vehicle_id}`);
          button.addEventListener('click', () => withBusy(button, async () => {
            try {
              await api(`/api/v1/fleet/alerts/${encodeURIComponent(alert.id)}/${action}`, { method: 'POST', body: '{}' });
              await loadInsights();
            } catch (e) { text('insightsStatus', e.message); }
          }));
          actions.append(button);
        }
        item.append(actions);
      }
      parent.append(item);
    }
    const noticeParent = $('fleetNotifications'); noticeParent.replaceChildren();
    text('notificationCount', notifications.filter(notice => !notice.acknowledged_at).length);
    if (!notifications.length) { const empty = document.createElement('p'); empty.className = 'empty'; empty.textContent = 'No vehicle notifications recorded.'; noticeParent.append(empty); }
    for (const notice of notifications) {
      const item = document.createElement('article'); item.className = 'fleet-alert'; item.dataset.severity = notice.priority;
      const title = document.createElement('strong'); title.textContent = `${notice.vehicle_id} · ${notice.kind.replaceAll('_', ' ')}`;
      const message = document.createElement('span'); message.textContent = notice.message;
      const meta = document.createElement('span'); meta.textContent = `${notice.priority} · ${fmtTime(notice.observed_at)} · ${notice.acknowledged_at ? 'acknowledged' : 'new'}`;
      item.append(title, message, meta);
      if (!notice.acknowledged_at && operatorRole !== 'viewer') {
        const actions = document.createElement('div'); actions.className = 'fleet-alert-actions';
        const button = document.createElement('button'); button.type = 'button'; button.textContent = 'Acknowledge';
        button.setAttribute('aria-label', `Acknowledge ${notice.kind.replaceAll('_', ' ')} for ${notice.vehicle_id}`);
        button.addEventListener('click', () => withBusy(button, async () => {
          try {
            await api(`/api/v1/fleet/notifications/${notice.id}/acknowledge`, { method: 'POST' });
            await loadInsights();
          } catch (e) { text('insightsStatus', e.message); }
        }));
        actions.append(button); item.append(actions);
      }
      noticeParent.append(item);
    }
    const historyParent = $('fleetAlertHistory'); historyParent.replaceChildren();
    if (!history.length) { const empty = document.createElement('p'); empty.className = 'empty'; empty.textContent = 'No incidents recorded.'; historyParent.append(empty); }
    for (const alert of history) {
      const item = document.createElement('article'); item.className = 'fleet-alert';
      const title = document.createElement('strong'); title.textContent = `${alert.vehicle_id} · ${alert.type.replaceAll('_', ' ')}`;
      const meta = document.createElement('span'); meta.textContent = `${alert.status} · opened ${fmtTime(alert.started_at)} · ${alert.resolution || 'condition under review'}`;
      item.append(title, meta);
      if (alert.note) { const note = document.createElement('span'); note.textContent = alert.note; item.append(note); }
      historyParent.append(item);
    }
  } catch (e) { if (generation === insightGeneration) text('insightsStatus', e.message); }
}
function optionalSoc(id) {
  const raw = $(id).value.trim();
  if (!raw) return null;
  const value = Number(raw);
  if (!Number.isFinite(value) || value < 0 || value > 100) throw new Error('Charge must be between 0 and 100%.');
  return value;
}
function commandId(prefix) {
  const bytes = new Uint8Array(16); crypto.getRandomValues(bytes);
  return prefix + Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
}
$('operationsDate').value = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Kolkata',
  year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date());
$('dailyOperationsForm').addEventListener('submit', event => {
  event.preventDefault();
  withBusy($('loadDailyOperations'), async () => {
    const generation = refreshGeneration;
    try {
      const params = new URLSearchParams({ date: $('operationsDate').value,
        timezone: $('operationsTimezone').value.trim() });
      const report = await api(`/api/v1/fleet/daily-operations?${params}`);
      if (generation !== refreshGeneration) return;
      const parent = $('dailyOperationsResult'); parent.replaceChildren();
      const summary = document.createElement('strong');
      summary.textContent = `${report.quality} · ${report.trips_completed}/${report.trips_started} trips completed · ${report.internal_assignments_created} internal assignments · ${report.internal_deliveries_manually_reconciled} manually reconciled deliveries`;
      parent.append(summary);
      const caveat = document.createElement('span');
      caveat.textContent = `${report.total_trip_records} trip records${report.truncated ? ' · first 500 shown' : ''} · ${report.wait_minutes} min driver-reported wait · ${report.charging_stop_minutes} min driver-reported charging stops · ${report.internal_deliveries_reported} driver-reported deliveries. Partner-confirmed orders, charging energy and savings remain unavailable without measured evidence.`;
      parent.append(caveat);
      for (const driver of report.driver_reports) {
        const item = document.createElement('div'); item.className = 'trip-card';
        const label = document.createElement('strong'); label.textContent = driver.driver_id;
        const detail = document.createElement('small');
        detail.textContent = `${driver.trips_completed}/${driver.trips_started} trips completed · ${driver.wait_minutes} min wait · ${driver.charging_stop_minutes} min charging stop · ${driver.open_stops} open stop${driver.open_stops === 1 ? '' : 's'}`;
        const body = document.createElement('div'); body.append(label, detail); item.append(body); parent.append(item);
      }
    } catch (error) { if (generation === refreshGeneration) text('dailyOperationsResult', error.message); }
  });
});
$('decisionPreviewForm').addEventListener('submit', event => {
  event.preventDefault();
  withBusy($('runDecisionPreview'), async () => {
    const generation = refreshGeneration;
    try {
      const preparation = Number($('decisionPrep').value);
      if (!Number.isFinite(preparation) || preparation < 0 || preparation > 240) throw new Error('Preparation must be 0–240 minutes.');
      const result = await api('/api/v1/fleet/decisions/preview', { method: 'POST', body: JSON.stringify({
        pickup: coordinateInput($('decisionPickup').value),
        destination: coordinateInput($('decisionDestination').value), preparation_min: preparation,
      }) });
      if (generation !== refreshGeneration) return;
      const parent = $('decisionResult'); parent.replaceChildren();
      const summary = document.createElement('strong');
      summary.textContent = result.recommended_vehicle_id
        ? `${result.quality} advisory: review ${result.recommended_vehicle_id} · no automatic assignment`
        : `No safe assignment preview · ${result.reason.replaceAll('_', ' ').toLowerCase()}`;
      parent.append(summary);
      if (result.candidates?.length) {
        const note = document.createElement('span');
        note.textContent = `${result.vehicles_considered} vehicles considered${result.vehicles_truncated ? ' · first 20 only' : ''} · traffic ${result.traffic_quality.toLowerCase()} · charger status ${result.charger_quality.toLowerCase()} · charging ${result.charging_decision.replaceAll('_', ' ').toLowerCase()}`;
        parent.append(note);
      }
      for (const candidate of result.candidates || []) {
        const card = document.createElement('div'); card.className = 'trip-card';
        const title = document.createElement('strong'); title.textContent = `${candidate.vehicle_id} · ${candidate.driver_id}`;
        const detail = document.createElement('small');
        detail.textContent = `${candidate.pickup_eta_min} min baseline to pickup · ${candidate.estimated_wait_min} min estimated wait · conservative arrival ${candidate.conservative_arrival_soc_pct}% vs ${candidate.reserve_soc_pct}% reserve · ${candidate.vehicle_quality}`;
        const body = document.createElement('div'); body.append(title, detail);
        const badge = document.createElement('span'); badge.textContent = candidate.range_safe ? 'RANGE SAFE' : 'REVIEW RANGE';
        card.append(body, badge); parent.append(card);
      }
    } catch (error) { if (generation === refreshGeneration) text('decisionResult', error.message); }
  });
});
$('startTrip').addEventListener('click', async () => {
  if (!selected) return;
  $('startTrip').disabled = true;
  try {
    const storageKey = `trickee-trip-start-${selected}`;
    let pending = JSON.parse(sessionStorage.getItem(storageKey) || 'null');
    if (!pending) {
      pending = { trip_id: commandId('TRIP-'), idempotency_key: commandId('start-'), starting_soc_pct: optionalSoc('startSoc') };
      sessionStorage.setItem(storageKey, JSON.stringify(pending));
    }
    const result = await api(`/api/v1/vehicles/${encodeURIComponent(selected)}/trips`, { method: 'POST', body: JSON.stringify(pending) });
    sessionStorage.removeItem(storageKey);
    selectedTripId = result.trip_id;
    $('startSoc').value = '';
    await loadTrips();
  } catch (e) { text('tripStatus', e.message); }
  drawTripControls();
});
$('endTrip').addEventListener('click', async () => {
  if (!selected || !selectedTripId) return;
  $('endTrip').disabled = true;
  try {
    const storageKey = `trickee-trip-end-${selectedTripId}`;
    let pending = JSON.parse(sessionStorage.getItem(storageKey) || 'null');
    if (!pending) {
      pending = { idempotency_key: commandId('end-'), ending_soc_pct: optionalSoc('endSoc') };
      sessionStorage.setItem(storageKey, JSON.stringify(pending));
    }
    await api(`/api/v1/vehicles/${encodeURIComponent(selected)}/trips/${encodeURIComponent(selectedTripId)}/complete`, { method: 'POST', body: JSON.stringify(pending) });
    sessionStorage.removeItem(storageKey);
    $('endSoc').value = '';
    await loadTrips();
  } catch (e) { text('tripStatus', e.message); }
  drawTripControls();
});
function drawTwin(twin) {
  text('vehicleTitle', twin.vehicle_id); text('quality', twin.quality); text('soc', twin.battery?.soc_pct);
  $('quality').dataset.quality = twin.quality;
  updateChargeMeter(twin.battery?.soc_pct);
  text('state', twin.state); text('health', twin.health.status); text('observed', fmtTime(twin.observed_at));
  $('health').dataset.status = twin.health.status;
  text('soh', twin.battery?.soh_pct == null ? 'UNKNOWN' : `${twin.battery.soh_pct}%`);
  text('location', twin.location ? `${twin.location.latitude.toFixed(3)}, ${twin.location.longitude.toFixed(3)}` : 'UNKNOWN');
  text('software', twin.software_version); text('driver', twin.driver_id || 'UNASSIGNED');
  $('alert').hidden = !twin.health.evidence?.length; text('alert', twin.health.evidence?.join(' · '));
  $('driverProvision').hidden = operatorRole !== 'admin' || !twin.driver_id;
  if (!$('driverProvision').hidden) loadDriverCredentials(twin.vehicle_id);
  const moving = twin.state === 'DRIVING'; $('drivingBanner').hidden = !moving;
  for (const field of $('planForm').querySelectorAll('input, button')) field.disabled = moving;
  for (const id of ['origin', 'destination', 'findRoute', 'roadRouteBtn']) $(id).disabled = moving;
  for (const field of $('placeSearchForm').querySelectorAll('input, button')) field.disabled = moving;
  window.TrickeeMap.render(twins, selected, selectVehicle);
  loadTrips();
  loadDailyPlans();
  loadNudges();
  loadDispatch();
  loadServiceCases();
}
async function loadDriverCredentials(vehicleId) {
  const generation = ++credentialGeneration;
  const requestedKey = key;
  try {
    const credentials = await api(`/api/v1/vehicles/${encodeURIComponent(vehicleId)}/driver-credentials`);
    if (generation !== credentialGeneration || selected !== vehicleId || key !== requestedKey) return;
    const parent = $('driverCredentialList'); parent.replaceChildren();
    for (const credential of credentials) {
      const row = document.createElement('div');
      const label = document.createElement('span');
      const name = document.createElement('strong'); name.textContent = credential.credential_id;
      label.append(name, document.createTextNode(` · ${credential.revoked_at ? 'REVOKED' : 'ACTIVE'} · ${fmtTime(credential.created_at)}`));
      row.append(label);
      if (!credential.revoked_at) {
        const revoke = document.createElement('button'); revoke.type = 'button'; revoke.className = 'secondary'; revoke.textContent = 'Revoke';
        revoke.setAttribute('aria-label', `Revoke ${credential.credential_id}`);
        revoke.addEventListener('click', () => withBusy(revoke, async () => {
          try {
            await api(`/api/v1/driver-credentials/${encodeURIComponent(credential.credential_id)}/revoke`, { method: 'POST' });
            $('driverCredentialResult').replaceChildren();
            await loadDriverCredentials(vehicleId);
          } catch (e) { $('driverCredentialResult').textContent = e.message; }
        }));
        row.append(revoke);
      }
      parent.append(row);
    }
  } catch (e) { if (generation === credentialGeneration) $('driverCredentialList').textContent = e.message; }
}
$('issueDriverCredential').addEventListener('click', () => withBusy($('issueDriverCredential'), async () => {
  if (!selected || operatorRole !== 'admin') return;
  const vehicleId = selected;
  const parent = $('driverCredentialResult'); parent.replaceChildren();
  try {
    const result = await api(`/api/v1/vehicles/${encodeURIComponent(vehicleId)}/driver-credentials`, { method: 'POST' });
    if (selected !== vehicleId) return;
    const instruction = document.createElement('span');
    instruction.textContent = `Credential ${result.credential_id} for ${result.driver_id}. Copy it now; the cloud will not show it again.`;
    const value = document.createElement('code'); value.textContent = result.token;
    const copy = document.createElement('button'); copy.type = 'button'; copy.className = 'secondary'; copy.textContent = 'Copy credential';
    copy.addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(result.token); copy.textContent = 'Copied'; }
      catch { copy.textContent = 'Select the credential above to copy'; }
    });
    parent.append(instruction, value, copy);
    await loadDriverCredentials(vehicleId);
  } catch (e) { parent.textContent = e.message; }
}));
async function loadMapConfig() {
  if (mapConfigured || mapConfigLoading) return;
  mapConfigLoading = true;
  try {
    window.TrickeeMap.configure(await api('/api/v1/map/config'));
    mapConfigured = true;
  } catch {
    window.TrickeeMap.configure(null);
  } finally {
    mapConfigLoading = false;
  }
}
async function refresh() {
  if (!key) return false;
  // Operator actions belong to an identity, not to a polling interval. Only
  // clearProtectedData changes that generation; overlapping snapshot requests
  // use their own counter so older snapshots still cannot replace newer ones.
  const generation = refreshGeneration;
  const requestGeneration = ++refreshRequestGeneration;
  const requestedKey = key;
  const requestedMode = authMode;
  try {
    const [identity, summary, vehicles] = await Promise.all([api('/api/v1/operator/me'), api('/api/v1/fleet/summary'), api('/api/v1/vehicles')]);
    if (generation !== refreshGeneration || requestGeneration !== refreshRequestGeneration ||
        key !== requestedKey || authMode !== requestedMode) return false;
    operatorRole = identity.role;
    $('auditWorkspace').hidden = operatorRole !== 'admin';
    $('chargingRecordsWorkspace').hidden = operatorRole !== 'admin';
    if (auditVehicleId !== selected) { auditGeneration++; auditNextBefore = null; auditVehicleId = null; $('auditList').replaceChildren(); $('moreAudit').hidden = true; text('auditStatus', 'Select a vehicle and load its audit trail.'); }
    for (const field of $('dailyPlanForm').querySelectorAll('input, textarea, button')) field.disabled = operatorRole === 'viewer';
    for (const field of $('nudgeForm').querySelectorAll('select, textarea, button')) field.disabled = operatorRole === 'viewer';
    for (const field of $('dispatchForm').querySelectorAll('input, select, textarea, button')) field.disabled = operatorRole === 'viewer';
    $('dailyPlanStatus').dataset.role = operatorRole;
    twins = vehicles; text('mVehicles', summary.vehicles); text('mOnline', summary.online); text('mLow', summary.low_battery); text('mCharging', summary.charging); text('mAlerts', summary.alerts);
    text('mServiceCases', summary.service_cases_open);
    $('mServiceCases').setAttribute('aria-label', `${summary.service_cases_open} open driver service cases, including ${summary.service_cases_urgent} urgent`);
    text('connection', 'CONNECTED'); $('connection').classList.add('connected'); error('');
    if (!twins.some(v => v.vehicle_id === selected)) {
      selected = twins[0]?.vehicle_id || null;
      leaveDailyPlanEdit();
      $('driverCredentialResult').replaceChildren();
    }
    drawVehicleList(); const active = twins.find(v => v.vehicle_id === selected); if (active) drawTwin(active);
    else { window.TrickeeMap.render(twins, selected, selectVehicle); loadTrips(); loadDailyPlans(); loadNudges(); loadDispatch(); loadServiceCases(); }
    loadMapConfig();
    loadInsights();
    startChangeWatch();
    return true;
  } catch (e) {
    if (generation !== refreshGeneration || requestGeneration !== refreshRequestGeneration ||
        key !== requestedKey || authMode !== requestedMode) return false;
    if (e.status === 401) {
      clearProtectedData();
      if (authMode === 'token') {
        key = '';
        $('operatorKey').value = '';
        sessionStorage.removeItem('trickeeOperatorKey');
      }
    }
    text('connection', 'DISCONNECTED'); $('connection').classList.remove('connected'); error(e.message);
    return false;
  }
}
$('connectBtn').addEventListener('click', () => withBusy($('connectBtn'), async () => {
  const nextKey = $('operatorKey').value.trim();
  const nextMode = $('operatorAuthType').value;
  if (!nextKey) { error(nextMode === 'token' ? 'Enter a signed access token.' : 'Enter an operator key.'); return; }
  if (nextKey !== key || nextMode !== authMode) clearProtectedData();
  key = nextKey;
  authMode = nextMode;
  mapConfigured = false;
  sessionStorage.setItem('trickeeOperatorKey', key);
  sessionStorage.setItem('trickeeOperatorAuthMode', authMode);
  await refresh();
}));
$('operatorAuthType').addEventListener('change', () => {
  const selectedMode = $('operatorAuthType').value;
  if (selectedMode !== authMode) {
    key = '';
    authMode = selectedMode;
    sessionStorage.removeItem('trickeeOperatorKey');
    sessionStorage.setItem('trickeeOperatorAuthMode', authMode);
    clearProtectedData();
    text('connection', 'DISCONNECTED');
    $('connection').classList.remove('connected');
  }
  $('operatorKey').value = '';
  $('operatorKey').placeholder = selectedMode === 'token' ? 'Paste signed access token' : 'Enter operator key';
  error('');
});
$('operatorKey').addEventListener('keydown', (event) => { if (event.key === 'Enter') $('connectBtn').click(); });
$('disconnectBtn').addEventListener('click', () => {
  key = '';
  sessionStorage.removeItem('trickeeOperatorKey');
  $('operatorKey').value = '';
  clearProtectedData();
  text('connection', 'DISCONNECTED');
  $('connection').classList.remove('connected');
  error('');
});
$('refreshBtn').addEventListener('click', () => withBusy($('refreshBtn'), refresh));
$('placeSearchForm').addEventListener('submit', event => {
  event.preventDefault();
  withBusy($('placeSearchBtn'), async () => {
    try {
      const query = $('placeQuery').value.trim();
      if (query.length < 3) throw new Error('Enter at least three characters.');
      const places = await api(`/api/v1/places/search?q=${encodeURIComponent(query)}`);
      const parent = $('placeResults'); parent.replaceChildren();
      if (!places.length) { parent.textContent = 'No places found by the configured provider.'; return; }
      for (const place of places) {
        const card = document.createElement('div'); card.className = 'place-result';
        const name = document.createElement('strong'); name.textContent = place.name;
        const provenance = document.createElement('small');
        provenance.textContent = `${place.source} place · ${osmRefLabel(place.osm_ref)} · ${place.latitude}, ${place.longitude} · © OpenStreetMap contributors`;
        const actions = document.createElement('div');
        for (const [target, label] of [['origin', 'Use as origin'], ['destination', 'Use as destination']]) {
          const button = document.createElement('button'); button.type = 'button'; button.textContent = label;
          button.addEventListener('click', () => {
            $(target).value = `${place.latitude}, ${place.longitude}`;
            route = null; routeSource = 'SIMULATED'; routeContextGeneration++; text('routeContext', 'Weather and elevation require a road preview with configured providers.'); $('routeOptions').replaceChildren(); window.TrickeeMap.showRoute(null);
            text('planResult', `${place.name} selected as ${target}. Find a road route to calculate provider distance; traffic remains unavailable.`);
          });
          actions.append(button);
        }
        card.append(name, provenance, actions); parent.append(card);
      }
    } catch (e) { text('placeResults', e.message); }
  });
});
$('findRoute').addEventListener('click', async () => {
  await withBusy($('findRoute'), async () => { try {
    const result = await api('/api/v1/routes', { method: 'POST', body: JSON.stringify({ origin: $('origin').value, destination: $('destination').value }) });
    routeContextGeneration++; text('routeContext', 'Mock route context is simulated; live weather and elevation are not attached.');
    $('distance').value = result.route.distance_km; $('eta').value = result.route.baseline_eta_min;
    $('speed').value = result.route.average_speed_kmh; $('traffic').value = result.route.traffic_factor;
    routeSource = 'SIMULATED';
    $('routeOptions').replaceChildren();
    window.TrickeeMap.showRoute(null);
    text('planResult', `Mock route loaded: ${result.route.distance_km} km. Traffic is simulated, not live.`);
    resultChanged('planResult');
  } catch (e) { text('planResult', e.message); resultChanged('planResult'); } });
});
function coordinateInput(value) {
  const parts = value.split(',').map(part => Number(part.trim()));
  if (parts.length !== 2 || parts.some(value => !Number.isFinite(value)) ||
      parts[0] < -90 || parts[0] > 90 || parts[1] < -180 || parts[1] > 180) {
    throw new Error('Enter each road endpoint as latitude, longitude.');
  }
  return { latitude: parts[0], longitude: parts[1] };
}
function chooseRoadRoute(candidate, index, candidates) {
  $('distance').value = candidate.route.distance_km;
  $('eta').value = candidate.route.baseline_eta_min;
  $('speed').value = candidate.route.average_speed_kmh;
  $('traffic').value = 1;
  routeSource = 'PROVIDER';
  window.TrickeeMap.showRoute(candidate.geometry);
  for (const [optionIndex, button] of [...$('routeOptions').querySelectorAll('button')].entries()) {
    button.setAttribute('aria-pressed', String(optionIndex === index));
  }
  text('planResult', `Road route ${index + 1} of ${candidates.length}: ${candidate.route.distance_km.toFixed(1)} km · ${candidate.route.baseline_eta_min.toFixed(1)} min. Traffic is unavailable.`);
  resultChanged('planResult');
}
$('roadRouteBtn').addEventListener('click', () => withBusy($('roadRouteBtn'), async () => {
  const generation = ++routeContextGeneration;
  text('routeContext', 'Checking sourced weather and elevation…');
  try {
    const origin = coordinateInput($('origin').value);
    const destination = coordinateInput($('destination').value);
    const result = await api('/api/v1/routes/road', { method: 'POST', body: JSON.stringify({ origin, destination }) });
    $('routeOptions').replaceChildren();
    result.routes.forEach((candidate, index) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = `Option ${index + 1} · ${candidate.route.distance_km.toFixed(1)} km`;
      button.addEventListener('click', () => chooseRoadRoute(candidate, index, result.routes));
      $('routeOptions').append(button);
    });
    chooseRoadRoute(result.routes[0], 0, result.routes);
    try {
      const context = await api('/api/v1/routes/context', { method: 'POST', body: JSON.stringify({ origin, destination }) });
      if (generation !== routeContextGeneration || routeSource !== 'PROVIDER') return;
      const weather = context.weather.quality === 'REPORTED'
        ? `${context.weather.temperature_c.toFixed(1)} °C at origin · ${context.weather.source}`
        : `Weather unavailable (${context.weather.reason})`;
      const elevation = context.elevation.quality === 'REPORTED'
        ? `${context.elevation.elevation_delta_m.toFixed(0)} m endpoint elevation difference · ${context.elevation.source}`
        : `Elevation unavailable (${context.elevation.reason})`;
      text('routeContext', `${weather}. ${elevation}. Traffic unavailable.`);
    } catch (_) {
      if (generation === routeContextGeneration && routeSource === 'PROVIDER') text('routeContext', 'Weather and elevation unavailable. Traffic unavailable.');
    }
  } catch (e) {
    if (generation === routeContextGeneration) text('routeContext', 'Weather and elevation unavailable.');
    text('planResult', e.message); resultChanged('planResult');
  }
}));
for (const id of ['distance', 'eta', 'speed']) {
  $(id).addEventListener('input', () => {
    if (routeSource !== 'PROVIDER') return;
    routeSource = 'SIMULATED';
    routeContextGeneration++; text('routeContext', 'Route measurements changed manually; provider context is not attached.');
    $('routeOptions').replaceChildren();
    window.TrickeeMap.showRoute(null);
    text('planResult', 'Route measurements changed manually. This is now a simulated estimate.');
  });
}
$('planForm').addEventListener('submit', async (event) => {
  event.preventDefault(); if (!selected) return;
  await withBusy($('planForm').querySelector('button[type="submit"]'), async () => { try {
    route = routeBody(); const result = await api(`/api/v1/vehicles/${encodeURIComponent(selected)}/plan`, { method: 'POST', body: JSON.stringify({ route }) });
    const box = $('planResult'); box.replaceChildren();
    const entries = result.range ? [
      ['Arrival charge', `${result.range.arrival_soc_pct}% (${result.range.arrival_soc_interval_pct.join('–')}%)`],
      ['Predicted energy', `${result.range.energy_kwh} kWh`], ['Route ETA', `${result.eta.trickee_eta_min} min`],
      ['Reserve risk', result.range.risk], ['Suggested charger', result.chargers[0]?.charger.name || 'None reachable']
    ] : [['Result', 'Battery data unavailable']];
    for (const [label, value] of entries) { const p = document.createElement('p'); const s = document.createElement('span'); s.textContent = label; const b = document.createElement('strong'); b.textContent = value; p.append(s, b); box.append(p); }
    resultChanged('planResult');
  } catch (e) { text('planResult', e.message); resultChanged('planResult'); } });
});
$('copilotForm').addEventListener('submit', async (event) => {
  event.preventDefault(); if (!selected) return;
  await withBusy($('copilotForm').querySelector('button[type="submit"]'), async () => { try {
    const result = await api('/api/v1/copilot', { method: 'POST', body: JSON.stringify({ vehicle_id: selected, question: $('question').value, route }) });
    text('copilotResult', `${result.tier} · ${result.answer}`);
    $('copilotResult').dataset.tier = result.tier;
    resultChanged('copilotResult');
  } catch (e) { text('copilotResult', e.message); resultChanged('copilotResult'); } });
});
$('operatorAuthType').value = authMode;
if (authMode === 'token') $('operatorKey').placeholder = 'Paste signed access token';
if (key) { $('operatorKey').value = key; refresh(); }
setInterval(refresh, 10000);

if ('IntersectionObserver' in window) {
  const links = [...document.querySelectorAll('.site-nav a')];
  const observer = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      for (const link of links) link.removeAttribute('aria-current');
      const active = links.find(link => link.hash === `#${entry.target.id}`);
      if (active) active.setAttribute('aria-current', 'location');
    }
  }, { rootMargin: '-90px 0px -60% 0px' });
  for (const id of ['fleet', 'twin', 'map', 'trips', 'dailyPlans', 'insights', 'route', 'copilot']) observer.observe($(id));
}
drawTripControls();
