/* Fleet map: locations come only from validated vehicle frames. */
(() => {
  const canvas = document.getElementById('mapCanvas');
  const status = document.getElementById('mapStatus');
  const list = document.getElementById('mapVehicleList');
  let map = null;
  let markers = null;
  let tileLayer = null;
  let tileLoadFailed = false;
  let roadLayer = null;
  let tripLayer = null;
  let replayMarker = null;
  let config = null;
  let vehicles = [];
  let selectedId = null;
  let onSelectVehicle = null;
  let lastCenteredId = null;

  function coordinates(twin) {
    if (twin.quality === 'INVALID') return null;
    const lat = twin.location?.latitude;
    const lon = twin.location?.longitude;
    if (typeof lat !== 'number' || typeof lon !== 'number' || !Number.isFinite(lat) ||
        !Number.isFinite(lon) || lat < -90 || lat > 90 || lon < -180 || lon > 180) return null;
    return [lat, lon];
  }

  function configure(next) {
    config = next;
    tileLoadFailed = false;
    if (config?.tile_url && window.L && !map) {
      map = L.map(canvas, { scrollWheelZoom: false, zoomControl: true }).setView([20.5937, 78.9629], 5);
      markers = L.layerGroup().addTo(map);
    }
    if (map) {
      if (tileLayer) map.removeLayer(tileLayer);
      tileLayer = null;
      if (config?.tile_url) {
        tileLayer = L.tileLayer(config.tile_url, {
          maxZoom: 19,
          attribution: config.quality === 'DEMO'
            ? config.attribution
            : String(config.attribution || '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
        });
        const configuredLayer = tileLayer;
        tileLayer.on('tileerror', () => {
          if (tileLayer === configuredLayer) { tileLoadFailed = true; draw(); }
        });
        tileLayer.addTo(map);
      }
      requestAnimationFrame(() => map.invalidateSize());
    }
    draw();
  }

  function render(twins, selected, selectVehicle) {
    if (selected !== selectedId && replayMarker && map) {
      map.removeLayer(replayMarker);
      replayMarker = null;
    }
    vehicles = Array.isArray(twins) ? twins : [];
    selectedId = selected;
    onSelectVehicle = selectVehicle;
    draw();
  }

  function showRoute(geometry) {
    if (roadLayer && map) map.removeLayer(roadLayer);
    roadLayer = null;
    if (!map || !geometry || geometry.type !== 'LineString' || !Array.isArray(geometry.coordinates)) return;
    const points = geometry.coordinates.map(point => [point[1], point[0]]);
    if (points.length < 2 || points.some(point => point.some(value => !Number.isFinite(value)))) return;
    roadLayer = L.polyline(points, { color: '#ffe000', weight: 5, opacity: 0.9,
      className: 'trickee-road-route' }).addTo(map);
    map.fitBounds(roadLayer.getBounds(), { padding: [32, 32], maxZoom: 13, animate: false });
  }

  function showTripTrace(trace) {
    if (tripLayer && map) map.removeLayer(tripLayer);
    if (replayMarker && map) map.removeLayer(replayMarker);
    replayMarker = null;
    tripLayer = null;
    if (!map || !Array.isArray(trace)) return;
    const points = trace.filter(point => typeof point.latitude === 'number' && typeof point.longitude === 'number' &&
      Number.isFinite(point.latitude) && Number.isFinite(point.longitude) &&
      Math.abs(point.latitude) <= 90 && Math.abs(point.longitude) <= 180)
      .map(point => [point.latitude, point.longitude]);
    if (!points.length) return;
    tripLayer = points.length === 1
      ? L.circleMarker(points[0], { radius: 7, color: '#48dff4', weight: 2 }).addTo(map)
      : L.polyline(points, { color: '#48dff4', weight: 4, opacity: 0.85, dashArray: '8 7' }).addTo(map);
    if (points.length === 1) map.setView(points[0], Math.max(map.getZoom(), 12), { animate: false });
    else map.fitBounds(tripLayer.getBounds(), { padding: [30, 30], maxZoom: 13, animate: false });
  }

  function showReplayPosition(frame) {
    if (!map) return;
    const lat = frame?.latitude;
    const lon = frame?.longitude;
    if (typeof lat !== 'number' || typeof lon !== 'number' ||
        !Number.isFinite(lat) || !Number.isFinite(lon) ||
        Math.abs(lat) > 90 || Math.abs(lon) > 180) {
      if (replayMarker) map.removeLayer(replayMarker);
      replayMarker = null;
      return;
    }
    if (replayMarker) replayMarker.setLatLng([lat, lon]);
    else replayMarker = L.circleMarker([lat, lon], {
      radius: 10, color: '#ffe000', fillColor: '#ffe000',
      fillOpacity: .9, weight: 2, className: 'trickee-replay-position',
    }).addTo(map);
    replayMarker.bindTooltip('Historical vehicle position · not live');
  }

  function draw() {
    const located = vehicles.map(twin => ({ twin, point: coordinates(twin) })).filter(item => item.point);
    const invalidCount = vehicles.filter(twin => twin.quality === 'INVALID').length;
    const staleCount = located.filter(item => item.twin.quality === 'STALE').length;
    const reported = `${located.length} reported location${located.length === 1 ? '' : 's'}`;
    const freshness = staleCount === located.length
      ? 'last known position · telemetry stale'
      : staleCount ? `${staleCount} stale position${staleCount === 1 ? '' : 's'}` : 'latest reported position';
    list.replaceChildren();
    for (const { twin } of located) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'map-vehicle' + (twin.vehicle_id === selectedId ? ' selected' : '');
      button.textContent = `${twin.vehicle_id} · ${twin.quality}${twin.quality === 'STALE' ? ' · last known' : ''}`;
      button.setAttribute('aria-pressed', String(twin.vehicle_id === selectedId));
      button.addEventListener('click', () => onSelectVehicle?.(twin.vehicle_id));
      list.append(button);
    }
    if (!located.length) {
      status.textContent = invalidCount
        ? 'No usable vehicle location: invalid telemetry timestamp. Map markers require a valid GPS frame.'
        : 'No vehicle location reported. Map markers will appear after a valid GPS frame.';
    } else if (!map || !tileLayer) {
      status.textContent = `${reported} · ${freshness} · map tiles not configured`;
    } else if (tileLoadFailed) {
      status.textContent = `${reported} · ${freshness} · map tiles unavailable`;
    } else {
      status.textContent = `${reported} · ${freshness} · ${config.quality === 'DEMO' ? 'demo map tiles' : 'configured map tiles'}`;
    }
    if (!map || !markers) return;
    markers.clearLayers();
    for (const { twin, point } of located) {
      const selected = twin.vehicle_id === selectedId;
      const marker = L.circleMarker(point, {
        radius: selected ? 11 : 8,
        color: selected ? '#ffe000' : '#48dff4',
        fillColor: selected ? '#ffe000' : '#48dff4',
        fillOpacity: twin.quality === 'STALE' ? 0.35 : 0.8,
        weight: 2,
      }).addTo(markers);
      const tooltip = document.createElement('span');
      tooltip.textContent = `${twin.vehicle_id} · ${twin.quality === 'STALE' ? 'last known position; telemetry stale' : twin.quality} · ${twin.observed_at || 'time unknown'}`;
      marker.bindTooltip(tooltip);
      marker.on('click', () => onSelectVehicle?.(twin.vehicle_id));
    }
    const active = located.find(item => item.twin.vehicle_id === selectedId);
    if (active && selectedId !== lastCenteredId) {
      map.setView(active.point, Math.max(map.getZoom(), 12), { animate: false });
      lastCenteredId = selectedId;
    } else if (!lastCenteredId && located.length) {
      map.fitBounds(located.map(item => item.point), { padding: [28, 28], maxZoom: 12, animate: false });
    }
  }

  window.TrickeeMap = { configure, render, showRoute, showTripTrace, showReplayPosition };
})();
