const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
let currentMatches = [];
let selectedSeeds = [];
let scanTimer;
let seedTimer;

function setMessage(text, error=false){ const el=$('#message'); el.textContent=text; el.classList.toggle('error',error); }
function escapeHtml(value){ const div=document.createElement('div'); div.textContent=value??''; return div.innerHTML; }
function checkedValues(selector){ return $$(selector).filter(input=>input.checked).map(input=>input.value); }

function currentFilters(){
  return {
    decades:checkedValues('#decades input'),
    popularity:$('#popularity').value,
    variety:$('#variety').value,
    max_per_artist:Number($('#max-per-artist').value),
    strictness:$('#strictness').value,
    include_genres:checkedValues('#include-genres input'),
    exclude_genres:checkedValues('#exclude-genres input'),
    avoid_versions:checkedValues('#avoid-versions input')
  };
}

function updateInterpretation(parts){
  if(parts){ $('#interpretation-text').textContent=parts.join(' · '); return; }
  const filters=currentFilters();
  const selectedDecades=filters.decades.filter(value=>value!=='any');
  const summary=[selectedDecades.length?selectedDecades.map(value=>`${value}s`).join(' + '):'Any decade'];
  if(filters.include_genres.length) summary.push(`Include ${filters.include_genres.join(', ')}`);
  if(filters.exclude_genres.length) summary.push(`No ${filters.exclude_genres.join(', ')}`);
  summary.push({mixed:'Big Hits and Still Popular Today',hits:'Big Hits When Released',enduring:'Still Popular Today'}[filters.popularity]);
  if(filters.max_per_artist) summary.push(`Maximum ${filters.max_per_artist} per artist`);
  if(filters.avoid_versions.length) summary.push(`Avoid ${filters.avoid_versions.join(', ')}`);
  if(selectedSeeds.length) summary.push(`Similar to ${selectedSeeds.length} seed song${selectedSeeds.length===1?'':'s'}`);
  summary.push(filters.strictness==='strict'?'Strict metadata':'Allow unknown metadata');
  const prompt=$('#prompt').value.trim();
  if(prompt) summary.push(`Also: “${prompt}”`);
  $('#interpretation-text').textContent=summary.join(' · ');
}

async function loadStatus(){
  try{
    const status=await fetch('/api/status').then(r=>r.json());
    $('#track-count').textContent=Number(status.track_count).toLocaleString();
    const total=Number(status.scan.total||status.last_scan_count||status.track_count||0);
    const scanned=Number(status.scan.scanned||0);
    const bar=$('#scan-progress-bar'); const dot=$('.status-dot');
    if(status.scan.running){
      $('#scan').disabled=true;
      const percent=total?Math.min(99,Math.round((scanned/total)*100)):0;
      $('#scan-note').textContent=`Scanning… ${scanned.toLocaleString()} of about ${total.toLocaleString()} files checked`;
      $('#scan-percent').textContent=total?`${percent}%`:''; bar.style.width=total?`${percent}%`:'35%';
      bar.classList.toggle('unknown',!total); dot.className='status-dot scanning';
      clearTimeout(scanTimer); scanTimer=setTimeout(loadStatus,1500);
    }else{
      $('#scan').disabled=false; bar.classList.remove('unknown');
      if(status.scan.error){
        $('#scan-note').textContent=`Scan stopped: ${status.scan.error}`; $('#scan-percent').textContent=''; bar.style.width='0'; dot.className='status-dot failed';
      }else if(status.last_scan){
        const changed=Number(status.scan.changed||0); const removed=Number(status.scan.removed||0);
        const details=(changed||removed)?` · ${changed.toLocaleString()} changed · ${removed.toLocaleString()} removed`:'';
        $('#scan-note').textContent=`Scan complete — ${Number(status.track_count).toLocaleString()} tracks indexed${details}`;
        $('#scan-percent').textContent='100%'; bar.style.width='100%'; dot.className='status-dot complete';
      }else{
        $('#scan-note').textContent='Not scanned yet'; $('#scan-percent').textContent=''; bar.style.width='0'; dot.className='status-dot';
      }
    }
    const enrich=status.enrichment||{};
    $('#enrichment-note').textContent=enrich.running?`MusicBrainz: ${Number(enrich.checked).toLocaleString()} checked, ${Number(enrich.enriched).toLocaleString()} enriched`:enrich.error?`MusicBrainz paused: ${enrich.error}`:status.musicbrainz_remaining?'MusicBrainz enrichment is cached and continues after scans':'';
    $('#playlists').innerHTML=status.playlists.length?status.playlists.map(p=>`<span>♪ ${escapeHtml(p)}</span>`).join(''):'<span>No .m3u8 playlists yet</span>';
  }catch{ $('#scan-note').textContent='Could not read app status'; }
}

$('#scan').addEventListener('click',async()=>{ $('#scan').disabled=true; await fetch('/api/scan',{method:'POST'}); loadStatus(); });

