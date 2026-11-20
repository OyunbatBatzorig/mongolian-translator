// Орчуулах хуудасны JavaScript.
// Google Translate шиг: бичих зуур автоматаар орчуулна, сумаар хэл солино, таб солино.
const $ = (id) => document.getElementById(id);
const DIRS = JSON.parse(document.querySelector('.langbar').dataset.directions); // {"en-mn": true, "mn-en": false}
const NAMES = { en: 'Англи', mn: 'Монгол' };
const API = Object.assign({ translate: '/api/translate', file: '/api/translate-file' }, APP.api || {});  // /simple хуудас өөр хаяг ашиглана
let direction = 'en-mn';

function needLogin() {
  if (APP.canTranslate) return false;
  $('status').textContent = '';
  $('out').textContent = APP.loggedIn ? APP.accessMsg : 'Орчуулахын тулд эхлээд нэвтэрнэ үү.';
  $('out').classList.add('placeholder');
  return true;
}

// ---- 1. Табууд
document.querySelectorAll('.tab').forEach((t) => t.addEventListener('click', () => {
  document.querySelectorAll('.tab').forEach((x) => x.classList.toggle('active', x === t));
  $('tab-text').classList.toggle('hidden', t.dataset.tab !== 'text');
  $('tab-doc').classList.toggle('hidden', t.dataset.tab !== 'doc');
}));

// ---- 2. Хэл солих сум
function showDirection() {
  const [s, t] = direction.split('-');
  $('srcLang').textContent = NAMES[s];
  $('tgtLang').textContent = NAMES[t];
  $('src').placeholder = s === 'mn' ? 'Текстээ энд бичнэ үү' : 'Type your text here';
}
$('swap').addEventListener('click', () => {
  const next = direction === 'en-mn' ? 'mn-en' : 'en-mn';
  if (!DIRS[next]) {   // загвар нь хараахан бэлэн биш
    $('dirmsg').textContent = 'Монгол → Англи чиглэлийн загвар удахгүй нэмэгдэнэ.';
    $('swap').classList.add('shake');
    setTimeout(() => $('swap').classList.remove('shake'), 400);
    return;
  }
  $('dirmsg').textContent = '';
  // Google шиг: орчуулгыг эх текст болгож байрыг нь солино
  const out = $('out').classList.contains('placeholder') ? '' : fullTranslation;
  if (out) $('src').value = out;
  direction = next;
  showDirection();
  updateCount();
  scheduleTranslate(0);
});
showDirection();

// ---- Текст: бичиж дуусаад 0.8 секунд болоход автоматаар орчуулна
let timer = null, lastSent = '';
function updateCount() { $('count').textContent = $('src').value.length; $('clear').hidden = !$('src').value; }
function scheduleTranslate(delay = 800) {
  clearTimeout(timer);
  timer = setTimeout(translateNow, delay);
}
// ---- Өгүүлбэр тодруулах: эх текстийн ард (backdrop) яг ижил текстийг өгүүлбэр бүрээр нь <span> болгож тавина.
let segs = [], segSource = '', fullTranslation = '';
const esc = (t) => t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
function renderBackdrop() {
  const text = $('src').value;
  if (text !== segSource || !segs.length) { $('backdrop').innerHTML = esc(text) + '\n'; return; }
  let html = '', pos = 0;
  segs.forEach((g, i) => {
    html += esc(text.slice(pos, g.start)) + `<span data-i="${i}">${esc(text.slice(g.start, g.end))}</span>`;
    pos = g.end;
  });
  $('backdrop').innerHTML = html + esc(text.slice(pos)) + '\n';
  syncScroll();
}
function syncScroll() { $('backdrop').scrollTop = $('src').scrollTop; }
function renderOutput() {
  const paras = [];
  segs.forEach((g, i) => { (paras[g.para] = paras[g.para] || []).push(`<span class="seg" data-i="${i}">${esc(g.translation)}</span>`); });
  $('out').innerHTML = paras.filter(Boolean).map((p) => `<p>${p.join(' ')}</p>`).join('');
}
function highlight(i) {
  document.querySelectorAll('.on').forEach((el) => el.classList.remove('on'));
  if (i == null || $('src').value !== segSource) return;
  document.querySelectorAll(`[data-i="${i}"]`).forEach((el) => el.classList.add('on'));
}
$('out').addEventListener('mouseover', (e) => { const s = e.target.closest('span.seg'); highlight(s ? s.dataset.i : null); });
$('out').addEventListener('mouseleave', () => highlight(null));
// Эх талын хулганы байрлалаас аль өгүүлбэр дээр байгааг олох (textarea-ийн ард байгаа span-уудыг шалгана)
$('src').addEventListener('mousemove', (e) => {
  // backdrop нь pointer-events:none тул span бүрийн мөрийн тэгш өнцөгтүүдийг шууд шалгана
  const hit = [...$('backdrop').querySelectorAll('span[data-i]')].find((sp) =>
    [...sp.getClientRects()].some((r) => e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom));
  highlight(hit ? hit.dataset.i : null);
});
$('src').addEventListener('mouseleave', () => highlight(null));
$('src').addEventListener('scroll', syncScroll);

