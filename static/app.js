'use strict';
const form = document.getElementById('lookup');
const keyInput = document.getElementById('key');
const message = document.getElementById('message');
const result = document.getElementById('result');
const rows = document.getElementById('rows');
const submit = document.getElementById('submit');
let activeRequest = null;
const statuses = {active: '正常', disabled: '停用', expired: '已过期', quota_exhausted: '额度耗尽',
  inactive: '未启用', banned: '封禁', suspended: '暂停', missing: '不存在'};
const status = value => value == null ? '未绑定分组' : (statuses[value] || value);
const time = value => value == null ? '—' : new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23'
}).format(new Date(value));
const money = value => value == null ? '不限制' : String(value);
const count = value => new Intl.NumberFormat('zh-CN').format(value);
function row(label, value) {
  const tr = document.createElement('tr');
  const th = document.createElement('th');
  const td = document.createElement('td');
  th.scope = 'row'; th.textContent = label; td.textContent = String(value);
  tr.append(th, td); rows.append(tr);
}
function show(data) {
  rows.replaceChildren();
  row('查询时间', time(data.checked_at));
  row('Key 有效状态', status(data.key_effective_status));
  row('分组', data.group_name || '未绑定 / 不存在');
  row('余额 / ¥', money(data.wallet_balance_usd));
  row('已使用额度 / ¥', money(data.usage.billed_usd));
  row('Key 过期时间', data.expires_at ? time(data.expires_at) : '永不过期');
  row('最后使用时间（约 30 秒防抖）', data.last_used_at ? time(data.last_used_at) : '暂无记录');
  row('累计请求数', count(data.usage.requests));
  row('已使用 Token 总数', count(data.usage.total_tokens));
  row('输入 Token', count(data.usage.input_tokens));
  row('输出 Token', count(data.usage.output_tokens));
  row('缓存创建 Token', count(data.usage.cache_creation_tokens));
  row('缓存读取 Token', count(data.usage.cache_read_tokens));
  row('当前并发数', data.concurrency_available ? count(data.current_concurrency) : '暂不可用（Redis 读取失败）');
  row('并发上限', data.user_concurrency_limit > 0 ? count(data.user_concurrency_limit) : '不限制');
  result.hidden = false;
}
form.addEventListener('submit', async event => {
  event.preventDefault();
  if (activeRequest) return;
  const key = keyInput.value.trim();
  if (!key) { message.textContent = '请输入 API Key。'; return; }
  result.hidden = true; message.textContent = '查询中…'; submit.disabled = true;
  const controller = new AbortController(); activeRequest = controller;
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch('/api/check', {method: 'POST', credentials: 'omit', cache: 'no-store',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify({key}), signal: controller.signal});
    const data = await response.json();
    if (!response.ok) { message.textContent = data.error || '查询失败。'; return; }
    show(data); message.textContent = '查询完成。';
  } catch (error) {
    if (activeRequest === controller) message.textContent = error.name === 'AbortError' ? '请求超时，请重试。' : '连接失败，请稍后重试。';
  } finally {
    clearTimeout(timeout);
    if (activeRequest === controller) { activeRequest = null; submit.disabled = false; }
  }
});
document.getElementById('clear').addEventListener('click', () => {
  if (activeRequest) activeRequest.abort();
  activeRequest = null; submit.disabled = false; keyInput.value = '';
  rows.replaceChildren(); result.hidden = true;
  message.textContent = '已清空。'; keyInput.focus();
});
window.addEventListener('pagehide', () => { keyInput.value = ''; if (activeRequest) activeRequest.abort(); });
