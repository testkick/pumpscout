const $ = id => document.getElementById(id);
const api = (p,o) => fetch(p,{credentials:"same-origin",...o}).then(r=>{if(!r.ok)throw r;return r.json()});
const fmt$ = v => (v>=0?"+":"")+"$"+Math.abs(v).toFixed(2);
const ago = ts => {const s=Math.round(Date.now()/1000-ts); if(s<60)return s+"s ago"; if(s<3600)return Math.round(s/60)+"m ago"; return Math.round(s/3600)+"h ago";};
const esc = s => (s||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

// ── login ──
$("login-btn").onclick = async () => {
  try { await api("/api/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({password:$("pw").value})});
    $("login").classList.add("hidden"); $("app").classList.remove("hidden"); start();
  } catch { $("login-err").textContent="Wrong password"; }
};
$("pw").addEventListener("keydown",e=>{if(e.key==="Enter")$("login-btn").click()});

// ── tabs ──
document.querySelectorAll(".tab").forEach(t=>t.onclick=()=>{
  document.querySelectorAll(".tab").forEach(x=>x.classList.toggle("on",x===t));
  document.querySelectorAll(".panel").forEach(p=>p.classList.add("hidden"));
  $("tab-"+t.dataset.tab).classList.remove("hidden");
  if(t.dataset.tab==="performance") loadPerformance();
});

// ── module cards (the 12) ──
const MODULES = [
  ["1","PUMP SCOUT", d=>d.last_scout_result?.fresh ?? "—", d=>"new tokens last sweep"],
  ["2","BOUNCE FINDER", d=>d.bounce ?? "—", d=>"coming off lows"],
  ["3","WHALE WATCH", d=>d.counts?.watch_wallets ?? "—", d=>"wallets tracked"],
  ["4","KOL WATCH", d=>d.kol ?? "—", d=>"KOL buys / 5min"],
  ["5","HOLDER FLOW", d=>d.holders ?? "—", d=>"new holders + pressure"],
  ["6","RUG GATE", d=>d.rug_blocked ?? "—", d=>"devs blocked"],
  ["7","BUNDLE HUNTER", d=>d.bundles ?? "—", d=>"bundled clusters caught"],
  ["8","SIZER", d=>(d.equity_frac*100||1.5).toFixed(1)+"%", d=>"of equity per trade"],
  ["9","EXECUTOR", d=>d.last_scout_result?.bought ?? "—", d=>"fills last sweep"],
  ["10","TP LADDER", d=>"2x / 3x", d=>"take-profit targets"],
  ["11","STOP GUARD", d=>"-12% / 25%", d=>"hard stop / trail"],
  ["12","SCRIBE", d=>d.counts?.candidates ?? "—", d=>"decisions logged"],
];

async function refresh(){
  let h,perf,cands;
  try { h = await api("/healthz"); } catch { $("dot").className="dot bad"; return; }
  try { perf = await api("/api/performance"); } catch {}
  try { cands = await api("/api/candidates?limit=1"); } catch {}
  // topbar
  $("mode-pill").textContent = h.dry_run?"DRY RUN":"LIVE";
  $("mode-pill").className = "pill"+(h.dry_run?"":" live");
  $("last-scout").textContent = h.last_scout_secs_ago!=null? h.last_scout_secs_ago+"s ago":"—";
  $("equity").textContent = "$"+(h.equity??0).toFixed(0);
  $("open-n").textContent = h.counts?.open_positions ?? "—";
  $("realized").textContent = perf? fmt$(perf.realized):"—";
  const healthy = h.scheduler_alive_secs_ago!=null && h.scheduler_alive_secs_ago<30;
  $("dot").className = "dot "+(healthy?"ok":"bad");
  // aggregate signals for the cards from recent candidates
  const agg = await aggSignals();
  const data = {...h, ...agg, equity_frac:(await settingsCache).equity_frac_per_trade};
  $("modgrid").innerHTML = MODULES.map(([n,name,v,s])=>{
    let val,sub; try{val=v(data)}catch{val="—"} try{sub=s(data)}catch{sub=""}
    return `<div class="modcard ${name==='RUG GATE'||name==='BUNDLE HUNTER'?'warn':'ok'}">
      <div class="num">MODULE ${n}</div><h3>${name}</h3>
      <div class="val">${val}</div><div class="sub">${sub}</div></div>`;
  }).join("");
}

let settingsCache = api("/api/settings").then(r=>r.settings).catch(()=>({}));
async function aggSignals(){
  try{
    const {candidates} = await api("/api/candidates?limit=60");
    const recent = candidates.filter(c=>Date.now()/1000-c.ts<900);
    return {
      bounce: recent.filter(c=>c.bounce).length,
      kol: recent.reduce((a,c)=>a+(c.kol_buys||0),0),
      holders: recent.reduce((a,c)=>a+(c.new_holders||0),0),
      rug_blocked: recent.filter(c=>!c.rug_pass).length,
      bundles: recent.reduce((a,c)=>a+(c.bundle_clusters||0),0),
    };
  }catch{return {}}
}

async function loadCandidates(){
  const {candidates} = await api("/api/candidates?limit=120");
  $("cand-body").innerHTML = candidates.map(c=>`<tr>
    <td class="mut">${ago(c.ts)}</td><td>${esc(c.symbol||c.mint.slice(0,8))}</td>
    <td>${c.bounce?"✓":"·"}</td><td>${c.whale_buys||0}</td><td>${c.kol_buys||0}</td>
    <td>${c.new_holders||0}</td><td>${(c.buy_pressure||0).toFixed(2)}</td>
    <td class="${c.rug_pass?'pos':'neg'}">${c.rug_pass?"pass":"FAIL"}</td>
    <td>${c.bundle_clusters||0}</td><td><b>${(c.score||0).toFixed(1)}</b></td>
    <td><span class="tag ${c.decision}">${c.decision}</span></td></tr>`).join("");
}
async function loadPositions(){
  const {positions} = await api("/api/positions");
  $("pos-body").innerHTML = positions.map(p=>{
    const pnl=p.pnl||0, tp=(p.tp1_done?"①":"·")+(p.tp2_done?"②":"·");
    return `<tr><td class="mut">${ago(p.ts)}</td><td>${esc(p.symbol||p.mint.slice(0,8))}</td>
    <td>$${(p.usd||0).toFixed(0)}</td><td>${(p.entry_price||0).toPrecision(3)}</td>
    <td>${(p.last_price||0).toPrecision(3)}</td><td>${(p.peak_price||0).toPrecision(3)}</td>
    <td>${tp}</td><td class="${pnl>=0?'pos':'neg'}">${fmt$(pnl)}</td>
    <td class="${p.status==='open'?'':'mut'}">${p.status}${p.close_reason?' · '+p.close_reason:''}</td></tr>`;
  }).join("");
}
async function loadScribe(){
  const {scribe} = await api("/api/scribe?limit=200");
  $("scribe-list").innerHTML = scribe.map(s=>`<div class="row">
    <span class="t">${ago(s.ts)}</span><span class="m">${esc(s.module)}</span>
    <span class="${s.level}">${esc(s.message)}</span></div>`).join("");
}
async function loadWallets(){
  const {wallets} = await api("/api/wallets");
  $("wallet-list").innerHTML = `<div class="hint">${wallets.length} wallets tracked</div>`+
    wallets.map(w=>`<div class="row"><span class="m">${esc(w.kind)}</span><span>${esc(w.label||w.address.slice(0,8))}</span><span class="t">${esc(w.address.slice(0,16))}…</span></div>`).join("");
}
$("wallet-discover").onclick = async ()=>{
  $("wallet-note").textContent = "discovering top-PnL wallets…";
  try{ const r = await api("/api/wallets/discover",{method:"POST"});
    $("wallet-note").textContent = r.discovered? `discovered ${r.discovered} wallets`:(r.error||"none found");
    loadWallets();
  }catch{ $("wallet-note").textContent="discover failed"; }
};
$("wallet-save").onclick = async ()=>{
  const rows = $("wallet-input").value.trim().split("\n").filter(Boolean).map(l=>{
    const [address,label,kind]=l.split(",").map(x=>x&&x.trim());
    return {address,label:label||"",kind:kind||"whale"};
  });
  await api("/api/wallets/import",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({wallets:rows})});
  $("wallet-note").textContent = `imported ${rows.length}`; loadWallets();
};

async function loadPerformance(){
  let p; try{ p = await api("/api/performance"); }catch{ return; }
  const wr = p.win_rate==null?"—":p.win_rate+"%";
  $("perf-stats").innerHTML = [
    ["EQUITY","$"+p.equity.toFixed(0), p.equity>=p.starting_equity?"pos":"neg"],
    ["REALIZED",fmt$(p.realized), p.realized>=0?"pos":"neg"],
    ["WIN RATE",wr,""],
    ["CLOSED",p.closed,""],
    ["OPEN",p.open,""],
    ["AVG WIN",fmt$(p.avg_win),"pos"],
    ["AVG LOSS",fmt$(p.avg_loss),"neg"],
    ["BEST",fmt$(p.best),"pos"],
    ["WORST",fmt$(p.worst),"neg"],
  ].map(([l,v,c])=>`<div class="stile"><label>${l}</label><b class="${c}">${v}</b></div>`).join("");

  // equity curve as inline SVG
  drawEquity(p.snapshots, p.starting_equity);

  // exit reasons bars
  const cr = p.close_reasons||{}; const crTot = Object.values(cr).reduce((a,b)=>a+b,0)||1;
  const crColor = r => r.includes("TP")?"good":r.includes("hard")?"bad":r.includes("trail")?"warn":"";
  $("close-reasons").innerHTML = Object.keys(cr).length?
    Object.entries(cr).sort((a,b)=>b[1]-a[1]).map(([r,n])=>`<div class="bar">
      <span class="lab">${esc(r)}</span><span class="track"><span class="fill ${crColor(r)}" style="width:${n/crTot*100}%"></span></span>
      <span class="n">${n}</span></div>`).join("")
    : '<div class="hint">No closed positions yet.</div>';

  // funnel
  const f = p.funnel||{}; const top = Math.max(f.scouted||1,1);
  const stages = [["Scouted",f.scouted,""],["Evaluated",f.evaluated,""],
    ["Rug-blocked",f.rug_blocked,"bad"],["Bundle-blocked",f.bundle_blocked,"bad"],["Bought",f.bought,"good"]];
  $("funnel").innerHTML = stages.map(([l,n,c])=>`<div class="bar">
    <span class="lab">${l}</span><span class="track"><span class="fill ${c}" style="width:${(n||0)/top*100}%"></span></span>
    <span class="n">${n||0}</span></div>`).join("");
}

function drawEquity(snaps, start){
  const host=$("equity-chart");
  if(!snaps||snaps.length<2){host.innerHTML='<div class="hint">Equity curve builds as snapshots accumulate (~20s cadence).</div>';return;}
  const W=900,H=240,pad=36;
  const xs=snaps.map(s=>s.ts), ys=snaps.map(s=>s.equity);
  const x0=Math.min(...xs),x1=Math.max(...xs),y0=Math.min(...ys,start),y1=Math.max(...ys,start);
  const yr=(y1-y0)||1, xr=(x1-x0)||1;
  const px=t=>pad+(t-x0)/xr*(W-2*pad), py=v=>H-pad-(v-y0)/yr*(H-2*pad);
  const pts=snaps.map(s=>`${px(s.ts).toFixed(1)},${py(s.equity).toFixed(1)}`).join(" ");
  const baseY=py(start);
  const up = ys[ys.length-1]>=start;
  host.innerHTML=`<svg viewBox="0 0 ${W} ${H}" width="100%" preserveAspectRatio="none" style="max-height:260px">
    <line x1="${pad}" y1="${baseY}" x2="${W-pad}" y2="${baseY}" stroke-dasharray="4 4" stroke="#334" />
    <polyline points="${pts}" fill="none" stroke="${up?'#5ac8a0':'#e06a6a'}" stroke-width="2"/>
    <text x="${pad}" y="14" fill="#7d8da3" font-size="11">$${y1.toFixed(0)}</text>
    <text x="${pad}" y="${H-6}" fill="#7d8da3" font-size="11">$${y0.toFixed(0)}</text>
    <text x="${W-pad-60}" y="14" fill="#7d8da3" font-size="11">start $${start.toFixed(0)}</text>
  </svg>`;
}

function start(){
  refresh(); loadCandidates(); loadPositions(); loadScribe(); loadWallets();
  setInterval(refresh, 8000);
  setInterval(()=>{loadCandidates();loadPositions();loadScribe();}, 12000);
}
// auto-enter if already authed
api("/api/settings").then(()=>{$("login").classList.add("hidden");$("app").classList.remove("hidden");start();}).catch(()=>{});
