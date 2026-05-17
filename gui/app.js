
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

function formatApiError(response, payload){
	const detail=payload && typeof payload==='object' && 'detail' in payload ? payload.detail : payload;
	const message=typeof detail==='string' ? detail : JSON.stringify(detail ?? payload ?? '', null, 2);
	return `HTTP ${response.status} ${response.statusText}${message ? `: ${message}` : ''}`;
}

async function apiJson(url, options){
	const response=await fetch(url, options);
	const text=await response.text();
	let payload=null;
	if(text){
		try{
			payload=JSON.parse(text);
		}catch{
			payload=text;
		}
	}
	if(!response.ok){
		throw new Error(formatApiError(response, payload));
	}
	return payload;
}

async function apiText(url, options){
	const response=await fetch(url, options);
	const text=await response.text();
	if(!response.ok){
		let payload=text;
		try{ payload=JSON.parse(text); }catch{}
		throw new Error(formatApiError(response, payload));
	}
	return text;
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
function statusClass(status){
	const value=String(status||'unknown');
	if(['pass','ready','available','complete','completed'].includes(value)) return 'status-pass';
	if(['warn','warning','review','usable_with_warnings','missing'].includes(value)) return 'status-warn';
	if(['fail','failed'].includes(value)) return 'status-fail';
	return 'status-muted';
}

function statusLabel(status){
	return String(status||'unknown').replace(/_/g,' ');
}

function renderStatusPill(status){
	return `<span class="status-pill ${statusClass(status)}">${escapeHtml(statusLabel(status))}</span>`;
}

async function loadWorkflowStatus(){
	const panel=document.getElementById('workflowStatusPanel');
	if(!panel) return;
	panel.textContent='Loading workflow confidence status...';
	try{
		const r=await fetch(`${API}/jobs/workflow-status`);
		const d=await r.json();
		if(!r.ok){
			panel.textContent=JSON.stringify(d,null,2);
			return;
		}
		const summary=d.summary||{};
		let html='<div class="workflow-summary">';
		html+=`<div><strong>Overall</strong>${renderStatusPill(d.overall_status)}</div>`;
		html+=`<div><strong>Dependency warnings</strong><span>${escapeHtml(summary.dependency_warnings??0)}</span></div>`;
		html+=`<div><strong>Gate warnings</strong><span>${escapeHtml(summary.gate_warnings??0)}</span></div>`;
		html+=`<div><strong>Interpretation limits</strong><span>${escapeHtml(summary.interpretation_blockers??0)}</span></div>`;
		html+='</div>';

		const deps=(d.dependencies||[]).map(dep=>(
			`<tr><td>${escapeHtml(dep.label||dep.key)}</td><td>${renderStatusPill(dep.status)}</td><td>${escapeHtml(dep.message||'')}</td></tr>`
		)).join('');
		const gates=(d.gates||[]).map(gate=>(
			`<tr><td>${escapeHtml(gate.label||gate.key)}</td><td>${renderStatusPill(gate.status)}</td><td>${escapeHtml(gate.message||'')}</td></tr>`
		)).join('');
		const stages=(d.stages||[]).map(stage=>(
			`<tr><td>${escapeHtml(stage.label||stage.key)}</td><td>${renderStatusPill(stage.status)}</td><td>${escapeHtml(stage.message||'')}</td></tr>`
		)).join('');

		html+=`<details open><summary>Confidence gates</summary><table class="status-table"><tbody>${gates}</tbody></table></details>`;
		html+=`<details><summary>Dependency health (non-blocking)</summary><table class="status-table"><tbody>${deps}</tbody></table></details>`;
		html+=`<details><summary>Workflow stages</summary><table class="status-table"><tbody>${stages}</tbody></table></details>`;
		panel.innerHTML=html;
	}catch(e){
		panel.textContent=`Workflow status failed: ${e}`;
	}
}

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
		if(d.status === 'completed'){
			loadKPIBanner();
			loadWorkflowStatus();
		}
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

async function loadDataReadiness(){
	const panel=document.getElementById('dataReadinessPanel');
	if(!panel) return;
	panel.textContent='Loading data readiness...';
	try{
		const r=await fetch(`${API}/cases/data-readiness`);
		const d=await r.json();
		if(!r.ok){
			panel.textContent=JSON.stringify(d,null,2);
			return;
		}
		const pct=value=>value===null||value===undefined?'n/a':`${Number(value).toFixed(1)}%`;
		const rows=(d.checks||[]).map(check=>{
			const status=check.missing>0?'needs_review':'ready';
			return `<tr>
				<td>${escapeHtml(check.label)}</td>
				<td>${escapeHtml(check.complete)}</td>
				<td>${escapeHtml(check.missing)}</td>
				<td>${escapeHtml(pct(check.percent))}</td>
				<td>${renderStatusPill(status)}</td>
			</tr>`;
		}).join('');
		panel.innerHTML=`
			<div class="readiness-summary">
				<div><strong>Total cases</strong><span>${escapeHtml(d.total_cases)}</span></div>
				<div><strong>Sequencing coverage</strong><span>${escapeHtml(pct(d.sequencing_coverage?.percent))}</span></div>
				<div><strong>QC completeness</strong><span>${escapeHtml(pct(d.qc_completeness?.percent))}</span></div>
				<div><strong>Status</strong>${renderStatusPill(d.status)}</div>
			</div>
			<table class="data-table readiness-table">
				<thead><tr><th>Readiness check</th><th>Complete</th><th>Missing</th><th>Percent</th><th>Status</th></tr></thead>
				<tbody>${rows||'<tr><td colspan="5">No readiness checks available</td></tr>'}</tbody>
			</table>`;
	}catch(e){
		panel.textContent=`Data readiness failed: ${e}`;
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
		const data=await apiJson(`${API}/cases/audit-trail?limit=20`);
		const formatted=(data.entries||[]).map(e=>({
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
let _cicCurrentMembers = [];

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
		_cicRenderEpiCaseOptions(d.members || []);
		await cicLoadStructuredEvidence();
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
	_cicCurrentMembers = members;
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

function _cicRenderEpiCaseOptions(members){
	const select=document.getElementById('cicEpiCaseSelect');
	if(!select) return;
	const current=select.value;
	select.innerHTML='';
	for(const member of members){
		const opt=document.createElement('option');
		opt.value=member.case_id||'';
		opt.textContent=`${(member.case_id||'').slice(0,8)} — ${member.region||'unknown region'}`;
		select.appendChild(opt);
	}
	if(current && members.some(m=>m.case_id===current)) select.value=current;
}

function _cicSelectedCaseId(){
	const select=document.getElementById('cicEpiCaseSelect');
	return (select?.value || _cicCurrentMembers[0]?.case_id || '').trim();
}

async function cicLoadStructuredEvidence(){
	const box=document.getElementById('cicStructuredEvidence');
	if(!box) return;
	const caseId=_cicSelectedCaseId();
	if(!caseId){
		box.innerHTML='<p class="hint">No case selected.</p>';
		return;
	}
	box.innerHTML='<p class="hint">Loading structured evidence...</p>';
	try{
		const [locationResp, contactResp]=await Promise.all([
			fetch(`${API}/epidemiology/case-location-events?case_id=${encodeURIComponent(caseId)}`),
			fetch(`${API}/epidemiology/case-contact-links?case_id=${encodeURIComponent(caseId)}`),
		]);
		const locations=locationResp.ok ? await locationResp.json() : [];
		const contacts=contactResp.ok ? await contactResp.json() : [];
		let html='<div class="cic-evidence-results">';
		html+='<h4>Location events</h4>';
		if(locations.length){
			html+='<table class="data-table"><thead><tr><th>Type</th><th>Location</th><th>Confidence</th><th>Notes</th></tr></thead><tbody>';
			for(const event of locations){
				html+=`<tr><td>${escapeHtml(event.event_type||'')}</td><td><code>${escapeHtml((event.location_id||'').slice(0,8))}</code></td><td>${escapeHtml(event.confidence||'')}</td><td>${escapeHtml(event.notes||'')}</td></tr>`;
			}
			html+='</tbody></table>';
		}else{
			html+='<p class="hint">No location events recorded.</p>';
		}
		html+='<h4>Contact links</h4>';
		if(contacts.length){
			html+='<table class="data-table"><thead><tr><th>Type</th><th>Contact</th><th>Confidence</th><th>Notes</th></tr></thead><tbody>';
			for(const link of contacts){
				html+=`<tr><td>${escapeHtml(link.link_type||'')}</td><td><code>${escapeHtml((link.contact_id||'').slice(0,8))}</code></td><td>${escapeHtml(link.confidence||'')}</td><td>${escapeHtml(link.notes||'')}</td></tr>`;
			}
			html+='</tbody></table>';
		}else{
			html+='<p class="hint">No contact links recorded.</p>';
		}
		html+='</div>';
		box.innerHTML=html;
	}catch(e){
		box.textContent='Structured evidence load failed: '+e;
	}
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

async function cicRecordLocationEvent(){
	const msg=document.getElementById('cicLocationStatus');
	const caseId=_cicSelectedCaseId();
	const locationName=document.getElementById('cicLocationName').value.trim();
	const eventType=document.getElementById('cicLocationEventType').value.trim();
	if(!caseId || !locationName || !eventType){
		msg.textContent='Select a case and enter location name and event type.';
		return;
	}
	const payload={
		case_id: caseId,
		location_name: locationName,
		location_type: document.getElementById('cicLocationType').value.trim() || null,
		event_type: eventType,
		arrived_at: document.getElementById('cicLocationArrived').value || null,
		departed_at: document.getElementById('cicLocationDeparted').value || null,
		confidence: document.getElementById('cicLocationConfidence').value || null,
		source: 'cluster_investigation_centre',
		notes: document.getElementById('cicLocationNotes').value.trim() || null,
	};
	try{
		const r=await fetch(`${API}/epidemiology/case-location-events`,{
			method:'POST',
			headers:{'Content-Type':'application/json'},
			body:JSON.stringify(payload),
		});
		if(r.ok){
			msg.textContent='✓ Location event recorded.';
			document.getElementById('cicLocationName').value='';
			document.getElementById('cicLocationType').value='';
			document.getElementById('cicLocationNotes').value='';
			await cicLoadStructuredEvidence();
		}else{
			const e=await r.json();
			msg.textContent='Error: '+(e.detail||r.status);
		}
	}catch(e){ msg.textContent='Error: '+e; }
}

async function cicRecordContactLink(){
	const msg=document.getElementById('cicContactStatus');
	const caseId=_cicSelectedCaseId();
	const contactLabel=document.getElementById('cicContactLabel').value.trim();
	const linkType=document.getElementById('cicContactLinkType').value.trim();
	if(!caseId || !contactLabel){
		msg.textContent='Select a case and enter a contact label.';
		return;
	}
	const payload={
		case_id: caseId,
		contact_label: contactLabel,
		relationship_type: document.getElementById('cicContactRelationship').value.trim() || null,
		link_type: linkType || null,
		exposure_start_date: document.getElementById('cicContactStart').value || null,
		exposure_end_date: document.getElementById('cicContactEnd').value || null,
		confidence: document.getElementById('cicContactConfidence').value || null,
		source: 'cluster_investigation_centre',
		notes: document.getElementById('cicContactNotes').value.trim() || null,
	};
	try{
		const r=await fetch(`${API}/epidemiology/case-contact-links`,{
			method:'POST',
			headers:{'Content-Type':'application/json'},
			body:JSON.stringify(payload),
		});
		if(r.ok){
			msg.textContent='✓ Contact link recorded.';
			document.getElementById('cicContactLabel').value='';
			document.getElementById('cicContactRelationship').value='';
			document.getElementById('cicContactNotes').value='';
			await cicLoadStructuredEvidence();
		}else{
			const e=await r.json();
			msg.textContent='Error: '+(e.detail||r.status);
		}
	}catch(e){ msg.textContent='Error: '+e; }
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

// ── Step 7: Phylogenetic + Visual Analytics ────────────────────────────────
function _analyticsClusterId(){
	return (document.getElementById('analyticsClusterId')?.value||'').trim();
}

function _analyticsParams(){
	const snpThreshold=Number(document.getElementById('analyticsSnpThreshold')?.value||12);
	const epiWindowDays=Number(document.getElementById('analyticsEpiWindowDays')?.value||45);
	const posteriorMin=Number(document.getElementById('analyticsPosteriorMin')?.value||0);
	return {
		snpThreshold: Number.isFinite(snpThreshold)?Math.max(1,Math.min(100,snpThreshold)):12,
		epiWindowDays: Number.isFinite(epiWindowDays)?Math.max(1,Math.min(365,epiWindowDays)):45,
		posteriorMin: Number.isFinite(posteriorMin)?Math.max(0,Math.min(1,posteriorMin)):0,
	};
}

function selectAnalyticsCluster(){
	const sel=document.getElementById('analyticsClusterSelect');
	const input=document.getElementById('analyticsClusterId');
	if(sel&&input) input.value=sel.value||'';
}

function selectSynthesisCluster(){
	const sel=document.getElementById('synthesisClusterSelect');
	const input=document.getElementById('synthesisClusterId');
	if(sel&&input) input.value=sel.value||'';
}

async function loadAnalyticsClusters(){
	const selectors=[
		document.getElementById('analyticsClusterSelect'),
		document.getElementById('synthesisClusterSelect'),
	].filter(Boolean);
	if(!selectors.length) return;
	try{
		const d=await fetch(`${API}/analytics/clusters`).then(r=>r.json());
		for(const sel of selectors){
			sel.innerHTML='<option value="">All clusters</option>';
		}
		for(const c of (d.clusters||[])){
			for(const sel of selectors){
				const o=document.createElement('option');
				o.value=c.cluster_id;
				o.textContent=`${c.cluster_short} (${c.case_count})`;
				sel.appendChild(o);
			}
		}
	}catch(_e){
		// keep default option on backend issues
	}
}

function _synthesisClusterId(){
	return (document.getElementById('synthesisClusterId')?.value||'').trim();
}

function _synthesisParams(){
	const snpThreshold=Number(document.getElementById('synthesisSnpThreshold')?.value||12);
	const temporalWindowDays=Number(document.getElementById('synthesisTemporalWindow')?.value||45);
	const posteriorMin=Number(document.getElementById('synthesisPosteriorMin')?.value||0);
	const highPosteriorThreshold=Number(document.getElementById('synthesisHighPosteriorThreshold')?.value||0.7);
	return {
		snpThreshold: Number.isFinite(snpThreshold)?Math.max(1,Math.min(100,snpThreshold)):12,
		temporalWindowDays: Number.isFinite(temporalWindowDays)?Math.max(1,Math.min(365,temporalWindowDays)):45,
		posteriorMin: Number.isFinite(posteriorMin)?Math.max(0,Math.min(1,posteriorMin)):0,
		highPosteriorThreshold: Number.isFinite(highPosteriorThreshold)?Math.max(0,Math.min(1,highPosteriorThreshold)):0.7,
	};
}

function _renderSynthesisClusterTable(clusters){
	if(!clusters.length) return '<p class="hint">No synthesis clusters available for the current dataset.</p>';
	let html='<table class="data-table"><thead><tr><th>Cluster</th><th>Members</th><th>Pairs</th><th>Priority</th><th>Band</th><th>Flags</th><th>Actions</th></tr></thead><tbody>';
	for(const c of clusters.slice(0,10)){
		const summary=c.summary||{};
		html+=`<tr><td><code>${escapeHtml(c.cluster_short||String(c.cluster_id||'').slice(0,8))}</code></td><td>${escapeHtml(summary.member_count||0)}</td><td>${escapeHtml(summary.pair_count||0)}</td><td>${escapeHtml(summary.priority_score||0)}</td><td>${escapeHtml(summary.priority_band||'low')}</td><td>${escapeHtml((c.flags||[]).join(', ')||'none')}</td><td>${escapeHtml((c.recommended_investigation_actions||[]).slice(0,3).join(' | ')||'none')}</td></tr>`;
	}
	html+='</tbody></table>';
	return html;
}

async function loadTransmissionSynthesisOverview(){
	const summary=document.getElementById('synthesisSummary');
	const view=document.getElementById('synthesisPrimaryView');
	const btn=document.getElementById('loadSynthesisBtn');
	const originalBtnLabel=btn?.textContent || 'Load synthesis overview';
	if(btn){ btn.disabled=true; btn.textContent='Loading synthesis…'; }
	if(summary) summary.textContent='Loading synthesis overview...';
	if(view) view.textContent='';
	try{
		const p=_synthesisParams();
		const cid=_synthesisClusterId();
		const url=cid
			? `${API}/analytics/transmission-synthesis/${encodeURIComponent(cid)}?min_posterior=${encodeURIComponent(p.posteriorMin)}&low_snp_threshold=${encodeURIComponent(p.snpThreshold)}&high_posterior_threshold=${encodeURIComponent(p.highPosteriorThreshold)}&temporal_window_days=${encodeURIComponent(p.temporalWindowDays)}`
			: `${API}/analytics/transmission-synthesis?min_posterior=${encodeURIComponent(p.posteriorMin)}&low_snp_threshold=${encodeURIComponent(p.snpThreshold)}&high_posterior_threshold=${encodeURIComponent(p.highPosteriorThreshold)}&temporal_window_days=${encodeURIComponent(p.temporalWindowDays)}`;
		const d=await fetch(url).then(r=>r.json());
		const s=d.summary||{};
		const warning=d.warning?`<p class="hint">${escapeHtml(d.warning)}</p>`:'';
		if(summary){
			summary.innerHTML=`<div class="kpi-strip">
				Clusters: ${escapeHtml(s.cluster_count||0)} |
				Pairs: ${escapeHtml(s.pair_count||0)} |
				High priority pairs: ${escapeHtml(s.high_priority_pairs||0)} |
				Contradictory pairs: ${escapeHtml(s.contradictory_pairs||0)}
			</div>${warning}`;
		}
		if(view){
			const clusters=d.clusters||[];
			let html=`<h4>${cid?'Cluster synthesis':'Transmission synthesis overview'}</h4>`;
			html+=`<p class="hint">Validation: ${escapeHtml(d.validation_status||'unknown')} · Generated ${escapeHtml((d.generated_at||'').replace('T',' ').replace('Z',' UTC'))}</p>`;
			html+=_renderSynthesisClusterTable(clusters);
			if(cid && clusters[0]){
				const cluster=clusters[0];
				html+=`<h5>Pairwise evidence for ${escapeHtml(cluster.cluster_short||cid.slice(0,8))}</h5>`;
				html+=`<div class="kpi-strip">${escapeHtml(cluster.explanation||'')}</div>`;
				html+='<table class="data-table"><thead><tr><th>Source</th><th>Target</th><th>Posterior</th><th>SNP</th><th>Confidence</th><th>Interpretation</th></tr></thead><tbody>';
				for(const pair of (cluster.pairwise_transmission_evidence||[]).slice(0,20)){
					html+=`<tr><td>${escapeHtml(String(pair.source||'').slice(0,8))}</td><td>${escapeHtml(String(pair.target||'').slice(0,8))}</td><td>${escapeHtml(pair.posterior_probability??'')}</td><td>${escapeHtml(pair.snp_distance??'n/a')}</td><td>${escapeHtml(pair.confidence||'')}</td><td>${escapeHtml(pair.interpretation||'')}</td></tr>`;
				}
				html+='</tbody></table>';
			}
			view.innerHTML=html;
		}
	}catch(e){
		if(summary) summary.textContent='Failed to load synthesis overview: '+e;
		if(view) view.textContent='';
	}finally{
		if(btn){ btn.disabled=false; btn.textContent=originalBtnLabel; }
	}
}

async function loadClusterRiskSummaryView(){
	const summary=document.getElementById('synthesisSummary');
	const view=document.getElementById('synthesisPrimaryView');
	if(summary) summary.textContent='Loading cluster risk summary...';
	if(view) view.textContent='';
	try{
		const p=_synthesisParams();
		const d=await fetch(`${API}/analytics/cluster-risk-summary?min_posterior=${encodeURIComponent(p.posteriorMin)}&low_snp_threshold=${encodeURIComponent(p.snpThreshold)}&high_posterior_threshold=${encodeURIComponent(p.highPosteriorThreshold)}&temporal_window_days=${encodeURIComponent(p.temporalWindowDays)}`).then(r=>r.json());
		if(summary){
			summary.innerHTML=`<div class="kpi-strip">
				Clusters ranked: ${escapeHtml((d.clusters||[]).length)} |
				Validation: ${escapeHtml(d.validation_status||'unknown')}
			</div>${d.warning?`<p class="hint">${escapeHtml(d.warning)}</p>`:''}`;
		}
		if(view){
			let html='<h4>Cluster risk summary</h4>';
			html+=_renderSynthesisClusterTable(d.clusters||[]);
			view.innerHTML=html;
		}
	}catch(e){
		if(summary) summary.textContent='Failed to load cluster risk summary: '+e;
		if(view) view.textContent='';
	}
}

async function loadAnalyticsSnapshot(){
	const box=document.getElementById('analyticsSummary');
	box.textContent='Loading analytics snapshot...';
	try{
		const p=_analyticsParams();
		const [timeline,geo,growth,cmp]=await Promise.all([
			fetch(`${API}/analytics/timeline`).then(r=>r.json()),
			fetch(`${API}/analytics/geo-map`).then(r=>r.json()),
			fetch(`${API}/analytics/cluster-growth`).then(r=>r.json()),
			fetch(`${API}/analytics/genomic-vs-epi?snp_threshold=${encodeURIComponent(p.snpThreshold)}&epi_window_days=${encodeURIComponent(p.epiWindowDays)}&posterior_min=${encodeURIComponent(p.posteriorMin)}`).then(r=>r.json()),
		]);
		const curves=(growth.curves||[]);
		const topCurve=curves.length?curves[0]:null;
		const s=cmp.summary||{};
		box.innerHTML=`<div class="kpi-strip">
			Timeline events: ${escapeHtml(timeline.event_count||0)} |
			Mapped regions: ${escapeHtml(geo.point_count||0)} |
			Tracked clusters: ${escapeHtml(curves.length)} |
			Largest cluster: ${escapeHtml(topCurve?topCurve.cluster_short+' ('+topCurve.final_size+')':'n/a')}
		</div>
		<div class="kpi-strip">
			Genomic vs epi pairs: ${escapeHtml(s.total_pairs||0)} |
			Both supported: ${escapeHtml(s.both_supported||0)} |
			Genomic only: ${escapeHtml(s.genomic_only||0)} |
			Epi only: ${escapeHtml(s.epi_only||0)}
		</div>`;
	}catch(e){
		box.textContent='Failed to load analytics snapshot: '+e;
	}
}

async function loadSnpMatrixView(){
	const view=document.getElementById('analyticsPrimaryView');
	view.textContent='Loading SNP matrix...';
	try{
		const cid=_analyticsClusterId();
		const p=_analyticsParams();
		const base=`${API}/analytics/snp-matrix?snp_threshold=${encodeURIComponent(p.snpThreshold)}`;
		const url=cid?`${base}&cluster_id=${encodeURIComponent(cid)}`:base;
		const d=await fetch(url).then(r=>r.json());
		if(!d.case_count){ view.textContent='No sequenced cases available for SNP matrix.'; return; }
		let html=`<h4>SNP Distance Matrix (${escapeHtml(d.case_count)} cases)</h4><p class="hint">${escapeHtml(d.message||'')}</p>`;
		html+='<div class="analytics-table-wrap"><table class="data-table snp-matrix"><thead><tr><th>Case</th>';
		for(const sid of d.short_case_ids){ html+=`<th>${escapeHtml(sid)}</th>`; }
		html+='</tr></thead><tbody>';
		for(let i=0;i<d.case_count;i++){
			html+=`<tr><th>${escapeHtml(d.short_case_ids[i])}</th>`;
			for(let j=0;j<d.case_count;j++){
				const val=d.matrix[i][j];
				const cls=val===0?'snp-self':(val<=12?'snp-close':(val<=25?'snp-mid':'snp-far'));
				html+=`<td class="${cls}">${escapeHtml(val)}</td>`;
			}
			html+='</tr>';
		}
		html+='</tbody></table></div>';
		view.innerHTML=html;
	}catch(e){
		view.textContent='Failed to load SNP matrix: '+e;
	}
}

async function loadPhyloTreeView(){
	const view=document.getElementById('analyticsPrimaryView');
	view.textContent='Loading phylogenetic view...';
	try{
		const d=await fetch(`${API}/analytics/phylo-tree`).then(r=>r.json());
		const g=d.graph||{};
		let html='<h4>Phylogenetic Tree Visualisation</h4>';
		html+=`<div class="kpi-strip">Nodes: ${escapeHtml(g.node_count||0)} | Edges: ${escapeHtml(g.edge_count||0)}</div>`;
		html+='<div class="analytics-image-row">';
		html+=`<img class="media-plot" src="${escapeAttr(API+(d.images?.outbreaker_tree||''))}" alt="outbreaker tree"/>`;
		html+=`<img class="media-plot" src="${escapeAttr(API+(d.images?.outbreaker_phylo||''))}" alt="phylogenetic tree"/>`;
		html+='</div>';
		if(Array.isArray(g.edges)&&g.edges.length){
			html+='<h5>Top inferred transmission links</h5><table class="data-table"><tr><th>Source</th><th>Target</th><th>Posterior</th><th>Confidence</th></tr>';
			for(const e of g.edges.slice(0,20)){
				html+=`<tr><td>${escapeHtml(String(e.source).slice(0,8))}</td><td>${escapeHtml(String(e.target).slice(0,8))}</td><td>${escapeHtml((e.posterior||0).toFixed(3))}</td><td>${escapeHtml(e.confidence||'')}</td></tr>`;
			}
			html+='</table>';
		}
		view.innerHTML=html;
	}catch(e){
		view.textContent='Failed to load phylogenetic view: '+e;
	}
}

function _sparkline(points, valueKey){
	if(!points.length) return '';
	const w=700,h=170,pad=26;
	const vals=points.map(p=>Number(p[valueKey]||0));
	const max=Math.max(1,...vals);
	const step=points.length>1?(w-pad*2)/(points.length-1):0;
	const path=points.map((p,i)=>{
		const x=pad+i*step;
		const y=h-pad-(Number(p[valueKey]||0)/max)*(h-pad*2);
		return `${i===0?'M':'L'}${x},${y}`;
	}).join(' ');
	const labels=points.map((p,i)=>{
		if(i%Math.max(1,Math.floor(points.length/8))!==0 && i!==points.length-1) return '';
		const x=pad+i*step;
		return `<text x="${x}" y="${h-7}" text-anchor="middle" font-size="10" fill="#6b7280">${escapeHtml((p.month||'').slice(2))}</text>`;
	}).join('');
	return `<svg viewBox="0 0 ${w} ${h}" class="analytics-svg">
		<line x1="${pad}" y1="${h-pad}" x2="${w-pad}" y2="${h-pad}" stroke="#d1d5db"/>
		<path d="${path}" fill="none" stroke="#1d4ed8" stroke-width="2.5"/>
		${labels}
	</svg>`;
}

async function loadTimelineView(){
	const view=document.getElementById('analyticsPrimaryView');
	view.textContent='Loading timeline...';
	try{
		const d=await fetch(`${API}/analytics/timeline`).then(r=>r.json());
		const months=d.monthly_counts||[];
		let html='<h4>Specimen Timeline by Month</h4>';
		html+=_sparkline(months,'count');
		html+='<table class="data-table"><tr><th>Month</th><th>Cases</th></tr>';
		for(const m of months){ html+=`<tr><td>${escapeHtml(m.month)}</td><td>${escapeHtml(m.count)}</td></tr>`; }
		html+='</table>';
		view.innerHTML=html;
	}catch(e){
		view.textContent='Failed to load timeline: '+e;
	}
}

function _geoToSvg(lon,lat,w,h){
	const x=((Number(lon)+180)/360)*w;
	const y=((90-Number(lat))/180)*h;
	return {x,y};
}

async function loadGeoMapView(){
	const view=document.getElementById('analyticsPrimaryView');
	view.textContent='Loading geography map...';
	try{
		const d=await fetch(`${API}/analytics/geo-map`).then(r=>r.json());
		const points=d.points||[];
		const w=760,h=340;
		const max=Math.max(1,...points.map(p=>Number(p.case_count||0)));
		let dots='';
		for(const p of points){
			const c=_geoToSvg(p.lon,p.lat,w,h);
			const r=3+(Number(p.case_count||0)/max)*10;
			dots+=`<circle cx="${c.x.toFixed(1)}" cy="${c.y.toFixed(1)}" r="${r.toFixed(1)}" fill="#0ea5e9" fill-opacity="0.55" stroke="#0369a1"><title>${escapeHtml(p.region)}: ${escapeHtml(p.case_count)} cases</title></circle>`;
		}
		let html='<h4>Geography Map (region centroids)</h4>';
		html+=`<svg viewBox="0 0 ${w} ${h}" class="analytics-svg map-svg"><rect x="0" y="0" width="${w}" height="${h}" fill="#eff6ff"/>${dots}</svg>`;
		html+='<table class="data-table"><tr><th>Region</th><th>Cases</th><th>Clusters</th><th>Recent 90d</th></tr>';
		for(const p of points.slice(0,20)){
			html+=`<tr><td>${escapeHtml(p.region)}</td><td>${escapeHtml(p.case_count)}</td><td>${escapeHtml(p.cluster_count)}</td><td>${escapeHtml(p.recent_cases_90d)}</td></tr>`;
		}
		html+='</table>';
		view.innerHTML=html;
	}catch(e){
		view.textContent='Failed to load map: '+e;
	}
}

async function loadGrowthCurvesView(){
	const view=document.getElementById('analyticsPrimaryView');
	view.textContent='Loading cluster growth curves...';
	try{
		const d=await fetch(`${API}/analytics/cluster-growth`).then(r=>r.json());
		const curves=d.curves||[];
		if(!curves.length){ view.textContent='No cluster growth data available.'; return; }
		let html='<h4>Cluster Growth Curves</h4>';
		for(const c of curves.slice(0,6)){
			html+=`<div class="analytics-subcard"><strong>${escapeHtml(c.cluster_short)}</strong> (${escapeHtml(c.final_size)} cases)`;
			html+=_sparkline(c.points||[],'cumulative');
			html+='</div>';
		}
		view.innerHTML=html;
	}catch(e){
		view.textContent='Failed to load growth curves: '+e;
	}
}

async function loadGenomicVsEpiView(){
	const view=document.getElementById('analyticsPrimaryView');
	view.textContent='Loading genomic vs epi comparison...';
	try{
		const p=_analyticsParams();
		const d=await fetch(`${API}/analytics/genomic-vs-epi?snp_threshold=${encodeURIComponent(p.snpThreshold)}&epi_window_days=${encodeURIComponent(p.epiWindowDays)}&posterior_min=${encodeURIComponent(p.posteriorMin)}`).then(r=>r.json());
		const s=d.summary||{};
		const cfg=d.parameters||{};
		let html='<h4>Genomic vs Epidemiological Link Comparison</h4>';
		html+=`<p class="hint">Using SNP <= ${escapeHtml(cfg.snp_threshold??p.snpThreshold)}, epi window ${escapeHtml(cfg.epi_window_days??p.epiWindowDays)} days, posterior >= ${escapeHtml((cfg.posterior_min??p.posteriorMin).toFixed ? (cfg.posterior_min??p.posteriorMin).toFixed(2) : (cfg.posterior_min??p.posteriorMin))}</p>`;
		html+=`<div class="kpi-strip">Total: ${escapeHtml(s.total_pairs||0)} | Both: ${escapeHtml(s.both_supported||0)} | Genomic-only: ${escapeHtml(s.genomic_only||0)} | Epi-only: ${escapeHtml(s.epi_only||0)} | Neither: ${escapeHtml(s.neither||0)}</div>`;
		html+='<table class="data-table"><tr><th>Pair</th><th>Posterior</th><th>SNP</th><th>Genomic</th><th>Epi</th><th>Category</th></tr>';
		for(const p of (d.pairs||[]).slice(0,40)){
			html+=`<tr><td>${escapeHtml(p.pair)}</td><td>${escapeHtml((p.posterior||0).toFixed(3))}</td><td>${escapeHtml(p.snp_distance??'n/a')}</td><td>${p.genomic_supported?'Y':'N'}</td><td>${p.epi_supported?'Y':'N'}</td><td>${escapeHtml(p.category)}</td></tr>`;
		}
		html+='</table>';
		if(Array.isArray(d.notes)&&d.notes.length){ html+=`<p class="hint">${escapeHtml(d.notes.join(' '))}</p>`; }
		view.innerHTML=html;
	}catch(e){
		view.textContent='Failed to load genomic vs epi view: '+e;
	}
}

function exportClusterDossier(format){
	const cid=_analyticsClusterId();
	if(!cid){ alert('Enter a cluster UUID first.'); return; }
	window.open(`${API}/analytics/cluster-dossier/${encodeURIComponent(cid)}/export?format=${encodeURIComponent(format)}`,'_blank');
}

// ─────────────────────────────────────────────────────────────────────────────

function _reportParams(){
	const weeks=Number(document.getElementById('reportWeeks')?.value||12);
	const topClusters=Number(document.getElementById('reportTopClusters')?.value||8);
	return {
		weeks: Number.isFinite(weeks)?Math.max(1,Math.min(104,weeks)):12,
		topClusters: Number.isFinite(topClusters)?Math.max(1,Math.min(20,topClusters)):8,
	};
}

function openActionableReport(){
	const p=_reportParams();
	window.open(`${API}/reports/actionable-surveillance.html?weeks=${encodeURIComponent(p.weeks)}&top_clusters=${encodeURIComponent(p.topClusters)}`,'_blank');
}

async function loadActionableReportSummary(){
	const box=document.getElementById('actionableReportSummary');
	if(!box) return;
	box.textContent='Loading final report summary...';
	try{
		const p=_reportParams();
		const d=await apiJson(`${API}/reports/actionable-surveillance?weeks=${encodeURIComponent(p.weeks)}&top_clusters=${encodeURIComponent(p.topClusters)}`);
		const s=d.executive_summary||{};
		const actions=(s.immediate_actions||[]).map(a=>`<li>${escapeHtml(a)}</li>`).join('');
		const clusters=(d.priority_clusters||[]).slice(0,8).map(c=>`<tr><td><code>${escapeHtml(c.cluster_short||String(c.cluster_id||'').slice(0,8))}</code></td><td>${escapeHtml(c.member_count??0)}</td><td>${escapeHtml(c.priority_score??0)}</td><td>${escapeHtml(c.priority_band||'')}</td><td>${escapeHtml((c.flags||[]).join(', ')||'none')}</td></tr>`).join('');
		box.innerHTML=`<div class="kpi-strip">Status: ${escapeHtml(d.status||'unknown')} | Cases: ${escapeHtml(s.total_cases??0)} | Clusters: ${escapeHtml(s.cluster_count??0)} | Urgent clusters: ${escapeHtml(s.urgent_cluster_count??0)} | Contradictory pairs: ${escapeHtml(s.contradictory_pairs??0)}</div><ul class="compact-list">${actions}</ul><table class="data-table"><thead><tr><th>Cluster</th><th>Cases</th><th>Score</th><th>Band</th><th>Flags</th></tr></thead><tbody>${clusters||'<tr><td colspan="5">No priority clusters available.</td></tr>'}</tbody></table>`;
	}catch(e){
		box.textContent='Failed to load final report summary: '+e;
	}
}

async function loadFullKpis(){
	const box=document.getElementById('fullKpisView');
	if(!box) return;
	box.textContent='Loading full surveillance KPIs...';
	try{
		const p=_reportParams();
		const d=await apiJson(`${API}/cases/kpis?weeks=${encodeURIComponent(p.weeks)}`);
		const pct=v=>v===null||v===undefined?'n/a':`${Number(v).toFixed(1)}%`;
		const rows=(d.representativeness_by_region||[]).map(r=>`<tr><td>${escapeHtml(r.region||'Unknown')}</td><td>${escapeHtml(r.eligible_cases??0)}</td><td>${escapeHtml(r.sequenced_cases??0)}</td><td>${escapeHtml(pct(r.sequenced_pct))}</td></tr>`).join('');
		box.innerHTML=`<div class="kpi-strip">Window: ${escapeHtml(d.window_weeks)} weeks | Eligible: ${escapeHtml(d.eligible_cases)} | Sequenced: ${escapeHtml(d.sequenced_cases)} (${escapeHtml(pct(d.sequenced_pct))}) | QC pass: ${escapeHtml(d.qc_pass_cases)} (${escapeHtml(pct(d.qc_pass_pct))}) | Median specimen to QC: ${escapeHtml(d.median_days_specimen_to_qc??'n/a')} days</div><table class="data-table"><thead><tr><th>Region</th><th>Eligible</th><th>Sequenced</th><th>Coverage</th></tr></thead><tbody>${rows||'<tr><td colspan="4">No regional KPI rows available.</td></tr>'}</tbody></table>`;
	}catch(e){
		box.textContent='Failed to load KPIs: '+e;
	}
}

async function loadOutbreakerStatus(){
	const box=document.getElementById('outbreakerStatusView');
	if(!box) return;
	box.textContent='Loading outbreaker status...';
	try{
		const d=await apiJson(`${API}/cases/outbreaker-status`);
		box.innerHTML=`<div class="kpi-strip">Cases export: ${d.cases_export?'available':'missing'} | DNA export: ${d.dna_export?'available':'missing'} | Results RDS: ${d.results_rds?'available':'missing'} | Provenance: ${escapeHtml(d.provenance||'unknown')} | Mock: ${escapeHtml(d.is_mock)}</div>`;
	}catch(e){
		box.textContent='Failed to load outbreaker status: '+e;
	}
}

async function loadCurrentJobLog(){
	const box=document.getElementById('jobLogsView');
	if(!box) return;
	if(!activeJob){
		box.textContent='No active job in this browser session. Start a pipeline or individual job, then load logs.';
		return;
	}
	box.textContent='Loading job log...';
	try{
		box.textContent=await apiText(`${API}/jobs/logs/${encodeURIComponent(activeJob)}`);
	}catch(e){
		box.textContent='Failed to load job log: '+e;
	}
}

async function loadResistanceValidationStatus(){
	const box=document.getElementById('resistanceValidationView');
	if(!box) return;
	box.textContent='Loading resistance validation status...';
	try{
		const d=await apiJson(`${API}/cases/resistance-validation/status`);
		const so=d.signoff||{};
		box.innerHTML=`<div class="kpi-strip">Status: ${escapeHtml(d.status||'under_review')} | Reviewer: ${escapeHtml(so.reviewer||'n/a')} | Catalogue: ${escapeHtml(so.catalogue_version||'n/a')} | Signed: ${escapeHtml(so.signed_off_at||'n/a')}</div>${so.notes?`<p class="hint">${escapeHtml(so.notes)}</p>`:''}`;
	}catch(e){
		box.textContent='Failed to load validation status: '+e;
	}
}

async function approveResistanceValidation(){
	const box=document.getElementById('resistanceValidationView');
	const payload={
		decision: document.getElementById('rvDecision').value,
		reviewer: document.getElementById('rvReviewer').value.trim(),
		notes: document.getElementById('rvNotes').value.trim(),
		catalogue_version: document.getElementById('rvCatalogueVersion').value.trim(),
	};
	if(!payload.reviewer){ if(box) box.textContent='Reviewer is required.'; return; }
	try{
		await apiJson(`${API}/cases/resistance-validation/approve`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
		if(box) box.textContent='Sign-off saved.';
		await loadResistanceValidationStatus();
	}catch(e){
		if(box) box.textContent='Failed to save sign-off: '+e;
	}
}

function _recordTable(rows, columns, empty){
	if(!Array.isArray(rows)||!rows.length) return `<p class="hint">${escapeHtml(empty)}</p>`;
	const header=columns.map(([label])=>`<th>${escapeHtml(label)}</th>`).join('');
	const body=rows.slice(0,25).map(row=>`<tr>${columns.map(([,key])=>`<td>${escapeHtml(row[key]??'')}</td>`).join('')}</tr>`).join('');
	return `<table class="data-table"><thead><tr>${header}</tr></thead><tbody>${body}</tbody></table>`;
}

async function loadExposureRecords(){
	const box=document.getElementById('exposureRecords');
	if(!box) return;
	box.innerHTML='<p class="hint">Loading exposures...</p>';
	try{
		const d=await apiJson(`${API}/epidemiology/exposures?limit=25`);
		box.innerHTML=_recordTable(d,[['ID','exposure_id'],['Type','exposure_type'],['Context','exposure_context'],['Confidence','confidence']], 'No exposures recorded.');
	}catch(e){ box.textContent='Failed to load exposures: '+e; }
}

async function createExposureRecord(){
	const box=document.getElementById('exposureRecords');
	const payload={exposure_type:document.getElementById('exposureType').value.trim(),exposure_context:document.getElementById('exposureContext').value.trim()||null,confidence:document.getElementById('exposureConfidence').value||null,source:'gui_reference_records'};
	if(!payload.exposure_type){ if(box) box.textContent='Exposure type is required.'; return; }
	try{
		await apiJson(`${API}/epidemiology/exposures`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
		document.getElementById('exposureType').value='';
		document.getElementById('exposureContext').value='';
		document.getElementById('exposureConfidence').value='';
		await loadExposureRecords();
	}catch(e){ if(box) box.textContent='Failed to create exposure: '+e; }
}

async function loadContactRecords(){
	const box=document.getElementById('contactRecords');
	if(!box) return;
	box.innerHTML='<p class="hint">Loading contacts...</p>';
	try{
		const d=await apiJson(`${API}/epidemiology/contacts?limit=25`);
		box.innerHTML=_recordTable(d,[['ID','contact_id'],['Label','contact_label'],['Type','contact_type'],['Relationship','relationship_type']], 'No contacts recorded.');
	}catch(e){ box.textContent='Failed to load contacts: '+e; }
}

async function createContactRecord(){
	const box=document.getElementById('contactRecords');
	const payload={contact_label:document.getElementById('contactLabel').value.trim(),contact_type:document.getElementById('contactType').value.trim()||null,relationship_type:document.getElementById('contactRelationship').value.trim()||null};
	if(!payload.contact_label){ if(box) box.textContent='Contact label is required.'; return; }
	try{
		await apiJson(`${API}/epidemiology/contacts`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
		document.getElementById('contactLabel').value='';
		document.getElementById('contactType').value='';
		document.getElementById('contactRelationship').value='';
		await loadContactRecords();
	}catch(e){ if(box) box.textContent='Failed to create contact: '+e; }
}

async function loadLocationRecords(){
	const box=document.getElementById('locationRecords');
	if(!box) return;
	box.innerHTML='<p class="hint">Loading locations...</p>';
	try{
		const d=await apiJson(`${API}/epidemiology/locations?limit=25`);
		box.innerHTML=_recordTable(d,[['ID','location_id'],['Name','location_name'],['Type','location_type'],['Region','geographic_region'],['Postcode','postcode_prefix']], 'No locations recorded.');
	}catch(e){ box.textContent='Failed to load locations: '+e; }
}

async function createLocationRecord(){
	const box=document.getElementById('locationRecords');
	const payload={location_name:document.getElementById('locationName').value.trim(),location_type:document.getElementById('locationType').value.trim()||null,geographic_region:document.getElementById('locationRegion').value.trim()||null,postcode_prefix:document.getElementById('locationPostcode').value.trim()||null};
	if(!payload.location_name){ if(box) box.textContent='Location name is required.'; return; }
	try{
		await apiJson(`${API}/epidemiology/locations`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
		document.getElementById('locationName').value='';
		document.getElementById('locationType').value='';
		document.getElementById('locationRegion').value='';
		document.getElementById('locationPostcode').value='';
		await loadLocationRecords();
	}catch(e){ if(box) box.textContent='Failed to create location: '+e; }
}

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
(async()=>{try{await fetch(`${API}/`);document.getElementById('status').innerHTML='<li>✅ Backend running</li>';}catch{document.getElementById('status').innerHTML='<li>❌ Backend unavailable</li>';}refreshDemoModeStatus();loadRegions();loadKPIBanner();loadWorkflowStatus();loadDataSafety();loadDataReadiness();loadAnalyticsClusters();loadTransmissionSynthesisOverview();loadActionableReportSummary();loadFullKpis();loadOutbreakerStatus();loadResistanceValidationStatus();})();