async function translateNow() {
  const raw = $('src').value;
  const text = raw.trim();
  if (!text) { setBusy(false); $('out').textContent = 'Орчуулга'; $('out').classList.add('placeholder'); lastSent = ''; segs = []; renderBackdrop(); return; }
  if (needLogin()) return;
  const key = direction + '|' + text;
  if (key === lastSent) return;
  lastSent = key;
  setBusy(true);
  try {
    const r = await fetch(API.translate, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: raw, direction }),
    });
    let data;
    try { data = await r.json(); } catch (e) { data = { error: r.status === 503 ? 'Сервер завгүй байна. Түр хүлээгээд дахин оролдоно уу.' : 'Серверт алдаа гарлаа. Дахин оролдоно уу.' }; }
    if (key !== lastSent) return;          // энэ хооронд хэрэглэгч дахин бичсэн
    if (!r.ok) throw new Error(data.error || 'Алдаа гарлаа');
    segs = data.segments || []; segSource = raw; fullTranslation = data.translation;
    renderOutput(); renderBackdrop();
    $('out').classList.remove('placeholder');
    setBusy(false);
  } catch (e) { setBusy(false); $('status').textContent = e.message; lastSent = ''; }
}
// Орчуулж байгааг тод харуулах: хөдөлгөөнтэй цэгүүд + орчуулгын талбарт гялбах (skeleton) мөрүүд
function setBusy(on) {
  const out = $('out');
  out.closest('.pane').classList.toggle('busy', on);
  $('status').innerHTML = on ? 'Орчуулж байна<span class="dots"><i>.</i><i>.</i><i>.</i></span>' : '';
  if (on && out.classList.contains('placeholder')) {
    out.innerHTML = '<span class="skel"></span><span class="skel"></span><span class="skel short"></span>';
  }
}
$('src').addEventListener('input', () => { updateCount(); renderBackdrop(); scheduleTranslate(); });
$('src').addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) scheduleTranslate(0); });
$('clear').addEventListener('click', () => { $('src').value = ''; updateCount(); scheduleTranslate(0); $('src').focus(); });
$('copysrc').addEventListener('click', () => {
  if (!$('src').value) return;
  navigator.clipboard.writeText($('src').value);
  $('status').textContent = 'Эх текстийг хуулсан';
  setTimeout(() => { $('status').textContent = ''; }, 1200);
});
$('copy').addEventListener('click', () => {
  if ($('out').classList.contains('placeholder')) return;
  navigator.clipboard.writeText(fullTranslation || $('out').textContent);
  $('status').textContent = 'Хуулсан';
  setTimeout(() => { $('status').textContent = ''; }, 1200);
});

