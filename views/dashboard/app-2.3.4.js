/* AstrBot 把 bridge-sdk.js 注入在 app.js **之后**（实测 56 行是本文件、57 行才是 SDK），
   所以脚本刚执行时 window.AstrBotPluginView 还不存在——只抓一次就会永远抓不到，
   面板会一直「读取失败」。这里每次调用都重新找，并给启动加一个等待桥接的重试。 */
let bridge = null;
function resolveBridge() {
  if (bridge) return bridge;
  const candidates = [];
  try { candidates.push(window.AstrBotPluginView, window.AstrBotPluginPage); } catch (err) {}
  try { candidates.push(window.parent.AstrBotPluginView, window.parent.AstrBotPluginPage); } catch (err) {}
  try { candidates.push(window.top.AstrBotPluginView, window.top.AstrBotPluginPage); } catch (err) {}
  bridge = candidates.find((item) => item && typeof item.apiGet === "function") || null;
  return bridge;
}
function waitForBridge(fn, tries) {
  const left = (typeof tries === "number") ? tries : 50;   // 默认最多等 6 秒
  if (resolveBridge()) { fn(); return; }
  if (left > 0) setTimeout(() => waitForBridge(fn, left - 1), 120);
}
const $ = (id) => document.getElementById(id);

function unwrap(res) {
  if (res == null) return null;
  let v = res;
  if (v && typeof v === "object" && "code" in v && "data" in v) v = v.data;
  if (v && typeof v === "object" && "data" in v && v.code === undefined) v = v.data;
  return v;
}

async function get(path) {
  const b = resolveBridge();
  if (!b) return null;
  try { return unwrap(await b.apiGet(path)); } catch (e) { console.warn(e); return null; }
}
async function post(path, body) {
  const b = resolveBridge();
  if (!b) return null;
  try { return unwrap(await b.apiPost(path, body || {})); } catch (e) { console.warn(e); return null; }
}

function money(v) {
  const n = Number(v || 0);
  return "¥" + n.toFixed(2);
}

const BANKRUPT_TEXT = {
  losing: "她最近亏得有点多，嘴上还硬，心里已经开始发慌了。",
  ruined: "她已经亏到快破产了，会来找你哭「老公我好像有点破产了」。",
  broke: "她卡里基本没钱了，会来找你哭「老公我好像有点破产了」。",
};

