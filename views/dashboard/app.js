const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
const $ = (id) => document.getElementById(id);

function unwrap(res) {
  if (res == null) return null;
  let v = res;
  if (v && typeof v === "object" && "code" in v && "data" in v) v = v.data;
  if (v && typeof v === "object" && "data" in v && v.code === undefined) v = v.data;
  return v;
}

async function get(path) {
  if (!bridge) return null;
  try { return unwrap(await bridge.apiGet(path)); } catch (e) { console.warn(e); return null; }
}
async function post(path, body) {
  if (!bridge) return null;
  try { return unwrap(await bridge.apiPost(path, body || {})); } catch (e) { console.warn(e); return null; }
}

function money(v) {
  const n = Number(v || 0);
  return "¥" + n.toFixed(2);
}

async function loadStatus() {
  const d = await get("status");
  if (!d) { $("balance").textContent = "读取失败"; return; }
  $("balance").textContent = money(d.balance);
  $("savings").textContent = money(d.savings);
  $("owed").textContent = d.owed_to_user ? money(d.owed_to_user) : "无";
  $("spent").textContent = money(d.month_spent);
  $("earned").textContent = money(d.month_income);
  $("city").textContent = (d.city || "未配置") + " · " + d.city_level;
  $("levelPill").textContent = d.own_house ? "有房" : "租房";
  $("fixedTotal").textContent = money(d.fixed_total) + "/月";
  const a = Number(d.anxiety || 0);
  $("anxietyHint").textContent = a >= 0.7 ? "她现在有点为钱发愁（" + a.toFixed(2) + "）"
    : a >= 0.3 ? "还算稳（" + a.toFixed(2) + "）" : "没什么压力";

  $("fixed").innerHTML = (d.fixed || []).map((i) =>
    '<li><span class="p">' + i.name + '</span><span class="s">' + money(i.amount) + "</span></li>"
  ).join("") || '<li class="empty">无</li>';

  $("own_house").checked = !!d.own_house;
  $("own_car").checked = !!d.own_car;
  $("mortgage").value = d.mortgage != null ? d.mortgage : "";
  $("car_loan").value = d.car_loan != null ? d.car_loan : "";
}

async function loadHistory() {
  const d = await get("history");
  const items = (d && d.items) || [];
  const list = $("history");
  if (!items.length) { list.innerHTML = '<li class="empty">还没有账</li>'; return; }
  list.innerHTML = items.slice().reverse().slice(0, 40).map((it) => {
    const neg = ["expense", "fixed", "repay"].includes(it.kind);
    const cls = it.kind === "expense_failed" ? "bad" : (neg ? "" : "ok");
    const sign = neg ? "-" : "+";
    return '<li><span class="t">' + (it.time || "") + '</span><span class="p">' +
      (it.reason || it.kind) + '</span><span class="s ' + cls + '">' +
      sign + money(it.amount).slice(1) + "</span></li>";
  }).join("");
}

$("saveAssets").addEventListener("click", async () => {
  const body = {
    own_house: $("own_house").checked,
    own_car: $("own_car").checked,
    mortgage: Number($("mortgage").value || 0),
    car_loan: Number($("car_loan").value || 0),
  };
  const res = await post("assets", body);
  $("assetsHint").textContent = res ? "已保存。" : "保存失败。";
  loadStatus();
});

loadStatus();
loadHistory();
setInterval(loadStatus, 30000);
