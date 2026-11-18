// Loads the desk's mascot. The 3D bot (bot3d.js: Three.js on WebGPU, or WebGL 2) is a module and takes
// a moment, so until it's ready this keeps whatever mood the desk sets. If the 3D bot can't load or
// can't run, the older flat-shaded bot (bot2d.js, WebGL 1) takes over, and failing that favicon.svg.
(() => {
  let pending = 'idle';
  window.deskBot = { mood(m) { pending = m; }, get pending() { return pending; } };
  const flat = () => {
    if (customElements.get('desk-bot')) return;
    const s = Object.assign(document.createElement('script'), { src: '/static/bot2d.js' });
    s.onload = () => window.deskBot.mood(pending);
    document.head.append(s);
  };
  import('/static/bot3d.js').catch((e) => { console.warn('Desk bot: the 3D version could not start, using the flat one.', e); flat(); });
})();
