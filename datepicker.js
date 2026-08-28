/* Мини-календарь (popup datepicker) для полей дд.мм
   Использование: attachDatepicker(inputElement)
   Значение пишется в input как дд.мм, мин. дата = сегодня, навигация по месяцам. */
(function () {
  const MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"];
  const WD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];

  let openPicker = null;

  function closePicker() {
    if (openPicker) { openPicker.remove(); openPicker = null; }
  }
  document.addEventListener('click', e => {
    if (openPicker && !openPicker.contains(e.target) && e.target !== openPicker._anchor) closePicker();
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closePicker(); });

  function build(input, onPick) {
    closePicker();
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const view = new Date(today.getFullYear(), today.getMonth(), 1);

    const pop = document.createElement('div');
    pop.className = 'dp-pop';
    pop._anchor = input;

    function render() {
      const y = view.getFullYear(), m = view.getMonth();
      let html = '<div class="dp-head"><button type="button" class="dp-nav" data-d="-1">‹</button>' +
        '<span class="dp-title">' + MONTHS[m] + ' ' + y + '</span>' +
        '<button type="button" class="dp-nav" data-d="1">›</button></div>' +
        '<div class="dp-grid">' + WD.map(w => '<span class="dp-wd">' + w + '</span>').join('');
      const first = new Date(y, m, 1);
      let shift = (first.getDay() + 6) % 7; // неделя с Пн
      for (let i = 0; i < shift; i++) html += '<span></span>';
      const days = new Date(y, m + 1, 0).getDate();
      for (let d = 1; d <= days; d++) {
        const dt = new Date(y, m, d);
        const past = dt < today;
        const isToday = dt.getTime() === today.getTime();
        html += '<button type="button" class="dp-day' + (past ? ' dp-past' : '') + (isToday ? ' dp-today' : '') +
          '" data-d="' + d + '"' + (past ? ' disabled' : '') + '>' + d + '</button>';
      }
      html += '</div><div class="dp-quick">' +
        '<button type="button" class="dp-q" data-q="0">Сегодня</button>' +
        '<button type="button" class="dp-q" data-q="1">Завтра</button>' +
        '<button type="button" class="dp-q" data-q="7">Через неделю</button></div>';
      pop.innerHTML = html;

      pop.querySelector('.dp-title').textContent = MONTHS[m] + ' ' + y;
      pop.querySelectorAll('.dp-nav').forEach(b => b.onclick = () => {
        view.setMonth(view.getMonth() + parseInt(b.dataset.d)); render();
      });
      pop.querySelectorAll('.dp-day:not(.dp-past)').forEach(b => b.onclick = () => {
        pick(new Date(y, m, parseInt(b.dataset.d)));
      });
      pop.querySelectorAll('.dp-q').forEach(b => b.onclick = () => {
        pick(new Date(today.getFullYear(), today.getMonth(), today.getDate() + parseInt(b.dataset.q)));
      });
    }

    function pick(dt) {
      const dd = String(dt.getDate()).padStart(2, '0');
      const mm = String(dt.getMonth() + 1).padStart(2, '0');
      input.value = dd + '.' + mm;
      input.dataset.iso = dt.getFullYear() + '-' + dd + '-' + mm;
      input.dispatchEvent(new Event('dp-change'));
      closePicker();
      if (onPick) onPick(input.value);
    }

    render();
    document.body.appendChild(pop);
    const r = input.getBoundingClientRect();
    pop.style.visibility = 'hidden';
    pop.style.display = 'block';
    const w = pop.offsetWidth, h = pop.offsetHeight;
    let left = Math.min(r.left, window.innerWidth - w - 10);
    left = Math.max(10, left);
    let top = (r.bottom + h > window.innerHeight - 10) ? (r.top - h - 6) : (r.bottom + 6);
    pop.style.left = left + window.scrollX + 'px';
    pop.style.top = top + window.scrollY + 'px';
    pop.style.visibility = 'visible';
    openPicker = pop;
  }

  function attach(input) {
    if (!input) return;
    input.readOnly = true;
    input.style.cursor = 'pointer';
    input.addEventListener('click', () => {
      if (openPicker && openPicker._anchor === input) { closePicker(); return; }
      build(input);
    });
  }

  window.attachDatepicker = attach;
  window.DP_STYLES_DONE = true;

  // стили вставляем один раз
  const css = document.createElement('style');
  css.textContent = `
  .dp-pop{position:absolute;z-index:9999;width:262px;background:#fff;border:1.5px solid #EADFCF;
    border-radius:14px;box-shadow:0 14px 40px rgba(43,33,23,.18);padding:10px;display:none;
    font-family:inherit;user-select:none}
  .dp-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:6px}
  .dp-title{font-weight:700;font-size:14px;color:#2B2117}
  .dp-nav{border:none;background:#F2E9DB;border-radius:8px;width:28px;height:28px;font-size:15px;
    cursor:pointer;color:#A26F2B;font-weight:700}
  .dp-nav:hover{background:#EADFCF}
  .dp-grid{display:grid;grid-template-columns:repeat(7,1fr);gap:2px;margin-bottom:6px}
  .dp-wd{font-size:10.5px;color:#8A7A68;text-align:center;padding:3px 0;font-weight:700}
  .dp-day{border:none;background:transparent;border-radius:8px;height:30px;font-size:13px;cursor:pointer;color:#2B2117}
  .dp-day:hover{background:#F7EFE2}
  .dp-past{color:#CFC4B2;cursor:default!important;background:transparent!important}
  .dp-today{box-shadow:inset 0 0 0 1.5px #C08A3E;font-weight:700}
  .dp-quick{display:flex;gap:5px;border-top:1px dashed #EADFCF;padding-top:7px}
  .dp-q{flex:1;border:1.5px solid #EADFCF;background:#fff;border-radius:16px;padding:5px 4px;
    font-size:11.5px;font-weight:600;color:#A26F2B;cursor:pointer}
  .dp-q:hover{background:#C08A3E;border-color:#C08A3E;color:#fff}`;
  document.head.appendChild(css);
})();