$$('[data-prompt]').forEach(button=>button.addEventListener('click',()=>{ $('#prompt').value=button.dataset.prompt; updateInterpretation(); $('#prompt').focus(); }));

$$('.filter-grid select, .choice-chips input').forEach(control=>control.addEventListener('change',event=>{
  const decades=event.target.closest('#decades');
  if(decades){
    const any=$('#decades input[value="any"]');
    const specific=$$('#decades input:not([value="any"])');
    if(event.target.value==='any'&&event.target.checked) specific.forEach(input=>input.checked=false);
    if(event.target.value!=='any'&&event.target.checked) any.checked=false;
    if(!$$('#decades input:checked').length) any.checked=true;
  }
  const parent=event.target.closest('#include-genres, #exclude-genres');
  if(parent&&event.target.checked){
    const other=parent.id==='include-genres'?'#exclude-genres':'#include-genres';
    const matching=$$(`${other} input`).find(input=>input.value===event.target.value); if(matching) matching.checked=false;
  }
  updateInterpretation();
}));
$('#prompt').addEventListener('input',updateInterpretation);

function renderSeeds(){
  $('#selected-seeds').innerHTML=selectedSeeds.length?selectedSeeds.map((seed,index)=>`<span class="seed-pill"><span>${escapeHtml(seed.title)} · ${escapeHtml(seed.artist)}</span><button type="button" data-remove-seed="${index}" aria-label="Remove ${escapeHtml(seed.title)}">×</button></span>`).join(''):'<span class="empty-note">No seed songs selected</span>';
  updateInterpretation();
}

$('#selected-seeds').addEventListener('click',event=>{
  const button=event.target.closest('[data-remove-seed]'); if(!button)return;
  selectedSeeds.splice(Number(button.dataset.removeSeed),1); renderSeeds();
});

$('#seed-search').addEventListener('input',()=>{
  clearTimeout(seedTimer); const term=$('#seed-search').value.trim();
  if(term.length<2){ $('#seed-suggestions').classList.add('hidden'); return; }
  seedTimer=setTimeout(async()=>{
    try{
      const data=await fetch(`/api/tracks/search?q=${encodeURIComponent(term)}`).then(r=>r.json());
      const available=data.tracks.filter(track=>!selectedSeeds.some(seed=>seed.path===track.path));
      $('#seed-suggestions').innerHTML=available.length?available.map((track,index)=>`<button type="button" class="seed-suggestion" data-seed-result="${index}"><b>${escapeHtml(track.title)}</b><small>${escapeHtml(track.artist)}${track.album?' · '+escapeHtml(track.album):''}${track.year?' · '+track.year:''}</small></button>`).join(''):'<button type="button" class="seed-suggestion" disabled>No matching songs found</button>';
      $('#seed-suggestions').dataset.results=JSON.stringify(available); $('#seed-suggestions').classList.remove('hidden');
    }catch{ $('#seed-suggestions').classList.add('hidden'); }
  },250);
});

$('#seed-suggestions').addEventListener('click',event=>{
  const button=event.target.closest('[data-seed-result]'); if(!button)return;
  if(selectedSeeds.length>=5){ setMessage('You can select up to five seed songs.',true); return; }
  const results=JSON.parse($('#seed-suggestions').dataset.results||'[]'); const seed=results[Number(button.dataset.seedResult)];
  if(seed) selectedSeeds.push(seed); $('#seed-search').value=''; $('#seed-suggestions').classList.add('hidden'); renderSeeds();
});
document.addEventListener('click',event=>{ if(!event.target.closest('.seed-search-wrap'))$('#seed-suggestions').classList.add('hidden'); });

function suggestedName(prompt,filters){
  if(prompt) return prompt.replace(/\btop\b/i,'').trim().replace(/\b\w/g,c=>c.toUpperCase());
  const decades=filters.decades.filter(value=>value!=='any');
  const pieces=[]; if(decades.length)pieces.push(decades.map(value=>`${value}s`).join(' + ')); if(filters.include_genres.length)pieces.push(filters.include_genres[0]); if(selectedSeeds.length)pieces.push('Similar Favorites');
  return (pieces.join(' ')||'My Mix').replace(/\b\w/g,c=>c.toUpperCase());
}

