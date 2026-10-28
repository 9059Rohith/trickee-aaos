const $ = id => document.getElementById(id);
const MAX_QUEUED = 5000;
let credential = sessionStorage.getItem('trickeeDriverCredential') || '';
let identity = null;
let trips = [];
let nudges = [];
let dispatchOffers = [];
let internalOrders = [];
let queuedOrderActions = [];
let syncingOrderActions = false;
let queuedDispatch = [];
let syncingDispatch = false;
const dispatchSeenRequests = new Set();
let serviceCases = [];
let serviceOutbox = [];
let syncingService = false;
let queuedAdvisories = [];
let syncingAdvisories = false;
let dailyPlans = [];
let activeTrip = null;
let phoneConsent = null;
let watchId = null;
let syncing = false;
let erasingPhoneData = false;
let refreshGeneration = 0;
let personalIdentityVersion = 0;
window.trickeeWorkspaceContext = () => ({connected: !!identity, generation: personalIdentityVersion, driving: identity?.twin?.state === 'DRIVING'});
let routeMap = null;
let routeLayer = null;
let routePoints = null;
let routeGeneration = 0;
let consentMutationGeneration = 0;
let changeController = null;
let changeCursor = 0;
const WITHDRAWAL_PREFIX = 'trickeePhoneConsentWithdrawalPending:';
const ERASURE_PREFIX = 'trickeePhoneDataErasurePending:';
function consentKey(me) { return `${me.tenant_id}:${me.driver_id}`; }
function withdrawalKey(me) { return WITHDRAWAL_PREFIX + consentKey(me); }
function pendingWithdrawal(me) { return !!me && localStorage.getItem(withdrawalKey(me)) === '1'; }
function erasureKey(me) { return ERASURE_PREFIX + consentKey(me); }
function pendingErasure(me) { return !!me && localStorage.getItem(erasureKey(me)) === '1'; }

