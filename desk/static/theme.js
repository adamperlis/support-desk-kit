// The desk goes dark by the clock, not by the Mac's appearance setting: dark from sunset to sunrise
// where you are. It sets <html data-theme="light|dark">, which app.css and bot.js read, and looks
// again every minute so the desk turns over by itself while the tab sits open. It runs in <head>,
// before anything is drawn, so the page never flashes the wrong theme on load.
(() => {
  // Where you are: desk.latitude and desk.longitude in config.json, which the server writes into
  // <meta name="desk-location">. Without them the browser's time zone stands in: its standard UTC offset
  // gives a longitude (15° an hour), and a mid latitude, south if summer time falls in January, gives a
  // day length close enough for a theme. Latitude sets how long the day is; longitude moves sunrise and
  // sunset by 4 minutes a degree.
  const [LAT, LON] = (() => {
    const set = (document.querySelector('meta[name="desk-location"]')?.content || '').split(',').map(Number);
    if (set.length === 2 && set.every(Number.isFinite)) return set;
    const year = new Date().getFullYear();
    const jan = new Date(year, 0, 1).getTimezoneOffset(), jul = new Date(year, 6, 1).getTimezoneOffset();
    return [jan < jul ? -35 : 40, -Math.max(jan, jul) / 4];
  })();

  // Sunrise and sunset on the local date of `date`, from NOAA's low-precision solar equations
  // (good to a minute or two away from the poles).
  function sunTimes(date) {
    const rad = Math.PI / 180;
    const midnight = Date.UTC(date.getFullYear(), date.getMonth(), date.getDate());
    const dayOfYear = (midnight - Date.UTC(date.getFullYear(), 0, 0)) / 864e5;
    const g = 2 * Math.PI / 365 * (dayOfYear - 1);  // the fractional year, in radians
    const eqTime = 229.18 * (0.000075 + 0.001868 * Math.cos(g) - 0.032077 * Math.sin(g)
      - 0.014615 * Math.cos(2 * g) - 0.040849 * Math.sin(2 * g));  // minutes
    const decl = 0.006918 - 0.399912 * Math.cos(g) + 0.070257 * Math.sin(g) - 0.006758 * Math.cos(2 * g)
      + 0.000907 * Math.sin(2 * g) - 0.002697 * Math.cos(3 * g) + 0.00148 * Math.sin(3 * g);
    // 90.833°: the sun's centre just under the horizon, allowing for its radius and for refraction.
    const cosH = (Math.cos(90.833 * rad) - Math.sin(LAT * rad) * Math.sin(decl))
      / (Math.cos(LAT * rad) * Math.cos(decl));
    const halfDay = Math.acos(Math.min(1, Math.max(-1, cosH))) / rad * 4;  // minutes from noon to sunset
    const noon = 720 - 4 * LON - eqTime;  // solar noon, in minutes after UTC midnight
    const at = (minutes) => new Date(midnight + minutes * 6e4);
    return { rise: at(noon - halfDay), set: at(noon + halfDay) };
  }

  // The rule for when the desk is dark: from sunset to sunrise (chosen 6 Nov 2026). Earlier dusk,
  // fixed hours, or following the Mac by day would each be a change to these lines alone.
  function isNight(now) {
    const { rise, set } = sunTimes(now);
    return now < rise || now >= set;
  }

  const root = document.documentElement;
  let theme = null;
  function apply() {
    const next = isNight(new Date()) ? 'dark' : 'light';
    if (next === theme) return;
    // At sunset the open page eases over rather than snapping; the first paint just starts right.
    if (theme && !matchMedia('(prefers-reduced-motion: reduce)').matches) {
      root.classList.add('theme-turning');
      setTimeout(() => root.classList.remove('theme-turning'), 900);
    }
    theme = next;
    root.dataset.theme = next;
    dispatchEvent(new CustomEvent('deskthemechange', { detail: next }));
  }
  apply();
  setInterval(apply, 60e3);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) apply(); });
  window.deskTheme = { sunTimes, isNight, apply, get theme() { return theme; } };
})();