async function loadStatus() {
  const d = await get("status");
  if (!d) { $("balance").textContent = "读取失败"; return; }
  if (d.link) amoLinkRender(d.link);
  $("balance").textContent = money(d.balance);
  $("savings").textContent = money(d.savings);
  $("owed").textContent = d.owed_to_user ? money(d.owed_to_user) : "无";
  $("savingsRate").textContent = d.savings_rate ? ("年化 " + d.savings_rate + "%") : "不计息";
  $("spent").textContent = money(d.month_spent);
  $("earned").textContent = money(d.month_income);
  $("city").textContent = (d.city || "未配置") + " · " + d.city_level;
  const pay = d.salary || {};
  $("salaryGross").textContent = money(pay.gross);
  $("salaryNet").textContent = money(pay.net);
  $("salaryDay").textContent = pay.pay_day ? ("每月 " + pay.pay_day + " 号") : "—";
  $("salarySource").textContent = pay.source || "—";
  const houseKind = String(d.house_kind || "");
  const kindText = { own: "全款房", loan: "贷款房", rent: "租房" }[houseKind] || (d.own_house ? "全款房" : "租房");
  $("levelPill").textContent = kindText;
  const houseDesc = $("houseDesc");
  if (houseDesc) houseDesc.textContent = d.house_desc || "—";
  const carDesc = $("carDesc");
  if (carDesc) carDesc.textContent = d.car_desc || "—";
  $("fixedTotal").textContent = money(d.fixed_total) + "/月";
  const a = Number(d.anxiety || 0);
  $("anxietyHint").textContent = a >= 0.7 ? "她现在有点为钱发愁（" + a.toFixed(2) + "）"
    : a >= 0.3 ? "还算稳（" + a.toFixed(2) + "）" : "没什么压力";

  const bor = d.borrow || {};
  if ($("borrowHint")) {
    const owed = Number(bor.owed || 0);
    $("borrowHint").textContent = owed > 0
      ? ("她欠你 " + money(owed) + "；今天转账还可借 " + money(bor.transfer_left || 0) +
         "，红包剩 " + money(bor.red_left || 0))
      : ("私聊里发「[转账]500 元」或「[红包]200 元」（可带 USD / HKD）才算借款；转账每天最多一万，红包单个最多 200、累计一万。");
  }

  const inv = d.invest || {};
  const pnl = Number(inv.total_pnl || 0);
  const dayPnl = Number(inv.day_pnl || 0);
  $("investValue").textContent = money(inv.total_value);
  $("investPnl").textContent = (pnl >= 0 ? "+" : "") + money(pnl);
  $("investDay").textContent = (dayPnl >= 0 ? "+" : "") + money(dayPnl);
  $("investCash").textContent = money(inv.cash);
  $("investPill").textContent = (inv.aggressive ? "FX 战士 · " : "") + (inv.count || 0) + " 只持仓";
  const CUR = { CNY: "¥", USD: "$", HKD: "HK$" };
  $("positions").innerHTML = (inv.positions || []).map((i) => {
    const cur = CUR[i.currency] || "¥";
    const native = (i.currency && i.currency !== "CNY")
      ? " · " + cur + Number(i.last || 0).toFixed(2)
      : "";
    return '<li><span class="p">' + (i.name || i.symbol) + " · " + i.qty + " 股" + native + "</span>" +
      '<span class="s">' + money(i.market_value) + "（" +
      (i.pnl >= 0 ? "+" : "") + i.pnl_pct.toFixed(1) + "%）</span></li>";
  }).join("") || '<li class="empty">还没有持仓，等她自己去买</li>';

  const bk = inv.bankrupt || {};
  const bkText = BANKRUPT_TEXT[bk.level] || "";
  $("investMeta").textContent = (bkText ? bkText + " " : "") +
    "模拟盘：行情来自腾讯公开接口。" +
    (inv.aggressive ? ("她现在最多拿 " + Math.round(Number(inv.max_weight || 0.6) * 100) +
      "% 的身家压一只股票、最多 " + (inv.max_positions || 6) + " 只。") : "");

  $("fixed").innerHTML = (d.fixed || []).map((i) =>
    '<li><span class="p">' + i.name + '</span><span class="s">' + money(i.amount) + "</span></li>"
  ).join("") || '<li class="empty">无</li>';

  $("own_house").checked = !!d.own_house;
  $("own_car").checked = !!d.own_car;
}

async function loadHistory() {
  const d = await get("history");
  const items = (d && d.items) || [];
  const list = $("history");
  if (!items.length) { list.innerHTML = '<li class="empty">还没有账</li>'; return; }
  list.innerHTML = items.slice().reverse().slice(0, 40).map((it) => {
    const neg = ["expense", "fixed", "repay", "stock_buy"].includes(it.kind);
    const cls = it.kind === "expense_failed" ? "bad" : (neg ? "" : "ok");
    const sign = neg ? "-" : "+";
    return '<li><span class="t">' + (it.time || "") + '</span><span class="p">' +
      (it.reason || it.kind) + '</span><span class="s ' + cls + '">' +
      sign + money(it.amount).slice(1) + "</span></li>";
  }).join("");
}

