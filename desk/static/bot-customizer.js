import { createDialKit, createDialRoot } from '/static/vendor/dialkit-2.1.0/index.js';

const appearance = window.deskBotAppearance;
const controls = document.querySelector('#bot-dials');
let dial, root;
const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
function updatePreviewAvailability() {
  const renderer = window.deskBot?.renderer;
  const ready = ['webgpu', 'webgl', 'flat'].includes(renderer);
  const simplified = renderer === 'flat' || renderer === 'still';
  document.querySelectorAll('[data-bot-view]').forEach(button => {
    button.disabled = !ready || (simplified && button.dataset.botView !== 'front');
  });
  document.querySelectorAll('[data-bot-expression]').forEach(button => {
    button.disabled = !ready;
    if (button.dataset.botExpression === 'neutral') button.hidden = !reducedMotion.matches;
  });
  const note = document.querySelector('#bot-preview-note');
  note.hidden = !simplified && !reducedMotion.matches;
  note.textContent = renderer === 'still' ? 'This browser shows a still illustration. Settings are saved for the 3D bot.'
    : [simplified ? 'This browser shows a simplified front view.' : '',
      reducedMotion.matches ? 'Reduced motion is on. Expressions show a still pose; Reset pose returns to normal.' : ''].filter(Boolean).join(' ');
}
addEventListener('deskbotready', updatePreviewAvailability);
reducedMotion.addEventListener('change', updatePreviewAvailability);
updatePreviewAvailability();
function setPreviewView(view, animate = false) {
  const host = document.querySelector('#bot-preview');
  if (window.deskBot?.setView) window.deskBot.setView(host, view, { animate });
  else host.setAttribute('view', view);
  document.querySelectorAll('[data-bot-view]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.botView === view)));
}
function framePath(frame, side, inset = 0) {
  const points = frame.points.map(([x, y]) => [side * (.43 + x * (frame.rx - inset)), .2 + y * (frame.ry - inset)]);
  const mid = (a, b) => [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
  return `M${mid(points[points.length - 1], points[0]).join(',')}` + points.map((point, i) => `Q${point.join(',')} ${mid(point, points[(i + 1) % points.length]).join(',')}`).join('') + 'Z';
}
function frameIcon(frame) {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '-1 -.68 2 1.02'); svg.setAttribute('aria-hidden', 'true');
  const group = document.createElementNS(ns, 'g'); group.setAttribute('transform', 'scale(1,-1)');
  const rims = document.createElementNS(ns, 'path');
  rims.setAttribute('d', [-1, 1].map(side => framePath(frame, side) + framePath(frame, side, frame.rim)).join(''));
  rims.setAttribute('fill-rule', 'evenodd'); rims.setAttribute('fill', frame.color);
  rims.setAttribute('fill-opacity', frame.finish === 'crystal' ? '.65' : '1');
  rims.setAttribute('stroke', frame.color); rims.setAttribute('stroke-width', '.006');
  const bridge = document.createElementNS(ns, 'path');
  const gap = .43 - frame.rx + frame.rim * .6;
  bridge.setAttribute('d', `M${-gap},.35 C${-gap * .5},.35 ${-gap * .6},${frame.bridge} 0,${frame.bridge} C${gap * .6},${frame.bridge} ${gap * .5},.35 ${gap},.35`
    + (frame.brow ? `M-.76,${.2 + frame.ry * .87} H.76` : ''));
  bridge.setAttribute('fill', 'none'); bridge.setAttribute('stroke', frame.color); bridge.setAttribute('stroke-width', String(Math.max(.032, frame.rim))); bridge.setAttribute('stroke-linecap', 'round');
  group.append(rims, bridge); svg.append(group); return svg;
}
for (const frame of [{ id: 'none', name: 'No glasses' }, ...appearance.frameCatalog]) {
  const button = document.createElement('button');
  button.type = 'button'; button.className = 'frame-choice'; button.dataset.frameStyle = frame.id;
  button.setAttribute('aria-label', frame.description ? `${frame.name}, ${frame.description}` : frame.name);
  if (frame.id !== 'none') button.append(frameIcon(frame));
  else { const empty = document.createElement('span'); empty.className = 'frame-none'; empty.textContent = '—'; button.append(empty); }
  const label = document.createElement('span'); label.textContent = frame.name; button.append(label);
  button.addEventListener('click', () => {
    if (!dial) initialize();
    dial.setValues({ glasses: frame.id === 'none' ? { style: 'none' } : {
      style: frame.id, finish: frame.finish, frameColor: frame.color,
      transparency: frame.finish === 'solid' ? 0 : frame.finish === 'crystal' ? 1.35 : 1,
      shine: frame.finish === 'solid' ? .5 : 1,
    } });
    setPreviewView('front');
  });
  document.querySelector('#bot-frame-choices').append(button);
}
const presetCards = document.querySelector('#bot-presets');
const savedCards = document.querySelector('#bot-saved-presets');
const saveForm = document.querySelector('#bot-save-form');
const saveError = document.querySelector('#bot-save-error');
let notice = '';
function choosePreset(preset) {
  if (!dial) initialize();
  dial.setValues(preset.values);
  window.deskBot?.express?.(document.querySelector('#bot-preview'), 'neutral');
  setPreviewView('front');
}
function presetCard(preset, saved = false) {
  const button = document.createElement('button');
  button.type = 'button'; button.className = 'bot-preset'; button.dataset.botPreset = preset.id;
  button.setAttribute('aria-label', saved ? preset.name : `${preset.name}, ${preset.description.replace(' · ', ', ')}`);
  const image = document.createElement('img');
  image.src = saved ? preset.thumbnail : `/static/bot-presets/${preset.id}.png`;
  image.alt = ''; image.width = image.height = 192;
  image.loading = 'lazy'; image.decoding = 'async';
  const name = document.createElement('strong'); name.textContent = preset.name;
  const description = document.createElement('span'); description.textContent = saved ? 'Saved preset' : preset.description;
  button.append(image, name, description);
  button.addEventListener('click', () => choosePreset(preset));
  if (!saved) return button;
  const card = document.createElement('div'); card.className = 'saved-preset';
  const remove = document.createElement('button');
  remove.type = 'button'; remove.className = 'saved-preset-remove'; remove.textContent = 'Remove';
  remove.setAttribute('aria-label', `Remove ${preset.name} preset`);
  remove.addEventListener('click', () => {
    try {
      appearance.removePreset(preset.id);
      notice = `Removed “${preset.name}” from your presets.`;
      updatePresetSelection();
      document.querySelector('#bot-save-preset').focus({ preventScroll: true });
    } catch (error) { showSaveError(error.message); }
  });
  card.append(button, remove);
  return card;
}
for (const preset of appearance.presets) presetCards.append(presetCard(preset));
function renderSavedPresets() {
  savedCards.replaceChildren(...appearance.savedPresets.map(preset => presetCard(preset, true)));
  document.querySelector('#bot-saved-section').hidden = !appearance.savedPresets.length;
  updatePresetSelection();
}
function updatePresetSelection() {
  const selected = appearance.presetFor(appearance.previewValues);
  document.querySelectorAll('[data-bot-preset]').forEach(button => {
    button.setAttribute('aria-pressed', String(button.dataset.botPreset === selected));
  });
  const preset = [...appearance.savedPresets, ...appearance.presets].find(preset => preset.id === selected);
  document.querySelector('#bot-preset-status').textContent = preset
    ? `${preset.name} ${appearance.pending ? 'preview' : 'on your desk'}` : appearance.pending ? 'Custom preview' : 'Custom bot on your desk';
  document.querySelector('#bot-apply').disabled = !dial || (!appearance.pending && appearance.persisted);
  document.querySelector('#bot-save-preset').disabled = !dial;
  document.querySelector('#bot-discard').hidden = !appearance.pending;
  document.querySelector('#bot-save-status').textContent = notice || (appearance.pending
    ? 'Preview only. Apply this look to use it on your desk.'
    : appearance.persisted ? 'On your desk · Saved on this browser.' : 'Applied for this session. This browser could not save the change.');
  const style = appearance.previewValues.glasses.style;
  document.querySelectorAll('[data-frame-style]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.frameStyle === style)));
  document.querySelector('#bot-frame-status').textContent = appearance.schema.glasses.style.options.find(option => option.value === style)?.label || style;
}
function showSaveError(message) {
  saveError.textContent = message; saveError.hidden = false;
}
addEventListener('deskbotpreviewchange', () => { notice = ''; updatePresetSelection(); });
addEventListener('deskbotappearancechange', updatePresetSelection);
addEventListener('deskbotpresetschange', renderSavedPresets);
renderSavedPresets();
document.querySelector('#bot-apply').addEventListener('click', () => {
  saveError.hidden = true;
  const saved = appearance.apply();
  notice = saved ? 'Your bot is now on your desk. Saved on this browser.' : 'Applied for this session. This browser could not save the change.';
  updatePresetSelection();
});
document.querySelector('#bot-discard').addEventListener('click', () => {
  dial.setValues(appearance.values);
  window.deskBot?.express?.(document.querySelector('#bot-preview'), 'neutral');
  notice = 'Preview discarded. Your desk bot is unchanged.';
  updatePresetSelection();
});
document.querySelector('#bot-save-preset').addEventListener('click', () => {
  saveForm.hidden = false; saveError.hidden = true;
  const preset = [...appearance.savedPresets, ...appearance.presets].find(preset => preset.id === appearance.presetFor(appearance.previewValues));
  document.querySelector('#bot-preset-name').value = preset ? `${preset.name} copy`.slice(0, 40) : 'My bot';
  document.querySelector('#bot-preset-name').focus();
  document.querySelector('#bot-preset-name').select();
});
document.querySelector('#bot-save-cancel').addEventListener('click', () => {
  saveForm.hidden = true; saveError.hidden = true;
  document.querySelector('#bot-save-preset').focus();
});
saveForm.addEventListener('submit', async event => {
  event.preventDefault(); saveError.hidden = true;
  const button = saveForm.querySelector('[type=submit]');
  if (button.disabled) return;
  button.disabled = true;
  try {
    window.deskBot?.express?.(document.querySelector('#bot-preview'), 'neutral');
    setPreviewView('front');
    const source = document.querySelector('#bot-preview canvas, #bot-preview img');
    if (!source) throw new Error('The preview is still loading. Try saving again in a moment.');
    if (source instanceof HTMLImageElement) await source.decode();
    const canvas = document.createElement('canvas'); canvas.width = canvas.height = 192;
    canvas.getContext('2d').drawImage(source, 0, 0, 192, 192);
    const preset = appearance.savePreset(document.querySelector('#bot-preset-name').value, canvas.toDataURL('image/png'));
    saveForm.hidden = true;
    notice = `Saved “${preset.name}” as a preset.${appearance.pending ? ' Your preview has not been applied.' : ''}`;
    updatePresetSelection();
    document.querySelector('#bot-save-preset').focus({ preventScroll: true });
  } catch (error) { showSaveError(error.message); }
  finally { button.disabled = false; }
});
function initialize() {
  if (dial) return;
  const current = appearance.previewValues;
  const config = Object.fromEntries(Object.entries(appearance.schema).map(([group, fields]) => [group,
    Object.fromEntries(Object.entries(fields).map(([name, spec]) => [name,
      Array.isArray(spec) ? [current[group][name], ...spec.slice(1)]
        : typeof spec === 'object' ? { ...spec, default: current[group][name] } : current[group][name]]))]));
  dial = createDialKit('Bot customizer', config, {
    id: 'support-desk-bot', persist: false,
  });
  root = createDialRoot({ target: controls, mode: 'inline', versions: 'menu', theme: document.documentElement.dataset.theme });
  dial.subscribe(values => {
    appearance.preview(values);
  });
  controls.removeAttribute('aria-busy');
  controls.querySelector('.customizer-loading')?.remove();
  document.querySelector('#bot-reset').disabled = false;
  document.querySelector('#bot-reset').addEventListener('click', () => dial.setValues(appearance.defaults));
  updatePresetSelection();
}
addEventListener('desksettingsopen', initialize);
addEventListener('deskthemechange', () => {
  if (root) root.element.dataset.theme = document.documentElement.dataset.theme;
});
// Settings may be the restored view before this module finishes loading.
if (!document.querySelector('#settings').hidden) initialize();

document.querySelector('#bot-view-controls').addEventListener('click', event => {
  const button = event.target.closest('button[data-bot-view]');
  if (!button) return;
  setPreviewView(button.dataset.botView, true);
});
document.querySelector('#bot-expression-controls').addEventListener('click', event => {
  const button = event.target.closest('button[data-bot-expression]');
  if (button) window.deskBot?.express?.(document.querySelector('#bot-preview'), button.dataset.botExpression);
});
