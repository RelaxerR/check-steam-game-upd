(() => {
  'use strict';
  const config = window.WIPE_CONFIG;
  const gathering = Date.parse(config.gatheringAt);
  const update = Date.parse(config.updateAt);
  const byId = id => document.getElementById(id);
  const toast = message => {
    byId('toast').textContent = message;
    byId('toast').classList.add('visible');
    clearTimeout(toast.timeout);
    toast.timeout = setTimeout(() => byId('toast').classList.remove('visible'), 3000);
  };
  if (!Number.isFinite(gathering) || !Number.isFinite(update) || update < gathering) {
    byId('countdown-label').textContent = 'ОШИБКА ДАТЫ';
    byId('timer-note').textContent = 'Проверь даты в site-config.js.';
    return;
  }
  const timeFormat = new Intl.DateTimeFormat('ru-RU', { timeZone: 'Etc/GMT-4', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
  const clockFormat = new Intl.DateTimeFormat('ru-RU', { timeZone: 'Etc/GMT-4', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' });
  const dateFormat = new Intl.DateTimeFormat('ru-RU', { timeZone: 'Etc/GMT-4', day: '2-digit', month: '2-digit', year: 'numeric' });
  byId('edition').textContent = `${dateFormat.format(gathering)} / GMT+4`;
  byId('gathering-time').textContent = timeFormat.format(gathering);
  byId('update-time').textContent = `≈${timeFormat.format(update)}`;
  const tick = () => {
    const now = Date.now();
    const assembled = now >= gathering;
    const due = now >= update;
    const remaining = Math.max(0, Math.ceil(((assembled ? update : gathering) - now) / 1000));
    byId('hours').textContent = String(Math.floor(remaining / 3600)).padStart(2, '0');
    byId('minutes').textContent = String(Math.floor(remaining / 60) % 60).padStart(2, '0');
    byId('seconds').textContent = String(remaining % 60).padStart(2, '0');
    byId('now').textContent = clockFormat.format(now);
    byId('countdown-label').textContent = due ? 'ВРЕМЯ ПРИШЛО. ПРОВЕРЬ STEAM.' : assembled ? 'ДО ОЖИДАЕМОГО ОБНОВЛЕНИЯ' : 'ДО СБОРА КАРТЕЛЯ';
    byId('timer').setAttribute('aria-label', assembled ? 'Время до ожидаемого обновления' : 'Время до сбора');
    byId('phase').textContent = due ? 'ЖДЁМ РЕЛИЗ В STEAM' : assembled ? 'СБОР ИДЁТ / ЖДЁМ ОБНОВУ' : 'ОЖИДАНИЕ ВАЙПА';
    byId('timer-note').textContent = due ? 'Расписание закончилось. Обновление могло задержаться — проверь клиент.' : assembled ? 'Картель собирается. Кто не зашёл — тот фармит серу.' : 'Последние часы нормальной жизни. Пользуйся.';
    byId('gathering-card').classList.toggle('passed', assembled);
    byId('gathering-card').classList.toggle('active', !assembled);
    byId('update-card').classList.toggle('active', assembled && !due);
    byId('gathering-status').textContent = assembled ? 'СБОР УЖЕ НАЧАЛСЯ' : 'ЖДЁМ ОПОЗДУНОВ';
    byId('update-status').textContent = due ? 'ПРОВЕРЬ STEAM — РЕЛИЗ НЕ ПОДТВЕРЖДЁН' : 'ПО РАСПИСАНИЮ, НЕ ПО ФАКТУ';
  };
  tick();
  setInterval(tick, 1000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) tick(); });

  const video = byId('meme-video');
  const clips = Array.isArray(config.videos) ? config.videos : [];
  let previous = -1;
  let failures = 0;
  let paused = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  let retryTimer;
  const nextVideo = (manual = false) => {
    clearTimeout(retryTimer);
    if (!clips.length) return;
    if (manual) failures = 0;
    const choices = clips.map((_, index) => index).filter(index => clips.length === 1 || index !== previous);
    previous = choices[Math.floor(Math.random() * choices.length)];
    const clip = clips[previous];
    video.src = clip.src;
    byId('meme-source').textContent = clip.name;
    byId('meme-source').href = clip.source;
    video.load();
    if (!paused) video.play().catch(() => {});
  };
  video.addEventListener('ended', () => nextVideo());
  video.addEventListener('loadeddata', () => { failures = 0; if (paused) video.pause(); });
  video.addEventListener('error', () => {
    failures += 1;
    // Keep a usable static background if all local files are unavailable.
    if (failures < clips.length) retryTimer = setTimeout(() => nextVideo(), 1000);
  });
  const pauseButton = byId('pause-video');
  const updatePauseButton = () => { pauseButton.textContent = paused ? 'ВКЛЮЧИТЬ ФОН' : 'ПАУЗА ФОНА'; pauseButton.setAttribute('aria-pressed', String(paused)); };
  updatePauseButton();
  if (paused) video.autoplay = false;
  nextVideo();
  byId('next-meme').addEventListener('click', () => nextVideo(true));
  pauseButton.addEventListener('click', () => { paused = !paused; if (paused) video.pause(); else video.play().catch(() => toast('Браузер не смог включить фон. Попробуй другой мем.')); updatePauseButton(); });

  const music = byId('music');
  music.volume = 0.35;
  byId('music-button').addEventListener('click', async () => {
    if (!music.paused) music.pause();
    else { try { await music.play(); } catch { toast('Не удалось включить фонк. Проверь звук в браузере.'); } }
    byId('music-button').textContent = music.paused ? 'ФОНК: ВЫКЛ' : 'ФОНК: ВКЛ';
    byId('music-button').setAttribute('aria-pressed', String(!music.paused));
  });
  byId('copy-link').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(location.href); toast('Ссылка скопирована. Собирай картель.'); }
    catch { window.prompt('Скопируй ссылку и кинь кентам:', location.href); }
  });
})();