async function openSubpage() {
  $("subpage").hidden = false;
  const d = await get("status");
  if (d) {
    $("subBalance").textContent = money(d.balance);
    $("subSavings").textContent = money(d.savings);
    $("subOwed").textContent = d.owed_to_user ? money(d.owed_to_user) : "无";
    const pay = d.salary || {};
    $("subGross").textContent = money(pay.gross);
    $("subNet").textContent = money(pay.net);
  }
  if (d) {
    const inv = d.invest || {};
    const pnl = Number(inv.total_pnl || 0);
    const day = Number(inv.day_pnl || 0);
    $("subInvestValue").textContent = money(inv.total_value);
    $("subInvestPnl").textContent = (pnl >= 0 ? "+" : "") + money(pnl);
    $("subInvestDay").textContent = (day >= 0 ? "+" : "") + money(day);
  }
  const m = await get("market");
  const prices = (m && m.prices) || {};
  const names = Object.keys(prices).sort();
  $("subMarket").innerHTML = names.length ? names.map((name) =>
    '<li><span class="p">' + name + '</span><span class="s">¥' +
    Number(prices[name] || 0).toFixed(2) + '</span></li>'
  ).join("") : '<li class="empty">菜价读取失败</li>';
  $("subMarketHint").textContent = m
    ? ((m.date || "") + (m.weather ? " · " + m.weather : "")) : "读取失败";

  const f = await get("food/quote");
  const ready = (f && f.ready) || [];
  $("subReady").innerHTML = ready.length ? ready.map((it) =>
    '<li><span class="p">' + (it.name || "") + " · 饱食 " + (it.satiety || 0) + '</span>' +
    '<span class="s">¥' + Number(it.price || 0).toFixed(2) + '</span></li>'
  ).join("") : '<li class="empty">外卖价读取失败</li>';
  $("subReadyHint").textContent = f
    ? ((f.city || "") + (f.level ? " · " + f.level : "")) : "读取失败";
}

$("brandTitle").addEventListener("dblclick", openSubpage);
let _lastBrandTap = 0;
$("brandTitle").addEventListener("touchend", (ev) => {
  const now = Date.now();
  if (now - _lastBrandTap < 360) {
    ev.preventDefault();
    _lastBrandTap = 0;
    openSubpage();
    return;
  }
  _lastBrandTap = now;
});
$("subClose").addEventListener("click", () => { $("subpage").hidden = true; });

$("saveAssets").addEventListener("click", async () => {
  const body = {
    own_house: $("own_house").checked,
    own_car: $("own_car").checked,
  };
  const res = await post("assets", body);
  $("assetsHint").textContent = res ? "已保存。" : "保存失败。";
  loadStatus();
});

waitForBridge(() => {
  loadStatus();
  loadHistory();
});
setInterval(loadStatus, 30000);

/* ---- 连接状态：我这个挂载插件自己有没有连上终端/记忆库 ---- */
function amoLinkDot(ok) { return '<span style="color:' + (ok ? "#2e9e5b" : "#d83b3b") + '">\u25cf</span>'; }
function amoLinkRender(d) {
  const box = document.getElementById("linkbar");
  if (!box) return;
  box.onclick = null;
  if (!d) {
    // 失败要能自己说明白、能点重试，别只留一句「读不到」
    box.innerHTML = '连接状态：读取失败（health 接口没响应，<span style="text-decoration:underline;cursor:pointer">点这里重试</span>）';
    box.onclick = () => amoLinkLoad();
    return;
  }
  const hb = (typeof d.last_heartbeat === "number" && d.last_heartbeat >= 0)
    ? ("心跳 " + d.last_heartbeat + " 秒前") : "从未连上过";
  box.innerHTML = [
    amoLinkDot(d.terminal_connected) + " 终端" + (d.terminal_connected ? " v" + (d.terminal_version || "?") : " 未连接"),
    amoLinkDot(d.data_connected) + " 记忆库" + (d.data_connected ? " v" + (d.data_version || "?") : " 未连接"),
    "bot_id " + (d.bot_id || "\u2014"),
    hb
  ].join(' <span style="opacity:.35">\uff5c</span> ');
  box.title = d.error || "";
}

async function amoLinkFetch() {
  try { return await get("health"); } catch (e) { return null; }
}
async function amoLinkLoad() {
  // 面板脚本比 AstrBot 的桥接先跑，头几次拿不到数据是正常的：
  // 等桥接就绪再重试，别一上来就把「读不到」画给人看。
  const box = document.getElementById("linkbar");
  if (box && /检查中|读取失败/.test(box.textContent || "")) box.textContent = "连接状态：读取中…";
  for (let i = 0; i < 20; i += 1) {
    const d = await amoLinkFetch();
    if (d) { amoLinkRender(d); return; }
    await new Promise((resolve) => setTimeout(resolve, 400));
  }
  amoLinkRender(null);
}

amoLinkLoad();
setInterval(amoLinkLoad, 30000);
