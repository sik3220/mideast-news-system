// 기사 선택 저장, 초안 복사, 수집 중 표시
document.addEventListener('change', async (e) => {
  const box = e.target.closest('input[data-select]');
  if (!box) return;
  const res = await fetch(`/api/article/${box.dataset.select}/select`, {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({selected: box.checked})});
  if (!res.ok) { box.checked = !box.checked; alert('선택을 저장하지 못했습니다.'); return; }
  const data = await res.json();
  box.closest('.card')?.classList.toggle('sel', box.checked);
  document.querySelectorAll('[data-selected-count]').forEach(el => el.textContent = data.selected_count);
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
