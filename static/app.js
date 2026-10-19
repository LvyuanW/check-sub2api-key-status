'use strict';
const form = document.getElementById('lookup');
const keyInput = document.getElementById('key');
const message = document.getElementById('message');
const result = document.getElementById('result');
const rows = document.getElementById('rows');
const windows = document.getElementById('windows');
const windowRows = document.getElementById('window-rows');
const submit = document.getElementById('submit');
let activeRequest = null;
const statuses = {active: '正常', disabled: '停用', expired: '已过期', quota_exhausted: '额度耗尽',
  inactive: '未启用', banned: '封禁', suspended: '暂停', missing: '不存在', standard: '钱包计费', subscription: '订阅计费'};
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
  rows.replaceChildren(); windowRows.replaceChildren();
  row('查询时间', time(data.checked_at));
  row('Key 有效状态（时间 / 总额度）', status(data.key_effective_status));
  row('Key 数据库状态', status(data.key_status));
  row('所属用户状态', status(data.user_status));
  row('分组', data.group_name || '未绑定 / 不存在');
  row('分组状态', status(data.group_status));
  row('计费方式', status(data.billing_mode));
  row('用户共享钱包余额 / USD', money(data.wallet_balance_usd));
  row('用户共享冻结金额 / USD', money(data.wallet_frozen_usd));
  row('Key 总额度 / USD', money(data.key_quota_limit_usd));
  row('Key 额度计数已用 / USD', money(data.key_quota_used_usd));
  row('Key 剩余额度 / USD', money(data.key_quota_remaining_usd));
  row('Key 过期时间', data.expires_at ? time(data.expires_at) : '永不过期');
  row('最后使用时间（约 30 秒防抖）', data.last_used_at ? time(data.last_used_at) : '暂无记录');
  row('累计已使用额度（保留记录）/ USD', money(data.usage.billed_usd));
  row('累计请求数（保留记录）', count(data.usage.requests));
  row('已使用 Token 总数', count(data.usage.total_tokens));
  row('输入 Token', count(data.usage.input_tokens));
  row('输出 Token', count(data.usage.output_tokens));
  row('缓存创建 Token', count(data.usage.cache_creation_tokens));
  row('缓存读取 Token', count(data.usage.cache_read_tokens));
  row('此 Key 当前并发数', data.concurrency_available ? count(data.current_concurrency) : '暂不可用（Redis 读取失败）');
  row('所属用户并发上限（全部 Key 共享）', data.user_concurrency_limit > 0 ? count(data.user_concurrency_limit) : '不限制');
  if (data.subscription) {
    row('订阅状态', status(data.subscription.status));
    row('订阅过期时间', time(data.subscription.expires_at));
    row('订阅本期最小剩余额度 / USD', money(data.subscription.remaining_usd));
  }
  const items = data.rate_windows.map(w => ({...w, label: 'Key / ' + w.window}));
  if (data.subscription) {
    const labels = {daily: '日', weekly: '周', monthly: '30 天'};
    items.push(...data.subscription.windows.map(w => ({...w, label: '订阅 / ' + labels[w.window]})));
  }
  for (const item of items) {
    const tr = document.createElement('tr');
    for (const text of [item.label, money(item.limit_usd), money(item.used_usd), money(item.remaining_usd), time(item.resets_at)]) {
      const td = document.createElement('td'); td.textContent = text; tr.append(td);
    }
    windowRows.append(tr);
  }
  windows.hidden = items.length === 0;
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
  rows.replaceChildren(); windowRows.replaceChildren(); result.hidden = true;
  message.textContent = '已清空。'; keyInput.focus();
});
window.addEventListener('pagehide', () => { keyInput.value = ''; if (activeRequest) activeRequest.abort(); });
