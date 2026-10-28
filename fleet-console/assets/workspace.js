/* Shared, dependency-free workspace navigation. Never stores operational data. */
(() => {
  'use strict';
  const header = document.querySelector('.topbar');
  if (!header || typeof HTMLDialogElement === 'undefined') return;
  const storageKey = 'trickee-reduced-motion';
  const systemMotion = matchMedia('(prefers-reduced-motion: reduce)');
  let preferred = false;
  try { preferred = localStorage.getItem(storageKey) === 'true'; } catch { /* Optional preference. */ }
  const trigger = document.getElementById('workspaceJump');
  if (!trigger) return;
  trigger.disabled = false;
  trigger.setAttribute('aria-haspopup', 'dialog');
  trigger.setAttribute('aria-controls', 'workspaceDialog');
  trigger.setAttribute('aria-keyshortcuts', 'Control+k Meta+k');
  const dialog = document.createElement('dialog');
  dialog.id = 'workspaceDialog';
  dialog.className = 'workspace-dialog';
  dialog.setAttribute('aria-labelledby', 'workspaceTitle');
  dialog.innerHTML = `<div class="workspace-heading"><h2 id="workspaceTitle">Your workspace</h2><button type="button" id="workspaceClose" aria-label="Close workspace navigation">Close</button></div>
    <label for="workspaceSearch">Find a section</label><input id="workspaceSearch" type="search" autocomplete="off" placeholder="Trips, charging, service…">
    <nav id="workspaceLinks" aria-label="All workspace sections"></nav><p id="workspaceEmpty" role="status" hidden>No matching sections. Try another name.</p>
    <label class="workspace-preference"><input id="workspaceMotion" type="checkbox">Reduce motion</label><p class="workspace-help" id="workspaceMotionHelp">Saved on this device. Your system’s reduced-motion setting always takes priority.</p>`;
  document.body.append(dialog);
  const search = dialog.querySelector('#workspaceSearch');
  const links = dialog.querySelector('#workspaceLinks');
  const empty = dialog.querySelector('#workspaceEmpty');
  const motion = dialog.querySelector('#workspaceMotion');
  motion.setAttribute('aria-describedby', 'workspaceMotionHelp');
  const sections = [...document.querySelectorAll('main .panel')];
  for (const [index, section] of sections.entries()) {
    const heading = section.querySelector('h2');
    if (!heading) continue;
    if (!section.id) section.id = `workspace-section-${index + 1}`;
    const link = document.createElement('a');
    link.href = `#${section.id}`;
    // Driver vehicle/trip headings contain live data; use stable section labels.
    const label = section.querySelector('.eyebrow')?.textContent.replace(/^\d+\s*\/\s*/, '').trim();
    link.textContent = label ? label.toLocaleLowerCase().replace(/(^|\s)\S/g, text => text.toUpperCase()) : heading.textContent;
    link.addEventListener('click', () => {
      dialog.close();
      section.setAttribute('tabindex', '-1');
      section.focus({preventScroll: true});
    });
    links.append(link);
  }
  function filter() {
    const term = search.value.trim().toLocaleLowerCase();
    for (const link of links.children) {
      const section = document.getElementById(link.hash.slice(1));
      link.hidden = !section?.getClientRects().length || !link.textContent.toLocaleLowerCase().includes(term);
    }
    empty.hidden = [...links.children].some(link => !link.hidden);
  }
  function open() {
    if (dialog.open) return;
    search.value = '';
    filter();
    dialog.showModal();
    search.focus();
  }
  function syncMotion() {
    const reduced = preferred || systemMotion.matches;
    document.documentElement.dataset.motion = reduced ? 'reduced' : 'full';
    motion.checked = reduced;
    motion.disabled = systemMotion.matches;
  }
  trigger.addEventListener('click', open);
  dialog.querySelector('#workspaceClose').addEventListener('click', () => dialog.close());
  dialog.addEventListener('click', event => { if (event.target === dialog) {
    const rect = dialog.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();
  }});
  dialog.addEventListener('close', () => { if (document.activeElement === document.body || dialog.contains(document.activeElement)) trigger.focus({preventScroll: true}); });
  search.addEventListener('input', filter);
  search.addEventListener('keydown', event => {
    if (event.key === 'ArrowDown') { event.preventDefault(); [...links.children].find(link => !link.hidden)?.focus(); }
    if (event.key === 'Enter') { event.preventDefault(); [...links.children].find(link => !link.hidden)?.click(); }
  });
  document.addEventListener('keydown', event => {
    if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      if (dialog.open) dialog.close(); else open();
    }
  });
  motion.addEventListener('change', () => {
    preferred = motion.checked;
    try { localStorage.setItem(storageKey, String(preferred)); } catch { /* In-memory remains useful. */ }
    syncMotion();
  });
  systemMotion.addEventListener('change', syncMotion);
  syncMotion();
})();