// ---- Баримт бичиг: олон файлыг чирж оруулах эсвэл сонгох. Файл бүр тусдаа ажил (job) болж орчуулагдана.
const ALLOWED = ['.pdf', '.docx', '.doc', '.odt', '.rtf', '.txt'];
let queue = [];   // {file, id, state: 'ready'|'sending'|'running'|'done'|'failed', msg, pct, job}
const escH = (t) => String(t).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
function renderFiles() {
  $('picked').classList.toggle('hidden', queue.length === 0);
  $('filelist').innerHTML = queue.map((q, i) => `
    <li class="fileitem st-${q.state}">
      <span class="fname">${escH(q.file.name)}</span>
      <span class="fstate">${q.state === 'done' ? `<a class="btn small" href="/download/${q.job}">Татах</a>`
        : q.state === 'running' ? `${q.pct}%` : q.state === 'sending' ? 'Илгээж байна...'
        : q.state === 'failed' ? `<span class="bad">${escH(q.msg)}</span>` : ''}</span>
      ${q.state !== 'done' ? `<button class="icon" data-rm="${i}" title="${q.state === 'running' || q.state === 'sending' ? 'Цуцлах' : 'Хасах'}" aria-label="Хасах">✕</button>` : ''}
      ${q.state === 'ask' ? `<div class="splitask">${q.info.pages} хуудастай байна (нэг удаад ${q.info.per_part} хүртэл).
        ${q.info.parts} хэсэгт (тус бүр ${q.info.per_part} хүртэл хуудас) хувааж орчуулах уу?
        <span class="splitbtns"><button class="btn small" data-split="${i}">Тийм, хувааж орчуулах</button>
        <button class="btn small secondary" data-nosplit="${i}">Үгүй</button></span></div>` : ''}
      <div class="fbar"><div style="width:${q.state === 'done' ? 100 : q.pct || 0}%"></div></div>
    </li>`).join('');
  const ready = queue.filter((q) => q.state === 'ready').length;
  $('sendfile').disabled = ready === 0;
  $('sendfile').textContent = ready > 1 ? `Бүгдийг орчуулах (${ready})` : 'Орчуулах';
  // 2 ба түүнээс олон файл бэлэн болбол нэг ZIP-ээр бүгдийг нь татах товч
  const done = queue.filter((q) => q.state === 'done');
  $('zipall').classList.toggle('hidden', done.length < 2);
  $('zipall').textContent = `Бүгдийг татах (${done.length}, ZIP)`;
  $('zipall').href = '/download-zip?ids=' + done.map((q) => encodeURIComponent(q.job)).join(',');
}
function addFiles(list) {
  for (const f of list) {
    const ext = f.name.slice(f.name.lastIndexOf('.')).toLowerCase();
    if (queue.some((q) => q.file.name === f.name && q.file.size === f.size && q.state === 'ready')) continue;
    queue.push(ALLOWED.includes(ext) ? { file: f, state: 'ready', pct: 0 }
      : { file: f, state: 'failed', msg: 'Зөвхөн PDF, DOCX, DOC, ODT, RTF, TXT', pct: 0 });
  }
  $('job').textContent = '';
  renderFiles();
}
$('file').addEventListener('change', () => { addFiles($('file').files); $('file').value = ''; });
['dragenter', 'dragover'].forEach((ev) => $('drop').addEventListener(ev, (e) => { e.preventDefault(); $('drop').classList.add('over'); }));
['dragleave', 'drop'].forEach((ev) => $('drop').addEventListener(ev, (e) => { e.preventDefault(); $('drop').classList.remove('over'); }));
$('drop').addEventListener('drop', (e) => addFiles(e.dataTransfer.files));
$('filelist').addEventListener('click', (e) => {
  const sp = e.target.closest('[data-split]');
  if (sp) { splitSend(queue[+sp.dataset.split]); return; }
  const ns = e.target.closest('[data-nosplit]');
  if (ns) { const q = queue[+ns.dataset.nosplit]; q.state = 'failed'; q.msg = `${q.info.per_part} хуудаснаас урт`; renderFiles(); return; }
  const b = e.target.closest('[data-rm]'); if (!b) return;
  const q = queue[+b.dataset.rm];
  q.cancelled = true;   // явцыг шалгахаа болино; орчуулж байгаа бол серверт цуцлах хүсэлт илгээнэ
  if (q.job && q.state === 'running') fetch('/api/jobs/' + encodeURIComponent(q.job) + '/cancel', { method: 'POST' });
  queue.splice(queue.indexOf(q), 1); renderFiles();
});
$('clearfiles').addEventListener('click', () => { queue = queue.filter((q) => q.state === 'sending' || q.state === 'running'); renderFiles(); });

