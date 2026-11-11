const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

let lastRoomSig = '', lastLogSig = '';
let hoverPause = false;        // 鼠标悬停表格时暂停重建，避免行从鼠标下跳走
let pendingRebuild = false;    // 悬停/节流期间攒下的重建任务
let lastRebuild = 0;           // 上次整表重建时刻（重建节流）
let lastLogRebuild = 0;        // 上次日志重建时刻（日志节流）
let drawerId = null, lastState = null;
const STATUS = {idle:'待检查', checking:'检测中', offline:'未开播', live:'直播中', recording:'录制中', error:'异常'};

async function api(path, opts = {}) {
  const res = await fetch(path, Object.assign({headers:{'Content-Type':'application/json'}}, opts));
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || `HTTP ${res.status}`);
  return res.json();
}
const post = (p, body) => api(p, {method:'POST', body:JSON.stringify(body || {})});

let toastTimer;
function toast(text) {
  const el = $('#toast');
  el.textContent = text; el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 2600);
}

function ago(ts) {
  if (!ts) return '—';
  const d = Math.max(0, Math.floor(Date.now()/1000 - ts));
  if (d < 60) return d + ' 秒前';
  if (d < 3600) return Math.floor(d/60) + ' 分钟前';
  if (d < 86400) return Math.floor(d/3600) + ' 小时前';
  return Math.floor(d/86400) + ' 天前';
}

