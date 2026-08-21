"""实时监控面板：只读 pm5hft DB，浏览器展示 paper/live 交易状态。

用法:
  python -m pm5hft.dashboard [--port 8090] [--db data/pm5hft.db]

浏览器打开 http://127.0.0.1:8090 （自动 2s 刷新）。
只读查询（不写库），与运行中的 bot 共用 SQLite（WAL 模式，读不阻塞写）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import default_db_url

PAGE = r"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="600">
<title>pm5hft 监控</title>
<style>
:root{--bg:#0b0f14;--card:#131a22;--card2:#0f151c;--border:#223041;--fg:#e8eef5;
--dim:#7f8ea3;--up:#2ecc71;--down:#ff5d5d;--warn:#f0b429;--acc:#4da3ff}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--fg);font:13px/1.55 "Segoe UI",system-ui,sans-serif;
margin:0;min-height:100vh}
.topbar{position:sticky;top:0;z-index:10;background:rgba(11,15,20,.94);
backdrop-filter:blur(8px);border-bottom:1px solid var(--border)}
.tb-in{max-width:1200px;margin:0 auto;padding:10px 18px 8px}
.tb-row1{display:flex;flex-wrap:wrap;align-items:center;gap:12px}
.logo{display:flex;align-items:center;gap:10px}
.logo-mark{width:34px;height:34px;border-radius:9px;background:linear-gradient(135deg,#1c64d9,#35c78f);
display:flex;align-items:center;justify-content:center;font-weight:800;font-size:15px;color:#fff}
.logo h1{font-size:24px;margin:0;font-weight:800;letter-spacing:.3px}
.logo .sub{color:var(--dim);font-size:12px;margin-top:-2px}
.status{display:flex;align-items:center;gap:6px;background:var(--card);border:1px solid var(--border);
border-radius:20px;padding:3px 12px;font-size:12px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--up);animation:pulse 2s infinite}
.dot.warn{background:var(--warn)}.dot.bad{background:var(--down)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.tb-row2{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}
.chip{background:var(--card);border:1px solid var(--border);border-radius:10px;
padding:5px 14px;min-width:96px}
.chip .k{color:var(--dim);font-size:10px;display:block;margin-bottom:1px}
.chip b{font-weight:700;font-size:15px}
.wrap{max-width:1200px;margin:0 auto;padding:14px 18px 60px}
h2{font-size:13px;margin:22px 0 8px;color:var(--acc);font-weight:600}
table{width:100%;border-collapse:separate;border-spacing:0;background:var(--card);
border:1px solid var(--border);border-radius:10px;overflow:hidden}
th,td{padding:5px 10px;text-align:left;border-bottom:1px solid #1a2736;white-space:nowrap}
th{color:var(--dim);font-weight:500;font-size:11px;background:var(--card2)}
tbody tr:hover{background:#182231}
tr.zone td{background:#13291d}
tr.zone td:first-child{box-shadow:inset 3px 0 0 var(--up)}
.up{color:var(--up)}.down{color:var(--down)}.warn{color:var(--warn)}.dim{color:var(--dim)}
.asset{display:inline-block;min-width:38px;text-align:center;border-radius:6px;
padding:1px 6px;font-weight:700;font-size:11px;letter-spacing:.5px}
.a-btc{background:#3b2414;color:#f7931a}.a-eth{background:#1a2033;color:#8ca5ff}
.a-sol{background:#16221d;color:#37d0a4}.a-xrp{background:#20262c;color:#c8d2dc}
.a-doge{background:#2c2412;color:#e5c34b}.a-bnb{background:#2c240d;color:#f3c544}
.a-hype{background:#241b33;color:#b48cff}
.pill{display:inline-block;border-radius:12px;padding:1px 8px;font-size:11px;font-weight:600}
.p-open{background:#132a3a;color:#4da3ff}.p-closed{background:#241f2e;color:#b0a6c8}
.p-win{background:#0f2b1c;color:#2ecc71}.p-loss{background:#32151a;color:#ff5d5d}
.p-unk{background:#262a2f;color:#8b96a5}
.cols{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px}
@media(max-width:1000px){.cols{grid-template-columns:1fr}}
.sec{color:var(--dim);font-size:11px;margin-bottom:4px}
.scroll{max-height:520px;overflow-y:auto;border-radius:10px;background:var(--card);
border:1px solid var(--border)}
.scroll table{border:none;border-radius:0}
.scroll thead th{position:sticky;top:0;z-index:2}
.scroll::-webkit-scrollbar{width:9px}
.scroll::-webkit-scrollbar-track{background:#0d131a}
.scroll::-webkit-scrollbar-thumb{background:#2c3d52;border-radius:6px}
.scroll::-webkit-scrollbar-thumb:hover{background:#3c5170}
	#stale{position:fixed;top:46px;right:14px;z-index:20;color:var(--warn);
	background:#2c2412;border:1px solid #6b5210;border-radius:8px;padding:4px 10px;display:none}
	.controls{margin-left:auto;display:flex;flex-wrap:wrap;align-items:center;gap:9px;background:var(--card);
	border:1px solid var(--border);border-radius:8px;padding:6px 8px}
	.control{display:flex;align-items:center;gap:6px;color:var(--dim);font-size:12px}
	.control input[type=number]{width:82px;background:var(--card2);color:var(--fg);border:1px solid var(--border);
	border-radius:5px;padding:5px 7px;font:inherit}
	.switch{position:relative;width:38px;height:20px}.switch input{opacity:0;width:0;height:0}
	.slider{position:absolute;inset:0;background:#374151;border-radius:12px;cursor:pointer}
	.slider:before{content:"";position:absolute;width:16px;height:16px;left:2px;top:2px;background:#fff;border-radius:50%;transition:.15s}
	.switch input:checked+.slider{background:var(--up)}.switch input:checked+.slider:before{transform:translateX(18px)}
	.btn{border:1px solid #326fba;background:#153a62;color:#dcecff;border-radius:5px;padding:5px 10px;cursor:pointer}
	.btn:disabled{opacity:.5;cursor:wait}#unlockBtn:disabled{opacity:.45;cursor:not-allowed}.ctl-msg{min-width:56px;font-size:11px;color:var(--dim)}
	@media(max-width:760px){.controls{width:100%;margin-left:0;flex-wrap:wrap}}
	</style></head><body>
<div class="topbar"><div class="tb-in">
<div class="tb-row1">
	<div class="logo"><div class="logo-mark">PM</div>
	<div><h1>pm5hft</h1><div class="sub">Polymarket 5m 多资产尾部策略 · <span id="mode">—</span></div></div></div>
	<div class="status"><span class="dot" id="sdot"></span><span id="stxt">连接中…</span></div>
	<div class="controls">
	<label class="control">自动交易 <span class="switch"><input id="autoTrade" type="checkbox"><span class="slider"></span></span></label>
	<label class="control">单笔 <input id="orderAmount" type="number" min="5" max="100" step="0.01" value="5"> USDC</label>
	<label class="control">止盈 <input id="profitTarget" type="number" min="0" step="0.01" value="0"> USDT</label>
	<label class="control" title="尾部策略最高进场买价（¢）。挂单价不高于它：设 80 = 只在市场价 ≥80¢ 时参与、且成交价不超过 80¢（市场价高于 80 就不成交）">进场价 <input id="tailEntry" type="number" min="50" max="99.9" step="0.1" value="98"> ¢</label>
	<label class="control" title="进场方式：限价=挂单（成交价不高于进场价，可能不成交）；市价=到价立即市价买入（保证成交，成交价=当时 ask，可能略高于进场价）">进场 <select id="entryMode">
	<option value="limit">限价</option><option value="market">市价</option></select></label>
	<label class="control" title="尾部止盈出场价（¢）。0 = 关闭（持有到结算）；>0 = 持仓方 bid 涨到该价就卖出落袋。例：进场 90 / 出场 99">出场价 <input id="tailExit" type="number" min="0" max="99.9" step="0.1" value="0"> ¢</label>
	<label class="control" title="止损价（¢）。0 = 关闭；>0 = 持仓方 bid 跌到该价 → 市价卖出锁亏（反向单）；若卖单未成交，自动买对侧对冲兜底（持有 YES 买 NO 锁亏）。例：85 进 / 95 出 / 45 止损">止损价 <input id="tailStop" type="number" min="0" max="99.9" step="0.1" value="0"> ¢</label>
	<label class="control">交易时段 <span class="switch"><input id="hoursOn" type="checkbox"><span class="slider"></span></span></label>
	<label class="control">北京 <input id="hoursStart" type="number" min="0" max="23" step="1" value="9">—<input id="hoursEnd" type="number" min="0" max="23" step="1" value="21"> 时</label>
	<label class="control" title="开启后：窗口开始后的第 N 分钟才允许进场（0=不延迟）。只挡新仓，已有持仓照常管理">延迟进场 <span class="switch"><input id="delayOn" type="checkbox"><span class="slider"></span></span> 第 <input id="delayMin" type="number" min="0" max="4" step="1" value="3"> 分钟</label>
	<button class="btn" id="unlockBtn" type="button" title="解除 KILL 熔断（全停）并恢复交易；解锁以当前权益为新回撤起点">🔓 解锁熔断</button>
	<button class="btn" id="saveControls" type="button">保存</button><span class="ctl-msg" id="controlMsg"></span>
	</div>
	</div>
<div class="tb-row2">
<div class="chip"><span class="k">权益</span><b id="c_eq">—</b></div>
<div class="chip"><span class="k">累计 PnL（主策略）</span><b id="c_pnl">—</b></div>
<div class="chip"><span class="k">实验 PnL</span><b id="c_fade">—</b></div>
<div class="chip"><span class="k">今日成交</span><b id="c_fills">—</b></div>
<div class="chip"><span class="k">回撤</span><b id="c_dd">—</b></div>
<div class="chip"><span class="k">日 / 时 PnL</span><b id="c_dh">—</b></div>
<div class="chip"><span class="k">休息状态</span><b id="c_rest">—</b></div>
<div class="chip"><span class="k">熔断</span><b id="c_kill">—</b></div>
<div class="chip"><span class="k">TWAP60 结算口径</span><b id="c_twap">—</b></div>
</div>
</div></div>
<div id="stale">⚠ 数据连接异常</div>
<div class="wrap">
<h2>资产状态</h2><div id="assets"></div>
<h2>mid 实验（BTC 94-96¢ · hold/profit3 A/B）</h2><div id="mid"></div>
<h2>决策流（连续重复已聚合）</h2><div id="dec"></div>
<h2>挂单 / 成交 / 交易记录</h2><div class="cols">
<div><div class="sec">最近挂单</div><div class="scroll" id="orders"></div></div>
<div><div class="sec">最近成交</div><div class="scroll" id="fills"></div></div>
<div><div class="sec" id="poslabel">交易记录（主策略）</div><div class="scroll" id="positions"></div></div>
</div>
<h2>结算对账</h2><div class="scroll" id="settle"></div>
</div>
<script>
const ASSET_CLS={btc:'a-btc',eth:'a-eth',sol:'a-sol',xrp:'a-xrp',doge:'a-doge',bnb:'a-bnb',hype:'a-hype'};
async function tick(){
  try{
    const r=await fetch('/api/status');if(!r.ok)throw 0;
	    const s=await r.json();
	    syncControls(s.controls||{});
    document.getElementById('stale').style.display='none';
    document.getElementById('mode').textContent='['+s.mode+']  '+s.now.slice(11,16)+' UTC';
    // 状态灯：按数据新鲜度
    const dot=document.getElementById('sdot'),stx=document.getElementById('stxt');
    const age=s.fresh_age;
    if(age==null){dot.className='dot bad';stx.textContent='无数据';}
    else if(age<=5){dot.className='dot';stx.textContent='运行中 · 数据 '+age+'s 前';}
    else if(age<=20){dot.className='dot warn';stx.textContent='数据延迟 '+age+'s';}
    else{dot.className='dot bad';stx.textContent='数据停滞 '+age+'s';}
    const e=s.equity||{},tw=s.twap||{};
    set('c_eq',e.equity??'—');
    set('c_pnl',s.total_pnl??'—',s.total_pnl>0?'up':(s.total_pnl<0?'down':''));
    const fadeOn=(s.fade_assets||[]).length>0;
    const expOn=fadeOn||s.mid_active;
    set('c_fade',expOn?(s.exp_pnl??0):'—',expOn?(s.exp_pnl>0?'up':(s.exp_pnl<0?'down':'')):'');
    set('c_fills',s.today_fills??'—');
    set('c_dd',e.dd??'—');
    set('c_dh',(e.daily??'—')+' / '+(e.hourly??'—'));
    const REST={none:'运行中',manual:'手动关闭',profit_target:'止盈达成',trading_hours:'时段外'};
    const rr=REST[s.rest_reason]||'—';
    set('c_rest',rr,s.rest_reason&&s.rest_reason!=='none'?'warn':'');
    const killed=s.risk_state==='KILL';
    set('c_kill',killed?'熔断中':'正常',killed?'down':'');
    const ub=document.getElementById('unlockBtn');
    ub.style.display='';          // 始终显示（熔断时可用，平时置灰）
    ub.disabled=!killed;
    ub.textContent=killed?'🔓 解锁熔断':'🔓 解锁熔断（未熔断）';
    set('c_twap',tw.v60?tw.v60+' ('+tw.a60+'s前)':'—');
    renderAssets(s);renderMid(s);renderDec(s);renderOrders(s);renderFills(s);
    renderPositions(s);renderSettle(s);
  }catch(err){document.getElementById('stale').style.display='block'}
	}
	let controlsDirty=false,controlsReady=false;
	function syncControls(c){
	  if(controlsDirty)return;
	  document.getElementById('autoTrade').checked=c.auto_trading_enabled!==false;
	  document.getElementById('orderAmount').value=c.fixed_order_notional||'5';
	  document.getElementById('profitTarget').value=c.daily_profit_target??'0';
	  document.getElementById('tailEntry').value=((parseFloat(c.tail_entry_price)||0.98)*100).toFixed(1);
	  document.getElementById('tailExit').value=((parseFloat(c.tail_exit_price)||0)*100).toFixed(1);
	  document.getElementById('tailStop').value=((parseFloat(c.tail_stop_price)||0)*100).toFixed(1);
	  document.getElementById('hoursOn').checked=!!c.trading_hours_enabled;
	  document.getElementById('hoursStart').value=c.trading_hours_start_bt??9;
	  document.getElementById('hoursEnd').value=c.trading_hours_end_bt??21;
	  document.getElementById('delayOn').checked=!!c.entry_delay_enabled;
	  document.getElementById('delayMin').value=c.entry_delay_minutes??3;
	  document.getElementById('entryMode').value=c.tail_entry_mode==='market'?'market':'limit';
	  controlsReady=true;
	}
	async function saveControls(){
	  const btn=document.getElementById('saveControls'),msg=document.getElementById('controlMsg');
	  const amount=Number(document.getElementById('orderAmount').value);
	  const target=Number(document.getElementById('profitTarget').value);
	  const entry=Number(document.getElementById('tailEntry').value);
	  const exitp=Number(document.getElementById('tailExit').value);
	  const stopv=Number(document.getElementById('tailStop').value);
	  const hs=parseInt(document.getElementById('hoursStart').value,10);
	  const he=parseInt(document.getElementById('hoursEnd').value,10);
	  const dmin=parseInt(document.getElementById('delayMin').value,10);
	  if(!Number.isFinite(amount)||amount<5||amount>100){msg.textContent='金额需 5-100';msg.className='ctl-msg down';return}
	  if(!Number.isFinite(target)||target<0){msg.textContent='止盈需 ≥0';msg.className='ctl-msg down';return}
	  if(!Number.isFinite(entry)||entry<50||entry>99.9){msg.textContent='进场价需 50-99.9¢';msg.className='ctl-msg down';return}
	  if(!Number.isFinite(exitp)||exitp<0||exitp>99.9){msg.textContent='出场价需 0-99.9¢';msg.className='ctl-msg down';return}
	  if(!Number.isFinite(stopv)||stopv<0||stopv>99.9){msg.textContent='止损价需 0-99.9¢';msg.className='ctl-msg down';return}
	  if(entry>0&&exitp>0&&exitp<=entry){msg.textContent='出场价需高于进场价';msg.className='ctl-msg down';return}
	  if(entry>0&&stopv>0&&stopv>=entry){msg.textContent='止损价需低于进场价';msg.className='ctl-msg down';return}
	  if(!Number.isInteger(hs)||hs<0||hs>23||!Number.isInteger(he)||he<0||he>23){msg.textContent='时段需 0-23 时';msg.className='ctl-msg down';return}
	  if(!Number.isInteger(dmin)||dmin<0||dmin>4){msg.textContent='延迟分钟需 0-4';msg.className='ctl-msg down';return}
	  btn.disabled=true;msg.textContent='保存中';
	  try{
	    const r=await fetch('/api/controls',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
	      auto_trading_enabled:document.getElementById('autoTrade').checked,
	      fixed_order_notional:amount,
	      daily_profit_target:target,
	      tail_entry_price:entry/100,
	      tail_exit_price:exitp/100,
	      tail_stop_price:stopv/100,
	      trading_hours_enabled:document.getElementById('hoursOn').checked,
	      trading_hours_start_bt:hs,
	      trading_hours_end_bt:he,
	      entry_delay_enabled:document.getElementById('delayOn').checked,
	      entry_delay_minutes:dmin,
	      tail_entry_mode:document.getElementById('entryMode').value})});
	    const data=await r.json();if(!r.ok)throw new Error(data.error||'保存失败');
	    controlsDirty=false;msg.textContent='已生效';msg.className='ctl-msg up';syncControls(data.controls||{});
	  }catch(e){msg.textContent=e.message||'保存失败';msg.className='ctl-msg down'}finally{btn.disabled=false}
	}
	document.getElementById('orderAmount').addEventListener('input',()=>{controlsDirty=true});
	document.getElementById('autoTrade').addEventListener('change',()=>{controlsDirty=true;saveControls()});
	document.getElementById('profitTarget').addEventListener('input',()=>{controlsDirty=true});
	document.getElementById('tailEntry').addEventListener('input',()=>{controlsDirty=true});
	document.getElementById('tailExit').addEventListener('input',()=>{controlsDirty=true});
	document.getElementById('tailStop').addEventListener('input',()=>{controlsDirty=true});
	document.getElementById('hoursOn').addEventListener('change',()=>{controlsDirty=true;saveControls()});
	document.getElementById('hoursStart').addEventListener('input',()=>{controlsDirty=true});
	document.getElementById('hoursEnd').addEventListener('input',()=>{controlsDirty=true});
	document.getElementById('delayOn').addEventListener('change',()=>{controlsDirty=true});
	document.getElementById('delayMin').addEventListener('input',()=>{controlsDirty=true});
	document.getElementById('saveControls').addEventListener('click',saveControls);
	document.getElementById('unlockBtn').addEventListener('click',async function(){
	  const msg=document.getElementById('controlMsg');
	  if(this.disabled){msg.textContent='当前未熔断，无需解锁';msg.className='ctl-msg';return}
	  if(!confirm('确认解锁熔断？解锁后以当前权益为新回撤起点恢复交易。'))return;
	  const b=this;b.disabled=true;
	  try{
	    const r=await fetch('/api/unlock',{method:'POST'});
	    const data=await r.json();
	    if(!r.ok)throw new Error(data.error||'解锁失败');
	    msg.textContent='已请求解锁（≤1s 生效）';msg.className='ctl-msg up';
	  }catch(e){msg.textContent=e.message||'解锁失败';msg.className='ctl-msg down'}
	  b.disabled=false;
	});
	function set(id,v,cls){const el=document.getElementById(id);el.textContent=v;el.className=cls||''}
function tbl(heads,rows){let h='<table><thead><tr>'+heads.map(x=>'<th>'+x+'</th>').join('')+'</tr></thead><tbody>';
  for(const r of rows){h+='<tr>'+r.map(c=>'<td>'+c+'</td>').join('')+'</tr>'}return h+'</tbody></table>'}
function esc(x){return x==null?'—':String(x).replace(/[<>&]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]))}
function abadge(a){return '<span class="asset '+(ASSET_CLS[a]||'')+'">'+(a||'?').toUpperCase()+'</span>'}
function pill(state){
  if(state==='OPEN')return '<span class="pill p-open">OPEN</span>';
  if(state==='CLOSED')return '<span class="pill p-closed">CLOSED</span>';
  if((state||'').includes('WIN'))return '<span class="pill p-win">'+state+'</span>';
  if((state||'').includes('LOSS'))return '<span class="pill p-loss">'+state+'</span>';
  return '<span class="pill p-unk">'+(state||'?')+'</span>';
}
function pnl(v){const f=parseFloat(v||0);return '<span class="'+(f>0?'up':f<0?'down':'dim')+'">'+(f>0?'+':'')+f.toFixed(4)+'</span>'}
function renderAssets(s){
  const by=(s.by_asset||[]).reduce((m,x)=>{m[x.asset]=x;return m},{});
  const fade=new Set(s.fade_assets||[]);
  const rows=[];
  for(const w of (s.windows||[])){
    const tg=(s.triggers||{})[w.asset]||{},b=by[w.asset]||{};
    const ep=parseFloat((s.controls||{}).tail_entry_price)||0.98;   // 可配进场价（¢）
    const inside=tg.hi_ask!=null&&tg.hi_ask>=ep&&tg.hi_ask<0.999;
    const far=tg.hi_ask!=null?(inside?'<span class="up">触发区内 ✓</span>':'差 '+((ep-tg.hi_ask)).toFixed(3)):'—';
    const badge=abadge(w.asset)+(fade.has(w.asset)?' <span class="pill p-win" style="font-size:10px">实验</span>':'');
    rows.push({inside,cells:[badge,w.remaining+'s',(tg.hi_ask!=null?tg.side+' '+tg.hi_ask:'—'),
      far,w.entry||'—',b.n||0,pnl(b.pnl??0)]});
  }
  let h='<table><thead><tr><th>资产</th><th>剩余</th><th>最高 ask</th><th>距触发区</th><th>已入场</th><th>笔数</th><th>PnL</th></tr></thead><tbody>';
  for(const r of rows){h+='<tr'+(r.inside?' class="zone"':'')+'>'+r.cells.map(c=>'<td>'+c+'</td>').join('')+'</tr>'}
  document.getElementById('assets').innerHTML=h+'</tbody></table>';
}
function renderMid(s){
  const m=s.mid||{variants:[],rows:[]};
  if(!m.variants.length){document.getElementById('mid').innerHTML='<span class="dim">暂无数据</span>';return}
  let h='<table><thead><tr><th>变体</th><th>挂单</th><th>成交</th><th>过期</th>'+
    '<th>持有到结算</th><th>出场平仓</th><th>赢/亏</th><th>PnL</th></tr></thead><tbody>';
  for(const [name,d] of m.variants){
    h+='<tr><td><span class="pill '+(name==='profit3'?'p-open':'p-unk')+'">'+esc(name)+'</span></td>'+
      '<td>'+d.orders+'</td><td>'+d.filled+'</td><td>'+d.expired+'</td>'+
      '<td>'+d.settled+'</td><td>'+d.closed+'</td><td>'+d.wins+'/'+d.losses+'</td>'+
      '<td>'+pnl(d.pnl)+'</td></tr>';
  }
  h+='</tbody></table>';
  if(m.rows.length){
    h+='<div class="scroll" style="margin-top:8px;max-height:260px"><table><thead><tr>'+
      '<th>变体</th><th>方向</th><th>状态</th><th>PnL</th></tr></thead><tbody>';
    for(const r of m.rows){
      h+='<tr><td>'+esc(r.v)+'</td><td>'+esc(r.side)+'</td><td>'+esc(r.state)+'</td><td>'+pnl(r.pnl)+'</td></tr>';
    }
    h+='</tbody></table></div>';
  }
  document.getElementById('mid').innerHTML=h;
}
function renderDec(s){const rows=(s.decisions||[]).map(d=>
    [d.t+(d.n>1?' ×'+d.n:''),abadge(d.a),esc(d.act),esc(d.why),esc(d.up),esc(d.down),esc(d.cal)]);
  document.getElementById('dec').innerHTML=tbl(['时间','资产','决策','原因','up','down','cal'],rows);}
function renderOrders(s){const rows=(s.orders||[]).map(o=>[o.t,o.side+' '+o.price,o.size,o.tif+(o.post?' PO':''),o.state]);
  document.getElementById('orders').innerHTML=tbl(['时间','方向/价','量','类型','状态'],rows.map(r=>r.map(esc)));}
function renderFills(s){const rows=(s.fills||[]).map(f=>[f.t,f.side+' '+f.price,f.qty,f.fee]);
  document.getElementById('fills').innerHTML=tbl(['时间','方向/价','量','费用'],rows.map(r=>r.map(esc)));}
function renderPositions(s){const rows=(s.positions||[]).map(p=>
    ['<div style="white-space:normal;max-width:150px">'+esc(p.win)+'</div>',p.side,p.avg||'—',pill(p.state),pnl(p.pnl)]);
  document.getElementById('poslabel').textContent='交易记录（共 '+(s.n_positions??0)+' 笔）';
  document.getElementById('positions').innerHTML=tbl(['窗口','方向','买价','状态','PnL'],rows);}
function renderSettle(s){const rows=(s.settlements||[]).map(x=>
    [x.t,abadge(x.a),esc(x.res),esc(x.ptb),esc(x.final),esc(x.gamma),esc(x.disp)]);
  document.getElementById('settle').innerHTML=tbl(['时间','资产','自结算','PTB','Final','gamma','对账'],rows);}
tick();setInterval(tick,2000);
</script></body></html>"""