async function postFile(q, split) {
  const fd = new FormData(); fd.append('file', q.file); fd.append('direction', q.dir || direction);
  if (split) fd.append('split', '1');
  const r = await fetch(API.file, { method: 'POST', body: fd });
  let data = {};
  try { data = await r.json(); } catch (e) { /* хоосон хариу */ }
  return [r, data];
}
async function sendOne(q) {
  q.state = 'sending'; q.dir = direction; renderFiles();
  let data;
  try {
    const [r, d] = await postFile(q, false); data = d;
    // Файл хэт урт: хэрэглэгчээс хувааж орчуулах эсэхийг асууна (файл серверт үлдээгүй, зөвшөөрвөл дахин илгээнэ)
    if (r.status === 409 && data.needs_split) { q.state = 'ask'; q.info = data; renderFiles(); return false; }
    if (!r.ok) throw new Error(data.error || 'Алдаа гарлаа');
  } catch (e) { q.state = 'failed'; q.msg = e.message; renderFiles(); return false; }
  q.job = data.job_id;
  if (q.cancelled) { fetch('/api/jobs/' + encodeURIComponent(q.job) + '/cancel', { method: 'POST' }); return false; }
  q.state = 'running'; renderFiles();
  return pollJob(q);
}
async function splitSend(q) {
  q.state = 'sending'; renderFiles();
  let data;
  try {
    const [r, d] = await postFile(q, true); data = d;
    if (!r.ok) throw new Error(data.error || 'Алдаа гарлаа');
  } catch (e) { q.state = 'failed'; q.msg = e.message; renderFiles(); return; }
  // Нэг файлын оронд хэсэг бүрийг тусдаа мөрөөр харуулна
  const parts = data.jobs.map((j) => ({ file: { name: j.name, size: 0 }, state: 'running', pct: 0, job: j.job_id }));
  queue.splice(queue.indexOf(q), 1, ...parts); renderFiles();
  await Promise.all(parts.map(pollJob));
  const done = queue.filter((x) => x.state === 'done').length;
  if (done > 1) $('job').textContent = `${done} файл бэлэн боллоо. "Бүгдийг татах (ZIP)" дарж нэг дор татна.`;
}
async function pollJob(q) {
  while (true) {   // 2 секунд тутам явцыг шалгана
    await new Promise((r) => setTimeout(r, 2000));
    if (q.cancelled) return false;
    const j = await (await fetch('/api/jobs/' + q.job)).json();
    q.pct = Math.round((j.progress || 0) * 100);
    if (j.status === 'done') { q.state = 'done'; renderFiles(); return true; }
    if (j.status === 'failed') { q.state = 'failed'; q.msg = j.error || 'Алдаа'; renderFiles(); return true; }
    renderFiles();
  }
}
$('sendfile').addEventListener('click', async () => {
  if (!APP.canFiles) { $('job').textContent = APP.loggedIn ? APP.fileMsg : 'Файл орчуулахын тулд эхлээд нэвтэрнэ үү.'; return; }
  $('sendfile').disabled = true;
  // Сервер файлыг дарааллаар нь орчуулдаг тул нэг нэгээр нь илгээнэ; эрх дуусвал үлдсэнийг нь зогсооно.
  for (const q of queue.filter((x) => x.state === 'ready')) {
    await sendOne(q);
    if (q.state === 'failed' && /эрх|дууссан/.test(q.msg)) {
      queue.filter((x) => x.state === 'ready').forEach((x) => { x.state = 'failed'; x.msg = 'Энэ сарын файлын эрх дууссан'; });
      renderFiles(); break;
    }
  }
  const done = queue.filter((q) => q.state === 'done').length;
  if (done > 1) $('job').textContent = `${done} файл бэлэн боллоо.`;
});