$('#find').addEventListener('click',async()=>{
  const prompt=$('#prompt').value.trim(); const sources=$$('input[name=source]:checked').map(input=>input.value);
  if(!sources.length&&!selectedSeeds.length){ setMessage('Choose at least one music source or add a seed song.',true); return; }
  const filters=currentFilters(); const button=$('#find'); const originalButton=button.innerHTML;
  const stages=['Contacting the selected music sources…','Finding songs related to your seeds…','Applying decade and genre rules…','Matching suggestions to your music library…','Balancing artists and ranking the results…'];
  let stage=0; $('#build-label').textContent=stages[0]; $('#build-progress').classList.remove('hidden');
  const stageTimer=setInterval(()=>{ stage=Math.min(stage+1,stages.length-1); $('#build-label').textContent=stages[stage]; },5500);
  button.disabled=true; button.textContent='Building playlist…'; setMessage('');
  try{
    const response=await fetch('/api/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prompt,sources,filters,seed_paths:selectedSeeds.map(seed=>seed.path),include_seeds:$('#include-seeds').checked,limit:Number($('input[name=limit]:checked').value)})});
    const data=await response.json(); if(!response.ok)throw new Error(data.error||'Could not build this playlist.');
    currentMatches=data.matches; updateInterpretation(data.interpretation);
    $('#match-count').textContent=data.matches.length;
    $('#song-list').innerHTML=data.matches.map((track,index)=>{ const badges=track.source_labels.map(label=>`<span class="source-badge">${escapeHtml(label)}</span>`).join(''); const agreement=track.agreement>1?`<span class="agreement">${track.agreement} signals agree</span>`:''; const year=track.match_year?` <span class="song-year">· ${track.match_year}</span>`:''; const relaxed=track.relaxed?.length?`<span class="source-badge">Allowed: ${escapeHtml(track.relaxed.join(', '))}</span>`:''; return `<label class="song"><input type="checkbox" data-index="${index}" checked><div><b>${escapeHtml(track.title)}</b><small>${escapeHtml(track.artist)}${track.album?' · '+escapeHtml(track.album):''}${year}</small></div><span class="source-note">${agreement}${badges}${relaxed}</span></label>`; }).join('');
    $('#playlist-name').value=suggestedName(prompt,filters); $('#results').classList.remove('hidden'); $('#results').scrollIntoView({behavior:'smooth',block:'start'});
    const requested=Number($('input[name=limit]:checked').value); const shortfall=data.matches.length<requested?` Strict rules found ${data.matches.length}; nothing outside those rules was added.`:'';
    const relaxed=data.relaxed_matches?` ${data.relaxed_matches} result${data.relaxed_matches===1?'':'s'} had unknown metadata allowed by Flexible matching.`:'';
    const warning=data.warnings?.length?` ${data.warnings.join(' ')}`:'';
    setMessage(`${data.matches.length} songs matched from ${data.unique_suggestions} unique suggestions.${shortfall}${relaxed}${warning}`,data.matches.length===0);
  }catch(error){ setMessage(error.message,true); }
  finally{ clearInterval(stageTimer); $('#build-progress').classList.add('hidden'); button.disabled=false; button.innerHTML=originalButton; }
});

$('#toggle-all').addEventListener('click',()=>{ const boxes=$$('#song-list input'); const shouldSelect=boxes.some(box=>!box.checked); boxes.forEach(box=>box.checked=shouldSelect); $('#toggle-all').textContent=shouldSelect?'Clear all':'Select all'; });

$('#save').addEventListener('click',async()=>{
  const paths=$$('#song-list input:checked').map(box=>currentMatches[Number(box.dataset.index)].path); const name=$('#playlist-name').value.trim(); const status=$('#save-status');
  if(!name){ status.className='save-status error'; $('.success-mark').textContent='!'; $('#save-title').textContent='Playlist needs a name'; $('#save-message').textContent='Enter a name above, then try again.'; return; }
  if(!paths.length){ status.className='save-status error'; $('.success-mark').textContent='!'; $('#save-title').textContent='No songs selected'; $('#save-message').textContent='Select at least one song, then try again.'; return; }
  const button=$('#save'); const originalButton=button.textContent; button.disabled=true; button.textContent='Saving playlist…'; status.className='save-status pending'; $('#save-title').textContent='Writing playlist file…'; $('#save-message').textContent='Please wait.';
  try{
    const response=await fetch('/api/playlists',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,paths})}); const data=await response.json(); if(!response.ok)throw new Error(data.error||'Could not save playlist.');
    status.className='save-status success'; $('.success-mark').textContent='✓'; $('#save-title').textContent=data.action==='updated'?'Playlist updated':'Playlist created'; $('#save-message').textContent=`${data.filename} now contains ${data.songs} selected songs.`; button.textContent='Created ✓'; loadStatus(); status.scrollIntoView({behavior:'smooth',block:'nearest'});
  }catch(error){ status.className='save-status error'; $('.success-mark').textContent='!'; $('#save-title').textContent='Could not create playlist'; $('#save-message').textContent=error.message; button.textContent=originalButton; }
  finally{ button.disabled=false; }
});

$('#theme').addEventListener('click',()=>{ const dark=document.documentElement.dataset.theme==='dark'; document.documentElement.dataset.theme=dark?'light':'dark'; localStorage.setItem('playlist-theme',dark?'light':'dark'); });
document.documentElement.dataset.theme=localStorage.getItem('playlist-theme')||'light';
updateInterpretation(); loadStatus();