def iso(ms: int | None) -> str:
    if not ms:
        return "—"
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%m-%d %H:%M:%S")


def _fmt_price(v: str | None) -> str:
    """自适应精度：大数 2 位小数，小数 4 位，微价 6 位（DOGE 0.0696 之类不被截成相同值）。"""
    if not v:
        return "—"
    f = float(v)
    if abs(f) >= 1000:
        return f"{f:,.2f}"
    if abs(f) >= 1:
        return f"{f:,.4f}"
    return f"{f:.6f}"


def _active_assets() -> set[str] | None:
    """启用中的资产（config/assets.yaml enabled: true）；配置不可读时不过滤。"""
    try:
        from pm5hft.config import Config

        return set(Config().enabled_assets().keys())
    except Exception:  # noqa: BLE001
        return None


ACTIVE_ASSETS = _active_assets()


class _Db:
    """Small sync adapter for the dashboard's SQLite/PostgreSQL read queries."""

    def __init__(self, target: str):
        self.postgres = target.startswith(("postgresql://", "postgresql+asyncpg://"))
        if self.postgres:
            import psycopg

            url = target.replace("postgresql+asyncpg://", "postgresql://", 1)
            self.conn = psycopg.connect(url, autocommit=True, connect_timeout=5)
        else:
            self.conn = sqlite3.connect(target, timeout=3)
            self.conn.execute("PRAGMA busy_timeout=3000")

    def execute(self, query: str, params=()):
        if self.postgres:
            query = query.replace("%", "%%")
            query = re.sub(r"(?<!['\"])\?(?!['\"])", "%s", query)
        return self.conn.execute(query, params)

    def close(self):
        self.conn.close()

    def commit(self):
        if not self.postgres:
            self.conn.commit()


