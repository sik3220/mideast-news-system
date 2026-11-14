// 기사 선택 저장, 초안 복사, 수집 중 표시
document.addEventListener('change', async (e) => {
  const box = e.target.closest('input[data-select]');
  if (!box) return;
  const res = await fetch(`/api/article/${box.dataset.select}/select`, {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({selected: box.checked})});
  if (!res.ok) { box.checked = !box.checked; alert('선택을 저장하지 못했습니다.'); return; }
  const data = await res.json();
  box.closest('.card, .item')?.classList.toggle('sel', box.checked);
  document.querySelectorAll('[data-selected-count]').forEach(el => el.textContent = data.selected_count);
  if (box.checked && box.hasAttribute('data-research')) fillDetails(box.dataset.select, box.closest('.item'), box);
});

// 체크한 기사의 본문을 Claude 가 웹에서 찾아 읽고 세부 내용을 채운다(기사당 한 번).
const RESEARCH_MSG = {limit: null, failed: '본문 찾기 실패', unconfigured: '요약 설정 필요', not_found: '웹에서 본문을 찾지 못함 — 원문을 열어 본문을 붙여 넣으세요'};
async function research(id, note) {
  note.textContent = '본문을 찾아 읽는 중…'; note.className = 'rs busy';
  let d;
  try { d = await (await fetch(`/api/article/${id}/research`, {method: 'POST'})).json(); }
  catch { d = {status: 'failed'}; }
  note.className = 'rs';
  note.textContent = d.status === 'ok' ? d.basis : d.status === 'limit' ? d.message : (RESEARCH_MSG[d.status] || '');
  return d;
}
function noteIn(parent) {
  let note = parent.querySelector('.rs');
  if (!note) { note = document.createElement('span'); note.className = 'rs'; parent.appendChild(note); }
  return note;
}
async function fillDetails(id, item, box) {
  if (!item) return;
  box.removeAttribute('data-research');                       // 같은 기사를 두 번 보내지 않는다
  const d = await research(id, noteIn(item.querySelector('.foot')));
  if (d.status !== 'ok') return;
  const h3 = item.querySelector('h3');
  item.querySelector('[data-title]').textContent = `${item.dataset.md}, ${d.title}`;
  item.querySelectorAll('.body > .sub, .body > .flag').forEach(el => el.remove());
  const lines = [...d.subs.map(s => ['sub', s.startsWith('☞') ? s : '* ' + s]), ...d.flags.map(f => ['flag', f])];
  let after = h3;
  for (const [cls, text] of lines) {
    const p = document.createElement('p'); p.className = cls;
    if (cls === 'flag') { const b = document.createElement('b'); b.textContent = '확인'; p.append(b, text); } else p.textContent = text;
    after.after(p); after = p;
  }
}
// 주간 재료: '추천만' 기사를 체크했을 때도 같은 방식으로 세부 내용을 채운다.
document.addEventListener('change', async (e) => {
  const box = e.target.closest('input[name=mat][data-research]');
  if (!box || !box.checked) return;
  const label = box.closest('.mat'), id = box.dataset.research;
  box.removeAttribute('data-research');
  const d = await research(id, noteIn(label.querySelector('span')));
  if (d.status !== 'ok') return;
  label.querySelector('[data-head]').textContent = `${label.dataset.md}, ${d.title} (${label.dataset.src})`;
  label.querySelectorAll('.msub').forEach(el => el.remove());
  for (const s of d.subs) {
    const el = document.createElement('span'); el.className = 'msub'; el.textContent = s.startsWith('☞') ? s : '* ' + s;
    label.querySelector('[data-head]').parentElement.appendChild(el);
  }
});
document.addEventListener('submit', (e) => {
  const form = e.target.closest('form[data-busy]');
  if (form) { form.classList.add('busy'); form.querySelectorAll('button').forEach(b => setTimeout(() => b.disabled = true, 0)); }
});
document.addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-copy]');
  if (!btn) return;
  const el = document.querySelector(btn.dataset.copy);
  try { await navigator.clipboard.writeText(el.value); }
  catch { el.select(); document.execCommand('copy'); }
  const old = btn.textContent; btn.textContent = '복사했습니다'; setTimeout(() => btn.textContent = old, 1500);
});
