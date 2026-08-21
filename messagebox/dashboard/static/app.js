async function act(ep,f){
 try{
  const r=await fetch('/api/'+ep+'?f='+f,{method:'POST'});
  if(!r.ok){const d=await r.json().catch(()=>({}));throw new Error(d.error||'Could not move message')}
 }catch(e){alert(e.message)}
 await load()
}
async function ringNow(){
 const btn=document.getElementById('ring'),status=document.getElementById('ringstatus');
 btn.disabled=true;status.textContent='Sending ring…';
 try{
  const r=await fetch('/api/ring',{method:'POST'});
  if(!r.ok)throw new Error();
  status.textContent='Ring requested ✓';
 }catch(e){status.textContent='Could not request ring.'}
 setTimeout(()=>{btn.disabled=false;status.textContent="Call the kids when they're home."},3000);
}
let contactsData=null;
function selectContact(){
 const jid=document.getElementById('contactjid').value;
 const chat=contactsData&&contactsData.discovered.find(item=>item.jid===jid);
 if(chat)document.getElementById('contactlabel').value=chat.label;
}
function renderContacts(d){
 contactsData=d;
 const contacts=Object.entries(d.contacts),automatic=contacts.length===1;
 document.getElementById('contactmode').textContent=contacts.length===0?'No contacts configured. Add a WhatsApp chat to begin.':
  automatic?'This contact is the automatic destination for new messages.':'Cards select the outgoing contact for new messages.';
 document.getElementById('contactlist').innerHTML=contacts.length?contacts.map(([jid,c])=>{
  const state=(automatic?'automatic · ':'')+(c.paired?'paired':'unpaired')+' · '+c.card_count+' card'+(c.card_count===1?'':'s');
  return '<div class="contactrow"><div><span class="contactname">'+esc(c.label)+'</span><span class="contactmeta">'+esc(c.kind)+' · '+state+'</span></div>'+
   '<button class="remove" data-jid="'+esc(jid)+'" onclick="removeContact(this.dataset.jid)">Remove</button></div>'}).join(''):
  '<div class="empty">No contacts yet.</div>';
 const choices=d.discovered.filter(chat=>!chat.configured),select=document.getElementById('contactjid');
 select.innerHTML=choices.length?choices.map(chat=>'<option value="'+esc(chat.jid)+'">'+esc(chat.label)+' ('+esc(chat.kind)+')</option>').join(''):
  '<option value="">No unconfigured chats found</option>';
 document.getElementById('contactadd').disabled=!choices.length;selectContact();
 const listeners=Object.entries(d.listeners);
 document.getElementById('listenerlist').innerHTML=listeners.length?listeners.map(([jid,p])=>
  '<div class="contactrow"><div><span class="contactname">'+esc(p.name)+'</span><span class="listenerjid">'+esc(jid)+'</span><span class="listenerclip">'+
  esc(p.listened_clip||'Default listened sound')+'</span></div><div class="actions"><button class="secondary" data-jid="'+esc(jid)+'" onclick="editListener(this.dataset.jid)">Edit</button>'+
  '<button class="remove" data-jid="'+esc(jid)+'" onclick="removeListener(this.dataset.jid)">Remove</button></div></div>').join(''):
  '<div class="empty">No listener profiles.</div>';
}
async function loadContacts(refresh=false){
 const btn=document.getElementById('contactrefresh'),status=document.getElementById('contactstatus');
 btn.disabled=true;status.textContent=refresh?'Syncing WhatsApp…':'Loading…';
 try{
  const r=await fetch('/api/contacts'+(refresh?'?refresh=1':'')),d=await r.json();
   if(!r.ok)throw new Error(d.error||'Could not load contacts');renderContacts(d);status.textContent=d.discovery_error||(refresh?'Refreshed ✓':'');
 }catch(e){status.textContent=e.message}
 btn.disabled=false;
}
async function contactMutation(payload){
 const status=document.getElementById('contactstatus');status.textContent='Saving…';
 try{
  const r=await fetch('/api/contacts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),d=await r.json();
  if(!r.ok)throw new Error(d.error||'Could not save contact');await loadContacts();status.textContent='Saved ✓';
 }catch(e){status.textContent=e.message}
}
function addContact(){contactMutation({action:'add',jid:document.getElementById('contactjid').value,label:document.getElementById('contactlabel').value})}
function removeContact(jid){contactMutation({action:'remove',jid})}
function editListener(jid){
 const p=contactsData.listeners[jid];document.getElementById('listenerjid').value=jid;
 document.getElementById('listenername').value=p.name;document.getElementById('listenerclip').value=p.listened_clip;
}
async function listenerMutation(payload){
 const status=document.getElementById('listenerstatus');status.textContent='Saving…';
 try{
  const r=await fetch('/api/listeners',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),d=await r.json();
  if(!r.ok)throw new Error(d.error||'Could not save listener');await loadContacts();status.textContent='Saved ✓';
 }catch(e){status.textContent=e.message}
}
function saveListener(){listenerMutation({action:'upsert',jid:document.getElementById('listenerjid').value,
 name:document.getElementById('listenername').value,listened_clip:document.getElementById('listenerclip').value})}
function removeListener(jid){listenerMutation({action:'remove',jid})}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function fmtd(s){
 if(s==null)return '—';
 if(s<60)return s.toFixed(0)+'s';
 if(s<3600)return (s/60).toFixed(1)+'m';
 if(s<86400)return (s/3600).toFixed(1)+'h';
 return (s/86400).toFixed(1)+'d';
}
function fmtt(ts){return new Date(ts*1000).toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})}
function pct(v){return v==null?'—':v+'%'}
let allInteractions=[],interactionsExpanded=false;
function interactionRows(items){
 if(!items.length)return '<div class="empty">No guided interactions yet.</div>';
 return items.map(i=>{
  const who=i.flow==='standalone'?'Child · new message':esc(i.sender)+' · reply';
  const meta=[];
  if(i.chat)meta.push(esc(i.chat));
  if(i.wait_to_play_s!=null&&i.source_confidence==='exact')meta.push('waited '+fmtd(i.wait_to_play_s)+' to play');
  if(i.duration!=null)meta.push(fmtd(i.duration)+' recording');
  const rail=i.stages.map(s=>'<div class="tstage '+esc(s.state)+'"><div class="tdot"></div><span>'+esc(s.label)+'</span></div>').join('');
  return '<div class="interaction"><div class="itop"><div><div class="ititle">'+who+'</div>'+
   '<div class="itime">'+fmtt(i.ts)+'</div></div><span class="outcome '+esc(i.outcome_tone)+'">'+
   esc(i.outcome_label)+'</span></div><div class="railwrap"><div class="rail">'+rail+
   '</div></div>'+(meta.length?'<div class="imeta">'+meta.join(' · ')+'</div>':'')+'</div>'}).join('');
}
function renderInteractions(){
 const shown=interactionsExpanded?allInteractions:allInteractions.slice(0,6);
 document.getElementById('interactions').innerHTML=interactionRows(shown);
 const btn=document.getElementById('moreInteractions'),extra=allInteractions.length-6;
 btn.hidden=extra<=0;btn.textContent=interactionsExpanded?'Show less':'Show '+extra+' more';
}
function toggleInteractions(){interactionsExpanded=!interactionsExpanded;renderInteractions()}
function rows(items,kind){
 if(!items.length)return '<div class="empty">'+(kind==='queue'?'Nothing waiting.':'Empty.')+'</div>';
 let btn=kind==='queue'?f=>'<div class="actions"><button class="hold" onclick="act(\'hold\',\''+f+'\')">hold</button><button class="del" onclick="act(\'delete\',\''+f+'\')">delete</button></div>'
        :kind==='hold'?f=>'<button class="rei" onclick="act(\'resume\',\''+f+'\')">reinstate</button>'
                     :f=>'<button class="rei" onclick="act(\'reinstate\',\''+f+'\')">reinstate</button>';
 const audioQuery=kind==='hold'?'?hold=1':kind==='trash'?'?trash=1':'';
 return '<table><tr><th>when</th><th>from</th><th>chat</th><th>len</th><th>listen</th><th></th></tr>'+
  items.map(i=>'<tr><td>'+fmtt(i.ts)+'</td><td>'+esc(i.sender)+'</td><td>'+esc(i.chat)+'</td><td>'+fmtd(i.dur)+
   '</td><td><audio controls preload="none" src="/audio/'+encodeURIComponent(i.file)+audioQuery+
   '"></audio></td><td>'+btn(encodeURIComponent(i.file))+'</td></tr>').join('')+'</table>'}