def get_controls(conn: _Db) -> dict:
    defaults = {
        "auto_trading_enabled": True,
        "fixed_order_notional": "5",
        "daily_profit_target": "0",
        "tail_entry_price": "0.98",
        "tail_exit_price": "0",
        "trading_hours_enabled": False,
        "trading_hours_start_bt": 9,
        "trading_hours_end_bt": 21,
        "entry_delay_enabled": False,
        "entry_delay_minutes": 3,
        "tail_entry_mode": "limit",
        "tail_stop_price": "0",
    }
    try:
        rows = conn.execute(
            "SELECT setting_key, value FROM runtime_settings WHERE setting_key IN (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(defaults),
        ).fetchall()
    except Exception:  # table is created by the bot during rolling upgrades
        return defaults
    values = dict(rows)
    out = dict(defaults)
    for k, v in values.items():
        if k in ("auto_trading_enabled", "trading_hours_enabled", "entry_delay_enabled"):
            out[k] = v.lower() == "true"
        elif k in ("trading_hours_start_bt", "trading_hours_end_bt", "entry_delay_minutes"):
            try:
                out[k] = int(v)
            except (TypeError, ValueError):
                pass
        else:
            out[k] = v
    return out


def save_controls(db_path: str, payload: dict) -> dict:
    enabled = payload.get("auto_trading_enabled")
    if not isinstance(enabled, bool):
        raise ValueError("auto_trading_enabled must be boolean")
    try:
        amount = float(payload.get("fixed_order_notional"))
    except (TypeError, ValueError) as exc:
        raise ValueError("fixed_order_notional must be numeric") from exc
    if not 5 <= amount <= 100:
        raise ValueError("fixed_order_notional must be between 5 and 100")
    try:
        target = float(payload.get("daily_profit_target", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("daily_profit_target must be numeric") from exc
    if target < 0:
        raise ValueError("daily_profit_target must be >= 0")
    try:
        entry = float(payload.get("tail_entry_price", 0.98))
    except (TypeError, ValueError) as exc:
        raise ValueError("tail_entry_price must be numeric") from exc
    if not 0.50 <= entry <= 0.999:
        raise ValueError("tail_entry_price must be between 0.50 and 0.999")
    try:
        exitp = float(payload.get("tail_exit_price", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("tail_exit_price must be numeric") from exc
    if not 0 <= exitp <= 0.999:
        raise ValueError("tail_exit_price must be between 0 and 0.999")
    if exitp > 0 and exitp <= entry:
        raise ValueError("tail_exit_price must be above tail_entry_price")
    hours_on = payload.get("trading_hours_enabled")
    if not isinstance(hours_on, bool):
        raise ValueError("trading_hours_enabled must be boolean")
    start_h = payload.get("trading_hours_start_bt", 9)
    end_h = payload.get("trading_hours_end_bt", 21)
    if not isinstance(start_h, int) or not isinstance(end_h, int):
        raise ValueError("trading hours must be integers")
    if not (0 <= start_h <= 23 and 0 <= end_h <= 23):
        raise ValueError("trading hours must be between 0 and 23")
    delay_on = payload.get("entry_delay_enabled")
    if not isinstance(delay_on, bool):
        raise ValueError("entry_delay_enabled must be boolean")
    delay_min = payload.get("entry_delay_minutes", 3)
    if not isinstance(delay_min, int) or not 0 <= delay_min <= 4:
        raise ValueError("entry_delay_minutes must be an integer between 0 and 4")
    entry_mode = payload.get("tail_entry_mode", "limit")
    if entry_mode not in ("limit", "market"):
        raise ValueError("tail_entry_mode must be 'limit' or 'market'")
    try:
        stopv = float(payload.get("tail_stop_price", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("tail_stop_price must be numeric") from exc
    if not 0 <= stopv <= 0.999:
        raise ValueError("tail_stop_price must be between 0 and 0.999")
    if stopv > 0 and stopv >= entry:
        raise ValueError("tail_stop_price must be below tail_entry_price")
    amount_text = f"{amount:.2f}".rstrip("0").rstrip(".")
    target_text = f"{target:.2f}".rstrip("0").rstrip(".")
    entry_text = f"{entry:.3f}".rstrip("0").rstrip(".")
    exit_text = f"{exitp:.3f}".rstrip("0").rstrip(".")
    stop_text = f"{stopv:.3f}".rstrip("0").rstrip(".")
    now = int(time.time() * 1000)
    conn = _Db(db_path)
    try:
        for key, value in (
            ("auto_trading_enabled", "true" if enabled else "false"),
            ("fixed_order_notional", amount_text),
            ("daily_profit_target", target_text),
            ("tail_entry_price", entry_text),
            ("tail_exit_price", exit_text),
            ("tail_stop_price", stop_text),
            ("trading_hours_enabled", "true" if hours_on else "false"),
            ("trading_hours_start_bt", str(start_h)),
            ("trading_hours_end_bt", str(end_h)),
            ("entry_delay_enabled", "true" if delay_on else "false"),
            ("entry_delay_minutes", str(delay_min)),
            ("tail_entry_mode", entry_mode),
        ):
            conn.execute(
                "INSERT INTO runtime_settings (setting_key, value, updated_ts_ms) VALUES (?, ?, ?) "
                "ON CONFLICT (setting_key) DO UPDATE SET value=excluded.value, updated_ts_ms=excluded.updated_ts_ms",
                (key, value, now),
            )
        conn.commit()
        return get_controls(conn)
    finally:
        conn.close()


def build_status(db_path: str) -> dict:
    conn = _Db(db_path)
    now_ms = int(time.time() * 1000)
    out: dict = {"now": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
                 "mode": os.environ.get("PM5HFT_MODE", "paper"), "window": {}, "twap": {}, "equity": {},
                 "decisions": [], "orders": [], "fills": [], "positions": [],
                 "settlements": [], "today_fills": 0, "total_pnl": None, "rest_reason": "none"}
    try:
        out["controls"] = get_controls(conn)
        # 休息原因（none|manual|profit_target|trading_hours；由 bot 每次变化时写回）
        try:
            rr = conn.execute(
                "SELECT value FROM runtime_settings WHERE setting_key='rest_reason'").fetchone()
            if rr and rr[0]:
                out["rest_reason"] = rr[0]
        except Exception:
            pass
        # 风控状态（NORMAL|COOLDOWN|KILL；bot 变化时写回，供「解锁熔断」按钮显示）
        out["risk_state"] = "NORMAL"
        try:
            rs = conn.execute(
                "SELECT value FROM runtime_settings WHERE setting_key='risk_state'").fetchone()
            if rs and rs[0]:
                out["risk_state"] = rs[0]
        except Exception:
            pass
        # 当前窗口（多资产：所有活动窗口 + 每资产触发进度）；只显示启用中的资产
        now_s = now_ms // 1000
        win_rows = conn.execute(
            "SELECT id, asset, question, t_start, t_end, twap_lookback_s, tick_size "
            "FROM markets WHERE t_start <= ? AND t_end > ? ORDER BY asset", (now_s, now_s)).fetchall()
        if ACTIVE_ASSETS is not None:
            win_rows = [r for r in win_rows if r[1] in ACTIVE_ASSETS]
        out["windows"] = []
        for mid, asset, _q, _ts, te, lb, tick in win_rows:
            acc = conn.execute(
                "SELECT accepting_orders FROM market_status WHERE market_id=? "
                "ORDER BY id DESC LIMIT 1", (mid,)).fetchone()
            entry = None
            er = conn.execute(
                "SELECT meta, price FROM orders WHERE market_id=? AND state IN "
                "('FILLED','LIVE','PENDING','PARTIAL') AND (meta LIKE '%tail_capture%' "
                "OR meta LIKE '%xrp_fade%' OR meta LIKE '%mid_capture%') "
                "ORDER BY created_ts_ms DESC LIMIT 1", (mid,)).fetchone()
            if er:
                try:
                    em = json.loads(er[0])
                    entry = f"{em.get('token_side','?')}@{er[1]}"
                except (json.JSONDecodeError, TypeError):
                    entry = None
            out["windows"].append({
                "asset": asset, "question": _q, "remaining": max(0, te - now_s),
                "lookback": lb, "tick": tick, "accepting": acc[0] if acc else None,
                "entry": entry,
            })
        out["triggers"] = {}
        for mid, asset, _q, ts, _te, _lb, _tick in win_rows:
            w0 = ts * 1000
            highs = conn.execute(
                "SELECT MAX(CAST(up_ask AS REAL)), MAX(CAST(down_ask AS REAL)) "
                "FROM decision_log WHERE market_id=? AND ts_ms>=? AND cal_prob IS NOT NULL",
                (mid, w0),
            ).fetchone()
            latest = conn.execute(
                "SELECT CAST(cal_prob AS REAL), up_ask, down_ask, ts_ms "
                "FROM decision_log WHERE market_id=? AND ts_ms>=? AND cal_prob IS NOT NULL "
                "ORDER BY id DESC LIMIT 1",
                (mid, w0),
            ).fetchone()
            if not highs or not latest:
                continue
            up_hi, down_hi = highs
            last_cal, up_now, down_now, _ts = latest
            side = "UP" if (up_hi or 0) >= (down_hi or 0) else "DOWN"
            hi = max(up_hi or 0, down_hi or 0)
            need = hi + 0.001
            out["triggers"][asset] = {
                "side": side, "hi_ask": round(float(hi), 4), "need_cal": round(float(need), 4),
                "last_cal": round(float(last_cal or 0), 4),
                "gap": round(max(0.0, need - (last_cal or 0)), 4),
                "up_now": round(float(up_now or 0), 3), "down_now": round(float(down_now or 0), 3),
            }
        # TWAP 最新样本（30s/60s 各自最新）
        for w in (60, 30):
            r = conn.execute(
                "SELECT value_e18, ts_ms FROM twap_samples WHERE window_s=? "
                "ORDER BY id DESC LIMIT 1", (w,)).fetchone()
            if r:
                val, ts = r
                age = max(0, (now_ms - ts) // 1000)
                try:
                    v = float(val) / 1e18
                    disp = f"{v:,.3f}"
                except ValueError:
                    disp = val
                out["twap"][f"v{w}"] = disp
                out["twap"][f"a{w}"] = age
        # 权益快照
        e = conn.execute(
            "SELECT equity, daily_pnl, hourly_pnl, drawdown FROM equity_snapshot "
            "ORDER BY id DESC LIMIT 1").fetchone()
        if e:
            out["equity"] = {"equity": e[0], "daily": e[1], "hourly": e[2], "dd": e[3]}
        # 决策流（含资产列；连续相同的 (资产,决策,原因) 聚合为一行）
        rows = conn.execute(
            "SELECT d.ts_ms, d.remaining_s, d.cal_prob, d.up_bid, d.up_ask, d.down_bid, "
            "d.down_ask, d.decision, d.reject_reason, COALESCE(m.asset, d.asset) "
            "FROM decision_log d LEFT JOIN markets m ON m.id = d.market_id "
            "ORDER BY d.id DESC LIMIT 120").fetchall()
        groups: list[dict] = []
        for r in rows:
            ts, rem, cal, ub, ua, db_, da, dec, rej, asset = r
            if ACTIVE_ASSETS is not None and asset not in ACTIVE_ASSETS:
                continue
            why = rej or (dec if dec != "NOOP" else "")
            key = (asset, dec, why)
            if groups and (groups[-1]["asset"], groups[-1]["act"], groups[-1]["why"]) == key:
                groups[-1]["n"] += 1
            else:
                if len(groups) >= 12:
                    break
                groups.append({
                    "t": iso(ts)[-8:], "a": asset or "?", "cal": cal or "—",
                    "up": f"{ub}/{ua}" if ub else "—", "down": f"{db_}/{da}" if db_ else "—",
                    "act": dec, "why": (why or "")[:60], "n": 1, "asset": asset,
                })
        out["decisions"] = groups
        # 挂单（全部）
        rows = conn.execute(
            "SELECT created_ts_ms, side, price, size, tif, post_only, state FROM orders "
            "ORDER BY created_ts_ms DESC").fetchall()
        for r in rows:
            out["orders"].append({"t": iso(r[0])[-8:], "side": r[1], "price": r[2],
                                  "size": r[3], "tif": r[4], "post": bool(r[5]), "state": r[6]})
        # 成交（全部）
        rows = conn.execute(
            "SELECT ts_ms, side, price, qty, fee FROM fills ORDER BY id DESC").fetchall()
        for r in rows:
            out["fills"].append({"t": iso(r[0])[-8:], "side": r[1], "price": r[2],
                                 "qty": r[3], "fee": r[4] or "0"})
        # 交易记录：每一笔仓位一行，含终态（OPEN 在手 / CLOSED 已平仓 / SETTLED 持有到结算）
        out["n_positions"] = conn.execute(
            "SELECT COUNT(*) FROM positions").fetchone()[0]
        rows = conn.execute(
            "SELECT p.market_id, p.avg_entry, p.realized_pnl, p.state, p.settled_result, "
            "m.question, m.asset, "
            "CASE WHEN p.token_id = m.token_up THEN 'UP' ELSE 'DOWN' END "
            "FROM positions p LEFT JOIN markets m ON m.id=p.market_id "
            "ORDER BY p.market_id DESC").fetchall()
        # 主策略交易记录：排除 mid 实验市场（mid 仓位在实验区单独展示）
        mid_markets = {r[0] for r in conn.execute(
            "SELECT DISTINCT o.market_id FROM orders o "
            "WHERE o.meta LIKE '%mid_capture%' AND o.state IN ('FILLED','PARTIAL')"
        ).fetchall()}
        for r in rows:
            mid, avg, pnl, state, res, q, asset, side = r
            if ACTIVE_ASSETS is not None and asset not in ACTIVE_ASSETS:
                continue
            if mid in mid_markets:
                continue
            if state == "SETTLED":
                disp = f"SETTLED·{res or '?'}"
            elif state == "CLOSED":
                disp = "CLOSED"
            else:
                disp = state or "?"
            out["positions"].append({"win": f"{asset}:{(q or str(mid))[-22:]}", "side": side,
                                     "avg": avg or "—", "pnl": pnl, "state": disp})
        # 结算（全部）
        rows = conn.execute(
            "SELECT s.self_settled_at_ms, s.self_result, s.ptb_e18, s.final_e18, "
            "s.gamma_result, s.dispute, COALESCE(m.asset,'?') "
            "FROM settlements s LEFT JOIN markets m ON m.id = s.market_id "
            "ORDER BY s.market_id DESC").fetchall()
        for r in rows:
            # settlements 的 ptb_e18/final_e18 列名有误导：实际存的是原始十进制串
            if ACTIVE_ASSETS is not None and r[6] not in ACTIVE_ASSETS:
                continue
            out["settlements"].append({"t": iso(r[0]), "a": r[6], "res": r[1] or "—",
                                       "ptb": _fmt_price(r[2]), "final": _fmt_price(r[3]),
                                       "gamma": r[4] or "—",
                                       "disp": "⚠" if r[5] else "✓"})
        # 汇总（累计 PnL = 主策略账本：启用中资产、且排除 mid 实验仓位——
        # mid/fade 实验盈亏只计入实验徽章与实验区，不污染主账本口径）
        day0 = now_ms - (now_ms % 86_400_000)
        out["today_fills"] = conn.execute(
            "SELECT COUNT(*) FROM fills WHERE ts_ms >= ?", (day0,)).fetchone()[0]
        mid_pnl_by = dict(conn.execute(
            "SELECT COALESCE(m.asset,'?'), SUM(CAST(p.realized_pnl AS REAL)) "
            "FROM positions p LEFT JOIN markets m ON m.id=p.market_id "
            "WHERE EXISTS (SELECT 1 FROM orders o WHERE o.market_id=p.market_id "
            "AND o.meta LIKE '%mid_capture%' AND o.state IN ('FILLED','PARTIAL')) "
            "GROUP BY COALESCE(m.asset,'?')"
        ).fetchall())
        if ACTIVE_ASSETS is not None:
            placeholders = ",".join("?" for _ in ACTIVE_ASSETS)
            tot = conn.execute(
                "SELECT SUM(CAST(p.realized_pnl AS REAL)) FROM positions p "
                "LEFT JOIN markets m ON m.id=p.market_id "
                f"WHERE m.asset IN ({placeholders})",
                tuple(sorted(ACTIVE_ASSETS)),
            ).fetchone()[0]
        else:
            tot = conn.execute("SELECT SUM(CAST(realized_pnl AS REAL)) FROM positions").fetchone()[0]
        out["total_pnl"] = round((tot or 0) - sum(mid_pnl_by.values()), 4)
        # 数据新鲜度（最新决策距今秒数）
        last_ts = conn.execute("SELECT MAX(ts_ms) FROM decision_log").fetchone()[0]
        out["fresh_age"] = max(0, (now_ms - last_ts) // 1000) if last_ts else None
        # 分资产贡献：成交笔数（买入成交）+ 已实现 PnL
        pnl_by = dict(conn.execute(
            "SELECT COALESCE(m.asset,'?'), SUM(CAST(p.realized_pnl AS REAL)) "
            "FROM positions p LEFT JOIN markets m ON m.id=p.market_id GROUP BY COALESCE(m.asset,'?')"
        ).fetchall())
        cnt_by = dict(conn.execute(
            "SELECT COALESCE(m.asset,'?'), COUNT(*) FROM fills f "
            "LEFT JOIN markets m ON m.id=f.market_id WHERE f.side='BUY' GROUP BY COALESCE(m.asset,'?')"
        ).fetchall())
        out["by_asset"] = []
        for a in sorted(set(pnl_by) | set(cnt_by)):
            if ACTIVE_ASSETS is not None and a not in ACTIVE_ASSETS:
                continue
            out["by_asset"].append({
                "asset": a, "n": cnt_by.get(a, 0),
                "pnl": round(float(pnl_by.get(a) or 0) - float(mid_pnl_by.get(a) or 0), 4),
            })
        out["by_asset"].sort(key=lambda x: -x["pnl"])
        # 反向实验（xrp_fade）与中段实验（mid_capture）：PnL 与主账本分开显示
        fade_rows = conn.execute(
            "SELECT DISTINCT COALESCE(m.asset,'?') FROM orders o "
            "LEFT JOIN markets m ON m.id=o.market_id WHERE o.meta LIKE '%xrp_fade%'"
        ).fetchall()
        out["fade_assets"] = [
            r[0] for r in fade_rows if ACTIVE_ASSETS is None or r[0] in ACTIVE_ASSETS
        ]
        out["fade_pnl"] = round(sum(float(pnl_by.get(a, 0)) for a in out["fade_assets"]), 4)
        # mid 实验：同窗口互斥保证"有 mid 成交的窗口其仓位=mid 仓位"（tail 同窗口不进）
        mid_pnl = conn.execute(
            "SELECT SUM(CAST(p.realized_pnl AS REAL)) FROM positions p "
            "WHERE EXISTS (SELECT 1 FROM orders o WHERE o.market_id=p.market_id "
            "AND o.meta LIKE '%mid_capture%' AND o.state IN ('FILLED','PARTIAL'))"
        ).fetchone()[0]
        out["mid_pnl"] = round(float(mid_pnl or 0), 4)
        out["mid_active"] = conn.execute(
            "SELECT 1 FROM orders WHERE meta LIKE '%mid_capture%' LIMIT 1"
        ).fetchone() is not None
        out["exp_pnl"] = round(out["fade_pnl"] + out["mid_pnl"], 4)
        # mid 实验单独统计（按出场变体 hold/profit3 分组）
        mid_orders = conn.execute(
            "SELECT o.meta, o.state FROM orders o WHERE o.meta LIKE '%mid_capture%'"
        ).fetchall()
        mid_pos = conn.execute(
            "SELECT p.realized_pnl, p.state, "
            "CASE WHEN p.token_id=m.token_up THEN 'UP' ELSE 'DOWN' END, "
            "(SELECT o.meta FROM orders o WHERE o.market_id=p.market_id "
            " AND o.meta LIKE '%mid_capture%' AND o.state IN ('FILLED','PARTIAL') "
            " ORDER BY o.created_ts_ms LIMIT 1) "
            "FROM positions p JOIN markets m ON m.id=p.market_id "
            "WHERE EXISTS (SELECT 1 FROM orders o2 WHERE o2.market_id=p.market_id "
            " AND o2.meta LIKE '%mid_capture%' AND o2.state IN ('FILLED','PARTIAL')) "
            "ORDER BY p.market_id DESC LIMIT 20"
        ).fetchall()
        variants: dict[str, dict] = {}
        for meta_json, state in mid_orders:
            try:
                m = json.loads(meta_json or "{}")
            except (json.JSONDecodeError, TypeError):
                m = {}
            v = m.get("exit_mode") or "hold"
            d = variants.setdefault(v, {"orders": 0, "filled": 0, "expired": 0,
                                        "settled": 0, "closed": 0, "wins": 0,
                                        "losses": 0, "pnl": 0.0})
            d["orders"] += 1
            if state in ("FILLED", "PARTIAL"):
                d["filled"] += 1
            elif state == "EXPIRED":
                d["expired"] += 1
        mid_rows: list[dict] = []
        for pnl, pstate, side, meta_json in mid_pos:
            v = "hold"
            if meta_json:
                try:
                    v = (json.loads(meta_json) or {}).get("exit_mode") or "hold"
                except (json.JSONDecodeError, TypeError):
                    pass
            d = variants.setdefault(v, {"orders": 0, "filled": 0, "expired": 0,
                                        "settled": 0, "closed": 0, "wins": 0,
                                        "losses": 0, "pnl": 0.0})
            pnl_f = float(pnl or 0)
            if pstate == "SETTLED":
                d["settled"] += 1
                d["pnl"] += pnl_f
                if pnl_f > 0:
                    d["wins"] += 1
                elif pnl_f < 0:
                    d["losses"] += 1
            elif pstate == "CLOSED":
                d["closed"] += 1
                d["pnl"] += pnl_f
            mid_rows.append({"v": v, "side": side, "state": pstate, "pnl": pnl_f})
        for d in variants.values():
            d["pnl"] = round(d["pnl"], 4)
        out["mid"] = {
            "variants": [[k, v] for k, v in sorted(variants.items())],
            "rows": mid_rows,
        }
    except Exception as exc:  # noqa: BLE001
        print(f"[dashboard] unexpected error: {exc!r}", flush=True)
    finally:
        conn.close()
    return out


class Handler(BaseHTTPRequestHandler):
    db_path = "data/pm5hft.db"

    def log_message(self, *a):  # noqa: ANN002, ANN003 — 静默访问日志
        pass

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/api/status"):
            body = json.dumps(build_status(self.db_path)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        if self.path.startswith("/api/unlock"):
            # 「解锁熔断」：写一次性请求，bot 每秒轮询 runtime_settings 时处理并置回 0
            try:
                conn = _Db(self.db_path)
                try:
                    now = int(time.time() * 1000)
                    conn.execute(
                        "INSERT INTO runtime_settings (setting_key, value, updated_ts_ms) VALUES ('reset_kill_request','1',?) "
                        "ON CONFLICT (setting_key) DO UPDATE SET value=excluded.value, updated_ts_ms=excluded.updated_ts_ms",
                        (now,),
                    )
                    conn.commit()
                finally:
                    conn.close()
                body = json.dumps({"ok": True}).encode("utf-8")
                status = 200
            except Exception as exc:  # noqa: BLE001
                body = json.dumps({"ok": False, "error": f"unlock failed: {exc}"}).encode("utf-8")
                status = 500
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if not self.path.startswith("/api/controls"):
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 4096:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(length))
            controls = save_controls(self.db_path, payload)
            body = json.dumps({"ok": True, "controls": controls}).encode("utf-8")
            status = 200
        except (ValueError, json.JSONDecodeError) as exc:
            body = json.dumps({"ok": False, "error": str(exc)}).encode("utf-8")
            status = 400
        except Exception as exc:  # noqa: BLE001
            body = json.dumps({"ok": False, "error": f"save failed: {exc}"}).encode("utf-8")
            status = 500
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8090)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--db", default=None, help="SQLite path or PostgreSQL URL")
    args = p.parse_args(argv)
    Handler.db_path = args.db or os.environ.get("PM5HFT_DB_URL") or default_db_url()
    if not Handler.db_path.startswith(("postgresql://", "postgresql+asyncpg://")):
        if Handler.db_path.startswith("sqlite+aiosqlite:///./"):
            Handler.db_path = str(Path(Handler.db_path.removeprefix("sqlite+aiosqlite:///./")).resolve())
        elif Handler.db_path.startswith("sqlite+aiosqlite:///"):
            Handler.db_path = Handler.db_path.removeprefix("sqlite+aiosqlite:///")
        if not Path(Handler.db_path).is_file():
            print(f"[warn] DB 不存在: {Handler.db_path}（bot 启动后自动出现）")
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"pm5hft dashboard: http://127.0.0.1:{args.port}  (db={Handler.db_path})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