const sizeText = (b) => !b ? '' : b > 1048576 ? (b/1048576).toFixed(1) + ' MB' : Math.round(b/1024) + ' KB';
/* 已录制时长：不足 1 小时用 m:ss，超过用 h:mm:ss */
function durText(s) {
  s = Math.max(0, Math.floor(Number(s) || 0));
  if (!s) return '—';
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const p = (n) => String(n).padStart(2, '0');
  return h ? `${h}:${p(m)}:${p(sec)}` : `${m}:${p(sec)}`;
}
const nameOf = (r) => r.display_name || r.alias || r.anchor || '未命名直播间';
/* 在线人数：0 = 这次没探到（未开播 / 接口降级），详情页显示占位符 */
const viewersText = (v) => Number(v) > 0 ? Number(v).toLocaleString('zh-CN') : '—';
const platformOf = (r) => r.platform || (() => {
  const u = typeof r === 'string' ? r : (r.url || '');
  if (u.startsWith('demo://')) return '演示';
  if (/douyin\.com/.test(u)) return '抖音';
  if (/kuaishou\.com|gifshow\.com/.test(u)) return '快手';
  if (/\.m3u8|\.flv/.test(u)) return '直链';
  return '自定义';
})();
/* 房间号：抖音是尾号数字，快手是 /u/<快手号>，只在详情里展示 */
const roomNo = (u) => {
  const m = String(u).match(/(\d{4,})\/?$/);
  if (m) return m[1];
  const m2 = String(u).match(/(?:u|profile|fw\/live)\/([^/?#]+)/);
  return m2 ? decodeURIComponent(m2[1]) : '';
};

/* 平台/操作图标：配合 data-tip 做悬停提示，列表更清爽 */
const ICON = {
  抖音: '<svg viewBox="0 0 24 24"><path d="M9 17V5l10-2v12"/><circle cx="6.5" cy="17.5" r="2.5"/><circle cx="16.5" cy="15.5" r="2.5"/></svg>',
  快手: '<svg viewBox="0 0 24 24"><rect x="2.5" y="6.5" width="12" height="11" rx="2.5"/><path d="M14.5 11.5l6-3.2v7.4l-6-3.2z"/></svg>',
  小红书: '<svg viewBox="0 0 24 24"><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H20v15.5H6.5A2.5 2.5 0 0 0 4 21z"/><path d="M8 7.5h7M8 11h5"/></svg>',
  直链: '<svg viewBox="0 0 24 24"><path d="M10 13.5a4.5 4.5 0 0 0 6.4 0l2-2a4.5 4.5 0 0 0-6.4-6.4l-1 1"/><path d="M14 10.5a4.5 4.5 0 0 0-6.4 0l-2 2a4.5 4.5 0 0 0 6.4 6.4l1-1"/></svg>',
  演示: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M10 8.5l6 3.5-6 3.5z"/></svg>',
  自定义: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3c2.5 2.4 3.8 5.6 3.8 9S14.5 18.6 12 21c-2.5-2.4-3.8-5.6-3.8-9S9.5 5.4 12 3z"/></svg>',
  folder: '<svg viewBox="0 0 24 24"><path d="M3 7.5h5.5l1.8 2H21v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-10z"/></svg>',
  info: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><circle cx="12" cy="7.8" r=".9" fill="currentColor" stroke="none"/></svg>',
  sun: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2.6v2.2M12 19.2v2.2M2.6 12h2.2M19.2 12h2.2M5.2 5.2l1.6 1.6M17.2 17.2l1.6 1.6M18.8 5.2l-1.6 1.6M6.8 17.2l-1.6 1.6"/></svg>',
  moon: '<svg viewBox="0 0 24 24"><path d="M20.5 14.6A8.6 8.6 0 0 1 9.4 3.5a8.6 8.6 0 1 0 11.1 11.1z"/></svg>',
  rec: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4" fill="currentColor" stroke="none"/></svg>',
  stop: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><rect x="8.6" y="8.6" width="6.8" height="6.8" rx="1.4" fill="currentColor" stroke="none"/></svg>',
  trash: '<svg viewBox="0 0 24 24"><path d="M4 7h16M9.5 7V5.2A1.2 1.2 0 0 1 10.7 4h2.6a1.2 1.2 0 0 1 1.2 1.2V7"/><path d="M6.5 7l.8 11.4A2 2 0 0 0 9.3 20h5.4a2 2 0 0 0 2-1.6L17.5 7"/><path d="M10.5 11v5M13.5 11v5"/></svg>',
};

/* ---------- 列表 ---------- */
/* 双列卡片里名字放不下时左右往返滚动；单列仍用省略号。
   整表重建 / 布局切换 / 窗口变化后都要重新量一次实际宽度。 */

function roomRow(r) {
  const st = r.status || 'idle';
  const canStop = st === 'recording';
  const plat = platformOf(r);
  const sub = r.error ? `<div class="room-err" title="${esc(r.error)}">${esc(r.error)}</div>` : '';
  // 平台级限流时，房间并没有出错，只是这一轮排不上：单独给个「限流等待」
  const blocked = (lastState?.blocked || {})[plat] || 0;
  const waiting = st === 'idle' && blocked > 0;
  return `<tr data-id="${r.id}" class="${st}">
    <td><span class="tag ${waiting ? 'wait' : st}" title="${waiting ? `${plat}限流静默中，约 ${Math.ceil(blocked / 60)} 分钟后重试` : ''}">${waiting ? '限流等待' : (STATUS[st] || st)}</span></td>
    <td><span class="tip-wrap plat-ico ${st}" data-tip="${esc(plat)}${st === 'recording' ? ' · 正在录制' : st === 'live' ? ' · 直播中' : ''}">${ICON[plat] || ICON['自定义']}</span></td>
    <td><div class="room-name"><span class="nm nm-click" data-act="detail" title="${r.is_live || r.status === 'recording' ? esc(viewersText(r.viewers)) + ' 在线 · ' : ''}点击查看详情"><span class="nm-txt">${esc(nameOf(r))}</span></span></div>${sub}</td>
    <td class="q-col"><span class="mono dur" data-dur="${r.id}">${durText(r.rec_seconds)}</span></td>
    <td><div class="row-actions">
      <button class="icon-act tip-wrap" data-act="folder" data-tip="打开文件夹">${ICON.folder}</button>
      ${canStop
        ? `<button class="icon-act stop tip-wrap" data-act="stop" data-tip="停止录制">${ICON.stop}</button>`
        : `<button class="icon-act rec tip-wrap" data-act="start" data-tip="立即录制">${ICON.rec}</button>`}
      <button class="icon-act del tip-wrap" data-act="del" data-tip="删除">${ICON.trash}</button>
    </div></td></tr>`;
}

function render(s) {
  lastState = s;
  const cfg = s.config || {};
  const rooms = s.rooms || [];
  // 只有「会改变行结构」的字段进入签名；时长/大小每轮都在跳，
  // 若一起进签名，表格会每 1.5 秒重建一次，画质下拉框会被反复打断。
  // 「checking」是扫描瞬态（几百个房间轮流翻转），也排除掉，避免无意义重建。
  const sig = JSON.stringify(rooms.map(r =>
    [r.id, r.status === 'checking' ? '' : r.status, r.enabled, r.quality,
     r.display_name, r.note, r.error, r.anchor]));
  if (sig !== lastRoomSig) pendingRebuild = true;
  // 悬停在表格上时不重建（否则行会从鼠标底下跳走，点不中）；
  // 非悬停时也最多 1.2 秒重建一次，压掉扫描期的高频重建。
  // 双列卡片的重排成本远高于表格行，重建间隔放宽一倍
  const rebuildGap = $('.table-wrap').classList.contains('two-col') ? 4000 : 1200;
  if (pendingRebuild && !hoverPause && Date.now() - lastRebuild >= rebuildGap) {
    lastRoomSig = sig;
    pendingRebuild = false;
    lastRebuild = Date.now();
    const html = rooms.map(roomRow).join('');
    // 上千行时 innerHTML 解析要占掉半秒，挪到浏览器空闲期做，别打断点击与滚动
    const apply = () => {
      $('#roomRows').innerHTML = html;
      syncTips();
      $('#empty').classList.toggle('hidden', (s.rooms || []).length > 0);
    };
    if (window.requestIdleCallback) requestIdleCallback(apply, { timeout: 800 });
    else apply();
    const onAir = (s.rooms || []).filter(r => r.status === 'recording' || r.status === 'live').length;
    $('#roomsHint').textContent =
      `${s.stats.total} 个房间 · ${s.stats.enabled} 个启用${onAir ? ` · ${onAir} 个在播` : ''} · 在播优先排序`;
  }
  // 时长走定点刷新（一次 querySelectorAll 建索引，不做逐行 querySelector——
  // 1381 行 × 每 1.5 秒一次选择器查询是长时间运行卡顿的主因）
  const durMap = {};
  for (const r of rooms) durMap[r.id] = r;
  for (const el of $('#roomRows').querySelectorAll('[data-dur]')) {
    const r = durMap[el.getAttribute('data-dur')];
    if (r) el.textContent = durText(r.rec_seconds);
  }
  $('#sTotal').textContent = s.stats.total;
  $('#sLive').textContent = s.stats.live;
  $('#sRec').textContent = s.stats.recording;
  $('#sErr').textContent = s.stats.error;

  const running = !!s.running;
  // 监测运行状态由标题闪动表示（顶栏不再放文字标签）
  $('.brand-name').classList.toggle('running', running);
  $('#btnEngineText').textContent = running ? '停止监测' : '启动监测';
  $('#btnEngine').classList.toggle('running', running);
  $('#footEngine').textContent = running ? '引擎运行中' : '引擎未启动';
  const blocked = Object.entries(s.blocked || {}).filter(([, v]) => v > 0);
  const paceTxt = Object.entries(s.pace || {})
    .filter(([, v]) => v > 0.6).map(([k, v]) => `${k} ${v}s/次`).join(' · ');
  $('#footConfig').textContent =
    `${cfg.interval}s · 1/${cfg.jitter_ratio} · ${cfg.concurrency} · ${String(cfg.format||'ts').toUpperCase()}`
    + (paceTxt ? ` · 降速` : '')
    + (blocked.length ? ` · 静默` : '');
  $('#footConfig').style.color = blocked.length ? 'var(--amber)' : '';
  $('#footFfmpeg').textContent = s.ffmpeg ? 'ffmpeg 已就绪' : 'ffmpeg 未找到';
  $('#footFfmpeg').style.color = s.ffmpeg ? '' : 'var(--red)';
  const hkEl = $('#footHotkey');
  hkEl.textContent = s.hotkey_hide
    ? `${s.hotkey_hide} 隐藏 · ${s.hotkey_show || '—'} 还原`
    : (s.hotkey_tray ? '热键被占用，已启用托盘图标' : '');
  hkEl.style.display = hkEl.textContent ? '' : 'none';

  const logs = s.logs || [];
  const lsig = logs.length + ':' + (logs.length ? logs[logs.length-1].t + logs[logs.length-1].text : '');
  // 日志也节流到最多 2 秒一次重建；节流跳过时不更新 lastLogSig，下一轮会自动补上
  if (lsig !== lastLogSig && Date.now() - lastLogRebuild >= 2000) {
    lastLogSig = lsig;
    lastLogRebuild = Date.now();
    const box = $('#logs');
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    box.innerHTML = logs.map(l => `<div class="log-row"><span class="log-t">${esc(l.t)}</span><span class="log-lv ${esc(l.level)}">${esc(l.level)}</span><span class="log-x">${esc(l.text)}</span></div>`).join('');
    if (atBottom) box.scrollTop = box.scrollHeight;
  }
  if (drawerId) updateDrawerLive();
}

async function refresh() {
  try { render(await api('/api/state')); }
  catch (e) { $('#footEngine').textContent = '连接中断：' + e.message; }
}

/* 一级菜单：直接打开该主播自己的录制文件夹 */
async function openRoomFolder(id) {
  try {
    const res = await post(`/api/rooms/${id}/open-folder`);
    toast(res.exact === false ? '该主播还没有录制文件，已打开上级目录' : '已打开该主播的文件夹');
  } catch (e) { toast(e.message); }
}

/* ---------- 目录选择器（内置，不依赖系统对话框） ---------- */
function folderPicker(startPath, onPick) {
  const mask = document.createElement('div');
  mask.className = 'picker-mask';
  mask.innerHTML = `<div class="picker">
    <div class="picker-head"><h3>选择保存位置</h3><button class="icon-btn" data-x="1">×</button></div>
    <div class="picker-tools">
      <button class="btn tiny ghost" id="pkUp">上一级</button>
      <button class="btn tiny ghost" id="pkRoot">此电脑</button>
      <input class="input pk-new" id="pkNew" placeholder="新建文件夹名">
      <button class="btn tiny ghost" id="pkMk">新建</button>
    </div>
    <div class="picker-path"><code id="pkPath">—</code></div>
    <div class="picker-list" id="pkList"></div>
    <div class="picker-foot">
      <button class="btn ghost" data-x="1">取消</button>
      <button class="btn primary" id="pkOk" disabled>选择此文件夹</button>
    </div>
  </div>`;
  document.body.appendChild(mask);
  let cur = '';

  const close = () => mask.remove();
  const load = async (p) => {
    $('#pkList').innerHTML = '<div class="picker-empty">读取中…</div>';
    let d;
    try { d = await api('/api/browse?path=' + encodeURIComponent(p || '')); }
    catch (e) { $('#pkList').innerHTML = `<div class="picker-empty err">${esc(e.message)}</div>`; return; }
    cur = d.path || '';
    $('#pkPath').textContent = cur || '此电脑';
    $('#pkOk').disabled = !cur;
    $('#pkUp').disabled = !cur;
    if (d.error) { $('#pkList').innerHTML = `<div class="picker-empty err">${esc(d.error)}</div>`; return; }
    if (!d.dirs.length) { $('#pkList').innerHTML = '<div class="picker-empty">此目录下没有子文件夹</div>'; return; }
    $('#pkList').innerHTML = d.dirs.map(it =>
      `<div class="picker-item" data-path="${esc(it.path)}">
        <svg viewBox="0 0 24 24"><path d="M3 7h6l2 2h10v9a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V7z"/></svg>
        <span>${esc(it.name)}</span></div>`).join('');
  };

  mask.addEventListener('click', async (e) => {
    if (e.target.dataset.x) return close();
    if (e.target.id === 'pkUp') return load(cur ? cur.replace(/[\\/][^\\/]*$/, '') || '' : '');
    if (e.target.id === 'pkRoot') return load('');
    if (e.target.id === 'pkMk') {
      const name = $('#pkNew').value.trim();
      if (!name) return toast('先输入文件夹名');
      try {
        const r = await post('/api/mkdir', {path: cur, name});
        if (!r.ok) return toast(r.error || '创建失败');
        $('#pkNew').value = '';
        await load(r.path);
      } catch (err) { toast(err.message); }
      return;
    }
    if (e.target.id === 'pkOk') { onPick(cur); return close(); }
    const item = e.target.closest('.picker-item');
    if (item) return load(item.dataset.path);
  });

  load(startPath || '');
}

/* ---------- 二次确认 ---------- */
function confirmDialog({title, text, okText = '确认', danger = true, onOk}) {
  const card = $('#modal .modal-card');
  card.classList.remove('narrow', 'settings', 'hide-close');
  card.classList.add('confirm');
  const foot = $('#modalFoot');
  // 取消在左、确认在右下角，两端分开
  foot.innerHTML = `<div class="foot-left">
      <button class="btn ghost" data-close="1">取消</button>
    </div>
    <button class="btn ${danger ? 'danger' : 'primary'}" id="confirmOk">${esc(okText)}</button>`;
  $('#modalTitle').textContent = title;
  $('#modalBody').innerHTML = `<p class="confirm-text">${text}</p>`;
  $('#modal').classList.remove('hidden');
  $('#confirmOk').onclick = async () => {
    card.classList.remove('confirm');
    $('#modal').classList.add('hidden');
    try { await onOk(); } catch (e) { toast(e.message); }
  };
}
function closeModal() {
  const card = document.querySelector('.modal-card');
  if (card) card.classList.remove('narrow', 'settings', 'confirm', 'hide-close');
  $('#modal').classList.add('hidden');
  resumeHotkeys();
}

/* ---------- 详情抽屉 ---------- */
function findRoom(id) {
  return (lastState?.rooms || []).find(r => r.id === id);
}

function openDrawer(id) {
  drawerId = id;
  const r = findRoom(id);
  if (!r) return;
  $('#drawerTitle').textContent = nameOf(r);
  $('#drawerBody').innerHTML = drawerHtml(r);
  $('#drawerFoot').innerHTML = '';   // 操作按钮已内联到监测开关下方
  $('#drawer').classList.remove('hidden');
  bindDrawer(r);
  updateDrawerLive();
}

function drawerHtml(r) {
  const cfg = lastState?.config || {};
  const file = r.file ? r.file.split(/[\\/]/).pop() : '';
  // 输入框同步显示当前生效的名字；data-origin 用来区分「用户真的改过」和「只是回填」
  const aliasShown = r.alias || r.anchor || '';
  return `
  <div class="d-section">
    <label class="d-label">显示名称</label>
    <input class="input" id="dAlias" maxlength="40" value="${esc(aliasShown)}"
           data-origin="${esc(aliasShown)}" placeholder="留空则使用解析到的昵称">
  </div>
  <div class="d-section">
    <label class="d-label">备注</label>
    <input class="input" id="dNote" maxlength="120" value="${esc(r.note)}" placeholder="例如：游戏区 / 每晚 8 点">
  </div>
  <div class="d-section">
    <label class="d-label">录制信息</label>
    <div class="d-live">
      <div class="d-row"><span>主播名称</span><b>${esc(r.anchor || '—')}</b></div>
      <div class="d-row"><span>在线人数</span><b id="dViewers">${viewersText(r.viewers)}</b></div>
      <div class="d-row span2"><span>直播标题</span><b class="wrap" id="dTitle">${esc(r.title || '—')}</b></div>
      <div class="d-row span2"><span>直播链接</span>
        <a id="dOpenLive" class="d-open" title="点击用浏览器打开直播链接">打开直播间</a></div>
      <div class="d-row"><span>录制格式</span><b>${esc(String(cfg.format || 'ts').toUpperCase())}</b></div>
      <div class="d-row"><span>录制画质</span>
        <div class="q-drop" id="dQuality" data-val="${esc(r.quality || '原画')}" title="切换该房间的录制画质">
          <button type="button" class="q-trigger">
            <span class="q-cur">${esc(r.quality || '原画')}</span>
            <svg class="q-arrow" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
          </button>
          <div class="q-menu">
            ${['原画','超清','高清','标清','流畅'].map(q => `<button type="button" class="q-opt${(r.quality||'原画')===q?' on':''}" data-v="${q}">${q}</button>`).join('')}
          </div>
        </div></div>
      <div class="d-row"><span>本次录制时长</span><b id="dDur">${durText(r.rec_seconds)}</b></div>
      <div class="d-row"><span>已录大小</span><b id="dSize">${sizeText(r.rec_bytes) || '—'}</b></div>
      <div class="d-row"><span>使用代理</span><b>${cfg.proxy ? esc(cfg.proxy) : '否'}</b></div>
      <div class="d-row"><span>监控状态</span><b id="dStatus" class="tag ${r.status}">${STATUS[r.status] || r.status}</b></div>
      <div class="d-row"><span>最近检查</span><b id="dCheck">${ago(r.last_check)}</b></div>
      <div class="d-row"><span>最近录像</span><code title="${esc(r.file)}">${esc(file || '—')}</code></div>
      <div class="d-row span2"><span>监测开关</span>
        <label class="switch" title="停用后不再检测该房间"><input type="checkbox" id="dEnabled" ${r.enabled ? 'checked' : ''}><i></i></label></div>
      <div class="d-actions span2">
        <button class="btn ghost danger-text" id="dRemove">移除此直播间</button>
        <button class="btn primary" id="dSave">保存修改</button>
      </div>
      <div id="dError" class="room-err span2">${r.error ? esc(r.error) : ''}</div>
    </div>
  </div>`;
}

/* ============================================================
   预览小窗模块【暂时下线，入口已移除】
   用户反馈弹窗无法关闭/无法缩放/不是无边框，先整体撤掉入口（状态图标
   与详情按钮都不再触发）。代码保留待修复后重新启用：
   未决问题 —— pywebview 弹窗的 _PlayerApi 拿不到窗口句柄时关闭/缩放全失效，
   frameless+resizable 的 WS_THICKFRAME 仍显边框。
   ============================================================ */
const previewOpen = {};    // rid -> 'pip' | 'doc' | 'popout' | 'popup'
const docPipWin = {}, popupWin = {};

function markPreview(rid, mode) {
  if (mode) previewOpen[rid] = mode; else delete previewOpen[rid];
  const ico = document.querySelector(`tr[data-id="${rid}"] .plat-ico`);
  if (ico) ico.classList.toggle('previewing', !!mode);
}

async function togglePreview(rid) {
  if (previewOpen[rid]) return closePreview(rid);
  return openPopout(rid);
}

async function closePreview(rid) {
  const mode = previewOpen[rid];
  markPreview(rid, null);
  try {
    if (mode === 'pip') {
      if (document.pictureInPictureElement) await document.exitPictureInPicture();
    } else if (mode === 'doc') {
      if (docPipWin[rid]) { docPipWin[rid].close(); delete docPipWin[rid]; }
    } else if (mode === 'popout') {
      await post('/api/popout/close', {rid});
    } else if (mode === 'popup') {
      if (popupWin[rid]) { popupWin[rid].close(); delete popupWin[rid]; }
    }
  } catch (e) {}
}

async function openPopout(rid) {
  /* ① Document PiP：真正的浏览器小窗，装完整播放页（可静音/全屏/关闭） */
  if ('documentPictureInPicture' in window) {
    try {
      const pip = await window.documentPictureInPicture.requestWindow({ width: 520, height: 330 });
      const doc = pip.document;
      doc.title = '直播预览';
      doc.body.style.cssText = 'margin:0;background:#000;width:100%;height:100%;overflow:hidden';
      const f = doc.createElement('iframe');
      f.src = '/player.html?rid=' + encodeURIComponent(rid);
      f.allow = 'autoplay';
      f.style.cssText = 'border:0;width:100%;height:100%;display:block';
      doc.body.appendChild(f);
      docPipWin[rid] = pip;
      markPreview(rid, 'doc');
      pip.addEventListener('pagehide', () => { delete docPipWin[rid]; markPreview(rid, null); });
      toast('已用小窗模式打开（可拉伸、置顶）');
      return { ok: true, mode: 'pip' };
    } catch (e) { /* 尝试下一级 */ }
  }

  /* ② 经典视频 PiP：只有画面（浏览器自带播放条），先解析流地址 */
  if (document.pictureInPictureEnabled) {
    try {
      const res = await post(`/api/rooms/${rid}/stream`);
      if (res.ok) {
        toast('正在接流…（慢流起播可能要几秒）');
        await startVideoPip(rid, res);
        markPreview(rid, 'pip');
        toast('已用视频小窗打开');
        return { ok: true, mode: 'video-pip' };
      }
      // 未开播/解析失败：提示就好，别弹个空窗
      toast(res.error || '当前未开播，没有可播放的画面');
      return { ok: false, error: res.error };
    } catch (e) { /* 网络异常再落到弹窗 */ }
  }

  /* ③ 兜底：pywebview 无边框弹窗；非 webview 模式用浏览器弹窗 */
  const r2 = await post(`/api/rooms/${rid}/popout`);
  if (r2 && r2.fallback) {
    const w = window.open('/player.html?rid=' + encodeURIComponent(rid),
                          'lrsPlayer_' + rid, 'width=520,height=330');
    popupWin[rid] = w;
    markPreview(rid, 'popup');
    return { ok: true, mode: 'browser-popup' };
  }
  if (r2 && r2.ok) {
    markPreview(rid, 'popout');
    return r2;
  }
  toast((r2 && r2.error) || '打开预览失败');
  return r2;
}

/* 经典视频 PiP：主窗口里藏一个 video，hls.js/flv.js 喂流后 requestPictureInPicture。
   注意：hls.js 出致命错误时 play() 的 Promise 既不返回也不拒绝，会无限挂起
   （用户实测「点图标卡死」就是它）—— 所以这里所有 await 都带超时/错误信号兜底。 */
let pipVideo = null, pipHls = null, pipFlv = null, pipRid = null;
function teardownVideoPip() {
  if (pipHls) { try { pipHls.destroy(); } catch (e) {} pipHls = null; }
  if (pipFlv) { try { pipFlv.pause(); pipFlv.unload(); pipFlv.detachMediaElement(); pipFlv.destroy(); } catch (e) {} pipFlv = null; }
  if (pipVideo) { try { pipVideo.remove(); } catch (e) {} pipVideo = null; }
  if (pipRid) { markPreview(pipRid, null); pipRid = null; }
}
async function startVideoPip(rid, res) {
  teardownVideoPip();
  const v = document.createElement('video');
  v.playsInline = true;
  v.style.cssText = 'position:fixed;left:-9999px;top:0;width:320px;height:180px';
  document.body.appendChild(v);
  pipVideo = v;
  pipRid = rid;

  // 错误信号：hls/flv 的致命错误在这里拒掉，避免 play() 永远挂起
  let rejectFail = null;
  const failSignal = new Promise((_, rej) => { rejectFail = rej; });
  const timeout = (ms, msg) => new Promise((_, rej) => setTimeout(() => rej(new Error(msg)), ms));
  const withTimeout = (p, ms, msg) => Promise.race([p, timeout(ms, msg), failSignal]);

  try {
    if (res.kind === 'hls' && window.Hls && Hls.isSupported()) {
      pipHls = new Hls({ lowLatencyMode: true, enableWorker: true, liveDurationInfinity: true });
      pipHls.on(Hls.Events.ERROR, (_e, data) => {
        if (data && data.fatal && rejectFail) rejectFail(new Error('hls ' + (data.details || data.type)));
      });
      pipHls.loadSource(res.url);
      pipHls.attachMedia(v);
    } else if (res.kind === 'hls' && v.canPlayType('application/vnd.apple.mpegurl')) {
      v.src = res.url;
    } else if (res.kind === 'flv' && window.flvjs && flvjs.isSupported()) {
      pipFlv = flvjs.createPlayer({ type: 'flv', isLive: true, url: res.url },
                                  { enableStashBuffer: false, lazyLoad: false });
      pipFlv.on(flvjs.Events.ERROR, (type, detail) => {
        if (rejectFail) rejectFail(new Error('flv ' + (detail || type)));
      });
      pipFlv.attachMediaElement(v);
      pipFlv.load();
      pipFlv.play().catch(() => {});
    } else {
      throw new Error('该流不支持视频小窗');
    }

    // 起播「不等完成」：慢流（竖屏/不规则分片）首帧可拖 10 秒以上，
    // 干等 play() 就是用户看到的「卡死」。拿到首帧信号或超时就进小窗，画面随后自动补上。
    const started = new Promise(res => {
      ['playing', 'loadeddata', 'canplay', 'loadedmetadata'].forEach(ev =>
        v.addEventListener(ev, () => res(1), { once: true }));
    });
    // 手势还在：先试带声起播；被拦就静音保底。都 fire-and-forget，不阻塞流程
    v.muted = false;
    v.play().catch(() => { v.muted = true; v.play().catch(() => {}); });
    await withTimeout(started, 6000, '等首帧').catch(() => {});   // 超时也继续进小窗
    // 进小窗：5 秒进不去就放弃（下一级兜底会接手）
    await withTimeout(v.requestPictureInPicture(), 5000, '小窗打开超时');
    v.addEventListener('leavepictureinpicture', teardownVideoPip, { once: true });
    rejectFail = null;   // 成功后失效错误信号，别让迟到的事件误伤
  } catch (e) {
    teardownVideoPip();
    throw e;
  }
}

function bindDrawer(r) {
  $('#dOpenLive').onclick = async (e) => {
    e.preventDefault();
    try { await post('/api/open-url', {url: r.url}); toast('已在浏览器中打开直播间'); }
    catch (err) { toast(err.message || '打开失败'); }
  };
  $('#dEnabled').onchange = async (e) => {
    await api('/api/rooms/' + r.id, {method:'PATCH', body: JSON.stringify({enabled: e.target.checked})});
    toast(e.target.checked ? '已启用该房间' : '已停用，不再检测');
    lastRoomSig = ''; refresh();
  };
  // 自定义画质下拉：原生 select 在 WebView2 里选项右对齐、样式不可控
  (function bindQualityDrop() {
    const drop = $('#dQuality');
    const cur = drop.querySelector('.q-cur');
    drop.querySelector('.q-trigger').onclick = (e) => {
      e.stopPropagation();
      drop.classList.toggle('open');
    };
    for (const opt of drop.querySelectorAll('.q-opt')) {
      opt.onclick = async () => {
        const v = opt.dataset.v;
        drop.classList.remove('open');
        cur.textContent = v;
        drop.dataset.val = v;
        for (const o of drop.querySelectorAll('.q-opt')) o.classList.toggle('on', o.dataset.v === v);
        await api('/api/rooms/' + r.id, {method:'PATCH', body:JSON.stringify({quality: v})});
        toast('画质已切换为' + v);
      };
    }
    // 点下拉以外收起
    document.addEventListener('click', function onDoc(e) {
      if (!document.body.contains(drop)) return;
      if (!drop.contains(e.target)) drop.classList.remove('open');
    });
  })();
  $('#dSave').onclick = async () => {
    const val = $('#dAlias').value.trim();
    const origin = $('#dAlias').dataset.origin || '';
    // 回填的名字只是「显示」，没被改动就不写死 alias：
    // 这样抖音以后改昵称，列表和录制目录还能自动跟着变。
    const alias = val === origin ? (r.alias || '') : val;
    await api('/api/rooms/' + r.id, {method:'PATCH', body: JSON.stringify({
      alias, note: $('#dNote').value.trim()})});
    toast('已保存'); lastRoomSig = ''; await refresh();
    const fresh = findRoom(r.id);
    if (fresh) { $('#drawerTitle').textContent = nameOf(fresh); }
  };
  $('#dRemove').onclick = () => confirmRemove(r.id);
}

function updateDrawerLive() {
  const r = findRoom(drawerId);
  if (!r) return;
  const set = (sel, v) => { const el = $(sel); if (el && el.textContent !== v) el.textContent = v; };
  const tag = $('#dStatus');
  if (tag) { tag.textContent = STATUS[r.status] || r.status; tag.className = 'tag ' + r.status; }
  set('#dCheck', ago(r.last_check));
  set('#dSize', sizeText(r.rec_bytes) || '—');
  set('#dDur', durText(r.rec_seconds));
  set('#dTitle', r.title || '—');
  set('#dViewers', viewersText(r.viewers));
  const err = $('#dError');
  if (err) err.textContent = r.error || '';
  const nameEl = $('#drawerTitle');
  if (nameEl) nameEl.textContent = nameOf(r);
}

function closeDrawer() { drawerId = null; $('#drawer').classList.add('hidden'); }
/* 点抽屉面板外的任意空白即关闭。面板内部一律不关闭，
   右上角那个小 × 太容易误触，已去掉。ESC 仍可关闭。 */
$('#drawer').addEventListener('click', (e) => {
  if (!e.target.closest('.drawer-panel')) closeDrawer();
});
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  if (!$('#drawer').classList.contains('hidden')) closeDrawer();
  else if (!$('#modal').classList.contains('hidden')) closeModal();
});

/* ---------- 事件 ---------- */
$('#btnEngine').onclick = async () => {
  const running = $('#btnEngine').classList.contains('running');
  await post('/api/engine', {action: running ? 'stop' : 'start'});
  toast(running ? '监测已停止' : '监测已启动');
  // 不再强制重建整表：上千行时点一下会卡住。按钮与统计由 refresh 就地更新，
  // 表格内容的下一次变化交给签名比对处理。
  refresh();
};
$('#btnFolder').onclick = async () => { try { await post('/api/open-folder'); } catch (e) { toast(e.message); } };
$('#btnClearLog').onclick = () => { lastLogSig = 'reset'; $('#logs').innerHTML = ''; };
/* 单列 / 双列布局切换：状态存本地，下次打开保持。
   纯 CSS 重排表格行，切完不需要重建表格，也不会打断画质下拉框。 */
let twoCol = localStorage.getItem('lrs.twocol') === '1';
/* 双列卡片有绘制裁剪，自绘气泡会变残影：切到双列改用原生 title，
   切回单列恢复自绘气泡，两种模式都不丢提示 */
function syncTips() {
  const on = twoCol;
  for (const el of $('#roomRows').querySelectorAll('[data-tip]')) {
    if (on) { if (el.title !== el.dataset.tip) el.title = el.dataset.tip; }
    else if (el.hasAttribute('title')) el.removeAttribute('title');
  }
}

function applyLayout() {
  $('.table-wrap').classList.toggle('two-col', twoCol);
  $('#btnLayoutText').textContent = twoCol ? '单列' : '双列';
  $('#btnLayout').dataset.tip = twoCol ? '切换为单列布局' : '切换为双列布局';
  $('#btnLayout').classList.toggle('active', twoCol);
  localStorage.setItem('lrs.twocol', twoCol ? '1' : '0');
  syncTips();
}
$('#btnLayout').onclick = () => { twoCol = !twoCol; applyLayout(); };
applyLayout();

/* 右侧状态/日志栏折叠：状态存本地，下次打开保持 */
// v2 键：默认隐藏侧栏（旧键作废，一次性重置成用户要的默认值）；点开后会记住
let sideHidden = localStorage.getItem('lrs.side2') !== '0';
function applySide() {
  $('#layout').classList.toggle('side-collapsed', sideHidden);
  $('#btnSide').dataset.tip = sideHidden ? '展开侧栏' : '收起侧栏';
  $('#btnSide').classList.toggle('active', !sideHidden);
  localStorage.setItem('lrs.side2', sideHidden ? '1' : '0');
  // 窄窗口下顶栏会折成两行，侧栏起点必须跟着顶栏实际高度走，否则会盖住「启动监测」
  const tb = document.querySelector('.topbar');
  if (tb) {
    const h = Math.round(tb.getBoundingClientRect().bottom);
    document.documentElement.style.setProperty('--side-top', (h > 24 ? h : 72) + 'px');
  }
}
$('#btnSide').onclick = () => { sideHidden = !sideHidden; applySide(); };
// 窗口尺寸变化会改变顶栏行数（72px ↔ 折行），侧栏起点要跟着重算
window.addEventListener('resize', () => { if (!sideHidden) applySide(); });
applySide();

/* 主题：浅色 / 深色，存本地记忆。默认深色，保持原有观感，不擅自换掉用户的默认外观 */
let themeMode = localStorage.getItem('lrs.theme') === 'light' ? 'light' : 'dark';
function applyTheme() {
  document.documentElement.dataset.theme = themeMode;
  const light = themeMode === 'light';
  $('#btnTheme').innerHTML = light ? ICON.moon : ICON.sun;
  $('#btnTheme').dataset.tip = light ? '切换为深色' : '切换为浅色';
}
$('#btnTheme').onclick = () => {
  themeMode = themeMode === 'light' ? 'dark' : 'light';
  localStorage.setItem('lrs.theme', themeMode);
  applyTheme();
};
applyTheme();

/* 快捷键录入：聚焦后直接按组合键，keydown 里拼成 'Ctrl+Alt+H'。
   两个坑（用户实测「只能单键、Alt+组合录不上」）：
   1) Alt 组合在 WebView2 走 WM_SYSKEYDOWN，e.key 常变 Dead/Unidentified 被忽略 —— 改用物理码 e.code；
   2) Alt+F1/F2 本身是全局热键，按下被系统吃掉（还把窗口藏了）—— 聚焦期间先卸载全局热键，失焦恢复。 */
function keyNameFrom(e) {
  const code = e.code || '';
  if (/^Key[A-Z]$/.test(code)) return code.slice(3);
  if (/^Digit[0-9]$/.test(code)) return code.slice(5);
  if (/^F[1-9]$|^F1[0-9]$|^F2[0-4]$/.test(code)) return code;
  const byCode = {
    Space: 'Space', Enter: 'Enter', Escape: 'Esc', Tab: 'Tab', Backspace: 'Backspace',
    Delete: 'Delete', Insert: 'Insert', Home: 'Home', End: 'End',
    PageUp: 'PageUp', PageDown: 'PageDown',
    ArrowUp: 'Up', ArrowDown: 'Down', ArrowLeft: 'Left', ArrowRight: 'Right',
    Backquote: '`', Minus: '-', Equal: '=', BracketLeft: '[', BracketRight: ']',
    Semicolon: ';', Quote: "'", Comma: ',', Period: '.', Slash: '/', Backslash: '\\',
  };
  if (byCode[code]) return byCode[code];
  const k = e.key;
  if (!k || ['Control', 'Alt', 'Shift', 'Meta', 'CapsLock', 'Dead', 'Unidentified'].includes(k)) return '';
  if (k === ' ') return 'Space';
  if (k === 'Escape') return 'Esc';
  return k.length === 1 ? k.toUpperCase() : k;
}

let hotkeysSuspended = false;
function suspendHotkeys() {
  if (hotkeysSuspended) return;
  hotkeysSuspended = true;
  post('/api/hotkeys/suspend').catch(() => {});
}
function resumeHotkeys() {
  if (!hotkeysSuspended) return;
  hotkeysSuspended = false;
  post('/api/hotkeys/resume').catch(() => {});
}

function bindHotkeyInput(el) {
  if (!el) return;
  el.addEventListener('keydown', (e) => {
    e.preventDefault();
    e.stopPropagation();
    const mods = [];
    if (e.ctrlKey) mods.push('Ctrl');
    if (e.altKey) mods.push('Alt');
    if (e.shiftKey) mods.push('Shift');
    if (e.metaKey) mods.push('Win');
    const name = keyNameFrom(e);
    if (!name) return;
    el.value = [...mods, name].join('+');
  });
  el.addEventListener('focus', () => { el.classList.add('capturing'); suspendHotkeys(); });
  el.addEventListener('blur', () => { el.classList.remove('capturing'); resumeHotkeys(); });
}

/* 关闭窗口：三选一（取消 / 缩到托盘 / 退出）。桌面窗口点 × 时由宿主拦下并回调这里。 */
window.onHostClose = () => {
  if (!$('#modal').classList.contains('hidden')) return;   // 已有弹窗时不叠加
  const showKey = lastState?.hotkey_show || 'Alt+F2';
  // 清理旧弹窗残留的样式类，避免串到这个弹窗（"偶尔正常"的根因）
  const card = document.querySelector('.modal-card');
  if (card) { card.classList.remove('narrow', 'settings', 'hide-close'); card.classList.add('confirm'); }
  $('#modalTitle').textContent = '关闭窗口';
  $('#modalBody').innerHTML = `<p class="confirm-text">
    缩到托盘：继续在后台监测与录制，点托盘图标或按 <b>${esc(showKey)}</b> 叫回窗口。<br>
    退出程序：先收尾正在录制的文件再关闭，之后需要重新启动。</p>`;
  $('#modalFoot').innerHTML = `
    <button class="btn ghost" data-close="1">取消</button>
    <button class="btn" id="closeTray">缩到托盘</button>
    <button class="btn danger" id="closeQuit">退出程序</button>`;
  $('#modal').classList.remove('hidden');
  $('#closeTray').onclick = async () => {
    if (card) card.classList.remove('confirm');
    $('#modal').classList.add('hidden');
    try { await post('/api/window', {action: 'tray'}); }
    catch (e) { toast(e.message); }
  };
  $('#closeQuit').onclick = async () => {
    if (card) card.classList.remove('confirm');
    $('#modal').classList.add('hidden');
    // 立即关窗不等后端收尾，退出体验零延迟
    try { await post('/api/window', {action: 'hide'}); } catch (e) {}
    toast('正在退出…');
    try { await post('/api/quit'); } catch (e) { /* 进程已在退出 */ }
    document.body.innerHTML = '<div class="bye">已退出，可关闭此窗口。</div>';
  };
};

$('#roomRows').addEventListener('change', async (e) => {
  const k = e.target.dataset.k, id = e.target.closest('tr')?.dataset.id;
  if (!k || !id) return;
  const patch = {[k]: k === 'enabled' ? e.target.checked : e.target.value};
  await api('/api/rooms/' + id, {method:'PATCH', body:JSON.stringify(patch)});
  lastRoomSig = ''; refresh();
});
$('#roomRows').addEventListener('click', async (e) => {
  // 必须用 closest：图标按钮被点中的是内部 <svg>/<path>，它们没有 data-act
  const hit = e.target.closest('[data-act]');
  const act = hit?.dataset.act, id = e.target.closest('tr')?.dataset.id;
  if (!act || !id) return;
  if (act === 'detail') return openDrawer(id);
  if (act === 'folder') return openRoomFolder(id);
  if (act === 'del') return confirmRemove(id);
  try {
    if (act === 'stop') {
      await post(`/api/record/${id}/stop`);
      toast('正在停止录制并转码为 MP4');
    } else if (act === 'start') {
      toast('正在解析流地址并开始录制…');
      await post(`/api/record/${id}/start`);
      toast('已开始录制');
    } else if (act === 'check') {
      await post(`/api/rooms/${id}/check`);
      toast('已发起检测');
    }
  } catch (err) {
    toast(err.message || '操作失败');
  }
  setTimeout(() => { lastRoomSig = ''; refresh(); }, 600);
});

/* 删除必须二次确认：误点会丢掉整条监测配置 */
function confirmRemove(id) {
  const r = findRoom(id);
  if (!r) return;
  confirmDialog({
    title: '删除直播间',
    text: `确定删除「<b>${esc(nameOf(r))}</b>」？<br><span class="muted">只会移除监测项，已录制的文件不会删除。</span>`,
    okText: '确认删除',
    onOk: async () => {
      await api('/api/rooms/' + id, {method:'DELETE'});
      if (drawerId === id) closeDrawer();
      toast('已删除'); lastRoomSig = ''; refresh();
    }
  });
}

/* ---------- 弹窗（导入 / 设置 / 确认） ---------- */
let modalSubmit = null;
function openModal(title, bodyHtml, okText, onSubmit, footExtra = '') {
  // 每次打开先清掉上一次可能留下的弹窗样式标记，避免串到别的弹窗
  const card = document.querySelector('.modal-card');
  if (card) card.classList.remove('narrow', 'hide-close', 'confirm', 'settings');
  $('#modalTitle').textContent = title;
  $('#modalBody').innerHTML = bodyHtml;
  // footExtra 放在底栏左侧，和取消/导入同一行，不再另行占一行
  $('#modalFoot').innerHTML = `${footExtra ? `<div class="foot-left">${footExtra}</div>` : ''}
    <button class="btn ghost" data-close="1">取消</button>
    <button class="btn primary" id="modalOk">${esc(okText)}</button>`;
  modalSubmit = onSubmit;
  $('#modal').classList.remove('hidden');
  $('#modalOk').onclick = async () => { if (modalSubmit) await modalSubmit(); };
}
$('#modal').addEventListener('click', (e) => {
  if (e.target.id === 'modal' || e.target.dataset.close) closeModal();
});

$('#btnImport').onclick = () => {
  openModal('导入', `
  <textarea id="importText" placeholder="支持批量导入&#10;一行一个直播间地址，例如：&#10;https://live.douyin.com/745964462470&#10;https://live.kuaishou.com/u/sunni777&#10;demo://离线演示"></textarea>
`,
  '导入', async () => {
    const text = $('#importText').value;
    if (!text.trim()) return closeModal();
    const r = await post('/api/rooms', {text});
    closeModal();
    const bits = [`新增 ${r.added} 个`];
    if (r.skipped) bits.push(`重复 ${r.skipped} 个`);
    if (r.ignored) bits.push(`无地址 ${r.ignored} 行`);
    toast(bits.join(' · '));
    lastRoomSig = ''; refresh();
  },
  `<button class="btn small tonal" id="btnPasteIn">粘贴</button>
   <button class="btn small ghost" id="btnExport">导出列表</button>`);
  // 导入弹窗内容少，用窄版：420px 宽、输入框 150px 高，不留大片空白
  document.querySelector('.modal-card').classList.add('narrow');
  bindImportExport();
};

function bindImportExport() {
  $('#btnExport').onclick = () => showExportModal();
  $('#btnPasteIn').onclick = () => pasteInto($('#importText'));
}

/* ---------- 文本框粘贴/右键菜单 ----------
   pywebview 默认关掉原生右键菜单，且剪贴板读权限默认拒绝（后端已放行）。
   这里自绘菜单（粘贴/复制/剪切/全选），并提供可点击的「粘贴」按钮兜底。 */
async function pasteInto(el) {
  if (!el) return;
  /* 优先让本地进程读系统剪贴板：WebView2 里的 navigator.clipboard.readText()
     要过浏览器权限，容易弹「允许访问剪贴板？」的原生提示，体验很差。
     本地进程读不弹窗，也不受权限模型影响；接口不可用时再回退浏览器 API。 */
  try {
    const r = await api('/api/clipboard');
    if (r && typeof r.text === 'string') {
      if (!r.text.trim()) { el.focus(); toast('剪贴板里没有文字'); return; }
      insertAtCursor(el, r.text);
      toast('已粘贴');
      return;
    }
  } catch (e) { /* 回退 */ }
  try {
    const t = await navigator.clipboard.readText();
    insertAtCursor(el, t || '');
    toast('已粘贴');
  } catch (e) {
    el.focus();
    toast('读不到剪贴板：请在输入框按 Ctrl+V 粘贴');
  }
}
function insertAtCursor(el, text) {
  const s = el.selectionStart ?? el.value.length;
  const e = el.selectionEnd ?? el.value.length;
  el.value = el.value.slice(0, s) + text + el.value.slice(e);
  el.selectionStart = el.selectionEnd = s + text.length;
  el.focus();
}
const ctxMenu = document.createElement('div');
ctxMenu.className = 'ctx-menu hidden';
document.body.appendChild(ctxMenu);
document.addEventListener('contextmenu', (e) => {
  const el = e.target;
  if (!(el instanceof HTMLTextAreaElement || el instanceof HTMLInputElement) || el.readOnly) return;
  e.preventDefault();
  const sel = el.value.slice(el.selectionStart, el.selectionEnd);
  const items = [
    { label: '粘贴', fn: () => pasteInto(el) },
    { label: '复制', fn: () => {
        if (!sel) return;
        navigator.clipboard.writeText(sel).then(() => toast('已复制'))
          .catch(() => { document.execCommand('copy'); toast('已复制'); });
      } },
    { label: '剪切', fn: () => {
        if (!sel) return;
        navigator.clipboard.writeText(sel).catch(() => document.execCommand('copy'));
        insertAtCursor(el, '');
        toast('已剪切');
      } },
    { label: '全选', fn: () => el.select() },
  ];
  ctxMenu.innerHTML = items.map((it, i) => `<button data-i="${i}">${it.label}</button>`).join('');
  ctxMenu.classList.remove('hidden');
  ctxMenu.style.left = Math.min(e.clientX, innerWidth - 132) + 'px';
  ctxMenu.style.top = Math.min(e.clientY, innerHeight - 30 - items.length * 34) + 'px';
  ctxMenu.onclick = (ev) => {
    const b = ev.target.closest('button');
    if (!b) return;
    ctxMenu.classList.add('hidden');
    items[+b.dataset.i].fn();
  };
});
document.addEventListener('mousedown', (e) => {
  if (!e.target.closest('.ctx-menu')) ctxMenu.classList.add('hidden');
});

function showExportModal() {
  const name = '直播间列表_' + new Date().toISOString().slice(0, 10) + '.txt';
  const dir = localStorage.getItem('exportDir') || (lastState?.save_path || '');
  openModal('导出直播间列表', `
    <div class="field"><label>文件名</label>
      <input class="input" id="xName" value="${esc(name)}"></div>
    <div class="field"><label>保存位置</label>
      <div class="path-row">
        <input class="input" id="xDir" value="${esc(dir)}" placeholder="选择一个文件夹">
        <button class="btn small" id="xBrowse">浏览…</button>
      </div>
      <p class="hint">导出为 UTF-8 文本，格式与导入一致，可直接再导入。</p>
    </div>`,
  '导出', async () => {
    const dir = $('#xDir').value.trim();
    const nm = $('#xName').value.trim() || name;
    if (!dir) { toast('请先选择保存位置'); return; }
    try {
      const res = await post('/api/rooms/export', {dir, name: nm});
      if (res.ok) {
        localStorage.setItem('exportDir', dir);
        closeModal();
        toast(`已导出 ${res.count} 条 → ${res.path}`);
      } else {
        toast(res.error || '导出失败');
      }
    } catch (e) { toast(e.message); }
  });
  $('#xBrowse').onclick = () => folderPicker($('#xDir').value || '', (p) => { $('#xDir').value = p; });
}

$('#btnSettings').onclick = async () => {
  const s = await api('/api/state');
  const c = s.config;
  openModal('设置', `
    <div class="field-row two-col">
      <div class="field"><label>默认录制画质</label>
        <select class="input" id="cQuality">
          ${['原画','超清','高清','标清','流畅'].map(q =>
            `<option ${(c.quality || '原画')===q?'selected':''}>${q}</option>`).join('')}
        </select></div>
      <div class="field"><label>录制格式</label><select class="input" id="cFormat">
        ${['ts','mkv','mp4'].map(f => `<option ${c.format===f?'selected':''}>${f}</option>`).join('')}</select></div>
    </div>
    <div class="field-row two-col">
      <div class="field"><label>循环间隔（秒）</label><input class="input" id="cInterval" type="number" min="10" value="${esc(c.interval)}"></div>
      <div class="field"><label>并发检查数</label><input class="input" id="cConcurrency" type="number" min="1" max="64" value="${esc(c.concurrency)}"></div>
    </div>
    <div class="field-row two-col">
      <div class="field"><label>分段录制（分钟，0 = 不分段）</label>
        <input class="input" id="cSegment" type="number" min="0" max="720" step="5" value="${esc(c.segment_minutes ?? 0)}"></div>
      <div class="field"><label>错峰比例（0 关闭）</label><input class="input" id="cJitter" type="number" min="0" max="20" value="${esc(c.jitter_ratio)}"></div>
    </div>
    <div class="field">
      <label>保存位置</label>
      <div class="path-row">
        <input class="input" id="cSaveDir" value="${esc(c.save_dir)}" placeholder="D:\\直播录像 或 downloads">
        <button class="btn small" id="cBrowse">浏览…</button>
        <button class="btn small ghost" id="cOpenDir">打开</button>
      </div>
      <p class="hint" id="cSaveHint">当前位置：<code>${esc(s.save_path || '')}</code></p>
    </div>
    <div class="field">
      <label>快捷键</label>
      <div class="field-row two-col">
        <div class="key-box"><span class="key-label">隐藏窗口</span>
          <input class="input keycap" id="cKeyHide" readonly value="${esc(c.hotkey_hide || 'Alt+F1')}"></div>
        <div class="key-box"><span class="key-label">还原窗口</span>
          <input class="input keycap" id="cKeyShow" readonly value="${esc(c.hotkey_show || 'Alt+F2')}"></div>
      </div>
      <p class="hint">点进输入框后直接按组合键录入（Ctrl / Alt / Shift / Win + 任意键）。录入期间全局热键临时停用，录完自动恢复。</p>
    </div>
    <div class="field">
      <label>代理地址（如 127.0.0.1:7890）</label>
      <div class="proxy-row">
        <input class="input" id="cProxy" value="${esc(c.proxy)}">
        <label class="check"><input type="checkbox" id="cSysProxy" ${c.follow_system_proxy !== false ? 'checked' : ''}>
          未填时跟随系统代理</label>
      </div>
    </div>
    <div class="field"><label>Cookie（可选，抖音风控时填写）</label><textarea id="cCookie">${esc(c.cookie)}</textarea></div>
    <div class="field-row check-row">
      <label class="check"><input type="checkbox" id="cAutoMp4" ${c.auto_mp4 ? 'checked' : ''}> 录制结束自动转 MP4</label>
      <label class="check"><input type="checkbox" id="cKeepTs" ${c.keep_ts ? 'checked' : ''}> 转换后保留原始 .ts</label>
    </div>
    <div class="d-section danger-zone">
      <label class="d-label">移除直播间（按名称，支持正则）</label>
      <div class="remove-re">
        <input class="input" id="cRemoveRe" placeholder="例如：账号已注销 或 ^已注销|^已封禁$">
        <button class="btn small ghost danger-text" id="cRemoveGo">移除</button>
      </div>
      <p class="remove-re-count" id="cRemoveCount"></p>
      <p class="hint">按主播显示名匹配；支持正则（区分大小写由正则本身决定，不区分可用 (?i)）。只会移除监测项，已录制的文件不受影响。</p>
    </div>
    <div class="d-section danger-zone">
      <label class="d-label">危险操作</label>
      <button class="btn small ghost danger-text" id="cClearRooms">清空直播间列表</button>
      <span class="hint" style="margin-left:8px">移除全部直播间，已录制的文件不受影响。</span>
    </div>`,
  '保存', async () => {
    const num = (id, d) => Math.max(0, parseInt($('#'+id).value, 10) || d);
    const keyHide = $('#cKeyHide').value.trim(), keyShow = $('#cKeyShow').value.trim();
    if (keyHide && keyHide === keyShow) { toast('隐藏和还原不能用同一个组合键'); return; }
    let res;
    try {
      res = await post('/api/config', {
        interval: Math.max(10, num('cInterval', 180)),
        concurrency: Math.max(1, num('cConcurrency', 4)),
        jitter_ratio: num('cJitter', 4),
        segment_minutes: Math.min(720, num('cSegment', 0)),
        format: $('#cFormat').value,
        quality: $('#cQuality').value,
        save_dir: $('#cSaveDir').value.trim() || 'downloads',
        proxy: $('#cProxy').value.trim(),
        follow_system_proxy: $('#cSysProxy').checked,
        cookie: $('#cCookie').value.trim(),
        auto_mp4: $('#cAutoMp4').checked,
        keep_ts: $('#cKeepTs').checked,
        hotkey_hide: keyHide,
        hotkey_show: keyShow,
      });
    } catch (e) { toast(e.message); return; }
    closeModal();
    const hk = res.hotkeys;
    if (hk && hk.hide && hk.hide !== keyHide) {
      toast(`隐藏键被占用，改用 ${hk.hide}`);
    } else if (hk && hk.show && hk.show !== keyShow) {
      toast(`还原键被占用，改用 ${hk.show}`);
    } else {
      toast('设置已保存');
    }
    refresh();
  });

  $('#cBrowse').onclick = async () => {
    // 优先系统原生文件夹选择器（和 StreamCap 的 FilePicker.get_directory_path 一个思路），
    // 自带盘符、网络路径、新建文件夹；拿不到再退回内置目录树，保证任何环境都能选。
    try {
      const r = await api('/api/pick-dir');
      if (r && r.ok && r.path) {
        $('#cSaveDir').value = r.path;
        $('#cSaveHint').innerHTML = `当前位置：<code>${esc(r.path)}</code><br>点「保存」后对新录制生效。`;
        return;
      }
      if (r && r.cancel) return;          // 用户点了取消，静默返回
    } catch (e) { /* 系统对话框不可用 → 回退 */ }
    folderPicker(s.save_path || '', (picked) => {
      $('#cSaveDir').value = picked;
      $('#cSaveHint').innerHTML = `当前位置：<code>${esc(picked)}</code><br>点「保存」后对新录制生效。`;
    });
  };
  $('#cOpenDir').onclick = async () => {
    try { await post('/api/open-folder'); } catch (e) { toast(e.message); }
  };
  ['#cKeyHide', '#cKeyShow'].forEach(sel => bindHotkeyInput($(sel)));
  // 设置弹窗收窄 + 去掉右上角 ×
  document.querySelector('.modal-card').classList.add('settings');
  // 按名称/正则移除：输入时实时预览匹配数，点移除弹一次确认批量删除
  const countEl = $('#cRemoveCount');
  const refreshRemoveCount = () => {
    const pat = $('#cRemoveRe').value.trim();
    if (!pat) { countEl.innerHTML = ''; return; }
    let re;
    try { re = new RegExp(pat); }
    catch (e) { countEl.innerHTML = `<span class="warn">正则无效：${esc(e.message)}</span>`; return; }
    const names = (lastState?.rooms || []).map(nameOf);
    const n = names.filter(n => re.test(n)).length;
    countEl.innerHTML = n > 0
      ? `匹配 <b>${n}</b> 个直播间，点「移除」将批量删除。`
      : '没有匹配到任何直播间。';
  };
  $('#cRemoveRe').addEventListener('input', refreshRemoveCount);
  $('#cRemoveGo').onclick = () => {
    const pat = $('#cRemoveRe').value.trim();
    if (!pat) { toast('先输入要匹配的名称或正则'); return; }
    let re;
    try { re = new RegExp(pat); }
    catch (e) { toast('正则无效：' + e.message); return; }
    const ids = (lastState?.rooms || []).filter(r => re.test(nameOf(r))).map(r => r.id);
    if (!ids.length) { toast('没有匹配到任何直播间'); return; }
    confirmDialog({
      title: '移除直播间',
      text: `按「<b>${esc(pat)}</b>」匹配到 <b>${ids.length}</b> 个直播间，将全部移除。<br><span class="muted">只会移除监测项，已录制的文件不受影响。此操作不可撤销。</span>`,
      okText: `移除 ${ids.length} 个`,
      onOk: async () => {
        const r = await post('/api/rooms/remove-matching', {pattern: pat});
        closeModal(); toast(`已移除 ${r.removed} 个直播间`);
        lastRoomSig = ''; refresh();
      }
    });
  };
  $('#cClearRooms').onclick = () => confirmDialog({
    title: '清空直播间列表',
    text: '将移除<b>全部</b>直播间，已录制的文件不受影响。<br><span class="muted">此操作不可撤销。</span>',
    okText: '确认清空',
    onOk: async () => {
      const r = await post('/api/rooms/clear');
      closeModal(); toast(`已移除 ${r.removed} 个房间`);
      lastRoomSig = ''; refresh();
    }
  });
};

/* 悬停暂停：鼠标在表格上时暂停整表重建（否则行会从鼠标底下跳走，点不中），
   移开后立即补一次重建 */
const _tableWrap = document.querySelector('.table-wrap');
if (_tableWrap) {
  _tableWrap.addEventListener('mouseenter', () => { hoverPause = true; });
  _tableWrap.addEventListener('mouseleave', () => {
    hoverPause = false;
    if (pendingRebuild && lastState) {
      lastRebuild = 0;
      render(lastState);
    }
  });
}

setInterval(refresh, 1500);
refresh();

/* 深链：?open=<房间id> 直接打开详情，方便定位与分享 */
const _openId = new URLSearchParams(location.search).get('open');
if (_openId) {
  const timer = setInterval(() => {
    if (lastState && findRoom(_openId)) { clearInterval(timer); openDrawer(_openId); }
  }, 300);
  setTimeout(() => clearInterval(timer), 12000);
}

/* 深链：#import / #settings 直接打开对应弹窗（快捷方式与自动化用得上） */
if (location.hash === '#import') $('#btnImport').click();
else if (location.hash === '#settings') $('#btnSettings').click();

/* 滚动条显形：鼠标靠近滚动容器的右/下边缘（24px 内）才淡入；滚动时短暂显示后自动隐去 */
(function () {
  const SEL = '.table-wrap, .drawer-body, .logs';
  const EDGE = 24;
  const flash = (el) => {
    el.classList.add('sb-on');
    clearTimeout(el._sbHide);
    el._sbHide = setTimeout(() => el.classList.remove('sb-on'), 900);
  };
  document.addEventListener('mousemove', (e) => {
    document.querySelectorAll(SEL).forEach((el) => {
      const r = el.getBoundingClientRect();
      if (!r.width) return;
      const near = (e.clientX >= r.right - EDGE && e.clientX <= r.right + 4) ||
                   (e.clientY >= r.bottom - EDGE && e.clientY <= r.bottom + 4);
      if (near) {
        el.classList.add('sb-on');
        clearTimeout(el._sbHide);
        el._sbHide = null;
      } else if (!el._sbHide) {
        el._sbHide = setTimeout(() => el.classList.remove('sb-on'), 300);
      }
    });
  }, { passive: true });
  // 滚动事件不冒泡，用捕获阶段兜住所有滚动容器
  document.addEventListener('scroll', (e) => {
    const el = e.target && e.target.closest ? e.target.closest(SEL) : null;
    if (el) flash(el);
  }, true);
})();

/* 双列长名字滚动：只对「鼠标停留」的那一个算位移。
   全量遍历读 scrollWidth 会触发上千张卡片重排（实测 1.3s），正是双列卡死的根因。 */
function markOverflowName(el) {
  if (!el || el.dataset.mx === '1') return;
  el.dataset.mx = '1';
  // 文字块自身带 max-width:100% 用于省略号，所以溢出量要量内层文字的 scrollWidth
  const txt = el.querySelector('.nm-txt') || el;
  const shift = txt.scrollWidth - txt.clientWidth;
  if (shift > 4) {
    el.classList.add('nm-overflow');
    el.style.setProperty('--nm-shift', shift + 'px');
  }
}
let nameHoverTimer;
$('#roomRows').addEventListener('mouseover', (e) => {
  const nm = e.target.closest && e.target.closest('.nm-click');
  if (!nm || !$('.table-wrap').classList.contains('two-col')) return;
  clearTimeout(nameHoverTimer);
  // 停留 140ms 才测：快速扫过列表时不触发任何布局
  nameHoverTimer = setTimeout(() => markOverflowName(nm), 140);
});