async function load(){
 const d=await (await fetch('/api/data')).json();
 document.getElementById('gen').textContent='updated '+d.generated;
 const c=d.cards;
 const cards=[['sent total',c.sent_total],['received total',c.recv_total],
  ['sent today',c.sent_today],['received today',c.recv_today],
  ['plays',c.plays],['rings',c.rings],['listened receipts',c.listened],
  ['waiting to announce',c.listened_pending],
  ['outbox pending',c.outbox_pending],['outbox attention',c.outbox_attention],
  ['avg sent len',c.avg_sent_dur?c.avg_sent_dur+'s':'—'],
 ['avg recv len',c.avg_recv_dur?c.avg_recv_dur+'s':'—'],
 ['avg wait to play',c.avg_wait_min?c.avg_wait_min+'m':'—']];
 document.getElementById('cards').innerHTML=cards.map(x=>'<div class="card"><b>'+x[1]+'</b><span>'+x[0]+'</span></div>').join('');
 const b=d.behavior;
 const noSend=b.no_speech+b.not_sent+b.incomplete;
 const behavior=[[pct(b.reply_rate),'reply rate',b.reply_approved+' of '+b.reply_sessions+' replies approved'],
  [pct(b.review_send_rate),'review → send',b.reviewed+' recordings reviewed'],
  [noSend,'sessions without a send',b.no_speech+' no speech · '+b.not_sent+' not approved · '+b.incomplete+' interrupted'],
  [b.standalone_sessions,'new messages','started directly by the kids']];
 document.getElementById('behavior').innerHTML=behavior.map(x=>'<div class="summarycard"><b>'+x[0]+'</b><span>'+x[1]+'</span><small>'+x[2]+'</small></div>').join('');
 allInteractions=d.interactions;renderInteractions();
 const mx=Math.max(1,...d.sent_per_day,...d.recv_per_day);
 document.getElementById('bars').innerHTML=d.days.map((day,i)=>{
  const s=d.sent_per_day[i],r=d.recv_per_day[i];
  return '<div class="bcol" title="'+day+': '+s+' sent, '+r+' recv">'+
   '<div class="bar s" style="height:'+(s/mx*80)+'px"></div>'+
   '<div class="bar r" style="height:'+(r/mx*80)+'px"></div>'+
   '<div class="blab">'+day.slice(5)+'</div></div>'}).join('');
 const bc=t=>t.length?'<table>'+t.map(x=>'<tr><td>'+esc(x[0])+'</td><td>'+x[1]+'</td></tr>').join('')+'</table>':'<div class="empty">No data yet.</div>';
 document.getElementById('bychat').innerHTML=
  '<h2 style="margin-top:0">incoming from</h2>'+bc(d.by_chat_in)+'<h2>outgoing to</h2>'+bc(d.by_chat_out);
 document.getElementById('queue').innerHTML=rows(d.queue,'queue');
 document.getElementById('hold').innerHTML=rows(d.hold,'hold');
 document.getElementById('trash').innerHTML=rows(d.trash,'trash');
}
load();loadContacts();setInterval(load,15000);