function notice(message, isError = false) {
  $('notice').textContent = message;
  $('notice').classList.toggle('error', isError);
}
function time(value) { return value ? new Date(value).toLocaleString() : '—'; }
function optionalSoc(id) {
  const raw = $(id).value.trim();
  if (!raw) return null;
  const value = Number(raw);
  if (!Number.isFinite(value) || value < 0 || value > 100) throw new Error('Dashboard charge must be between 0 and 100%.');
  return value;
}
function parseCoordinates(value) {
  const parts = value.split(',').map(part => Number(part.trim()));
  if (parts.length !== 2 || !parts.every(Number.isFinite) || parts[0] < -90 || parts[0] > 90 || parts[1] < -180 || parts[1] > 180)
    throw new Error('Enter a valid destination as latitude, longitude.');
  return { latitude: parts[0], longitude: parts[1] };
}
function clearRoute() {
  routeGeneration++;
  if (routeMap) { routeMap.remove(); routeMap = null; }
  routeLayer = null; routePoints = null;
  $('driverRouteOptions').replaceChildren();
  $('driverRouteSteps').replaceChildren();
  $('routeStatus').textContent = 'Connect to preview a route.';
  $('routeContext').textContent = 'Weather and elevation require configured providers.';
}
function advisoryOwner(me) { return `${me.tenant_id}:${me.driver_id}:${me.vehicle_id}`; }
function advisoryKey(me, nudgeId) { return `${advisoryOwner(me)}:${nudgeId}`; }
function dispatchOwner(me) { return advisoryOwner(me); }
function dispatchKey(me, offerId) { return `${dispatchOwner(me)}:${offerId}`; }
function serviceOwner(me) { return advisoryOwner(me); }
function openServiceQueue() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open('trickee-service-case-outbox-v1', 1);
    request.onupgradeneeded = () => request.result.createObjectStore('commands', { keyPath: 'key' });
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}
async function listServiceCommands() {
  const db = await openServiceQueue();
  return new Promise((resolve, reject) => {
    const tx = db.transaction('commands', 'readonly');
    const request = tx.objectStore('commands').getAll();
    request.onsuccess = () => resolve(request.result.sort((a, b) =>
      (a.created_at - b.created_at) || (a.kind === 'CREATE' ? -1 : b.kind === 'CREATE' ? 1 : a.key.localeCompare(b.key))));
    request.onerror = () => reject(request.error);
    tx.oncomplete = () => db.close();
  });
}
async function queueServiceCommand(me, kind, caseId, path, body) {
  const db = await openServiceQueue();
  const key = `${serviceOwner(me)}:${body.idempotency_key}`;
  return new Promise((resolve, reject) => {
    const tx = db.transaction('commands', 'readwrite');
    const store = tx.objectStore('commands');
    const existing = store.get(key);
    let failure = null;
    existing.onsuccess = () => {
      if (existing.result) {
        if (existing.result.kind !== kind || existing.result.case_id !== caseId ||
            JSON.stringify(existing.result.body) !== JSON.stringify(body)) {
          failure = 'A different service command already uses this retry key.'; tx.abort();
        }
        return;
      }
      const count = store.count();
      count.onsuccess = () => {
        if (count.result >= 100) { failure = 'The service case queue is full. Sync existing reports first.'; tx.abort(); return; }
        store.put({ key, owner: serviceOwner(me), kind, case_id: caseId, path, body,
                    state: 'QUEUED', created_at: Date.now() });
      };
    };
    tx.oncomplete = () => { db.close(); resolve(); };
    tx.onabort = () => { db.close(); reject(new Error(failure || 'Could not save the service command on this device.')); };
    tx.onerror = () => { db.close(); reject(tx.error || new Error('Service case queue unavailable.')); };
  });
}
async function updateServiceCommand(item) {
  const db = await openServiceQueue();
  return new Promise((resolve, reject) => {
    const tx = db.transaction('commands', 'readwrite');
    tx.objectStore('commands').put(item);
    tx.oncomplete = () => { db.close(); resolve(); };
    tx.onerror = () => { db.close(); reject(tx.error); };
  });
}
async function removeServiceCommand(key) {
  const db = await openServiceQueue();
  return new Promise((resolve, reject) => {
    const tx = db.transaction('commands', 'readwrite');
    tx.objectStore('commands').delete(key);
    tx.oncomplete = () => { db.close(); resolve(); };
    tx.onerror = () => { db.close(); reject(tx.error); };
  });
}
async function syncServiceCommands() {
  if (!identity || syncingService) return;
  const owner = serviceOwner(identity), credentialAtStart = credential;
  syncingService = true;
  render();
  try {
    serviceOutbox = await listServiceCommands();
    for (const item of serviceOutbox.filter(entry => entry.owner === owner)) {
      if (!identity || serviceOwner(identity) !== owner || credential !== credentialAtStart) return;
      if (item.state === 'NEEDS_REVIEW') continue;
      try {
        await api(item.path, { method: 'POST', body: JSON.stringify(item.body) });
        if (!identity || serviceOwner(identity) !== owner || credential !== credentialAtStart) return;
        await removeServiceCommand(item.key);
      } catch (error) {
        if (error.status && error.status !== 429 && error.status >= 400 && error.status < 500) {
          await updateServiceCommand({ ...item, state: 'NEEDS_REVIEW', http_status: error.status });
          notice(`Service command needs review (HTTP ${error.status}): ${error.message}`, true);
          if (item.kind === 'CREATE') break;
          continue;
        }
        notice(`Service report remains queued: ${error.message}`, true);
        break;
      }
    }
    serviceOutbox = await listServiceCommands();
    if (identity && serviceOwner(identity) === owner && credential === credentialAtStart)
      serviceCases = await api('/api/v1/driver/service-cases');
  } catch (error) { notice(`Service case queue unavailable: ${error.message}`, true); }
  finally { syncingService = false; render(); }
}
function openDispatchQueue() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open('trickee-dispatch-response-v1', 1);
    request.onupgradeneeded = () => request.result.createObjectStore('responses', { keyPath: 'key' });
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}
async function listDispatchResponses() {
  const db = await openDispatchQueue();
  return new Promise((resolve, reject) => {
    const tx = db.transaction('responses', 'readonly');
    const request = tx.objectStore('responses').getAll();
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
    tx.oncomplete = () => db.close();
  });
}
async function queueDispatchResponse(me, offerId, outcome) {
  const db = await openDispatchQueue();
  const key = dispatchKey(me, offerId);
  return new Promise((resolve, reject) => {
    const tx = db.transaction('responses', 'readwrite');
    const store = tx.objectStore('responses');
    const existing = store.get(key);
    let failure = null;
    existing.onsuccess = () => {
      if (existing.result) {
        if (existing.result.outcome !== outcome) { failure = 'A different decision is already queued for this proposal.'; tx.abort(); }
        return;
      }
      const count = store.count();
      count.onsuccess = () => {
        if (count.result >= 100) { failure = 'The pickup decision queue is full.'; tx.abort(); return; }
        store.put({ key, owner: dispatchOwner(me), offer_id: offerId, outcome,
                    idempotency_key: `answer-${offerId}-${outcome}` });
      };
    };
    tx.oncomplete = () => { db.close(); resolve(); };
    tx.onabort = () => { db.close(); reject(new Error(failure || 'Could not save the pickup decision on this device.')); };
    tx.onerror = () => { db.close(); reject(tx.error || new Error('Pickup decision queue is unavailable.')); };
  });
}
async function removeDispatchResponse(key) {
  const db = await openDispatchQueue();
  return new Promise((resolve, reject) => {
    const tx = db.transaction('responses', 'readwrite');
    tx.objectStore('responses').delete(key);
    tx.oncomplete = () => { db.close(); resolve(); };
    tx.onerror = () => { db.close(); reject(tx.error); };
  });
}
async function syncDispatchResponses() {
  if (!identity || syncingDispatch) return;
  const owner = dispatchOwner(identity), credentialAtStart = credential;
  syncingDispatch = true;
  render();
  try {
    queuedDispatch = await listDispatchResponses();
    for (const item of queuedDispatch.filter(entry => entry.owner === owner)) {
      if (!identity || dispatchOwner(identity) !== owner || credential !== credentialAtStart) return;
      const serverOffer = dispatchOffers.find(offer => offer.offer_id === item.offer_id);
      if (!serverOffer) continue;
      if (serverOffer.status !== 'PENDING') {
        if (serverOffer.status === item.outcome) await removeDispatchResponse(item.key);
        continue;
      }
      try {
        const updated = await api(`/api/v1/driver/dispatch-proposals/${encodeURIComponent(item.offer_id)}/respond`, {
          method: 'POST', body: JSON.stringify({ outcome: item.outcome, idempotency_key: item.idempotency_key }),
        });
        if (!identity || dispatchOwner(identity) !== owner || credential !== credentialAtStart) return;
        await removeDispatchResponse(item.key);
        dispatchOffers = dispatchOffers.map(offer => offer.offer_id === item.offer_id ? updated : offer);
      } catch (error) {
        notice(`Pickup decision remains queued: ${error.message}`, true);
        break;
      }
    }
    queuedDispatch = await listDispatchResponses();
  } catch (error) { notice(`Pickup decision queue unavailable: ${error.message}`, true); }
  finally { syncingDispatch = false; render(); }
}
function openOrderQueue() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open('trickee-order-actions-v1', 1);
    request.onupgradeneeded = () => request.result.createObjectStore('actions', { keyPath: 'key' });
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}
async function listOrderActions() {
  const db = await openOrderQueue();
  return new Promise((resolve, reject) => {
    const tx = db.transaction('actions', 'readonly');
    const request = tx.objectStore('actions').getAll();
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
    tx.oncomplete = () => db.close();
  });
}
async function saveOrderAction(item) {
  const db = await openOrderQueue();
  return new Promise((resolve, reject) => {
    const tx = db.transaction('actions', 'readwrite');
    tx.objectStore('actions').put(item);
    tx.oncomplete = () => { db.close(); resolve(); };
    tx.onerror = () => { db.close(); reject(tx.error); };
  });
}
async function queueOrderAction(me, orderId, action) {
  if (!['PICKUP', 'REPORT_DELIVERY'].includes(action)) throw new Error('Invalid assignment action.');
  const db = await openOrderQueue();
  const key = `${dispatchOwner(me)}:${orderId}`;
  return new Promise((resolve, reject) => {
    const tx = db.transaction('actions', 'readwrite');
    const store = tx.objectStore('actions');
    const existing = store.get(key);
    let failure = null;
    existing.onsuccess = () => {
      if (existing.result) {
        if (existing.result.action !== action) { failure = 'Sync the previous assignment report first.'; tx.abort(); }
        return;
      }
      const count = store.count();
      count.onsuccess = () => {
        if (count.result >= 100) { failure = 'The assignment report queue is full.'; tx.abort(); return; }
        store.put({ key, owner: dispatchOwner(me), order_id: orderId, action,
                    idempotency_key: `order-${crypto.randomUUID()}`, state: 'QUEUED' });
      };
    };
    tx.oncomplete = () => { db.close(); resolve(); };
    tx.onabort = () => { db.close(); reject(new Error(failure || 'Could not save the assignment report.')); };
    tx.onerror = () => { db.close(); reject(tx.error || new Error('Assignment report queue is unavailable.')); };
  });
}
async function removeOrderAction(key) {
  const db = await openOrderQueue();
  return new Promise((resolve, reject) => {
    const tx = db.transaction('actions', 'readwrite');
    tx.objectStore('actions').delete(key);
    tx.oncomplete = () => { db.close(); resolve(); };
    tx.onerror = () => { db.close(); reject(tx.error); };
  });
}
async function syncOrderActions() {
  if (!identity || syncingOrderActions) return;
  const owner = dispatchOwner(identity), credentialAtStart = credential;
  syncingOrderActions = true; render();
  try {
    queuedOrderActions = await listOrderActions();
    for (const item of queuedOrderActions.filter(entry => entry.owner === owner)) {
      if (!identity || dispatchOwner(identity) !== owner || credential !== credentialAtStart) return;
      const server = internalOrders.find(order => order.order_id === item.order_id);
      if (!server) continue;
      const completed = item.action === 'PICKUP'
        ? ['PICKED_UP', 'DELIVERED_REPORTED', 'RECONCILED'].includes(server.status)
        : ['DELIVERED_REPORTED', 'RECONCILED'].includes(server.status);
      if (completed) { await removeOrderAction(item.key); continue; }
      const expected = item.action === 'PICKUP' ? 'ASSIGNED' : 'PICKED_UP';
      if (server.status !== expected) {
        await saveOrderAction({ ...item, state: 'NEEDS_REVIEW', http_status: 409 });
        continue;
      }
      if (item.state === 'NEEDS_REVIEW') continue;
      try {
        const endpoint = item.action === 'PICKUP' ? 'pickup' : 'report-delivery';
        const updated = await api(`/api/v1/driver/internal-orders/${encodeURIComponent(item.order_id)}/${endpoint}`, {
          method: 'POST', body: JSON.stringify({ idempotency_key: item.idempotency_key }),
        });
        if (!identity || dispatchOwner(identity) !== owner || credential !== credentialAtStart) return;
        await removeOrderAction(item.key);
        internalOrders = internalOrders.map(order => order.order_id === item.order_id ? updated : order);
      } catch (error) {
        if (error.status && error.status !== 429 && error.status < 500)
          await saveOrderAction({ ...item, state: 'NEEDS_REVIEW', http_status: error.status });
        notice(`Assignment report remains on this device: ${error.message}`, true);
        break;
      }
    }
    queuedOrderActions = await listOrderActions();
  } catch (error) { notice(`Assignment report queue unavailable: ${error.message}`, true); }
  finally { syncingOrderActions = false; render(); }
}
function openAdvisoryQueue() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open('trickee-advisory-response-v1', 1);
    request.onupgradeneeded = () => request.result.createObjectStore('responses', { keyPath: 'key' });
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}
async function listAdvisoryResponses() {
  const db = await openAdvisoryQueue();
  return new Promise((resolve, reject) => {
    const tx = db.transaction('responses', 'readonly');
    const request = tx.objectStore('responses').getAll();
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
    tx.oncomplete = () => db.close();
  });
}
async function queueAdvisoryResponse(me, nudgeId, outcome) {
  const db = await openAdvisoryQueue();
  const key = advisoryKey(me, nudgeId);
  return new Promise((resolve, reject) => {
    const tx = db.transaction('responses', 'readwrite');
    const store = tx.objectStore('responses');
    const existing = store.get(key);
    let failure = null;
    existing.onsuccess = () => {
      if (existing.result) {
        if (existing.result.outcome !== outcome) { failure = 'This advisory already has a different queued response.'; tx.abort(); }
        return;
      }
      const count = store.count();
      count.onsuccess = () => {
        if (count.result >= 100) { failure = 'The local advisory queue is full.'; tx.abort(); return; }
        store.put({ key, owner: advisoryOwner(me), nudge_id: nudgeId, outcome,
                    idempotency_key: `response-${nudgeId}-${outcome}` });
      };
    };
    tx.oncomplete = () => { db.close(); resolve(); };
    tx.onabort = () => { db.close(); reject(new Error(failure || 'Could not save the advisory response on this device.')); };
    tx.onerror = () => { db.close(); reject(tx.error || new Error('Advisory queue is unavailable.')); };
  });
}
async function removeAdvisoryResponse(key) {
  const db = await openAdvisoryQueue();
  return new Promise((resolve, reject) => {
    const tx = db.transaction('responses', 'readwrite');
    tx.objectStore('responses').delete(key);
    tx.oncomplete = () => { db.close(); resolve(); };
    tx.onerror = () => { db.close(); reject(tx.error); };
  });
}
async function syncAdvisoryResponses() {
  if (!identity || syncingAdvisories) return;
  const owner = advisoryOwner(identity), credentialAtStart = credential;
  syncingAdvisories = true;
  render();
  try {
    queuedAdvisories = await listAdvisoryResponses();
    for (const item of queuedAdvisories.filter(entry => entry.owner === owner)) {
      if (!identity || advisoryOwner(identity) !== owner || credential !== credentialAtStart) return;
      const serverNudge = nudges.find(nudge => nudge.nudge_id === item.nudge_id);
      if (!serverNudge) continue; // Keep it for the original assignment or an operator review.
      if (serverNudge.status !== 'PENDING') {
        if (serverNudge.status === item.outcome) await removeAdvisoryResponse(item.key);
        continue; // A different recorded outcome is a visible conflict, not a silent overwrite.
      }
      try {
        const updated = await api(`/api/v1/driver/nudges/${encodeURIComponent(item.nudge_id)}/respond`, {
          method: 'POST', body: JSON.stringify({ outcome: item.outcome, idempotency_key: item.idempotency_key }),
        });
        if (!identity || advisoryOwner(identity) !== owner || credential !== credentialAtStart) return;
        await removeAdvisoryResponse(item.key);
        nudges = nudges.map(nudge => nudge.nudge_id === item.nudge_id ? updated : nudge);
      } catch (error) {
        notice(`Advisory response remains queued: ${error.message}`, true);
        break;
      }
    }
    queuedAdvisories = await listAdvisoryResponses();
  } catch (error) { notice(`Advisory queue unavailable: ${error.message}`, true); }
  finally { syncingAdvisories = false; render(); }
}
async function api(path, options = {}) {
  const { responseType = 'json', ...fetchOptions } = options;
  const response = await fetch(path, {
    ...fetchOptions,
    headers: {'X-Driver-Key': credential, 'Content-Type': 'application/json', ...(options.headers || {})},
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const detail = typeof body?.detail === 'string' ? body.detail : `Request failed (${response.status}).`;
    const error = new Error(detail); error.status = response.status; throw error;
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
  if (changeController || !identity || !credential) return;
  const controller = new AbortController();
  const owner = advisoryOwner(identity), watchedCredential = credential;
  changeController = controller;
  void (async () => {
    while (changeController === controller && credential === watchedCredential &&
           identity && advisoryOwner(identity) === owner) {
      try {
        const batch = await api(`/api/v1/driver/updates?after=${changeCursor}&wait_seconds=15`,
          { signal: controller.signal, cache: 'no-store' });
        if (changeController !== controller || credential !== watchedCredential) break;
        if (batch.reset_required || batch.events.length) {
          if (!await refresh()) throw new Error('Driver snapshot refresh failed');
          if (!identity || advisoryOwner(identity) !== owner) break;
        }
        changeCursor = batch.next_cursor;
      } catch (error) {
        if (controller.signal.aborted) break;
        if (error.status === 401) { stopChangeWatch(); void refresh(); break; }
        await new Promise(resolve => setTimeout(resolve, 1500));
      }
    }
    if (changeController === controller) changeController = null;
  })();
}
function openQueue() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open('trickee-driver-queue-v1', 1);
    request.onupgradeneeded = () => {
      const db = request.result;
      const points = db.createObjectStore('points', { keyPath: ['trip_id', 'sequence'] });
      points.createIndex('trip_id', 'trip_id');
      db.createObjectStore('meta', { keyPath: 'trip_id' });
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}
async function queuePoint(tripId, point) {
  const db = await openQueue();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(['points', 'meta'], 'readwrite');
    const meta = tx.objectStore('meta');
    const current = meta.get(tripId);
    current.onsuccess = () => {
      const state = current.result || { trip_id: tripId, sequence: 0, count: 0 };
      if (state.count >= MAX_QUEUED) { tx.abort(); return; }
      state.sequence += 1; state.count += 1;
      tx.objectStore('points').put({ ...point, trip_id: tripId, sequence: state.sequence });
      meta.put(state);
    };
    tx.oncomplete = () => { db.close(); resolve(); };
    tx.onabort = () => { db.close(); reject(new Error('GPS queue is full or unavailable. Tracking stopped to protect data.')); };
    tx.onerror = () => { db.close(); reject(tx.error || new Error('Could not save GPS locally.')); };
  });
}
async function pendingPoints(tripId) {
  const db = await openQueue();
  return new Promise((resolve, reject) => {
    const tx = db.transaction('points', 'readonly');
    const request = tx.objectStore('points').index('trip_id').getAll(IDBKeyRange.only(tripId));
    request.onsuccess = () => resolve(request.result.sort((a, b) => a.sequence - b.sequence));
    request.onerror = () => reject(request.error);
    tx.oncomplete = () => db.close();
  });
}
async function removePoints(tripId, batch) {
  const db = await openQueue();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(['points', 'meta'], 'readwrite');
    for (const point of batch) tx.objectStore('points').delete([tripId, point.sequence]);
    const meta = tx.objectStore('meta');
    const current = meta.get(tripId);
    current.onsuccess = () => {
      if (current.result) meta.put({ ...current.result, count: Math.max(0, current.result.count - batch.length) });
    };
    tx.oncomplete = () => { db.close(); resolve(); };
    tx.onerror = () => { db.close(); reject(tx.error); };
  });
}
async function clearTripQueue(tripId) {
  const db = await openQueue();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(['points', 'meta'], 'readwrite');
    const points = tx.objectStore('points');
    const keys = points.index('trip_id').getAllKeys(IDBKeyRange.only(tripId));
    keys.onsuccess = () => {
      for (const key of keys.result) points.delete(key);
      tx.objectStore('meta').delete(tripId);
    };
    tx.oncomplete = () => { db.close(); resolve(); };
    tx.onerror = () => { db.close(); reject(tx.error); };
  });
}
async function syncGps() {
  if (!credential || syncing || erasingPhoneData) return;
  if (!phoneConsent?.granted || pendingErasure(identity)) {
    notice('Queued GPS remains on this device while trip phone-data consent is withdrawn or unavailable.', true);
    return;
  }
  syncing = true;
  $('syncGps').disabled = true;
  let uploaded = 0;
  try {
    for (const trip of trips) {
      while (true) {
        const batch = (await pendingPoints(trip.trip_id)).slice(0, 100);
        if (!batch.length) break;
        const points = batch.map(({ trip_id, ...point }) => point);
        const batchId = `b-${batch[0].sequence}-${batch[batch.length - 1].sequence}`;
        await api(`/api/v1/driver/trips/${encodeURIComponent(trip.trip_id)}/gps-batches`, {
          method: 'POST', body: JSON.stringify({ batch_id: batchId, points }),
        });
        await removePoints(trip.trip_id, batch);
        uploaded += batch.length;
      }
    }
    if (uploaded) notice(`${uploaded} queued GPS point${uploaded === 1 ? '' : 's'} synced. Phone GPS remains separate from vehicle telemetry.`);
  } catch (error) {
    notice(`GPS remains queued on this device: ${error.message}`, true);
    if (error.status === 401) stopTracking();
    if (error.status === 403) {
      try {
        phoneConsent = await api('/api/v1/driver/consent/phone-trip');
        if (!phoneConsent.granted) stopTracking();
        render();
      } catch (_) { stopTracking(); phoneConsent = null; render(); }
    }
  } finally {
    syncing = false;
    render();
  }
}
function stopTracking() {
  if (watchId !== null && navigator.geolocation) navigator.geolocation.clearWatch(watchId);
  watchId = null;
  $('trackingState').textContent = 'Foreground GPS tracking is off. Queued points remain on this device until synced.';
  $('startTracking').textContent = 'Start GPS';
}
function startTracking() {
  if (!phoneConsent?.granted || pendingErasure(identity)) {
    notice('Agree to trip GPS and motion collection before starting GPS.', true); return;
  }
  if (!activeTrip || !navigator.geolocation || !window.isSecureContext) {
    notice('Foreground GPS needs a secure browser connection and an active trip.', true); return;
  }
  if (watchId !== null) { stopTracking(); return; }
  const tripId = activeTrip.trip_id;
  watchId = navigator.geolocation.watchPosition(position => {
    if (!phoneConsent?.granted) return;
    const sample = {
      timestamp: new Date(position.timestamp).toISOString(),
      lat: position.coords.latitude, lng: position.coords.longitude,
      accuracy: position.coords.accuracy ?? null,
      speed: position.coords.speed >= 0 ? position.coords.speed : null,
      heading: position.coords.heading ?? null,
      altitude: position.coords.altitude ?? null,
      mocked: false, provider: 'BROWSER_GEOLOCATION',
    };
    queuePoint(tripId, sample).then(() => {
      $('trackingState').textContent = `Foreground GPS active · last point ${time(sample.timestamp)} · queue syncs every 20 seconds.`;
    }).catch(error => { stopTracking(); notice(error.message, true); });
  }, error => {
    stopTracking(); notice(`GPS unavailable: ${error.message}`, true);
  }, { enableHighAccuracy: true, maximumAge: 0, timeout: 15000 });
  $('startTracking').textContent = 'Stop GPS';
  $('trackingState').textContent = 'Waiting for location permission and first GPS point…';
}
function render() {
  const connected = !!identity;
  $('connection').textContent = connected ? 'CONNECTED' : 'DISCONNECTED';
  $('connection').classList.toggle('connected', connected);
  $('disconnect').hidden = !credential;
  const twin = identity?.twin;
  $('vehicleId').textContent = identity?.vehicle_id || 'No vehicle connected';
  $('soc').textContent = twin?.battery?.soc_pct == null ? '—' : `${twin.battery.soc_pct}%`;
  $('signalQuality').textContent = twin?.quality || '—';
  $('vehicleState').textContent = twin?.state || '—';
  $('vehicleObserved').textContent = twin?.observed_at
    ? `Last vehicle telemetry: ${time(twin.observed_at)} · ${twin.quality}`
    : 'Vehicle telemetry is unavailable. Phone GPS cannot supply battery or BMS signals.';
  $('tripTitle').textContent = activeTrip ? `${activeTrip.trip_id} · ACTIVE` : 'No active trip';
  const withdrawalPending = pendingWithdrawal(identity);
  $('phoneConsentStatus').textContent = !connected ? 'Connect to review phone data consent.'
    : pendingErasure(identity) ? 'Erasure pending server confirmation. Phone trip data stays on this device.'
    : withdrawalPending ? 'Withdrawal pending server confirmation. Phone trip data stays on this device.'
    : phoneConsent?.granted ? `Trip GPS and motion consent granted · ${time(phoneConsent.updated_at)}`
    : 'Trip GPS and motion consent is not granted.';
  $('grantPhoneConsent').disabled = !connected || withdrawalPending || pendingErasure(identity) || !!phoneConsent?.granted;
  $('withdrawPhoneConsent').disabled = !connected || pendingErasure(identity) || (!withdrawalPending && !phoneConsent?.granted);
  $('withdrawPhoneConsent').textContent = withdrawalPending ? 'Retry withdrawal' : 'Withdraw phone data consent';
  $('startTrip').disabled = !connected || !!activeTrip;
  $('startTracking').disabled = !connected || !activeTrip || !phoneConsent?.granted || withdrawalPending || pendingErasure(identity);
  const openStop = activeTrip?.active_stop;
  $('stopStatus').textContent = openStop
    ? `${openStop.kind} stop · driver reported · started ${time(openStop.started_at)}`
    : activeTrip ? 'No driver-reported stop is open.' : 'No driver-reported stop.';
  $('startWait').disabled = !connected || !activeTrip || !!openStop;
  $('startChargeStop').disabled = !connected || !activeTrip || !!openStop;
  $('endStop').disabled = !connected || !openStop;
  $('completeTrip').disabled = !connected || !activeTrip || !!openStop;
  $('syncGps').disabled = !connected || syncing || !phoneConsent?.granted || withdrawalPending || pendingErasure(identity);
  $('exportPhoneData').disabled = !connected;
  $('erasePhoneData').disabled = !connected || (!pendingErasure(identity) && !!activeTrip) || syncing || erasingPhoneData;
  $('erasePhoneData').textContent = pendingErasure(identity) ? 'Retry phone data erasure' : 'Erase phone data';
  $('phoneDataStatus').textContent = !connected ? 'Connect to manage trip phone data.'
    : pendingErasure(identity) ? 'Erasure pending server confirmation. Collection and upload remain off.'
    : activeTrip ? 'Complete your active trip before erasing phone data.'
    : 'Cloud export and erasure are available for this driver.';
  $('findChargers').disabled = !connected;
  $('previewRoute').disabled = !connected;
  const parent = $('tripList'); parent.replaceChildren();
  if (!trips.length) { const p = document.createElement('p'); p.className = 'subtle'; p.textContent = connected ? 'No trips yet.' : 'Connect to review your trips.'; parent.append(p); }
  for (const trip of trips) {
    const card = document.createElement('article'); card.className = 'trip-card';
    const heading = document.createElement('strong'); heading.textContent = trip.trip_id;
    const sub = document.createElement('small'); sub.textContent = `${time(trip.started_at)} · ${trip.source} · ${trip.quality} · ${trip.phone_gps_sample_count} phone GPS points`;
    const left = document.createElement('div'); left.append(heading, sub);
    const status = document.createElement('span'); status.textContent = trip.status;
    card.append(left, status); parent.append(card);
  }
  const schedule = $('driverPlanList'); schedule.replaceChildren();
  for (const plan of dailyPlans) {
    const card = document.createElement('article'); card.className = 'driver-plan-card';
    const heading = document.createElement('strong'); heading.textContent = `${plan.service_date} · ${plan.timezone}`;
    const source = document.createElement('small'); source.textContent = `${plan.stops.length} stops · ${plan.starting_soc_pct}% ${plan.starting_soc_source} starting charge · ${plan.result?.quality || 'route unavailable'}`;
    card.append(heading, source);
    for (const [index, stop] of plan.stops.entries()) {
      const row = document.createElement('p');
      const leg = plan.result?.legs?.[index];
      row.textContent = `${stop.requested_arrival_local} · ${stop.label} · ${stop.coordinates.latitude}, ${stop.coordinates.longitude}${leg?.route_source === 'PROVIDER' ? ` · ${leg.distance_km} km provider route${leg.arrival_soc_pct == null ? '' : ` · ${leg.arrival_soc_pct}% estimated arrival charge`}` : ' · route estimate unavailable'}`;
      card.append(row);
    }
    schedule.append(card);
  }
  if (!dailyPlans.length) schedule.textContent = connected ? 'No confirmed daily plans for your assigned vehicle.' : 'Connect to review your schedule.';
  const inbox = $('nudgeList'); inbox.replaceChildren();
  const ownedQueued = connected ? queuedAdvisories.filter(item => item.owner === advisoryOwner(identity)) : [];
  $('syncAdvisories').disabled = !connected || !ownedQueued.length || syncingAdvisories;
  $('nudgeStatus').textContent = connected
    ? `${nudges.filter(nudge => nudge.status === 'PENDING').length} pending advisories · ${ownedQueued.length} response${ownedQueued.length === 1 ? '' : 's'} queued on this device. Saved changes refresh automatically while connected.`
    : 'Connect to review your inbox.';
  for (const nudge of nudges) {
    const card = document.createElement('article'); card.className = 'nudge-card';
    const queued = ownedQueued.find(item => item.nudge_id === nudge.nudge_id);
    const displayStatus = queued && nudge.status === 'PENDING' ? `${queued.outcome} QUEUED` : nudge.status;
    const heading = document.createElement('strong'); heading.textContent = `${nudge.kind.replace('_', ' ')} · ${displayStatus}`;
    const message = document.createElement('p'); message.textContent = nudge.message;
    const meta = document.createElement('small'); meta.textContent = `${time(nudge.created_at)}${nudge.plan_id ? ` · plan ${nudge.plan_id}` : ''}`;
    card.append(heading, message, meta);
    if (nudge.status === 'PENDING' && !queued) {
      const actions = document.createElement('div'); actions.className = 'actions';
      for (const [label, outcome] of [['Acknowledge', 'ACKNOWLEDGED'], ['Decline', 'DECLINED']]) {
        const button = document.createElement('button'); button.type = 'button'; button.className = outcome === 'DECLINED' ? 'quiet' : 'secondary'; button.textContent = label;
        button.addEventListener('click', async () => {
          const responseOwner = identity;
          const credentialAtStart = credential;
          for (const action of actions.querySelectorAll('button')) action.disabled = true;
          try {
            const updated = await api(`/api/v1/driver/nudges/${encodeURIComponent(nudge.nudge_id)}/respond`, {
              method: 'POST', body: JSON.stringify({ outcome, idempotency_key: `response-${nudge.nudge_id}-${outcome}` }),
            });
            if (!identity || advisoryOwner(identity) !== advisoryOwner(responseOwner) || credential !== credentialAtStart) return;
            nudges = nudges.map(item => item.nudge_id === nudge.nudge_id ? updated : item);
            render(); notice(`Advisory ${outcome.toLowerCase()}. Your fleet can review the outcome.`);
          } catch (error) {
            if (!error.status || error.status === 429 || error.status >= 500) {
              try {
                await queueAdvisoryResponse(responseOwner, nudge.nudge_id, outcome);
                queuedAdvisories = await listAdvisoryResponses();
                render(); notice(`Advisory ${outcome.toLowerCase()} response saved on this device; sync after reconnecting.`);
              } catch (storageError) { render(); notice(storageError.message, true); }
            } else { render(); notice(error.message, true); }
          }
        });
        actions.append(button);
      }
      card.append(actions);
    }
    inbox.append(card);
  }
  if (connected && !nudges.length) inbox.textContent = 'No advisories in your inbox.';
  const dispatchInbox = $('dispatchList'); dispatchInbox.replaceChildren();
  const ownedDecisions = connected ? queuedDispatch.filter(item => item.owner === dispatchOwner(identity)) : [];
  $('syncDispatch').disabled = !connected || !ownedDecisions.length || syncingDispatch;
  $('dispatchStatus').textContent = connected
    ? `${dispatchOffers.filter(offer => offer.status === 'PENDING').length} pending pickup proposals · ${ownedDecisions.length} decision${ownedDecisions.length === 1 ? '' : 's'} queued on this device. Saved changes refresh automatically while connected.`
    : 'Connect to review pickup proposals.';
  for (const offer of dispatchOffers) {
    const queued = ownedDecisions.find(item => item.offer_id === offer.offer_id);
    const card = document.createElement('article'); card.className = 'nudge-card';
    const title = document.createElement('strong');
    title.textContent = `${offer.pickup_label} → ${offer.destination_label} · ${queued && offer.status === 'PENDING' ? `${queued.outcome} QUEUED` : offer.status}`;
    const message = document.createElement('p'); message.textContent = offer.message;
    const pins = document.createElement('small');
    pins.textContent = `Pickup ${offer.pickup.latitude}, ${offer.pickup.longitude} · destination ${offer.destination.latitude}, ${offer.destination.longitude}`;
    const meta = document.createElement('small');
    meta.textContent = `Sent ${time(offer.created_at)} · expires ${time(offer.expires_at)}${offer.external_reference ? ` · reference ${offer.external_reference}` : ''}`;
    card.append(title, message, pins, meta);
    if (offer.status === 'PENDING' && !offer.seen_at && !dispatchSeenRequests.has(offer.offer_id)) {
      dispatchSeenRequests.add(offer.offer_id);
      const owner = dispatchOwner(identity), credentialAtStart = credential;
      void api(`/api/v1/driver/dispatch-proposals/${encodeURIComponent(offer.offer_id)}/seen`, { method: 'POST' })
        .then(updated => {
          if (identity && dispatchOwner(identity) === owner && credential === credentialAtStart)
            dispatchOffers = dispatchOffers.map(item => item.offer_id === offer.offer_id ? updated : item);
        })
        .catch(() => { dispatchSeenRequests.delete(offer.offer_id); });
    }
    if (offer.status === 'PENDING' && !queued) {
      const actions = document.createElement('div'); actions.className = 'actions';
      for (const [label, outcome] of [['Accept proposal', 'ACCEPTED'], ['Decline', 'DECLINED']]) {
        const button = document.createElement('button'); button.type = 'button';
        button.className = outcome === 'DECLINED' ? 'quiet' : 'secondary'; button.textContent = label;
        button.addEventListener('click', async () => {
          const responseOwner = identity, credentialAtStart = credential;
          for (const action of actions.querySelectorAll('button')) action.disabled = true;
          try {
            const updated = await api(`/api/v1/driver/dispatch-proposals/${encodeURIComponent(offer.offer_id)}/respond`, {
              method: 'POST', body: JSON.stringify({ outcome, idempotency_key: `answer-${offer.offer_id}-${outcome}` }),
            });
            if (!identity || dispatchOwner(identity) !== dispatchOwner(responseOwner) || credential !== credentialAtStart) return;
            dispatchOffers = dispatchOffers.map(item => item.offer_id === offer.offer_id ? updated : item);
            render(); notice(`Pickup proposal ${outcome.toLowerCase()}. Your fleet can review the decision.`);
          } catch (error) {
            if (!error.status || error.status === 429 || error.status >= 500) {
              try {
                await queueDispatchResponse(responseOwner, offer.offer_id, outcome);
                queuedDispatch = await listDispatchResponses();
                render(); notice(`Pickup decision saved on this device; sync after reconnecting.`);
              } catch (storageError) { render(); notice(storageError.message, true); }
            } else { render(); notice(error.message, true); }
          }
        });
        actions.append(button);
      }
      card.append(actions);
    }
    if (queued && offer.status !== 'PENDING' && offer.status !== queued.outcome) {
      const conflict = document.createElement('p'); conflict.className = 'subtle';
      conflict.textContent = `The server says ${offer.status}. The queued ${queued.outcome.toLowerCase()} decision cannot be applied.`;
      const discard = document.createElement('button'); discard.type = 'button'; discard.className = 'quiet'; discard.textContent = 'Discard queued decision';
      discard.addEventListener('click', async () => {
        discard.disabled = true;
        try { await removeDispatchResponse(queued.key); queuedDispatch = await listDispatchResponses(); render(); }
        catch (error) { notice(error.message, true); discard.disabled = false; }
      });
      card.append(conflict, discard);
    }
    dispatchInbox.append(card);
  }
  if (connected && !dispatchOffers.length) dispatchInbox.textContent = 'No pickup proposals for your assigned vehicle.';
  const orderList = $('orderList'); orderList.replaceChildren();
  const ownedOrderActions = connected ? queuedOrderActions.filter(item => item.owner === dispatchOwner(identity)) : [];
  $('syncOrders').disabled = !connected || syncingOrderActions || !ownedOrderActions.some(item => item.state === 'QUEUED');
  $('orderStatus').textContent = connected
    ? `${internalOrders.filter(order => order.status === 'ASSIGNED' || order.status === 'PICKED_UP').length} active internal assignments · ${ownedOrderActions.length} report${ownedOrderActions.length === 1 ? '' : 's'} saved on this device.`
    : 'Connect to review assignments.';
  for (const order of internalOrders) {
    const queued = ownedOrderActions.find(item => item.order_id === order.order_id);
    const card = document.createElement('article'); card.className = 'nudge-card';
    const title = document.createElement('strong'); title.textContent = `${order.pickup_label} → ${order.destination_label} · ${order.status.replaceAll('_', ' ')}${queued ? ` · PHONE ${queued.state} ${queued.action.replaceAll('_', ' ')}` : ''}`;
    const pins = document.createElement('small'); pins.textContent = `Pickup ${order.pickup.latitude}, ${order.pickup.longitude} · destination ${order.destination.latitude}, ${order.destination.longitude}`;
    const quality = document.createElement('p'); quality.className = 'subtle'; quality.textContent = `${order.order_id} · ${order.fulfilment_quality.replaceAll('_', ' ').toLowerCase()}${order.reconciliation_note ? ` · ${order.reconciliation_note}` : ''}`;
    card.append(title, pins, quality);
    if ((order.status === 'ASSIGNED' || order.status === 'PICKED_UP') && !queued) {
      const action = order.status === 'ASSIGNED' ? 'PICKUP' : 'REPORT_DELIVERY';
      const button = document.createElement('button'); button.type = 'button'; button.className = 'secondary';
      button.textContent = order.status === 'ASSIGNED' ? 'Report pickup' : 'Report delivery';
      button.addEventListener('click', async () => {
        const owner = identity, credentialAtStart = credential;
        button.disabled = true;
        try {
          await queueOrderAction(owner, order.order_id, action);
          if (!identity || dispatchOwner(identity) !== dispatchOwner(owner) || credential !== credentialAtStart) return;
          queuedOrderActions = await listOrderActions(); render();
          await syncOrderActions();
          if (queuedOrderActions.some(item => item.owner === dispatchOwner(owner) && item.order_id === order.order_id))
            notice('Assignment report saved on this device; sync after reconnecting.');
          else notice(action === 'PICKUP' ? 'Pickup reported to your fleet.' : 'Delivery reported. Your fleet must reconcile it.');
        } catch (error) { if (identity) notice(error.message, true); button.disabled = false; }
      });
      card.append(button);
    }
    if (queued?.state === 'NEEDS_REVIEW') {
      const warning = document.createElement('small'); warning.textContent = `Server rejected this report (HTTP ${queued.http_status}). Review the current assignment before retrying or clearing it.`;
      const retry = document.createElement('button'); retry.type = 'button'; retry.className = 'secondary'; retry.textContent = 'Retry reviewed assignment report';
      retry.addEventListener('click', async () => {
        retry.disabled = true;
        try { await saveOrderAction({ ...queued, state: 'QUEUED', http_status: null }); queuedOrderActions = await listOrderActions(); await syncOrderActions(); }
        catch (error) { notice(error.message, true); retry.disabled = false; }
      });
      const discard = document.createElement('button'); discard.type = 'button'; discard.className = 'quiet'; discard.textContent = 'Clear reviewed assignment report';
      discard.addEventListener('click', async () => {
        discard.disabled = true;
        try { await removeOrderAction(queued.key); queuedOrderActions = await listOrderActions(); render(); }
        catch (error) { notice(error.message, true); discard.disabled = false; }
      });
      card.append(warning, retry, discard);
    }
    orderList.append(card);
  }
  for (const queued of ownedOrderActions.filter(item => !internalOrders.some(order => order.order_id === item.order_id))) {
    const card = document.createElement('article'); card.className = 'nudge-card';
    const title = document.createElement('strong'); title.textContent = `${queued.order_id} · PHONE ${queued.state} ${queued.action.replaceAll('_', ' ')}`;
    const note = document.createElement('p'); note.textContent = 'This assignment is absent from the server inbox. Review it with your fleet before clearing the local report.';
    const discard = document.createElement('button'); discard.type = 'button'; discard.className = 'quiet'; discard.textContent = 'Clear reviewed assignment report';
    discard.addEventListener('click', async () => {
      discard.disabled = true;
      try { await removeOrderAction(queued.key); queuedOrderActions = await listOrderActions(); render(); }
      catch (error) { notice(error.message, true); discard.disabled = false; }
    });
    card.append(title, note, discard); orderList.append(card);
  }
  if (connected && !internalOrders.length && !ownedOrderActions.length) orderList.textContent = 'No internal assignments for your vehicle.';
  const serviceList = $('serviceList');
  const serviceDrafts = new Map([...serviceList.querySelectorAll('.nudge-card[data-case-id]')].map(card => [
    card.dataset.caseId, {
      reply: card.querySelector('.service-reply-form textarea')?.value || '',
      repliesOpen: card.querySelector('.service-messages')?.dataset.open === '1',
    },
  ]));
  serviceList.replaceChildren();
  const ownedServiceCommands = connected ? serviceOutbox.filter(item => item.owner === serviceOwner(identity)) : [];
  $('submitServiceCase').disabled = !connected || syncingService;
  $('syncService').disabled = !connected || syncingService || !ownedServiceCommands.some(item => item.state === 'QUEUED');
  $('serviceStatus').textContent = connected
    ? `${serviceCases.filter(item => item.status !== 'RESOLVED').length} open service cases · ${ownedServiceCommands.length} command${ownedServiceCommands.length === 1 ? '' : 's'} stored on this device.`
    : 'Connect to review service cases.';
  for (const item of ownedServiceCommands) {
    const card = document.createElement('article'); card.className = 'nudge-card';
    const title = document.createElement('strong');
    title.textContent = `${item.kind === 'CREATE' ? 'Issue report' : 'Follow-up'} · PHONE ${item.state}`;
    const detail = document.createElement('p');
    detail.textContent = item.kind === 'CREATE' ? `${item.body.title} · ${item.body.description}` : item.body.body;
    card.append(title, detail);
    if (item.state === 'NEEDS_REVIEW') {
      const reason = document.createElement('small'); reason.textContent = `Server rejected this command (HTTP ${item.http_status}). Review before retrying or clearing it.`;
      const retry = document.createElement('button'); retry.type = 'button'; retry.className = 'secondary'; retry.textContent = 'Retry reviewed command';
      retry.addEventListener('click', async () => {
        retry.disabled = true;
        try { await updateServiceCommand({ ...item, state: 'QUEUED', http_status: null }); await syncServiceCommands(); }
        catch (error) { notice(error.message, true); retry.disabled = false; }
      });
      const clear = document.createElement('button'); clear.type = 'button'; clear.className = 'quiet'; clear.textContent = 'Clear local command';
      clear.addEventListener('click', async () => {
        clear.disabled = true;
        try { await removeServiceCommand(item.key); serviceOutbox = await listServiceCommands(); render(); }
        catch (error) { notice(error.message, true); clear.disabled = false; }
      });
      card.append(reason, retry, clear);
    }
    serviceList.append(card);
  }
  for (const serviceCase of serviceCases) {
    const card = document.createElement('article'); card.className = 'nudge-card'; card.dataset.caseId = serviceCase.case_id;
    const title = document.createElement('strong');
    title.textContent = `${serviceCase.priority} · ${serviceCase.category.replaceAll('_', ' ')} · ${serviceCase.status}`;
    const heading = document.createElement('p'); heading.textContent = serviceCase.title;
    const description = document.createElement('p'); description.textContent = serviceCase.description;
    const meta = document.createElement('small');
    meta.textContent = `Reported ${time(serviceCase.created_at)} · updated ${time(serviceCase.updated_at)}`;
    card.append(title, heading, description, meta);
    if (serviceCase.resolution_note) {
      const resolved = document.createElement('p');
      resolved.textContent = `Resolution: ${serviceCase.resolution_code} · ${serviceCase.resolution_note}`;
      card.append(resolved);
    }
    const messages = document.createElement('div'); messages.className = 'subtle service-messages';
    const view = document.createElement('button'); view.type = 'button'; view.className = 'quiet'; view.textContent = 'View replies';
    view.addEventListener('click', async () => {
      messages.dataset.open = '1';
      view.disabled = true;
      try {
        const rows = await api(`/api/v1/driver/service-cases/${encodeURIComponent(serviceCase.case_id)}/messages`);
        messages.replaceChildren();
        for (const row of rows) {
          const entry = document.createElement('p'); entry.textContent = `${row.actor_kind} · ${time(row.created_at)} · ${row.body}`;
          messages.append(entry);
        }
        if (!rows.length) messages.textContent = 'No follow-up messages yet.';
      } catch (error) { messages.textContent = error.message; }
      finally { view.disabled = false; }
    });
    card.append(view, messages);
    if (serviceCase.status !== 'RESOLVED') {
      const form = document.createElement('form'); form.className = 'service-reply-form';
      const label = document.createElement('label'); label.textContent = 'Add a follow-up';
      const field = document.createElement('textarea'); field.maxLength = 1000; field.rows = 2; field.required = true;
      field.value = serviceDrafts.get(serviceCase.case_id)?.reply || '';
      label.append(field);
      const send = document.createElement('button'); send.className = 'secondary'; send.type = 'submit'; send.textContent = 'Send update';
      form.append(label, send);
      form.addEventListener('submit', event => {
        event.preventDefault();
        const body = field.value.trim();
        if (!body || !identity) return;
        const owner = identity, credentialAtStart = credential;
        const message = { message_id: `MSG-${crypto.randomUUID()}`, idempotency_key: `update-${crypto.randomUUID()}`, body };
        send.disabled = true;
        void (async () => {
          try {
            await queueServiceCommand(owner, 'MESSAGE', serviceCase.case_id,
              `/api/v1/driver/service-cases/${encodeURIComponent(serviceCase.case_id)}/messages`, message);
            if (!identity || serviceOwner(identity) !== serviceOwner(owner) || credential !== credentialAtStart) return;
            serviceOutbox = await listServiceCommands(); field.value = ''; render();
            notice('Follow-up saved on this device. Syncing with your fleet.');
            await syncServiceCommands();
          } catch (error) { notice(error.message, true); send.disabled = false; }
        })();
      });
      card.append(form);
    }
    serviceList.append(card);
    if (serviceDrafts.get(serviceCase.case_id)?.repliesOpen) view.click();
  }
  if (connected && !ownedServiceCommands.length && !serviceCases.length)
    serviceList.textContent = 'No service cases reported yet.';
}
async function refresh() {
  if (!credential) return false;
  const generation = ++refreshGeneration;
  const consentGeneration = consentMutationGeneration;
  try {
    const [me, ownTrips, ownNudges, ownPlans, consent, ownDispatch, ownCases, ownOrders] = await Promise.all([api('/api/v1/driver/me'), api('/api/v1/driver/trips'), api('/api/v1/driver/nudges'), api('/api/v1/driver/daily-plans'), api('/api/v1/driver/consent/phone-trip'), api('/api/v1/driver/dispatch-proposals'), api('/api/v1/driver/service-cases'), api('/api/v1/driver/internal-orders')]);
    if (generation !== refreshGeneration || consentGeneration !== consentMutationGeneration) return false;
    identity = me; trips = ownTrips; nudges = ownNudges; dailyPlans = ownPlans; dispatchOffers = ownDispatch; serviceCases = ownCases; internalOrders = ownOrders; activeTrip = trips.find(trip => trip.status === 'ACTIVE') || null;
    try { queuedAdvisories = await listAdvisoryResponses(); }
    catch (error) { queuedAdvisories = []; notice(`Advisory queue unavailable: ${error.message}`, true); }
    try { queuedDispatch = await listDispatchResponses(); }
    catch (error) { queuedDispatch = []; notice(`Pickup decision queue unavailable: ${error.message}`, true); }
    try { queuedOrderActions = await listOrderActions(); }
    catch (error) { queuedOrderActions = []; notice(`Assignment report queue unavailable: ${error.message}`, true); }
    try { serviceOutbox = await listServiceCommands(); }
    catch (error) { serviceOutbox = []; notice(`Service case queue unavailable: ${error.message}`, true); }
    if (generation !== refreshGeneration || consentGeneration !== consentMutationGeneration) return false;
    phoneConsent = consent;
    if (pendingWithdrawal(me)) {
      phoneConsent = { granted: false };
      try {
        phoneConsent = await api('/api/v1/driver/consent/phone-trip', {
          method: 'POST', body: JSON.stringify({ granted: false }),
        });
        if (generation !== refreshGeneration) return false;
        localStorage.removeItem(withdrawalKey(me));
      } catch (error) { notice(`Withdrawal is pending: ${error.message}`, true); }
    }
    if (pendingErasure(me) && !erasingPhoneData) {
      phoneConsent = { granted: false };
      stopTracking();
      try {
        await api('/api/v1/driver/phone-data', { method: 'DELETE' });
        for (const trip of ownTrips) await clearTripQueue(trip.trip_id);
        if (generation !== refreshGeneration) return false;
        localStorage.removeItem(erasureKey(me));
        localStorage.removeItem(withdrawalKey(me));
        notice('Cloud phone data erased and listed-trip local queue cleared.');
      } catch (error) { notice(`Erasure is pending: ${error.message}`, true); }
    }
    if (!activeTrip || !phoneConsent?.granted) stopTracking();
    render();
    if (!pendingWithdrawal(me) && !pendingErasure(me)) notice(`Connected as ${identity.driver_id}. Vehicle signals and phone GPS have separate provenance.`);
    if (phoneConsent?.granted) void syncGps();
    if (queuedAdvisories.some(item => item.owner === advisoryOwner(me))) void syncAdvisoryResponses();
    if (queuedDispatch.some(item => item.owner === dispatchOwner(me))) void syncDispatchResponses();
    if (queuedOrderActions.some(item => item.owner === dispatchOwner(me) && item.state === 'QUEUED')) void syncOrderActions();
    if (serviceOutbox.some(item => item.owner === serviceOwner(me) && item.state === 'QUEUED')) void syncServiceCommands();
    startChangeWatch();
    return true;
  } catch (error) {
    if (generation !== refreshGeneration) return false;
    stopChangeWatch();
    identity = null; phoneConsent = null; trips = []; nudges = []; dispatchOffers = []; internalOrders = []; queuedOrderActions = []; serviceCases = []; dailyPlans = []; activeTrip = null; stopTracking(); clearRoute(); render();
    notice(error.message, true);
    return false;
  }
}
$('connectForm').addEventListener('submit', event => {
  event.preventDefault();
  const nextCredential = $('driverKey').value.trim();
  if (nextCredential !== credential) {
    personalIdentityVersion++;
    refreshGeneration++; stopChangeWatch(); stopTracking(); clearRoute(); identity = null; phoneConsent = null; trips = []; nudges = []; dispatchOffers = []; internalOrders = []; queuedOrderActions = []; serviceCases = []; dailyPlans = []; activeTrip = null;
    render();
  }
  credential = nextCredential;
  sessionStorage.setItem('trickeeDriverCredential', credential);
  $('driverKey').value = '';
  void refresh();
});
$('disconnect').addEventListener('click', () => {
  personalIdentityVersion++;
  refreshGeneration++; stopChangeWatch(); stopTracking(); clearRoute(); credential = ''; identity = null; phoneConsent = null; trips = []; nudges = []; dispatchOffers = []; internalOrders = []; queuedOrderActions = []; serviceCases = []; dailyPlans = []; activeTrip = null;
  sessionStorage.removeItem('trickeeDriverCredential'); render();
  notice('Disconnected. Any unsynced GPS points remain on this device for later recovery.');
});
$('syncAdvisories').addEventListener('click', () => { void syncAdvisoryResponses(); });
$('syncDispatch').addEventListener('click', () => { void syncDispatchResponses(); });
$('syncOrders').addEventListener('click', () => { void syncOrderActions(); });
$('syncService').addEventListener('click', () => { void syncServiceCommands(); });
$('serviceForm').addEventListener('submit', event => {
  event.preventDefault();
  if (!identity) return;
  const owner = identity, credentialAtStart = credential;
  const title = $('serviceTitleInput').value.trim(), description = $('serviceDescription').value.trim();
  if (!title || !description) { notice('Enter a short title and description.', true); return; }
  const body = { case_id: `CASE-${crypto.randomUUID()}`, idempotency_key: `report-${crypto.randomUUID()}`,
    category: $('serviceCategory').value, priority: $('servicePriority').value, title, description };
  $('submitServiceCase').disabled = true;
  void (async () => {
    try {
      await queueServiceCommand(owner, 'CREATE', body.case_id, '/api/v1/driver/service-cases', body);
      if (!identity || serviceOwner(identity) !== serviceOwner(owner) || credential !== credentialAtStart) return;
      serviceOutbox = await listServiceCommands();
      $('serviceForm').reset(); render();
      notice('Issue report saved on this device. Syncing with your fleet.');
      await syncServiceCommands();
    } catch (error) { notice(error.message, true); $('submitServiceCase').disabled = false; }
  })();
});
$('grantPhoneConsent').addEventListener('click', async () => {
  if (!identity || pendingWithdrawal(identity)) return;
  const owner = consentKey(identity), consentGeneration = ++consentMutationGeneration;
  $('grantPhoneConsent').disabled = true;
  try {
    const result = await api('/api/v1/driver/consent/phone-trip', {
      method: 'POST', body: JSON.stringify({ granted: true }),
    });
    if (!identity || consentKey(identity) !== owner || consentGeneration !== consentMutationGeneration) return;
    phoneConsent = result;
    render(); notice('Trip GPS and motion consent recorded. Start GPS separately when your trip is active.');
    void syncGps();
  } catch (error) { render(); notice(error.message, true); }
});
$('withdrawPhoneConsent').addEventListener('click', async () => {
  if (!identity) return;
  const owner = consentKey(identity), key = withdrawalKey(identity);
  const consentGeneration = ++consentMutationGeneration;
  localStorage.setItem(key, '1');
  stopTracking(); phoneConsent = { granted: false }; render();
  try {
    const result = await api('/api/v1/driver/consent/phone-trip', {
      method: 'POST', body: JSON.stringify({ granted: false }),
    });
    localStorage.removeItem(key);
    if (!identity || consentKey(identity) !== owner || consentGeneration !== consentMutationGeneration) return;
    phoneConsent = result;
    render(); notice('Phone trip data consent withdrawn. Queued GPS remains on this device.');
  } catch (error) { render(); notice(`Withdrawal is pending: ${error.message}`, true); }
});
$('exportPhoneData').addEventListener('click', async () => {
  if (!identity) return;
  const owner = consentKey(identity);
  $('exportPhoneData').disabled = true;
  try {
    const samples = [];
    let offset = 0;
    while (true) {
      const page = await api(`/api/v1/driver/phone-data/export?limit=1000&offset=${offset}`);
      if (!identity || consentKey(identity) !== owner) return;
      samples.push(...page.samples);
      if (page.next_offset === null) break;
      offset = page.next_offset;
    }
    const blob = new Blob([JSON.stringify({ purpose: 'PHONE_TRIP_GPS_IMU', total: samples.length,
      exported_at: new Date().toISOString(), samples }, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url; anchor.download = 'trickee-phone-trip-data.json';
    document.body.append(anchor); anchor.click(); anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
    notice(`${samples.length} phone data sample${samples.length === 1 ? '' : 's'} exported.`);
  } catch (error) { notice(`Phone data export failed: ${error.message}`, true); }
  finally { render(); }
});
$('erasePhoneData').addEventListener('click', async () => {
  if (!identity || (!pendingErasure(identity) && activeTrip) || syncing || erasingPhoneData) return;
  if (!pendingErasure(identity) && !window.confirm('Erase this driver’s cloud trip GPS and motion data and queued points for listed trips in this browser? This cannot be undone.')) return;
  const owner = consentKey(identity), ownTrips = trips.map(trip => trip.trip_id);
  localStorage.setItem(erasureKey(identity), '1');
  erasingPhoneData = true;
  stopTracking(); phoneConsent = { granted: false }; render();
  try {
    const result = await api('/api/v1/driver/phone-data', { method: 'DELETE' });
    if (!identity || consentKey(identity) !== owner) return;
    try {
      for (const tripId of ownTrips) await clearTripQueue(tripId);
    } catch (localError) {
      notice(`Cloud phone data erased, but local queue cleanup is pending: ${localError.message}.`, true);
      return;
    }
    localStorage.removeItem(erasureKey(identity));
    localStorage.removeItem(withdrawalKey(identity));
    await refresh();
    notice(`Cloud phone data erased: ${result.deleted_samples} sample${result.deleted_samples === 1 ? '' : 's'}. Consent withdrawn.`);
  } catch (error) {
    notice(`Phone data erasure is pending server confirmation: ${error.message}`, true);
  } finally { erasingPhoneData = false; render(); }
});
$('startTrip').addEventListener('click', async () => {
  $('startTrip').disabled = true;
  try {
    const payload = { trip_id: `P-${crypto.randomUUID()}`, idempotency_key: `start-${crypto.randomUUID()}`,
      starting_soc_pct: optionalSoc('startSoc') };
    const started = await api('/api/v1/driver/trips', { method: 'POST', body: JSON.stringify(payload) });
    activeTrip = started; trips.unshift(started); render();
    notice('Trip started. Start GPS to collect foreground location with your permission.');
  } catch (error) { notice(error.message, true); render(); }
});
$('startTracking').addEventListener('click', startTracking);
async function startStop(kind) {
  if (!activeTrip || activeTrip.active_stop) return;
  const tripId = activeTrip.trip_id;
  const storageKey = `trickee-stop-start-${tripId}`;
  let pending = JSON.parse(sessionStorage.getItem(storageKey) || 'null');
  if (!pending) {
    pending = { stop_id: `S-${crypto.randomUUID()}`, idempotency_key: `stop-${crypto.randomUUID()}`, kind };
    sessionStorage.setItem(storageKey, JSON.stringify(pending));
  }
  $('startWait').disabled = true; $('startChargeStop').disabled = true;
  try {
    const updated = await api(`/api/v1/driver/trips/${encodeURIComponent(tripId)}/stops`, {
      method: 'POST', body: JSON.stringify(pending),
    });
    sessionStorage.removeItem(storageKey);
    activeTrip = updated; trips = trips.map(trip => trip.trip_id === tripId ? updated : trip); render();
    notice(`${updated.active_stop.kind} stop recorded by driver. End it before completing the trip.`);
  } catch (error) {
    if (error.status === 409 || error.status === 422) sessionStorage.removeItem(storageKey);
    notice(error.message, true); render();
  }
}
$('startWait').addEventListener('click', () => startStop('WAITING'));
$('startChargeStop').addEventListener('click', () => startStop('CHARGING'));
$('endStop').addEventListener('click', async () => {
  const stop = activeTrip?.active_stop;
  if (!stop) return;
  const tripId = activeTrip.trip_id;
  $('endStop').disabled = true;
  try {
    const updated = await api(`/api/v1/driver/trips/${encodeURIComponent(tripId)}/stops/${encodeURIComponent(stop.stop_id)}/complete`, {
      method: 'POST', body: JSON.stringify({ idempotency_key: `end-${stop.stop_id}` }),
    });
    activeTrip = updated; trips = trips.map(trip => trip.trip_id === tripId ? updated : trip); render();
    notice(`${stop.kind} stop ended. Driver-reported timing is saved on the trip.`);
  } catch (error) { notice(error.message, true); render(); }
});
$('completeTrip').addEventListener('click', async () => {
  if (!activeTrip) return;
  const tripId = activeTrip.trip_id;
  $('completeTrip').disabled = true;
  stopTracking();
  await syncGps();
  try {
    const completed = await api(`/api/v1/driver/trips/${encodeURIComponent(tripId)}/complete`, {
      method: 'POST', body: JSON.stringify({ idempotency_key: `end-${tripId}`, ending_soc_pct: optionalSoc('endSoc') }),
    });
    trips = trips.map(trip => trip.trip_id === tripId ? completed : trip);
    activeTrip = null; render();
    notice('Trip completed. Any GPS points still queued on this device can be synced after reconnecting.');
  } catch (error) { notice(error.message, true); render(); }
});
$('syncGps').addEventListener('click', syncGps);
$('findChargers').addEventListener('click', () => {
  if (!identity || !navigator.geolocation || !window.isSecureContext) {
    $('chargerStatus').textContent = 'Location permission and a secure browser connection are required.'; return;
  }
  $('findChargers').disabled = true;
  $('chargerStatus').textContent = 'Requesting your location and charger availability…';
  navigator.geolocation.getCurrentPosition(async position => {
    try {
      const search = `latitude=${encodeURIComponent(position.coords.latitude)}&longitude=${encodeURIComponent(position.coords.longitude)}`;
      const result = await api(`/api/v1/driver/chargers/nearby?${search}`);
      const parent = $('chargerList'); parent.replaceChildren();
      if (result.quality !== 'PROVIDER') {
        let ocpi = null;
        try { ocpi = await api(`/api/v1/driver/chargers/ocpi/nearby?${search}`); }
        catch (_) { /* Keep the original, explicitly labeled feed result. */ }
        if (ocpi && ocpi.quality === 'PROVIDER') {
          $('chargerStatus').textContent = `OCPI provider stations · ${ocpi.stations.length} nearby. Status may age; price and queue are unknown until supplied by a contracted provider.`;
          for (const station of ocpi.stations) {
            const card = document.createElement('article'); card.className = 'charger-card';
            const evses = station.evses || [];
            card.dataset.available = String(evses.some(evse => evse.availability === 'AVAILABLE'));
            const title = document.createElement('strong');
            title.textContent = station.name || station.station_id;
            const summary = document.createElement('small');
            summary.textContent = `OCPI provider · ${station.distance_km} km away · ${station.station_id}`;
            const details = document.createElement('small');
            details.textContent = evses.length
              ? evses.map(evse => `${evse.availability} · ${(evse.connectors || []).map(connector => `${connector.standard} ${connector.power_kw == null ? 'power unknown' : `${connector.power_kw} kW`}`).join(', ')}`).join(' | ')
              : 'EVSE status unknown';
            const unknowns = document.createElement('small');
            unknowns.textContent = 'Price and queue unknown · booking unavailable';
            card.append(title, summary, details, unknowns); parent.append(card);
          }
          return;
        }
      }
      $('chargerStatus').textContent = result.quality === 'PROVIDER'
        ? `Provider-reported availability · observed ${time(result.observed_at)}. Queue and price come from the provider.`
        : result.quality === 'SIMULATED'
          ? 'SIMULATED local demonstration chargers · no live availability or price.'
          : `Charger availability ${result.quality.toLowerCase()}: ${result.reason || 'no usable data'}.`;
      for (const charger of result.chargers || []) {
        const card = document.createElement('article'); card.className = 'charger-card';
        card.dataset.available = String(charger.available);
        const title = document.createElement('strong'); title.textContent = charger.name;
        const detail = document.createElement('small');
        detail.textContent = `${charger.source} · ${charger.available ? 'AVAILABLE' : 'UNAVAILABLE'} · ${charger.connector} · ${charger.power_kw} kW · ${charger.distance_km} km away`;
        card.append(title, detail);
        if (result.quality === 'PROVIDER') {
          const cost = document.createElement('small');
          cost.textContent = `${charger.queue_min} min provider queue · ${charger.price_per_kwh} ${charger.currency}/kWh`;
          card.append(cost);
        }
        parent.append(card);
      }
    } catch (error) { $('chargerStatus').textContent = error.message; }
    finally { $('findChargers').disabled = !identity; }
  }, error => {
    $('chargerStatus').textContent = `Location unavailable: ${error.message}`;
    $('findChargers').disabled = !identity;
  }, { enableHighAccuracy: true, maximumAge: 0, timeout: 15000 });
});
$('driverRouteForm').addEventListener('submit', async event => {
  event.preventDefault();
  if (!identity || !navigator.geolocation || !window.isSecureContext) {
    $('routeStatus').textContent = 'A secure browser connection and location permission are required.'; return;
  }
  $('previewRoute').disabled = true;
  const generation = ++routeGeneration;
  $('routeStatus').textContent = 'Requesting your location and a provider road route…';
  $('routeContext').textContent = 'Checking sourced weather and elevation…';
  try {
    const destination = parseCoordinates($('routeDestination').value);
    const position = await new Promise((resolve, reject) => navigator.geolocation.getCurrentPosition(resolve, reject,
      { enableHighAccuracy: true, maximumAge: 0, timeout: 15000 }));
    const origin = { latitude: position.coords.latitude, longitude: position.coords.longitude };
    const [route, mapConfig] = await Promise.all([
      api('/api/v1/driver/routes/road', { method: 'POST', body: JSON.stringify({ origin, destination }) }),
      api('/api/v1/driver/map/config'),
    ]);
    if (generation !== routeGeneration || !identity) return;
    const options = $('driverRouteOptions'); options.replaceChildren();
    const show = (candidate, index) => {
      $('routeStatus').textContent = `Route ${index + 1}: ${candidate.route.distance_km} km · ${Math.round(candidate.route.baseline_eta_min)} min provider baseline · traffic unavailable.`;
      for (const button of options.querySelectorAll('button')) button.setAttribute('aria-pressed', String(Number(button.dataset.index) === index));
      const steps = $('driverRouteSteps'); steps.replaceChildren();
      for (const step of candidate.steps || []) {
        const item = document.createElement('li');
        const distance = step.distance_m >= 1000 ? `${(step.distance_m / 1000).toFixed(1)} km` : `${Math.round(step.distance_m)} m`;
        item.textContent = `${step.type.replaceAll('_', ' ')}${step.modifier ? ` ${step.modifier}` : ''}${step.name ? ` · ${step.name}` : ''} · ${distance}`;
        steps.append(item);
      }
      if (!steps.childElementCount) {
        const item = document.createElement('li'); item.textContent = 'This provider returned no turn details for the selected route.'; steps.append(item);
      }
      if (!mapConfig.tile_url || !window.L) return;
      if (!routeMap) {
        routeMap = L.map('driverMap', { scrollWheelZoom: false }).setView([origin.latitude, origin.longitude], 12);
        L.tileLayer(mapConfig.tile_url, { maxZoom: 19, attribution: mapConfig.attribution || '' }).addTo(routeMap);
      }
      if (routeLayer) routeMap.removeLayer(routeLayer);
      if (routePoints) routeMap.removeLayer(routePoints);
      routeLayer = L.geoJSON(candidate.geometry, { style: { color: '#ffe000', weight: 5, opacity: .95 } }).addTo(routeMap);
      routePoints = L.layerGroup([
        L.circleMarker([origin.latitude, origin.longitude], { radius: 7, color: '#48dff4', fillOpacity: 1 }),
        L.circleMarker([destination.latitude, destination.longitude], { radius: 7, color: '#ffe000', fillOpacity: 1 }),
      ]).addTo(routeMap);
      routeMap.invalidateSize(); routeMap.fitBounds(routeLayer.getBounds(), { padding: [22, 22] });
    };
    for (const [index, candidate] of route.routes.entries()) {
      const button = document.createElement('button'); button.type = 'button'; button.className = 'quiet';
      button.dataset.index = String(index);
      button.textContent = `Route ${index + 1} · ${candidate.route.distance_km} km · ${Math.round(candidate.route.baseline_eta_min)} min`;
      button.addEventListener('click', () => show(candidate, index)); options.append(button);
    }
    show(route.routes[0], 0);
    if (!mapConfig.tile_url) $('routeStatus').textContent += ' Map tiles are not configured.';
    try {
      const context = await api('/api/v1/driver/routes/context', { method: 'POST', body: JSON.stringify({ origin, destination }) });
      if (generation !== routeGeneration || !identity) return;
      const weather = context.weather.quality === 'REPORTED'
        ? `${context.weather.temperature_c.toFixed(1)} °C at origin · ${context.weather.source}`
        : `Weather unavailable (${context.weather.reason})`;
      const elevation = context.elevation.quality === 'REPORTED'
        ? `${context.elevation.elevation_delta_m.toFixed(0)} m endpoint elevation difference · ${context.elevation.source}`
        : `Elevation unavailable (${context.elevation.reason})`;
      $('routeContext').textContent = `${weather}. ${elevation}. Traffic unavailable.`;
    } catch (_) {
      if (generation === routeGeneration) $('routeContext').textContent = 'Weather and elevation unavailable. Traffic unavailable.';
    }
  } catch (error) {
    if (generation !== routeGeneration) return;
    $('driverRouteOptions').replaceChildren();
    $('driverRouteSteps').replaceChildren();
    $('routeStatus').textContent = `Route unavailable: ${error.message}`;
    $('routeContext').textContent = 'Route context unavailable until a route preview succeeds.';
  } finally { $('previewRoute').disabled = !identity; }
});
if ('serviceWorker' in navigator && window.isSecureContext) navigator.serviceWorker.register('/driver/sw.js', {scope: '/driver'}).catch(() => {});
if (credential) void refresh(); else render();
setInterval(() => { if (credential && phoneConsent?.granted) void syncGps(); }, 20000);
setInterval(() => { if (credential && !document.hidden) void refresh(); }, 60000);
