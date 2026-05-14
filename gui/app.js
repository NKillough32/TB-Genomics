
let API='http://localhost:8000';const API_FALLBACK='http://127.0.0.1:8010';let activeJob=null;
let demoModeActive=sessionStorage.getItem('tb_demo_mode_active')==='1';

function refreshDemoModeStatus(){
	const status=document.getElementById('demoModeStatus');
	if(!status) return;
	status.textContent=demoModeActive?'Demo mode: ON (session only)':'Demo mode: OFF';
	status.className=demoModeActive?'demo-mode-status on':'demo-mode-status';
}

function openDemoModeDialog(){
	const modal=document.getElementById('demoModeModal');
	const input=document.getElementById('demoModeConfirmInput');
	if(!modal||!input) return;
	input.value='';
	modal.classList.add('open');
	modal.setAttribute('aria-hidden','false');
	setTimeout(()=>input.focus(),0);
}


function escapeHtml(value){
	return String(value??'')
		.replace(/&/g,'&amp;')
		.replace(/</g,'&lt;')
		.replace(/>/g,'&gt;')
		.replace(/"/g,'&quot;')
		.replace(/'/g,'&#39;');
}

function escapeAttr(value){
	return escapeHtml(value).replace(/`/g,'&#96;');
}

function closeDemoModeDialog(){
	const modal=document.getElementById('demoModeModal');
	if(!modal) return;
	modal.classList.remove('open');
	modal.setAttribute('aria-hidden','true');
}

function confirmDemoMode(){
	const input=document.getElementById('demoModeConfirmInput');
	const value=(input?.value||'').trim().toUpperCase();
	if(value!=='DEMO'){
		alert('Please type DEMO exactly to activate demo mode.');
		return;
	}
	demoModeActive=true;
	sessionStorage.setItem('tb_demo_mode_active','1');
	refreshDemoModeStatus();
	closeDemoModeDialog();
}

// ── KPI Banner ──────────────────────────────────────────────────────────────
async function loadKPIBanner(){
	try{
		const [summaryResp, lastRunResp] = await Promise.all([
			fetch(`${API}/cases/summary`),
			fetch(`${API}/jobs/last-run-times`),
		]);
		if(summaryResp.ok){
			const s = await summaryResp.json();
			document.querySelector('#kpiTotalCases .kpi-num').textContent = s.total_cases ?? '—';
			document.querySelector('#kpiClustered .kpi-num').textContent = s.clustered_cases ?? '—';
			document.querySelector('#kpiUnclustered .kpi-num').textContent = s.unclustered_cases ?? '—';
			document.querySelector('#kpiOpenClusters .kpi-num').textContent = s.open_clusters ?? '—';
			document.getElementById('kpiBannerTimestamp').textContent = 'refreshed ' + new Date().toLocaleTimeString();
		}
		if(lastRunResp.ok){
			const lr = await lastRunResp.json();
			const fmt = iso => iso ? new Date(iso).toLocaleString() : 'never';
			const lines = [
				`Lineage/DR: ${fmt(lr.lineage_dr_validation)}`,
				`Seq clusters: ${fmt(lr.sequence_clusters)}`,
				`Outbreaker2: ${fmt(lr.outbreaker2)}`,
				`Comparison: ${fmt(lr.cluster_comparison)}`,
			];
			document.getElementById('kpiLastRun').textContent = 'Last run — ' + lines.join(' · ');
		}
	}catch(e){
		// silently fail — banner is informational only
	}
}

// ── Full Pipeline ────────────────────────────────────────────────────────────
async function runFullPipeline(){
	const btn = document.getElementById('runPipelineBtn');
	btn.disabled = true;
	btn.textContent = '⏳ Pipeline running…';
	document.getElementById('jobStatus').textContent = 'Starting full pipeline…';
	document.getElementById('pipelineStepLabel').textContent = '';
	try{
		const r = await fetch(`${API}/jobs/run-pipeline`, {method:'POST'});
		const d = await r.json();
		if(!d.job_id){
			document.getElementById('jobStatus').textContent = JSON.stringify(d, null, 2);
			btn.disabled = false;
			btn.textContent = '▶ Run full pipeline (all steps)';
			return;
		}
		activeJob = d.job_id;
		pollPipeline(d.steps || []);
	}catch(e){
		document.getElementById('jobStatus').textContent = `Pipeline start failed: ${e}`;
		btn.disabled = false;
		btn.textContent = '▶ Run full pipeline (all steps)';
	}
}
async function pollPipeline(steps){
	if(!activeJob) return;
	const r = await fetch(`${API}/jobs/status/${activeJob}`);
	const d = await r.json();
	const bar = document.getElementById('progressBar');
	bar.style.width = (d.progress||0)+'%';
	bar.textContent = (d.progress||0)+'%';
	const stepIdx = d.pipeline_step || 0;
	const total = d.pipeline_total || steps.length;
	if(stepIdx > 0 && stepIdx <= steps.length){
		document.getElementById('pipelineStepLabel').textContent =
			`Step ${stepIdx} of ${total}: ${steps[stepIdx-1]}`;
	}
	document.getElementById('jobStatus').textContent = JSON.stringify(d, null, 2);
	if(d.status !== 'completed' && d.status !== 'failed'){
		setTimeout(()=>pollPipeline(steps), 1500);
	} else {
		const btn = document.getElementById('runPipelineBtn');
		btn.disabled = false;
		btn.textContent = '▶ Run full pipeline (all steps)';
		if(d.status === 'completed') loadKPIBanner();
	}
}

// ── Bulk Export ──────────────────────────────────────────────────────────────
function downloadAllExports(){
	window.open(`${API}/jobs/download-all-exports`, '_blank');
}

async function loadDataSafety(){
	const statusEl = document.getElementById('status');
	if(!statusEl) return;
	try{
		const r = await fetch(`${API}/cases/data-safety`);
		if(!r.ok) return;
		const d = await r.json();
		const existing = document.getElementById('dataSafetyStatus');
		const html = d.operational_safe
			? `✅ Dataset mode: OPERATIONAL (${d.total_cases} cases)`
			: `⚠️ Dataset mode: NON-OPERATIONAL (synthetic/demo detected: ${d.synthetic_case_count} synthetic cases, ${d.synthetic_seed_events} seed events)`;
		if(existing){
			existing.textContent = html;
		}else{
			statusEl.innerHTML = `${statusEl.innerHTML}<li id="dataSafetyStatus">${html}</li>`;
		}
	}catch(_e){
		// no-op; keep existing status text
	}
}

async function uploadFile(){const f=document.getElementById('fileInput').files[0];if(!f)return;const fd=new FormData();fd.append('file',f);const r=await fetch(`${API}/ingest/file`,{method:'POST',body:fd});document.getElementById('uploadResult').textContent=JSON.stringify(await r.json());}
async function seedSyntheticData(){
	if(!demoModeActive){
		openDemoModeDialog();
		return;
	}
	const caseCount=Number(document.getElementById('seedCaseCount').value||250);
	const seed=Number(document.getElementById('seedValue').value||42);
	const reset=document.getElementById('seedReset').checked;
	const selectedCountries=Array.from(document.getElementById('seedCountries').selectedOptions).map(o=>o.value);
	const resultBox=document.getElementById('seedResult');
	resultBox.textContent='Generating synthetic dataset...';
	try{
		let url=`${API}/ingest/seed-synthetic?case_count=${encodeURIComponent(caseCount)}&reset=${encodeURIComponent(reset)}&seed=${encodeURIComponent(seed)}`;
		if(selectedCountries.length>0) url+=`&countries=${encodeURIComponent(selectedCountries.join(','))}`;
		const r=await fetch(url,{method:'POST'});
		const payload=await r.json();
		if(!r.ok){
			if(payload?.detail?.error==='synthetic_seeding_disabled'){
				resultBox.textContent='Synthetic seeding is disabled on backend. For demo mode set TB_ENABLE_SYNTHETIC_SEEDING=1 in backend terminal and restart backend.';
			}else{
				resultBox.textContent=JSON.stringify(payload,null,2);
			}
			loadDataSafety();
			return;
		}
		resultBox.textContent=JSON.stringify(payload,null,2);
		// Refresh region dropdown after seeding
		loadRegions();
		loadDataSafety();
	}catch(e){
		resultBox.textContent=`Failed to generate synthetic data: ${e}`;
		loadDataSafety();
	}
}
async function runJob(job){
	document.getElementById('jobStatus').textContent='Starting '+job;
	let r=await fetch(`${API}/jobs/run/${job}`,{method:'POST'});
	let d=await r.json();

	// If main API points to an older backend process, retry derive step on fallback port.
	if(
		job==='derive_sequence_clusters' &&
		d && d.error==='Job not allowed' &&
		API!==API_FALLBACK
	){
		const r2=await fetch(`${API_FALLBACK}/jobs/run/${job}`,{method:'POST'});
		const d2=await r2.json();
		if(d2 && d2.job_id){
			API=API_FALLBACK;
			d=d2;
		}
	}

	if(!d.job_id){
		document.getElementById('jobStatus').textContent=JSON.stringify(d,null,2);
		return;
	}
	activeJob=d.job_id;
	poll();
}
async function poll(){if(!activeJob)return;const r=await fetch(`${API}/jobs/status/${activeJob}`);const d=await r.json();document.getElementById('jobStatus').textContent=JSON.stringify(d,null,2);const bar=document.getElementById('progressBar');bar.style.width=(d.progress||0)+'%';bar.textContent=(d.progress||0)+'%';if(d.status!=='completed'&&d.status!=='failed'){setTimeout(poll,1500);} }
async function loadCases(){const r=await fetch(`${API}/cases`);document.getElementById('cases').textContent=JSON.stringify(await r.json(),null,2);} 
async function loadOutbreakerResults(){
	const box=document.getElementById('outbreakerResults');
	box.textContent='Loading analysis...';
	try{
		const [summaryResp,analysisResp]=await Promise.all([
			fetch(`${API}/cases/summary`),
			fetch(`${API}/cases/outbreaker-analysis`),
		]);
		const summary=await summaryResp.json();
		const analysis=await analysisResp.json();
		
		// Build HTML display
		let html='<div class="result-panel">';
		
		// Summary stats
		html+='<h4>Case Summary</h4>';
		html+=`<p>Total: ${escapeHtml(summary.total_cases)} | Clustered: ${escapeHtml(summary.clustered_cases)} | Unclustered: ${escapeHtml(summary.unclustered_cases)}</p>`;
		
		// Outbreaker analysis
		html+='<h4>Outbreak Analysis</h4>';
		html+=`<p>Status: ${escapeHtml(analysis.status)}</p>`;
		if(analysis.summary){
			const posteriorSamples=(analysis.summary.n_samples??'n/a');
			const caseCount=(analysis.summary.case_count??'n/a');
			html+=`<div class="kpi-strip">Case count: ${escapeHtml(caseCount)} | Posterior samples (MCMC): ${escapeHtml(posteriorSamples)} | Mean Likelihood: ${escapeHtml(analysis.summary.likelihood_mean?.toFixed(2))}</div>`;
		}

		if(analysis.transmission_network){
			const net=analysis.transmission_network;
			html+='<h4>Transmission Network Insights</h4>';
			html+=`<div class="kpi-strip">Nodes: ${escapeHtml(net.node_count||0)} | Links: ${escapeHtml(net.edge_count||0)} | Clusters: ${escapeHtml(net.cluster_count||0)} | High-confidence links: ${escapeHtml(net.high_confidence_edges||0)}</div>`;
			if(Array.isArray(net.key_nodes)&&net.key_nodes.length>0){
				html+='<p><strong>Potential priority spreaders</strong></p>';
				html+='<table class="data-table">';
				html+='<tr><th>Case</th><th>Cluster</th><th>Region</th><th>Risk</th><th>Band</th><th>Out</th><th>In</th></tr>';
				for(const n of net.key_nodes.slice(0,8)){
					html+=`<tr><td>${escapeHtml(n.case_id)}</td><td>${escapeHtml((n.cluster_id||'').toString().slice(0,8))}</td><td>${escapeHtml(n.region||'Unknown')}</td><td>${escapeHtml(n.risk_score??0)}</td><td>${escapeHtml(n.risk_band||'low')}</td><td>${escapeHtml(n.outgoing_links??0)}</td><td>${escapeHtml(n.incoming_links??0)}</td></tr>`;
				}
				html+='</table>';
			}
		}
		
		// Graphics
		if(analysis.graphics.length > 0){
			html+='<h4>Diagnostic Plots</h4>';
			for(const graphic of analysis.graphics){
				const fullUrl=graphic.url.startsWith('http')?graphic.url:`${API}${graphic.url}`;
				html+=`<img src="${escapeAttr(fullUrl)}" class="media-plot" alt="${escapeAttr(graphic.type)}"/>`;
			}
		}
		
		html+='</div>';
		box.innerHTML=html;
	}catch(e){
		box.textContent=`Failed to load analysis: ${e}`;
	}
}
async function loadLineageDrValidation(){
	const box=document.getElementById('lineageDrResults');
	box.textContent='Loading lineage/DR validation...';
	try{
		const r=await fetch(`${API}/cases/lineage-dr-validation`);
		const payload=await r.json();
		let html='<div class="result-panel">';
		html+=`<h4>Validation status: ${escapeHtml(payload.status||'unknown')}</h4>`;
		const summary=payload.analysis_summary||{};
		const epi=payload.analysis_epi_summary||{};
		html+=`<div class="kpi-strip">Interpreted samples: ${summary.interpreted_samples||0} | With lineage: ${summary.samples_with_lineage||0} | With resistance calls: ${summary.samples_with_resistance_calls||0}</div>`;
		html+=`<div class="kpi-strip">Any resistance signal: ${epi.samples_with_any_resistance_signal||0} | Rifampicin-resistant (suspected): ${epi.rifampicin_resistant_suspected||0} | MDR (suspected): ${epi.mdr_suspected||0}</div>`;
		if(Array.isArray(epi.top_lineages)&&epi.top_lineages.length>0){
			const topLineages=epi.top_lineages.slice(0,4).map(x=>`${escapeHtml(x.lineage)}: ${escapeHtml(x.count)}`).join(' | ');
			html+=`<p><strong>Top lineages:</strong> ${topLineages}</p>`;
		}
		html+=`<pre class="log-box">${escapeHtml(JSON.stringify(payload,null,2))}</pre>`;
		html+='</div>';
		box.innerHTML=html;
	}catch(e){
		box.textContent=`Failed to load lineage/DR validation: ${e}`;
	}
}
function downloadOutbreakReport(){
	window.open(`${API}/cases/outbreak-report`, '_blank');
}
function openOutbreakReportHtml(){
	window.open(`${API}/cases/outbreak-report.html`, '_blank');
}
function openFullOutbreakReportHtml(){
	window.open(`${API}/cases/outbreak-report.full.html`, '_blank');
}
async function loadAuditTrail(){
	const box=document.getElementById('auditTrail');
	box.textContent='Loading audit trail...';
	try{
		const r=await fetch(`${API}/cases/audit-trail?limit=20`);
		const data=await r.json();
		const formatted=data.entries.map(e=>({
			timestamp:e.timestamp,
			action:e.action,
			user:e.user,
			details:e.details,
		}));
		box.textContent=JSON.stringify(formatted,null,2);
	}catch(e){
		box.textContent=`Failed to load audit trail: ${e}`;
	}
}
async function advancedSearch(){
	const region=document.getElementById('searchRegion').value||null;
	const lineage=document.getElementById('searchLineage').value||null;
	const dateFrom=document.getElementById('searchDateFrom').value||null;
	const dateTo=document.getElementById('searchDateTo').value||null;
	const resistance=document.getElementById('searchResistance').value||null;
	const box=document.getElementById('searchResults');
	box.textContent='Searching cases...';
	try{
		let url=`${API}/cases/search?`;
		const params=[];
		if(region) params.push(`region=${encodeURIComponent(region)}`);
		if(lineage) params.push(`lineage=${encodeURIComponent(lineage)}`);
		if(dateFrom) params.push(`date_from=${encodeURIComponent(dateFrom)}`);
		if(dateTo) params.push(`date_to=${encodeURIComponent(dateTo)}`);
		if(resistance) params.push(`resistance=${encodeURIComponent(resistance)}`);
		url+=params.join('&');
		const r=await fetch(url);
		const data=await r.json();
		let html=`<div class="result-panel"><h4>Search Results: ${escapeHtml(data.total_results)} cases found</h4>`;
		if(data.total_results>0){
			html+='<table class="data-table">';
			html+='<tr><th>Case ID</th><th>Date</th><th>Region</th><th>Lineage</th><th>Cluster</th><th>Action</th></tr>';
			for(const c of data.cases){
				html+=`<tr><td>${escapeHtml(c.case_id)}</td><td>${escapeHtml(c.specimen_date)}</td><td>${escapeHtml(c.region)}</td><td>${escapeHtml(c.lineage)}</td><td>${escapeHtml(c.cluster_id||'-')}</td><td><button type="button" class="mini-btn" onclick="loadCaseHistory(${escapeAttr(JSON.stringify(c.case_id||''))})">View history</button> <button type="button" class="mini-btn" onclick="generateCaseReport(${escapeAttr(JSON.stringify(c.case_id||''))})">Report</button></td></tr>`;
			}
			html+='</table>';
		}
		html+='</div>';
		box.innerHTML=html;
	}catch(e){
		box.textContent=`Search failed: ${e}`;
	}
}
async function loadCaseHistory(caseIdOverride){
	const input=document.getElementById('caseHistoryId');
	const caseId=caseIdOverride||input.value;
	const box=document.getElementById('caseHistory');
	if(!caseId){box.textContent='Please enter a case ID';return;}
	input.value=caseId;
	box.textContent='Loading case history...';
	try{
		const r=await fetch(`${API}/cases/case-history/${encodeURIComponent(caseId)}`);
		const data=await r.json();
		if(data.error){box.textContent=`Case not found: ${data.error}`;return;}
		let html=`<div class="result-panel"><h4>Case ${escapeHtml(data.case_id)}</h4>`;
		html+=`<div class="kpi-strip">Related Cases: ${escapeHtml(data.related_cases)} | Observation Span: ${escapeHtml(data.observation_span_days)} days</div>`;
		if(data.history.length>0){
			html+='<table class="data-table">';
			html+='<tr><th>Specimen Date</th><th>Region</th><th>Lineage</th><th>Status</th><th>Index?</th></tr>';
			for(const h of data.history){
				html+=`<tr><td>${escapeHtml(h.specimen_date)}</td><td>${escapeHtml(h.region)}</td><td>${escapeHtml(h.lineage||'-')}</td><td>${escapeHtml(h.status)}</td><td>${h.is_index_case?'Y':''}</td></tr>`;
			}
			html+='</table>';
		}
		html+='</div>';
		box.innerHTML=html;
		box.scrollIntoView({behavior:'smooth',block:'start'});
	}catch(e){
		box.textContent=`Failed to load case history: ${e}`;
	}
}
function generateCaseReport(caseIdOverride){
	const input=document.getElementById('caseHistoryId');
	const caseId=caseIdOverride||input.value;
	if(!caseId){alert('Please enter a Case ID first.');return;}
	window.open(`${API}/cases/case-report/${encodeURIComponent(caseId)}`,'_blank');
}
// ── Cluster Investigation Centre ─────────────────────────────────────────────
let _cicCurrentCluster = null;

const _CIC_BAND_COLOUR = {
	critical: '#b91c1c',
	high:     '#c2410c',
	medium:   '#b45309',
	low:      '#15803d',
};

async function loadClusterInvestigations(){
	const listEl = document.getElementById('cicList');
	listEl.innerHTML = '<p class="hint">Loading clusters…</p>';
	closeCicPanel();
	try{
		const r = await fetch(`${API}/cluster-investigations`);
		if(!r.ok){ listEl.textContent = 'Failed to load: ' + r.status; return; }
		const data = await r.json();
		if(!data.investigations || data.investigations.length === 0){
			listEl.innerHTML = '<p class="hint">No clusters found. Run analysis first (Step 3).</p>';
			return;
		}
		let html = '<table class="data-table cic-cluster-table">';
		html += '<thead><tr><th>Cluster</th><th>Cases</th><th>Risk</th><th>Score</th>'
		      + '<th>Status</th><th>Assigned to</th><th>Actions</th><th></th></tr></thead><tbody>';
		for(const inv of data.investigations){
			const band = inv.risk_band || 'low';
			const colour = _CIC_BAND_COLOUR[band] || '#374151';
			const statusLabel = (inv.status || 'open').replace(/_/g,' ');
			const signed = inv.status === 'signed_off';
			html += `<tr>
				<td><code>${escapeHtml(inv.cluster_id.slice(0,8))}</code></td>
				<td>${escapeHtml(inv.case_count)}</td>
				<td><span class="cic-band-badge" style="background:${escapeAttr(colour)}">${escapeHtml(band.toUpperCase())}</span></td>
				<td>${escapeHtml(inv.risk_score)}</td>
				<td>${escapeHtml(statusLabel)}${signed ? ' ✓' : ''}</td>
				<td>${escapeHtml(inv.assigned_to || '—')}</td>
				<td>${escapeHtml(inv.action_count)}</td>
				<td><button class="mini-btn" onclick="openCicPanel(${escapeAttr(JSON.stringify(inv.cluster_id))})">Investigate</button></td>
			</tr>`;
		}
		html += '</tbody></table>';
		listEl.innerHTML = html;
	}catch(e){
		listEl.textContent = 'Error: ' + e;
	}
}

async function openCicPanel(clusterId){
	_cicCurrentCluster = clusterId;
	const panel = document.getElementById('cicPanel');
	panel.style.display = 'block';
	panel.removeAttribute('aria-hidden');
	document.getElementById('cicPanelTitle').textContent =
		'Cluster ' + clusterId.slice(0,8) + '…';
	// reset tabs to first
	cicTab(document.querySelector('.cic-tab'), 'cicTabMembers');
	await _cicRefreshDetail();
	panel.scrollIntoView({behavior:'smooth', block:'start'});
}

function closeCicPanel(){
	_cicCurrentCluster = null;
	const panel = document.getElementById('cicPanel');
	panel.style.display = 'none';
	panel.setAttribute('aria-hidden','true');
}

async function _cicRefreshDetail(){
	if(!_cicCurrentCluster) return;
	try{
		const r = await fetch(`${API}/cluster-investigations/${encodeURIComponent(_cicCurrentCluster)}`);
		if(!r.ok) return;
		const d = await r.json();
		_cicRenderRiskStrip(d);
		_cicRenderMembers(d);
		_cicRenderActions(d);
		if(d.epi_notes) document.getElementById('cicEpiNotes').value = d.epi_notes;
		if(d.assigned_to) document.getElementById('cicAssignee').value = d.assigned_to;
	}catch(e){
		console.warn('CIC detail load failed', e);
	}
}

function _cicRenderRiskStrip(d){
	const band = d.risk_band || 'low';
	const colour = _CIC_BAND_COLOUR[band] || '#374151';
	const status = (d.status || 'open').replace(/_/g,' ');
	const assignee = d.assigned_to ? `Assigned to: ${escapeHtml(d.assigned_to)}` : 'Unassigned';
	const comp = d.risk_components || {};
	const compStr = Object.entries(comp)
		.map(([k,v]) => `${k.replace(/_/g,' ')}: ${v}`)
		.join(' | ');
	document.getElementById('cicRiskStrip').innerHTML =
		`<span class="cic-band-badge" style="background:${escapeAttr(colour)};font-size:1rem">${escapeHtml(band.toUpperCase())}</span>
		<strong>${escapeHtml(String(d.risk_score))}</strong> pts &nbsp;|&nbsp;
		Status: <strong>${escapeHtml(status)}</strong> &nbsp;|&nbsp; ${escapeHtml(assignee)}
		${compStr ? `<div class="hint" style="margin-top:.25rem">${escapeHtml(compStr)}</div>` : ''}`;
}

function _cicRenderMembers(d){
	const members = d.members || [];
	let html = `<p class="hint">${members.length} case(s) in this cluster.</p>`;
	if(members.length > 0){
		html += '<table class="data-table"><thead><tr><th>Case ID</th><th>Date</th><th>Region</th><th>Lineage</th><th>Resistance</th></tr></thead><tbody>';
		for(const m of members){
			let dr = m.resistance;
			if(dr && typeof dr === 'object') dr = Object.keys(dr).join(', ') || 'none';
			else if(!dr || dr === 'null') dr = 'none';
			html += `<tr><td><code>${escapeHtml((m.case_id||'').slice(0,8))}</code></td>
				<td>${escapeHtml(m.specimen_date||'')}</td>
				<td>${escapeHtml(m.region||'')}</td>
				<td>${escapeHtml(m.lineage||'')}</td>
				<td>${escapeHtml(String(dr))}</td></tr>`;
		}
		html += '</tbody></table>';
	}
	document.getElementById('cicMembersTable').innerHTML = html;
}

function _cicRenderActions(d){
	const actions = d.actions || [];
	let html = '';
	if(actions.length === 0){
		html = '<p class="hint">No actions recorded yet.</p>';
	}else{
		html = '<table class="data-table"><thead><tr><th>Type</th><th>Description</th><th>By</th><th>Date</th></tr></thead><tbody>';
		for(const a of actions){
			html += `<tr><td>${escapeHtml(a.action_type||'')}</td>
				<td>${escapeHtml(a.description||'')}</td>
				<td>${escapeHtml(a.performed_by||'')}</td>
				<td>${escapeHtml((a.performed_at||'').slice(0,10))}</td></tr>`;
		}
		html += '</tbody></table>';
	}
	document.getElementById('cicActionsTable').innerHTML = html;
}

function cicTab(btn, tabId){
	document.querySelectorAll('.cic-tab').forEach(t => t.classList.remove('active'));
	document.querySelectorAll('.cic-tab-content').forEach(t => { t.style.display='none'; });
	btn.classList.add('active');
	document.getElementById(tabId).style.display = 'block';
}

async function cicAssign(){
	const val = document.getElementById('cicAssignee').value.trim();
	const msg = document.getElementById('cicAssignStatus');
	if(!val || !_cicCurrentCluster){ msg.textContent='Please enter a reviewer name.'; return; }
	try{
		const r = await fetch(`${API}/cluster-investigations/${encodeURIComponent(_cicCurrentCluster)}/assign`,
			{method:'POST', headers:{'Content-Type':'application/json'},
			 body: JSON.stringify({assigned_to: val})});
		if(r.ok){ msg.textContent='✓ Assigned to ' + val; await _cicRefreshDetail(); loadClusterInvestigations(); }
		else { const e=await r.json(); msg.textContent='Error: '+(e.detail||r.status); }
	}catch(e){ msg.textContent='Error: '+e; }
}

async function cicSaveEpiNotes(){
	const notes = document.getElementById('cicEpiNotes').value;
	const msg = document.getElementById('cicEpiStatus');
	if(!_cicCurrentCluster) return;
	try{
		const r = await fetch(`${API}/cluster-investigations/${encodeURIComponent(_cicCurrentCluster)}/epi-notes`,
			{method:'PUT', headers:{'Content-Type':'application/json'},
			 body: JSON.stringify({epi_notes: notes})});
		if(r.ok){ msg.textContent='✓ Notes saved.'; }
		else { const e=await r.json(); msg.textContent='Error: '+(e.detail||r.status); }
	}catch(e){ msg.textContent='Error: '+e; }
}

async function cicRecordAction(){
	const msg = document.getElementById('cicActionStatus');
	if(!_cicCurrentCluster){ msg.textContent='No cluster selected.'; return; }
	const atype = document.getElementById('cicActionType').value;
	const desc  = document.getElementById('cicActionDesc').value.trim();
	const by    = document.getElementById('cicActionBy').value.trim();
	const date  = document.getElementById('cicActionDate').value || null;
	if(!desc || !by){ msg.textContent='Please fill in description and performed by.'; return; }
	try{
		const r = await fetch(`${API}/cluster-investigations/${encodeURIComponent(_cicCurrentCluster)}/actions`,
			{method:'POST', headers:{'Content-Type':'application/json'},
			 body: JSON.stringify({action_type:atype, description:desc, performed_by:by, performed_at:date})});
		if(r.ok){
			msg.textContent='✓ Action recorded.';
			document.getElementById('cicActionDesc').value='';
			document.getElementById('cicActionBy').value='';
			document.getElementById('cicActionDate').value='';
			await _cicRefreshDetail();
			loadClusterInvestigations();
		}else{ const e=await r.json(); msg.textContent='Error: '+(e.detail||r.status); }
	}catch(e){ msg.textContent='Error: '+e; }
}

async function cicSignOff(){
	const msg = document.getElementById('cicSignOffStatus');
	if(!_cicCurrentCluster){ msg.textContent='No cluster selected.'; return; }
	const decision   = document.getElementById('cicDecision').value;
	const decisionBy = document.getElementById('cicDecisionBy').value.trim();
	const notes      = document.getElementById('cicSignOffNotes').value.trim();
	if(!decisionBy){ msg.textContent='Please enter your name / role.'; return; }
	if(!confirm(`Sign off cluster ${_cicCurrentCluster.slice(0,8)} with decision "${decision}"?`)) return;
	try{
		const r = await fetch(`${API}/cluster-investigations/${encodeURIComponent(_cicCurrentCluster)}/sign-off`,
			{method:'POST', headers:{'Content-Type':'application/json'},
			 body: JSON.stringify({decision, decision_by:decisionBy, notes: notes||null})});
		if(r.ok){
			msg.textContent='✓ Investigation signed off.';
			await _cicRefreshDetail();
			loadClusterInvestigations();
		}else{ const e=await r.json(); msg.textContent='Error: '+(e.detail||r.status); }
	}catch(e){ msg.textContent='Error: '+e; }
}

function cicOpenReport(){
	if(!_cicCurrentCluster){ alert('No cluster selected.'); return; }
	window.open(`${API}/cluster-investigations/${encodeURIComponent(_cicCurrentCluster)}/report`, '_blank');
}

// ─────────────────────────────────────────────────────────────────────────────

async function loadRegions(){
	const sel=document.getElementById('searchRegion');
	try{
		const r=await fetch(`${API}/cases/regions`);
		const data=await r.json();
		// Keep the "All regions" placeholder, then rebuild options
		sel.innerHTML='<option value="">All regions</option>';
		for(const region of (data.regions||[])){
			const opt=document.createElement('option');
			opt.value=region;
			opt.textContent=region;
			sel.appendChild(opt);
		}
	}catch{
		// Backend unavailable — leave placeholder only
	}
}
(async()=>{try{await fetch(`${API}/`);document.getElementById('status').innerHTML='<li>✅ Backend running</li>';}catch{document.getElementById('status').innerHTML='<li>❌ Backend unavailable</li>';}refreshDemoModeStatus();loadRegions();loadKPIBanner();loadDataSafety();})();
