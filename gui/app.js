
let API='http://localhost:8000';const API_FALLBACK='http://127.0.0.1:8010';let activeJob=null;
let demoModeActive=sessionStorage.getItem('tb_demo_mode_active')==='1';
let outbreakerStatusRefreshCount=0;

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

// -- KPI Banner --------------------------------------------------------------
async function loadKPIBanner(){
	try{
		const [summaryResp, lastRunResp] = await Promise.all([
			fetch(`${API}/cases/summary`),
			fetch(`${API}/jobs/last-run-times`),
		]);
		if(summaryResp.ok){
			const s = await summaryResp.json();
			document.querySelector('#kpiTotalCases .kpi-num').textContent = s.total_cases ?? '-';
			document.querySelector('#kpiClustered .kpi-num').textContent = s.operational_clustered_cases ?? s.clustered_cases ?? '-';
			document.querySelector('#kpiUnclustered .kpi-num').textContent = s.singleton_unclustered_cases ?? s.unclustered_cases ?? '-';
			document.querySelector('#kpiOpenClusters .kpi-num').textContent = s.open_clusters ?? '-';
			document.getElementById('kpiBannerTimestamp').textContent = 'refreshed ' + new Date().toLocaleTimeString();
		}
		if(lastRunResp.ok){
			const lr = await lastRunResp.json();
			const fmt = iso => iso ? new Date(iso).toLocaleString() : 'never';
			const lines = [
				`Lineage/DR: ${fmt(lr.lineage_dr_validation)}`,
				`Seq clusters: ${fmt(lr.sequence_clusters)}`,
				`FASTA analysis: ${fmt(lr.fasta_analysis)}`,
				`Outbreaker2: ${fmt(lr.outbreaker2)}`,
				`Comparison: ${fmt(lr.cluster_comparison)}`,
			];
			document.getElementById('kpiLastRun').textContent = 'Last run - ' + lines.join('  |  ');
		}
	}catch(e){
		// silently fail - banner is informational only
	}
}

// -- Full Pipeline ------------------------------------------------------------
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

function renderBadge(label, tone){
	return `<span class="status-pill ${tone || 'status-muted'}">${escapeHtml(label)}</span>`;
}

function renderSystemStatusItem(label, headline, tone, note){
	return `<li class="status-item">
		<div class="status-item-top">
			<span class="status-item-label">${escapeHtml(label)}</span>
			${renderBadge(headline, tone)}
		</div>
		${note ? `<p class="status-item-note">${escapeHtml(note)}</p>` : ''}
	</li>`;
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
	const cancelBtn = document.getElementById('cancelJobBtn');
	btn.disabled = true;
	cancelBtn.disabled = false;
	btn.textContent = 'Pipeline running...';
	document.getElementById('jobStatus').textContent = 'Starting full pipeline...';
	document.getElementById('pipelineStepLabel').textContent = '';
	document.getElementById('jobProgressDetails').innerHTML = '';
	try{
		const r = await fetch(`${API}/jobs/run-pipeline`, {method:'POST'});
		const d = await r.json();
		if(!d.job_id){
			document.getElementById('jobStatus').textContent = JSON.stringify(d, null, 2);
			btn.disabled = false;
			cancelBtn.disabled = true;
			btn.textContent = '> Run full pipeline (all steps)';
			return;
		}
		activeJob = d.job_id;
		pollPipeline(d.steps || []);
	}catch(e){
		document.getElementById('jobStatus').textContent = `Pipeline start failed: ${e}`;
		btn.disabled = false;
		cancelBtn.disabled = true;
		btn.textContent = '> Run full pipeline (all steps)';
	}
}

function terminalJobStatus(status){
	return ['completed','failed','cancelled','unknown'].includes(status);
}

function secondsSince(iso){
	if(!iso) return null;
	const ts = Date.parse(iso);
	if(Number.isNaN(ts)) return null;
	return Math.max(0, Math.round((Date.now()-ts)/1000));
}

function fmtDuration(seconds){
	if(seconds == null) return 'n/a';
	const mins = Math.floor(seconds/60);
	const secs = seconds % 60;
	return mins ? `${mins}m ${secs}s` : `${secs}s`;
}

function renderJobProgress(d, steps=[]){
	const progress = Math.max(0, Math.min(100, Number(d.progress||0)));
	const bar = document.getElementById('progressBar');
	bar.style.width = progress+'%';
	bar.textContent = `${progress}%`;
	bar.classList.toggle('progress-bar--failed', d.status === 'failed');
	bar.classList.toggle('progress-bar--cancelled', d.status === 'cancelled' || d.status === 'cancelling');

	const stepIdx = d.pipeline_step || 0;
	const total = d.pipeline_total || steps.length || 0;
	const currentStep = d.current_step || (stepIdx > 0 && stepIdx <= steps.length ? steps[stepIdx-1] : d.job || 'Job');
	document.getElementById('pipelineStepLabel').textContent =
		total ? `Step ${stepIdx || 0} of ${total}: ${currentStep || 'waiting'}` : currentStep;

	const elapsed = fmtDuration(secondsSince(d.started_at || d.created_at));
	const child = d.active_child_id ? `${d.child_status || 'running'}${d.child_progress != null ? ` (${d.child_progress}%)` : ''}` : 'none';
	document.getElementById('jobProgressDetails').innerHTML = `
		<div class="job-progress-metric">
			<strong>Status</strong>
			<span>${escapeHtml(d.status || 'unknown')}</span>
		</div>
		<div class="job-progress-metric">
			<strong>Overall</strong>
			<span>${progress}%</span>
		</div>
		<div class="job-progress-metric">
			<strong>Completed</strong>
			<span>${escapeHtml(String(d.completed_steps ?? 0))}/${escapeHtml(String(total || '-'))}</span>
		</div>
		<div class="job-progress-metric">
			<strong>Current child</strong>
			<span>${escapeHtml(child)}</span>
		</div>
		<div class="job-progress-metric">
			<strong>Elapsed</strong>
			<span>${escapeHtml(elapsed)}</span>
		</div>
	`;

	document.getElementById('jobStatus').textContent =
		`Status: ${d.status || 'unknown'}\n`+
		`Job: ${d.job || 'unknown'}\n`+
		`Progress: ${progress}%\n`+
		`Current step: ${currentStep || 'n/a'}\n`+
		`Active child: ${d.active_child_id || 'none'}\n`+
		`Log file: ${d.logfile || 'n/a'}`;
}

async function cancelActiveJob(){
	if(!activeJob) return;
	const btn = document.getElementById('cancelJobBtn');
	btn.disabled = true;
	btn.textContent = 'Cancelling...';
	try{
		await fetch(`${API}/jobs/cancel/${encodeURIComponent(activeJob)}`, {method:'POST'});
	}catch(e){
		document.getElementById('jobStatus').textContent = `Cancel request failed: ${e}`;
		btn.disabled = false;
		btn.textContent = 'Cancel run';
	}
}

async function pollPipeline(steps){
	if(!activeJob) return;
	const r = await fetch(`${API}/jobs/status/${activeJob}`);
	const d = await r.json();
	renderJobProgress(d, steps);
	if(!terminalJobStatus(d.status)){
		setTimeout(()=>pollPipeline(steps), 1500);
	} else {
		const btn = document.getElementById('runPipelineBtn');
		const cancelBtn = document.getElementById('cancelJobBtn');
		btn.disabled = false;
		cancelBtn.disabled = true;
		cancelBtn.textContent = 'Cancel run';
		btn.textContent = '> Run full pipeline (all steps)';
		if(d.status === 'completed'){
			loadKPIBanner();
			loadWorkflowStatus();
		}
	}
}

// -- Bulk Export --------------------------------------------------------------
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
		const safetyHtml = d.operational_safe
			? renderSystemStatusItem('Dataset mode', 'Operational', 'status-pass', `${d.total_cases} cases available for analysis`)
			: renderSystemStatusItem('Dataset mode', 'Non-operational', 'status-warn', `Synthetic/demo detected: ${d.synthetic_case_count} synthetic cases, ${d.synthetic_seed_events} seed events`);
		const backendItem = document.getElementById('backendStatusItem');
		const backendHtml = backendItem
			? backendItem.outerHTML
			: renderSystemStatusItem('Backend', 'Running', 'status-pass', 'API responded successfully');
		statusEl.innerHTML = `${backendHtml}${safetyHtml}`;
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
		const statusTone=d.status==='ready'?'status-pass':'status-warn';
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
			<div class="readiness-intro">
				<div>
					<span class="readiness-kicker">Overall readiness</span>
					<strong>${escapeHtml(d.status.replace(/_/g, ' '))}</strong>
					<p>Review sequencing and QC coverage before moving into analysis or reporting.</p>
				</div>
				${renderBadge(d.status === 'ready' ? 'READY' : 'NEEDS REVIEW', statusTone)}
			</div>
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
	const cancelBtn=document.getElementById('cancelJobBtn');
	if(cancelBtn) cancelBtn.disabled=false;
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
		if(cancelBtn) cancelBtn.disabled=true;
		return;
	}
	activeJob=d.job_id;
	poll();
}
async function poll(){
	if(!activeJob)return;
	const r=await fetch(`${API}/jobs/status/${activeJob}`);
	const d=await r.json();
	renderJobProgress(d, []);
	if(!terminalJobStatus(d.status)){
		setTimeout(poll,1500);
	}else{
		const cancelBtn=document.getElementById('cancelJobBtn');
		if(cancelBtn){
			cancelBtn.disabled=true;
			cancelBtn.textContent='Cancel run';
		}
	}
}
async function loadCases(){
	const box=document.getElementById('cases');
	box.textContent='Loading cases...';
	try{
		const r=await fetch(`${API}/cases`);
		const cases=await r.json();
		if(!Array.isArray(cases)){box.textContent='Error: unexpected response format';return;}
		
		// Summary stats
		const byStatus={};
		const byLineage={};
		const byRegion={};
		cases.forEach(c=>{
			byStatus[c.case_status||'Unknown']=(byStatus[c.case_status||'Unknown']||0)+1;
			byLineage[c.lineage||'Unknown']=(byLineage[c.lineage||'Unknown']||0)+1;
			byRegion[c.geographic_region||'Unknown']=(byRegion[c.geographic_region||'Unknown']||0)+1;
		});
		
		let html='<div class="result-panel"><div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:1rem;margin-bottom:1.5rem">';
		html+='<div style="background:var(--surface-soft);padding:1rem;border-radius:8px;border-left:4px solid var(--brand)"><div style="font-size:0.85rem;color:var(--muted);margin-bottom:0.25rem">Total cases</div><div style="font-size:1.8rem;font-weight:700">'+cases.length+'</div></div>';
		html+='<div style="background:var(--surface-soft);padding:1rem;border-radius:8px;border-left:4px solid var(--ok)"><div style="font-size:0.85rem;color:var(--muted);margin-bottom:0.25rem">Confirmed</div><div style="font-size:1.8rem;font-weight:700">'+(byStatus['confirmed']||0)+'</div></div>';
		html+='<div style="background:var(--surface-soft);padding:1rem;border-radius:8px;border-left:4px solid var(--warn)"><div style="font-size:0.85rem;color:var(--muted);margin-bottom:0.25rem">Unconfirmed</div><div style="font-size:1.8rem;font-weight:700">'+(byStatus['unconfirmed']||0)+'</div></div>';
		html+='</div>';
		
		// Distribution summary
		html+='<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:1rem;margin-bottom:1.5rem"><div><strong>By Status:</strong><ul style="margin:0.5rem 0;padding-left:1.5rem">';
		Object.entries(byStatus).sort((a,b)=>b[1]-a[1]).forEach(([s,n])=>html+='<li>'+s+': <strong>'+n+'</strong></li>');
		html+='</ul></div><div><strong>By Lineage:</strong><ul style="margin:0.5rem 0;padding-left:1.5rem">';
		Object.entries(byLineage).sort((a,b)=>b[1]-a[1]).slice(0,5).forEach(([l,n])=>html+='<li>'+l+': <strong>'+n+'</strong></li>');
		html+='</ul></div><div><strong>By Region:</strong><ul style="margin:0.5rem 0;padding-left:1.5rem">';
		Object.entries(byRegion).sort((a,b)=>b[1]-a[1]).forEach(([r,n])=>html+='<li>'+r+': <strong>'+n+'</strong></li>');
		html+='</ul></div></div>';
		
		// Table
		html+='<table style="width:100%;border-collapse:collapse;font-size:0.9rem"><thead style="background:var(--surface-soft);border-bottom:2px solid var(--line)"><tr style="text-align:left"><th style="padding:0.75rem;font-weight:700">Case ID</th><th style="padding:0.75rem;font-weight:700">Specimen Date</th><th style="padding:0.75rem;font-weight:700">Status</th><th style="padding:0.75rem;font-weight:700">Region</th><th style="padding:0.75rem;font-weight:700">Lineage</th><th style="padding:0.75rem;font-weight:700">DR Status</th></tr></thead><tbody>';
		cases.slice(0,50).forEach(c=>{
			const statusColor=c.case_status==='confirmed'?'var(--ok)':c.case_status==='unconfirmed'?'var(--warn)':'var(--muted)';
			// Extract DR profile from predicted_drug_resistance JSONB field
			let drProfile='—';
			if(c.predicted_drug_resistance){
				try{
					const dr=typeof c.predicted_drug_resistance==='string'?JSON.parse(c.predicted_drug_resistance):c.predicted_drug_resistance;
					const drugsResistant=Object.entries(dr).filter(([k,v])=>v==='R'||v===true).map(([k])=>k.replace(/_/g,' ').substring(0,3).toUpperCase());
					if(drugsResistant.length>0)drProfile=drugsResistant.slice(0,2).join(', ')+(drugsResistant.length>2?'+':'');
					else drProfile='S';
				}catch(e){drProfile='—';}
			}
			html+='<tr style="border-bottom:1px solid var(--line);transition:background 0.2s"><td style="padding:0.75rem"><code style="background:var(--surface-soft);padding:0.25rem 0.5rem;border-radius:4px;font-size:0.85rem">'+escapeHtml(c.pseudonymised_case_id||'—')+'</code></td><td style="padding:0.75rem">'+escapeHtml(c.specimen_date||'—')+'</td><td style="padding:0.75rem"><span style="color:'+statusColor+';font-weight:600">'+escapeHtml(c.case_status||'—')+'</span></td><td style="padding:0.75rem">'+escapeHtml(c.geographic_region||'—')+'</td><td style="padding:0.75rem"><span style="font-weight:600">'+escapeHtml(c.lineage||'Unknown')+'</span></td><td style="padding:0.75rem">'+escapeHtml(drProfile)+'</td></tr>';
		});
		html+='</tbody></table>';
		if(cases.length>50)html+='<p style="color:var(--muted);font-size:0.9rem;margin-top:1rem">Showing 50 of '+cases.length+' cases. Use advanced search above for detailed filtering.</p>';
		html+='</div>';
		box.innerHTML=html;
	}catch(e){box.textContent='Error loading cases: '+e.message;}
}

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
		html+=`<p>Dataset cases: ${escapeHtml(summary.dataset_cases??summary.total_cases)} | Operational clustered: ${escapeHtml(summary.operational_clustered_cases??summary.clustered_cases)} | Singleton/unclustered: ${escapeHtml(summary.singleton_unclustered_cases??summary.unclustered_cases)}</p>`;
		
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
				const fullUrl=(graphic.url.startsWith('http')?graphic.url:`${API}${graphic.url}`)+`?t=${Date.now()}`;
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
		const summary=payload.analysis_summary||{};
		const epi=payload.analysis_epi_summary||{};
		const engines=payload.engines||{};
		const effectiveEngines=payload.effective_engines||{};
		const tbRun=payload.tbprofiler_run||{};
		const mkRun=payload.mykrobe_run||{};
		const concordance=payload.dr_concordance||{};
		const rvSummary=(payload.resistance_validation||{}).summary||{};
		const warnings=Array.isArray(payload.warnings)?payload.warnings:[];
		const rawStatus=payload.status||'unknown';
		const statusTone=rawStatus==='completed'?'status-pass':(rawStatus.includes('warning')?'status-warn':'status-fail');

		let html='<div class="result-panel">';
		html+=`<h4>Lineage &amp; DR Validation ${renderBadge(rawStatus.replace(/_/g,' '),statusTone)}</h4>`;

		if(warnings.length){
			html+='<div style="background:#fef3c7;border:1px solid #f59e0b;border-radius:6px;padding:10px 14px;margin:8px 0;">';
			html+=`<strong>&#9888; Warnings (${warnings.length})</strong><ul style="margin:4px 0 0 16px;padding:0;">`;
			for(const w of warnings) html+=`<li>${escapeHtml(w)}</li>`;
			html+='</ul></div>';
		}

		html+='<h5>Sample summary</h5>';
		html+='<table class="data-table"><thead><tr><th>Metric</th><th>Count</th></tr></thead><tbody>';
		html+=`<tr><td>Interpreted samples</td><td>${escapeHtml(summary.interpreted_samples??0)}</td></tr>`;
		html+=`<tr><td>With lineage assigned</td><td>${escapeHtml(summary.samples_with_lineage??0)}</td></tr>`;
		html+=`<tr><td>With resistance calls</td><td>${escapeHtml(summary.samples_with_resistance_calls??0)}</td></tr>`;
		html+=`<tr><td>Any resistance signal</td><td>${escapeHtml(epi.samples_with_any_resistance_signal??0)}</td></tr>`;
		html+=`<tr><td>Rifampicin-resistant (suspected)</td><td>${escapeHtml(epi.rifampicin_resistant_suspected??0)}</td></tr>`;
		html+=`<tr><td>MDR (suspected)</td><td>${escapeHtml(epi.mdr_suspected??0)}</td></tr>`;
		html+='</tbody></table>';

		if(Array.isArray(epi.top_lineages)&&epi.top_lineages.length){
			html+='<h5>Top lineages</h5>';
			html+='<table class="data-table"><thead><tr><th>Lineage</th><th>Cases</th></tr></thead><tbody>';
			for(const x of epi.top_lineages) html+=`<tr><td>${escapeHtml(x.lineage)}</td><td>${escapeHtml(x.count)}</td></tr>`;
			html+='</tbody></table>';
		}

		html+='<h5>Tool engines</h5>';
		html+='<table class="data-table"><thead><tr><th>Tool</th><th>Status</th><th>Run outcome</th><th>Message</th></tr></thead><tbody>';
		const tbEng=engines.tb_profiler||{};
		const mkEng=engines.mykrobe||{};
		const toolStatus=(engine,run,effectiveKey)=>{
			if(run.status==='completed'){
				return `available via ${escapeHtml(run.runner||'runner')}`;
			}
			return effectiveEngines[effectiveKey]||engine.status||run.status||'unknown';
		};
		const toolOutcome=(engine,run)=>{
			if(run.status==='completed'){
				return `${escapeHtml(run.successful_samples??0)}/${escapeHtml(run.attempted_samples??0)} completed`;
			}
			if(run.attempted_samples!==undefined&&Number(run.attempted_samples)>0){
				return `${escapeHtml(run.attempted_samples)} attempted, none usable`;
			}
			return 'not run';
		};
		const toolMessage=(engine,run)=>{
			if(run.status==='completed'){
				const local=engine.message?` Local: ${engine.message}`:'';
				return `${run.message||'completed'}${local}`;
			}
			return engine.message||run.message||'';
		};
		html+=`<tr><td>TBProfiler</td><td>${renderStatusPill(toolStatus(tbEng,tbRun,'tb_profiler'))}</td><td>${toolOutcome(tbEng,tbRun)}</td><td>${escapeHtml(toolMessage(tbEng,tbRun))}</td></tr>`;
		html+=`<tr><td>Mykrobe</td><td>${renderStatusPill(toolStatus(mkEng,mkRun,'mykrobe'))}</td><td>${toolOutcome(mkEng,mkRun)}</td><td>${escapeHtml(toolMessage(mkEng,mkRun))}</td></tr>`;
		html+='</tbody></table>';

		html+='<h5>Cross-engine DR concordance</h5>';
		const comparableDrugCalls=Number(concordance.comparable_drug_calls??0);
		const comparedSamples=Number(concordance.samples_compared??0);
		const concordanceNote=comparedSamples>0&&comparableDrugCalls===0
			?' | No overlapping per-drug R/S calls available'
			:'';
		const artifactNote=concordance.used_existing_artifacts?' | Existing output JSONs used':'';
		html+=`<div class="kpi-strip">Samples with both engine outputs: ${escapeHtml(comparedSamples)} | Comparable drug calls: ${escapeHtml(comparableDrugCalls)} | Discordant samples: ${escapeHtml(concordance.discordant_sample_count??0)}${concordanceNote}${artifactNote}</div>`;

		if(rvSummary.total_mutation_calls!==undefined){
			html+='<h5>Resistance call summary</h5>';
			html+='<table class="data-table"><thead><tr><th>Category</th><th>Calls</th></tr></thead><tbody>';
			html+=`<tr><td>Total mutation calls</td><td>${escapeHtml(rvSummary.total_mutation_calls??0)}</td></tr>`;
			html+=`<tr><td>Expected gene-drug mappings</td><td>${escapeHtml(rvSummary.expected_gene_calls??0)}</td></tr>`;
			html+=`<tr><td>Unusual gene-drug mappings</td><td>${escapeHtml(rvSummary.unusual_gene_drug_mapping_calls??0)}</td></tr>`;
			html+=`<tr><td>Suppressed calls</td><td>${escapeHtml(rvSummary.suppressed_calls??0)}</td></tr>`;
			html+='</tbody></table>';
		}

		html+='</div>';
		box.innerHTML=html;
	}catch(e){
		box.textContent=`Failed to load lineage/DR validation: ${e}`;
	}
}
function downloadOutbreakReport(){
	window.open(`${API}/cases/outbreak-report`, '_blank');
}

function _writeReportWindow(win, title, bodyHtml){
	if(!win) return;
	win.document.open();
	win.document.write(`<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/><title>${escapeHtml(title)}</title><style>body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;margin:2rem;line-height:1.5;color:#1f2937}.msg{max-width:48rem;padding:1rem 1.25rem;border:1px solid #cbd5e1;border-radius:6px;background:#f8fafc}.err{border-color:#fca5a5;background:#fff1f2;color:#7f1d1d;white-space:pre-wrap}</style></head><body>${bodyHtml}</body></html>`);
	win.document.close();
}

async function openHtmlReport(endpoint, label){
	const el=document.getElementById('lineageDrResults');
	const win=window.open('about:blank', '_blank');
	if(!win){
		if(el) el.innerHTML=`<div class="result-panel"><p style="color:#9a3412;"><strong>Cannot open ${escapeHtml(label)} report:</strong> The browser blocked the report window. Allow pop-ups for this page and try again.</p></div>`;
		return;
	}
	_writeReportWindow(win, `Loading ${label} report`, `<div class="msg"><strong>Generating ${escapeHtml(label)} report...</strong><br/>This can take a few seconds for large outbreak reports.</div>`);
	try{
		const html=await apiText(`${API}${endpoint}`);
		win.document.open();
		win.document.write(html);
		win.document.close();
	}catch(e){
		const message=escapeHtml(e.message);
		_writeReportWindow(win, `${label} report failed`, `<div class="msg err"><strong>Cannot open ${escapeHtml(label)} report:</strong>\n${message}</div>`);
		if(el) el.innerHTML=`<div class="result-panel"><p style="color:#9a3412;"><strong>Cannot open ${escapeHtml(label)} report:</strong> ${message}</p></div>`;
	}
}

async function openOutbreakReportHtml(){
	return openHtmlReport('/cases/outbreak-report.html', 'short');
}

async function openFullOutbreakReportHtml(){
	return openHtmlReport('/cases/outbreak-report.full.html', 'full');
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
				const rowCaseId=String(c.case_id||'');
				html+=`<tr><td>${escapeHtml(c.case_id)}</td><td>${escapeHtml(c.specimen_date)}</td><td>${escapeHtml(c.region)}</td><td>${escapeHtml(c.lineage)}</td><td>${escapeHtml(c.cluster_id||'-')}</td><td><button type="button" class="mini-btn js-case-history-btn" data-case-id="${escapeAttr(rowCaseId)}">View history</button> <button type="button" class="mini-btn js-case-report-btn" data-case-id="${escapeAttr(rowCaseId)}">Report</button></td></tr>`;
			}
			html+='</table>';
		}
		html+='</div>';
		box.innerHTML=html;
		box.querySelectorAll('.js-case-history-btn').forEach(btn=>{
			btn.addEventListener('click',()=>loadCaseHistory(btn.dataset.caseId||''));
		});
		box.querySelectorAll('.js-case-report-btn').forEach(btn=>{
			btn.addEventListener('click',()=>generateCaseReport(btn.dataset.caseId||''));
		});
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

async function loadDataManagementCases(){
	const list=document.getElementById('dataManagementList');
	const editor=document.getElementById('dataManagementEditor');
	if(!list) return;
	list.textContent='Loading managed cases...';
	if(editor) editor.innerHTML='';
	try{
		const query=document.getElementById('dmSearch')?.value.trim()||'';
		const include=document.getElementById('dmIncludeDeleted')?.checked||false;
		const params=new URLSearchParams({limit:'100',include_entered_in_error:String(include)});
		if(query) params.set('query',query);
		const data=await apiJson(`${API}/data-management/cases?${params.toString()}`);
		const rows=data.cases||[];
		let html=`<div class="kpi-strip">Returned records: ${escapeHtml(rows.length)} | Entered-in-error visible: ${include?'yes':'no'}</div>`;
		html+='<table class="data-table"><thead><tr><th>Case</th><th>Lab sample</th><th>Date</th><th>Status</th><th>Region</th><th>Lineage</th><th>QC</th><th>State</th><th></th></tr></thead><tbody>';
		for(const row of rows){
			const caseId=String(row.case_id||'');
			html+=`<tr>
				<td><code>${escapeHtml(caseId.slice(0,8))}</code></td>
				<td>${escapeHtml(row.local_lab_sample_id||'')}</td>
				<td>${escapeHtml(row.specimen_date||'')}</td>
				<td>${escapeHtml(row.case_status||'')}</td>
				<td>${escapeHtml(row.geographic_region||'')}</td>
				<td>${escapeHtml(row.lineage||'')}</td>
				<td>${escapeHtml(row.qc_status||'')}</td>
				<td>${row.entered_in_error?renderBadge('entered in error','status-fail'):renderBadge('active','status-pass')}</td>
				<td><button type="button" class="mini-btn" onclick="openDataManagementCase('${escapeAttr(caseId)}')">Edit</button></td>
			</tr>`;
		}
		html+=rows.length?'</tbody></table>':'<tr><td colspan="9">No matching records.</td></tr></tbody></table>';
		list.innerHTML=html;
	}catch(e){
		list.textContent='Failed to load managed cases: '+e;
	}
}

function _dmDate(value){
	return value ? String(value).slice(0,10) : '';
}

async function openDataManagementCase(caseId){
	const editor=document.getElementById('dataManagementEditor');
	if(!editor) return;
	editor.textContent='Loading case editor...';
	try{
		const data=await apiJson(`${API}/data-management/cases/${encodeURIComponent(caseId)}`);
		const c=data.case||{};
		const counts=data.linked_counts||{};
		const countRows=Object.entries(counts).map(([key,value])=>`<tr><td>${escapeHtml(key.replace(/_/g,' '))}</td><td>${escapeHtml(value)}</td></tr>`).join('');
		const caseState=c.entered_in_error?renderBadge('entered in error','status-fail'):renderBadge('active','status-pass');
		editor.innerHTML=`<div class="result-panel data-management-editor">
			<h4>Case ${escapeHtml(c.case_short||String(c.case_id||'').slice(0,8))} ${caseState}</h4>
			<input id="dmCaseId" type="hidden" value="${escapeAttr(c.case_id||'')}" />
			<div class="data-management-form">
				<label>Local lab sample ID<input id="dmLocalLabSampleId" type="text" value="${escapeAttr(c.local_lab_sample_id||'')}" /></label>
				<label>Specimen date<input id="dmSpecimenDate" type="date" value="${escapeAttr(_dmDate(c.specimen_date))}" /></label>
				<label>Geographic region<input id="dmGeographicRegion" type="text" value="${escapeAttr(c.geographic_region||'')}" /></label>
				<label>Case status<input id="dmCaseStatus" type="text" value="${escapeAttr(c.case_status||'')}" /></label>
				<label>Symptom onset<input id="dmSymptomOnsetDate" type="date" value="${escapeAttr(_dmDate(c.symptom_onset_date))}" /></label>
				<label>Treatment start<input id="dmTreatmentStartDate" type="date" value="${escapeAttr(_dmDate(c.treatment_start_date))}" /></label>
				<label>Smear status<input id="dmSmearStatus" type="text" value="${escapeAttr(c.smear_status||'')}" /></label>
				<label>Cavitation status<input id="dmCavitationStatus" type="text" value="${escapeAttr(c.cavitation_status||'')}" /></label>
				<label>Culture status<input id="dmCultureStatus" type="text" value="${escapeAttr(c.culture_status||'')}" /></label>
				<label>Culture positivity duration days<input id="dmCultureDuration" type="number" min="0" value="${escapeAttr(c.culture_positivity_duration_days??'')}" /></label>
				<label class="dm-wide">Infectiousness notes<textarea id="dmInfectiousnessNotes" rows="3">${escapeHtml(c.infectiousness_notes||'')}</textarea></label>
				<label class="dm-wide">Change reason<textarea id="dmReason" rows="2" placeholder="Required for audit"></textarea></label>
			</div>
			<div class="data-management-actions">
				<button class="btn-primary" onclick="saveDataManagementCase()">Save changes</button>
				${c.entered_in_error
					? '<button onclick="restoreDataManagementCase()">Restore case</button>'
					: '<button class="btn-danger" onclick="markDataManagementCaseEnteredInError()">Mark entered in error</button>'}
			</div>
			<div id="dmEditorStatus" class="cic-msg"></div>
			<h4>Linked data retained for audit</h4>
			<table class="data-table"><tbody>${countRows}</tbody></table>
		</div>`;
	}catch(e){
		editor.textContent='Failed to load case editor: '+e;
	}
}

function _dmValue(id){
	const el=document.getElementById(id);
	return el ? el.value.trim() : '';
}

function _dmNullable(id){
	const value=_dmValue(id);
	return value || null;
}

function _dmPayload(){
	const duration=_dmValue('dmCultureDuration');
	return {
		reason:_dmValue('dmReason'),
		local_lab_sample_id:_dmNullable('dmLocalLabSampleId'),
		specimen_date:_dmNullable('dmSpecimenDate'),
		geographic_region:_dmNullable('dmGeographicRegion'),
		case_status:_dmNullable('dmCaseStatus'),
		symptom_onset_date:_dmNullable('dmSymptomOnsetDate'),
		treatment_start_date:_dmNullable('dmTreatmentStartDate'),
		smear_status:_dmNullable('dmSmearStatus'),
		cavitation_status:_dmNullable('dmCavitationStatus'),
		culture_status:_dmNullable('dmCultureStatus'),
		culture_positivity_duration_days:duration?Number(duration):null,
		infectiousness_notes:_dmNullable('dmInfectiousnessNotes'),
	};
}

async function saveDataManagementCase(){
	const status=document.getElementById('dmEditorStatus');
	const caseId=_dmValue('dmCaseId');
	const payload=_dmPayload();
	if(!payload.reason){ if(status) status.textContent='Change reason is required.'; return; }
	try{
		await apiJson(`${API}/data-management/cases/${encodeURIComponent(caseId)}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
		if(status) status.textContent='Case changes saved.';
		await openDataManagementCase(caseId);
		await loadDataManagementCases();
	}catch(e){
		if(status) status.textContent='Failed to save case: '+e;
	}
}

async function markDataManagementCaseEnteredInError(){
	const status=document.getElementById('dmEditorStatus');
	const caseId=_dmValue('dmCaseId');
	const reason=_dmValue('dmReason');
	if(!reason){ if(status) status.textContent='Reason is required before marking entered in error.'; return; }
	if(!confirm('Mark this case as entered in error? Linked data will be retained for audit but excluded from standard case lists.')) return;
	try{
		await apiJson(`${API}/data-management/cases/${encodeURIComponent(caseId)}`,{method:'DELETE',headers:{'Content-Type':'application/json'},body:JSON.stringify({reason})});
		if(status) status.textContent='Case marked entered in error.';
		await openDataManagementCase(caseId);
		await loadDataManagementCases();
	}catch(e){
		if(status) status.textContent='Failed to mark entered in error: '+e;
	}
}

async function restoreDataManagementCase(){
	const status=document.getElementById('dmEditorStatus');
	const caseId=_dmValue('dmCaseId');
	const reason=_dmValue('dmReason');
	if(!reason){ if(status) status.textContent='Reason is required before restoring.'; return; }
	try{
		await apiJson(`${API}/data-management/cases/${encodeURIComponent(caseId)}/restore`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reason})});
		if(status) status.textContent='Case restored.';
		await openDataManagementCase(caseId);
		await loadDataManagementCases();
	}catch(e){
		if(status) status.textContent='Failed to restore case: '+e;
	}
}
// -- Cluster Investigation Centre ---------------------------------------------
let _cicCurrentCluster = null;
let _cicCurrentMembers = [];
let _cicLatestReviewsData = null;
let _cicVisiblePairs = [];

const _CIC_BAND_COLOUR = {
	critical: '#b91c1c',
	high:     '#c2410c',
	medium:   '#b45309',
	low:      '#15803d',
};

async function loadClusterInvestigations(){
	const listEl = document.getElementById('cicList');
	listEl.innerHTML = '<p class="hint">Loading clusters...</p>';
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
				<td>${escapeHtml(statusLabel)}${signed ? ' [OK]' : ''}</td>
				<td>${escapeHtml(inv.assigned_to || '-')}</td>
				<td>${escapeHtml(inv.action_count)}</td>
				<td><button class="mini-btn cic-investigate-btn" data-cluster-id="${escapeAttr(inv.cluster_id)}">Investigate</button></td>
			</tr>`;
		}
		html += '</tbody></table>';
		listEl.innerHTML = html;
		listEl.querySelectorAll('.cic-investigate-btn').forEach(btn => {
			btn.addEventListener('click', () => openCicPanel(btn.dataset.clusterId));
		});
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
		'Cluster ' + clusterId.slice(0,8) + '...';
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
		await cicLoadAnalyticsReview();
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
		opt.textContent=`${(member.case_id||'').slice(0,8)} - ${member.region||'unknown region'}`;
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

function _cicPairKey(caseA, caseB){
	const left=String(caseA||'').trim();
	const right=String(caseB||'').trim();
	if(!left && !right) return '';
	return [left, right].sort().join('::');
}

function _cicFormatDistribution(distribution){
	const entries=Object.entries(distribution||{}).sort((a,b)=>{
		const countDiff=Number(b[1]||0)-Number(a[1]||0);
		return countDiff || String(a[0]).localeCompare(String(b[0]));
	});
	if(!entries.length) return '';
	return entries.map(([label,count])=>`${label}: ${count}`).join(' | ');
}

function _cicGrowthWindowSummary(growthWindows){
	if(!growthWindows || typeof growthWindows!=='object') return [];
	return [
		['30 days', growthWindows.last_30_days, growthWindows.previous_30_days],
		['60 days', growthWindows.last_60_days, growthWindows.previous_60_days],
		['90 days', growthWindows.last_90_days, growthWindows.previous_90_days],
	].filter(([, recent, previous])=>Number.isFinite(Number(recent)) || Number.isFinite(Number(previous)))
		.map(([label, recent, previous])=>`${label}: ${Number(recent||0)} recent vs ${Number(previous||0)} prior`);
}

function _cicSnpHistogramSummary(pairs){
	const bins=[
		{label:'0-5 SNPs', min:0, max:5, count:0},
		{label:'6-12 SNPs', min:6, max:12, count:0},
		{label:'13-25 SNPs', min:13, max:25, count:0},
		{label:'>25 SNPs', min:26, max:Number.POSITIVE_INFINITY, count:0},
	];
	for(const pair of (Array.isArray(pairs)?pairs:[])){
		const snp=Number(pair?.snp_distance);
		if(!Number.isFinite(snp) || snp<0) continue;
		const bucket=bins.find(entry=>snp>=entry.min && snp<=entry.max);
		if(bucket) bucket.count+=1;
	}
	return bins.filter(bin=>bin.count>0).map(bin=>`${bin.label}: ${bin.count} pairs`);
}

function _cicBuildSynthesisPairIndex(cluster){
	const index=new Map();
	for(const pair of (Array.isArray(cluster?.pairwise_transmission_evidence)?cluster.pairwise_transmission_evidence:[])){
		const key=_cicPairKey(pair.source, pair.target);
		if(key) index.set(key, pair);
	}
	return index;
}

function _cicRenderAnalyticsWhy(data, synthesisCluster, evidenceData){
	const box=document.getElementById('cicAnalyticsWhy');
	if(!box) return;
	if(!data && !synthesisCluster && !evidenceData){
		box.innerHTML='<p class="hint">No prioritisation data available.</p>';
		return;
	}
	const reasons=Array.isArray(data?.reasons)?data.reasons:[];
	const metrics=data?.metrics||{};
	const clusterSummary=synthesisCluster?.summary||{};
	const tx=clusterSummary.transmission_generations||{};
	const growthSummary=_cicGrowthWindowSummary(clusterSummary.growth_windows);
	const lineageSummary=_cicFormatDistribution(synthesisCluster?.lineage_distribution);
	const snpHistogram=_cicSnpHistogramSummary(evidenceData?.pairs);
	let html=`<div class="kpi-strip">Cases: ${escapeHtml(metrics.case_count||clusterSummary.member_count||0)} | Recent: ${escapeHtml(metrics.recent_cases||clusterSummary.recent_case_count||0)} | Regions: ${escapeHtml(metrics.region_count||(Array.isArray(clusterSummary.regions)?clusterSummary.regions.length:0)||0)} | High-confidence edges: ${escapeHtml(metrics.high_confidence_edges||0)}</div>`;
	if(tx.max_generation != null){
		html+=`<p class="hint">Sustained transmission: ${escapeHtml(tx.sustained_transmission_flag ? 'Yes' : 'No')} | Max generation: ${escapeHtml(tx.max_generation)}</p>`;
	}
	if(growthSummary.length){
		html+=`<p class="hint">Growth windows: ${escapeHtml(growthSummary.join(' | '))}</p>`;
	}
	if(lineageSummary){
		html+=`<p class="hint">Lineage mix: ${escapeHtml(lineageSummary)}</p>`;
	}
	if(snpHistogram.length){
		html+=`<p class="hint">SNP distance histogram: ${escapeHtml(snpHistogram.join(' | '))}</p>`;
	}
	if(data?.summary){
		html+=`<p class="hint">${escapeHtml(data.summary)}</p>`;
	}
	if(reasons.length){
		html+='<ul class="hint">';
		for(const reason of reasons){
			html+=`<li>${escapeHtml(reason)}</li>`;
		}
		html+='</ul>';
	}else{
		html+='<p class="hint">No prioritisation reasons were returned.</p>';
	}
	box.innerHTML=html;
}

function _cicRenderPairEvidence(data, synthesisCluster){
	const box=document.getElementById('cicPairEvidence');
	const pairSelect=document.getElementById('cicReviewPairSelect');
	if(!box) return;
	const pairs=Array.isArray(data?.pairs)?data.pairs:[];
	_cicVisiblePairs = pairs.slice(0,25);
	const synthesisPairIndex=_cicBuildSynthesisPairIndex(synthesisCluster);
	if(!pairs.length){
		box.innerHTML='<p class="hint">No pair evidence available for this cluster.</p>';
		if(pairSelect) pairSelect.innerHTML='<option value="">Select a pair from the loaded evidence</option>';
		return;
	}
	if(pairSelect){
		let selectHtml='<option value="">Select a pair from the loaded evidence</option>';
		for(const [index,pair] of _cicVisiblePairs.entries()){
			const optionValue=JSON.stringify({caseA: pair.case_a || '', caseB: pair.case_b || ''});
			selectHtml+=`<option value="${escapeAttr(optionValue)}">${escapeHtml(pair.pair||'')}  |  ${escapeHtml(pair.overall_interpretation||'')}</option>`;
		}
		pairSelect.innerHTML=selectHtml;
	}
	let html=`<div class="kpi-strip">Pairs: ${escapeHtml(data.pair_count||pairs.length)}</div>`;
	const summary=Object.entries(data.support_summary||{}).map(([label,count])=>`${escapeHtml(label)}: ${escapeHtml(count)}`);
	if(summary.length){
		html+=`<p class="hint">${summary.join(' | ')}</p>`;
	}
	html+='<div class="analytics-table-wrap"><table class="data-table"><thead><tr><th>Pair</th><th>Genomic</th><th>Epi</th><th>Temporal</th><th>Lineage</th><th>Resistance</th><th>Interpretation</th><th>Evidence</th><th>Review</th><th></th><th></th><th></th></tr></thead><tbody>';
	for(const [index,pair] of _cicVisiblePairs.entries()){
		const genomic=pair.genomic_plausibility||{};
		const epi=pair.epidemiological_support||{};
		const temporal=pair.temporal_plausibility||{};
		const evidence=pair.evidence||{};
		const review=pair.reviewer_classification||{};
		const synthesisPair=synthesisPairIndex.get(_cicPairKey(pair.case_a, pair.case_b))||{};
		const lineageConcordance=synthesisPair.lineage_concordance||'n/a';
		const resistanceConcordance=synthesisPair.resistance_profile_concordance||'n/a';
		const reviewText=review.classification
			? `${review.classification}${review.reviewer ? ` by ${review.reviewer}` : ''}${review.reviewed_at ? ` (${String(review.reviewed_at).replace('T',' ').replace('Z',' UTC')})` : ''}`
			: 'not reviewed';
		html+=`<tr>
			<td><code>${escapeHtml(pair.pair||'')}</code></td>
			<td>${escapeHtml(genomic.support||'')}</td>
			<td>${escapeHtml(epi.support||'')}</td>
			<td>${escapeHtml(temporal.delta_days ?? 'n/a')}</td>
			<td>${escapeHtml(lineageConcordance)}</td>
			<td>${escapeHtml(resistanceConcordance)}</td>
			<td>${escapeHtml(pair.overall_interpretation||'')}</td>
			<td>${escapeHtml([evidence.basis, Array.isArray(evidence.supports) && evidence.supports.length ? evidence.supports.slice(0,3).join(', ') : ''].filter(Boolean).join(' | '))}</td>
			<td>${escapeHtml(reviewText)}</td>
			<td><button class="mini-btn" onclick="cicPairEvidenceAction('use', ${index})">Use</button></td>
			<td><button class="mini-btn" onclick="cicPairEvidenceAction('copy', ${index})">Copy IDs</button></td>
			<td><button class="mini-btn" onclick="cicPairEvidenceAction('evidence', ${index})">Evidence</button></td>
		</tr>`;
	}
	html+='</tbody></table></div>';
	if(pairs.length > 25){
		html += `<p class="hint">Showing the first 25 pairs of ${escapeHtml(pairs.length)}.</p>`;
	}
	box.innerHTML=html;
}

async function cicPairEvidenceAction(action, index){
	const pair=_cicVisiblePairs[Number(index)];
	if(!pair) return;
	if(action==='use'){
		cicPrefillPairReview(pair.case_a||'', pair.case_b||'');
		return;
	}
	if(action==='copy'){
		await cicCopyPairIds(pair.case_a||'', pair.case_b||'');
		return;
	}
	if(action==='evidence'){
		cicShowEvidenceCard(pair);
	}
}

function _cicRenderPairReviews(data){
	const box=document.getElementById('cicPairReviews');
	if(!box) return;
	const allReviews=Array.isArray(data?.reviews)?data.reviews:[];
	if(!allReviews.length){
		box.innerHTML='<p class="hint">No saved pair reviews yet.</p>';
		return;
	}
	const filterValue=(document.getElementById('cicPairReviewsFilter')?.value||'').trim().toLowerCase();
	const reviews = filterValue
		? allReviews.filter((review)=>[
			review.pair,
			review.reviewer,
			review.reviewer_classification,
			review.notes,
		].filter(Boolean).join(' ').toLowerCase().includes(filterValue))
		: allReviews;
	let html=`<div class="kpi-strip">Saved reviews: ${escapeHtml(data.total||allReviews.length)} | Matches: ${escapeHtml(reviews.length)}</div>`;
	if(filterValue && !reviews.length){
		box.innerHTML=html + '<p class="hint">No saved reviews match the current filter.</p>';
		return;
	}
	html+='<div class="analytics-table-wrap"><table class="data-table"><thead><tr><th>Pair</th><th>Classification</th><th>Reviewer</th><th>Notes</th><th>Reviewed at</th></tr></thead><tbody>';
	for(const review of reviews.slice(0,25)){
		html+=`<tr><td><code>${escapeHtml(review.pair||'')}</code></td><td>${escapeHtml(review.reviewer_classification||'')}</td><td>${escapeHtml(review.reviewer||'')}</td><td>${escapeHtml(review.notes||'')}</td><td>${escapeHtml((review.reviewed_at||'').replace('T',' ').replace('Z',' UTC'))}</td></tr>`;
	}
	html+='</tbody></table></div>';
	if(reviews.length > 25){
		html += `<p class="hint">Showing the first 25 saved reviews of ${escapeHtml(reviews.length)}.</p>`;
	}
	box.innerHTML=html;
}

function cicApplyReviewsFilter(){
	if(_cicLatestReviewsData){
		_cicRenderPairReviews(_cicLatestReviewsData);
	}
}

async function cicCopyPairIds(caseA, caseB){
	const text=`${caseA || ''}\n${caseB || ''}`;
	const status=document.getElementById('cicReviewStatus');
	try{
		if(navigator.clipboard && navigator.clipboard.writeText){
			await navigator.clipboard.writeText(text);
			if(status) status.textContent='Pair IDs copied to clipboard.';
			return;
		}
		throw new Error('Clipboard API unavailable');
	}catch(_e){
		if(status) status.textContent='Copy failed. Select the IDs from the pair row manually.';
	}
}

// -- Transmission Evidence Card ------------------------------------------------

function cicShowEvidenceCard(pair){
	const modal=document.getElementById('evidenceCardModal');
	const labelEl=document.getElementById('evidenceCardPairLabel');
	const contentEl=document.getElementById('evidenceCardContent');
	if(!modal || !contentEl) return;

	if(labelEl) labelEl.textContent=`${pair.case_a||''} -> ${pair.case_b||''}`;

	const card=pair.evidence_card;
	if(!card){
		contentEl.innerHTML='<p class="hint">Evidence card not available for this pair.</p>';
		modal.style.display='flex';
		return;
	}

	const confidenceColour={strong:'#1a7a4a',moderate:'#c07000',weak:'#5a5a8a',contradicted:'#b02020',insufficient:'#666'};
	const directionIcon={supports:'[OK] supports','contradicts':'X contradicts',neutral:'- neutral',weak:'~ weak',missing:'? missing'};
	const directionColour={supports:'#1a6a3a',contradicts:'#b02020',neutral:'#555',weak:'#666',missing:'#8a6000'};

	let html=`<div style="display:flex;align-items:center;gap:.75rem;margin-bottom:1rem">
		<span style="font-weight:700;font-size:1rem;color:${escapeHtml(confidenceColour[card.confidence]||'#444')}">
			Confidence: ${escapeHtml(card.confidence||'unknown')}
		</span>
		<span style="font-size:.8rem;color:#666">
			${escapeHtml(card.supporting_signal_count||0)} supporting  |  ${escapeHtml(card.contradicting_signal_count||0)} contradicting
		</span>
	</div>`;

	// Signal table
	html+='<table class="data-table" style="margin-bottom:1rem"><thead><tr><th>Signal</th><th>Evidence</th><th>Direction</th></tr></thead><tbody>';
	for(const sig of (card.signals||[])){
		const dir=sig.direction||'neutral';
		const iconText=directionIcon[dir]||dir;
		const colour=directionColour[dir]||'#444';
		html+=`<tr>
			<td style="white-space:nowrap;font-weight:600">${escapeHtml(sig.signal||'')}</td>
			<td>${escapeHtml(sig.value||'')}</td>
			<td style="white-space:nowrap;color:${colour};font-weight:600">${escapeHtml(iconText)}</td>
		</tr>`;
	}
	html+='</tbody></table>';

	// Interpretation paragraph
	if(card.interpretation){
		html+=`<div style="background:#f5f7fa;border-left:3px solid #0a84c8;padding:.75rem 1rem;margin-bottom:.75rem;border-radius:0 4px 4px 0">
			<strong>Interpretation</strong><br>
			<span style="font-size:.9rem">${escapeHtml(card.interpretation)}</span>
		</div>`;
	}

	// Recommendation
	if(card.recommendation){
		html+=`<div style="background:#f0faf4;border-left:3px solid #1a7a4a;padding:.75rem 1rem;border-radius:0 4px 4px 0">
			<strong>Recommendation</strong><br>
			<span style="font-size:.9rem">${escapeHtml(card.recommendation)}</span>
		</div>`;
	}

	html+=`<p style="font-size:.75rem;color:#999;margin-top:.75rem">
		Heuristic evidence synthesis only. Not a validated transmission model. Requires expert review.
	</p>`;

	contentEl.innerHTML=html;
	modal.style.display='flex';
}

function closeEvidenceCardModal(){
	const modal=document.getElementById('evidenceCardModal');
	if(modal) modal.style.display='none';
}

function cicHandleEvidenceCardBackdropClick(event){
	if(event && event.target && event.target.id==='evidenceCardModal'){
		closeEvidenceCardModal();
	}
}


function cicPrefillPairReview(caseA, caseB){

	const caseAInput=document.getElementById('cicReviewCaseA');
	const caseBInput=document.getElementById('cicReviewCaseB');
	const pairSelect=document.getElementById('cicReviewPairSelect');
	const clusterInput=document.getElementById('cicReviewClusterId');
	if(caseAInput) caseAInput.value=caseA || '';
	if(caseBInput) caseBInput.value=caseB || '';
	if(pairSelect) pairSelect.value=JSON.stringify({caseA: caseA || '', caseB: caseB || ''});
	if(clusterInput && _cicCurrentCluster) clusterInput.value=_cicCurrentCluster;
	const status=document.getElementById('cicReviewStatus');
	if(status) status.textContent='Pair loaded into review form.';
}

function cicSelectPairForReview(){
	const select=document.getElementById('cicReviewPairSelect');
	if(!select || !select.value) return;
	try{
		const pair=JSON.parse(select.value);
		cicPrefillPairReview(pair.caseA || '', pair.caseB || '');
	}catch(_e){
		const status=document.getElementById('cicReviewStatus');
		if(status) status.textContent='Could not load the selected pair.';
	}
}

async function cicSubmitPairReview(){
	const status=document.getElementById('cicReviewStatus');
	const caseA=(document.getElementById('cicReviewCaseA')?.value||'').trim();
	const caseB=(document.getElementById('cicReviewCaseB')?.value||'').trim();
	const reviewer_classification=document.getElementById('cicReviewClassification')?.value||'insufficient evidence';
	const reviewer=(document.getElementById('cicReviewReviewer')?.value||'').trim();
	const notes=(document.getElementById('cicReviewNotes')?.value||'').trim();
	const clusterId=(document.getElementById('cicReviewClusterId')?.value||_cicCurrentCluster||'').trim();
	if(!caseA || !caseB){
		if(status) status.textContent='Enter both case UUIDs before saving a review.';
		return;
	}
	if(!reviewer){
		if(status) status.textContent='Enter the reviewer name or initials.';
		return;
	}
	if(caseA === caseB){
		if(status) status.textContent='Case A and Case B must be different.';
		return;
	}
	if(status) status.textContent='Saving pair review...';
	try{
		const response=await fetch(`${API}/analytics/case-pair-review`,{
			method:'POST',
			headers:{'Content-Type':'application/json'},
			body:JSON.stringify({
				case_a: caseA,
				case_b: caseB,
				reviewer_classification,
				reviewer,
				notes: notes || null,
				cluster_id: clusterId || null,
			}),
		});
		if(!response.ok){
			const payload=await response.json().catch(()=>null);
			if(status) status.textContent='Error: ' + ((payload && (payload.detail || payload.error)) || response.status);
			return;
		}
		if(document.getElementById('cicReviewNotes')) document.getElementById('cicReviewNotes').value='';
		if(status) status.textContent='[OK] Pair review saved.';
		await cicLoadAnalyticsReview();
	}catch(e){
		if(status) status.textContent='Error: ' + e;
	}
}

async function cicLoadAnalyticsReview(){
	const whyBox=document.getElementById('cicAnalyticsWhy');
	const evidenceBox=document.getElementById('cicPairEvidence');
	const reviewsBox=document.getElementById('cicPairReviews');
	if(whyBox) whyBox.innerHTML='<p class="hint">Loading cluster prioritisation...</p>';
	if(evidenceBox) evidenceBox.innerHTML='<p class="hint">Loading pair evidence...</p>';
	if(reviewsBox) reviewsBox.innerHTML='<p class="hint">Loading saved pair reviews...</p>';
	const clusterId=_cicCurrentCluster;
	if(!clusterId){
		if(whyBox) whyBox.innerHTML='<p class="hint">Open a cluster to view analytics.</p>';
		if(evidenceBox) evidenceBox.innerHTML='';
		if(reviewsBox) reviewsBox.innerHTML='';
		_cicLatestReviewsData = null;
		const clusterInput=document.getElementById('cicReviewClusterId');
		if(clusterInput) clusterInput.value='';
		return;
	}
	const clusterInput=document.getElementById('cicReviewClusterId');
	if(clusterInput) clusterInput.value=clusterId;
	try{
		const [whyResult, evidenceResult, reviewsResult, synthesisResult] = await Promise.allSettled([
			fetch(`${API}/analytics/cluster-why/${encodeURIComponent(clusterId)}`),
			fetch(`${API}/analytics/case-pair-evidence?cluster_id=${encodeURIComponent(clusterId)}`),
			fetch(`${API}/analytics/case-pair-reviews?cluster_id=${encodeURIComponent(clusterId)}&limit=100`),
			fetch(`${API}/analytics/transmission-synthesis/${encodeURIComponent(clusterId)}`),
		]);

		const whyResp = whyResult.status === 'fulfilled' ? whyResult.value : null;
		const evidenceResp = evidenceResult.status === 'fulfilled' ? evidenceResult.value : null;
		const reviewsResp = reviewsResult.status === 'fulfilled' ? reviewsResult.value : null;
		const synthesisResp = synthesisResult.status === 'fulfilled' ? synthesisResult.value : null;

		const whyData = whyResp && whyResp.ok ? await whyResp.json() : null;
		const evidenceData = evidenceResp && evidenceResp.ok ? await evidenceResp.json() : null;
		const reviewsData = reviewsResp && reviewsResp.ok ? await reviewsResp.json() : null;
		const synthesisData = synthesisResp && synthesisResp.ok ? await synthesisResp.json() : null;
		const synthesisCluster = (Array.isArray(synthesisData?.clusters)?synthesisData.clusters:[]).find(cluster=>String(cluster.cluster_id||'')===String(clusterId)) || null;

		if(whyBox){
			if(whyData || synthesisCluster || evidenceData) _cicRenderAnalyticsWhy(whyData, synthesisCluster, evidenceData);
			else whyBox.innerHTML='<p class="hint">Cluster prioritisation could not be loaded.</p>';
		}
		if(evidenceBox){
			if(evidenceData) _cicRenderPairEvidence(evidenceData, synthesisCluster);
			else evidenceBox.innerHTML='<p class="hint">Pair evidence could not be loaded.</p>';
		}
		if(reviewsBox){
			if(reviewsData){
				_cicLatestReviewsData = reviewsData;
				_cicRenderPairReviews(reviewsData);
			}
			else reviewsBox.innerHTML='<p class="hint">Saved pair reviews could not be loaded.</p>';
		}
	}catch(e){
		if(whyBox) whyBox.textContent='Cluster prioritisation failed: ' + e;
		if(evidenceBox) evidenceBox.textContent='Pair evidence failed: ' + e;
		if(reviewsBox) reviewsBox.textContent='Pair reviews failed: ' + e;
	}
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
			msg.textContent='[OK] Location event recorded.';
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
			msg.textContent='[OK] Contact link recorded.';
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
		if(r.ok){ msg.textContent='[OK] Assigned to ' + val; await _cicRefreshDetail(); loadClusterInvestigations(); }
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
		if(r.ok){ msg.textContent='[OK] Notes saved.'; }
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
			msg.textContent='[OK] Action recorded.';
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
			msg.textContent='[OK] Investigation signed off.';
			await _cicRefreshDetail();
			loadClusterInvestigations();
		}else{ const e=await r.json(); msg.textContent='Error: '+(e.detail||r.status); }
	}catch(e){ msg.textContent='Error: '+e; }
}

function cicOpenReport(){
	if(!_cicCurrentCluster){ alert('No cluster selected.'); return; }
	window.open(`${API}/cluster-investigations/${encodeURIComponent(_cicCurrentCluster)}/report`, '_blank');
}

// -- Step 7: Phylogenetic + Visual Analytics --------------------------------
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
	let html='<table class="data-table"><thead><tr><th>Cluster</th><th>Members</th><th>Pairs</th><th>Priority</th><th>Band</th><th>Sustained/max gen</th><th>Flags</th><th>Actions</th></tr></thead><tbody>';
	for(const c of clusters.slice(0,10)){
		const summary=c.summary||c||{};
		const tx=summary.transmission_generations||{};
		const maxGeneration=tx.max_generation ?? c.max_generation ?? null;
		const sustainedFlag=(typeof tx.sustained_transmission_flag==='boolean') ? tx.sustained_transmission_flag : c.sustained_transmission_flag;
		const txText=maxGeneration==null ? 'n/a' : `${sustainedFlag ? 'Yes' : 'No'} (max ${maxGeneration})`;
		html+=`<tr><td><code>${escapeHtml(c.cluster_short||String(c.cluster_id||'').slice(0,8))}</code></td><td>${escapeHtml(summary.member_count ?? c.member_count ?? 0)}</td><td>${escapeHtml(summary.pair_count ?? c.pair_count ?? 0)}</td><td>${escapeHtml(summary.priority_score ?? c.priority_score ?? 0)}</td><td>${escapeHtml(summary.priority_band ?? c.priority_band ?? 'low')}</td><td>${escapeHtml(txText)}</td><td>${escapeHtml((c.flags||[]).join(', ')||'none')}</td><td>${escapeHtml((c.recommended_investigation_actions||c.top_recommended_actions||[]).slice(0,3).join(' | ')||'none')}</td></tr>`;
	}
	html+='</tbody></table>';
	return html;
}

function _renderClusterRiskSummaryTable(clusters){
	if(!clusters.length) return '<p class="hint">No cluster risk summaries are available for the current dataset.</p>';
	let html='<table class="data-table"><thead><tr><th>Rank</th><th>Cluster</th><th>Priority score</th><th>Band</th><th>Members</th><th>Pairs</th><th>Top flags</th><th>Action focus</th></tr></thead><tbody>';
	for(const [index,c] of clusters.slice(0,15).entries()){
		const priorityScore=c.priority_score ?? 0;
		const flags=(c.flags||[]).slice(0,2).join(', ') || 'none';
		const actions=(c.top_recommended_actions||[]).slice(0,2).join(' | ') || 'review dossier';
		html+=`<tr><td>${escapeHtml(index+1)}</td><td><code>${escapeHtml(c.cluster_short||String(c.cluster_id||'').slice(0,8))}</code></td><td>${escapeHtml(priorityScore)}</td><td>${escapeHtml(c.priority_band||'low')}</td><td>${escapeHtml(c.member_count ?? 0)}</td><td>${escapeHtml(c.pair_count ?? 0)}</td><td>${escapeHtml(flags)}</td><td>${escapeHtml(actions)}</td></tr>`;
	}
	html+='</tbody></table>';
	return html;
}

async function loadTransmissionSynthesisOverview(){
	const summary=document.getElementById('synthesisSummary');
	const view=document.getElementById('synthesisPrimaryView');
	const btn=document.getElementById('loadSynthesisBtn');
	const originalBtnLabel=btn?.textContent || 'Load synthesis overview';
	if(btn){ btn.disabled=true; btn.textContent='Loading synthesis...'; }
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
				Operational review pairs: ${escapeHtml(s.operational_review_pair_count??s.high_priority_pairs??0)} |
				Contradictory pairs: ${escapeHtml(s.contradictory_pairs||0)}
			</div>${warning}`;
		}
		if(view){
			const clusters=d.clusters||[];
			let html=`<h4>${cid?'Cluster synthesis':'Transmission synthesis overview'}</h4>`;
			html+=`<p class="hint">Validation: ${escapeHtml(d.validation_status||'unknown')}  |  Generated ${escapeHtml((d.generated_at||'').replace('T',' ').replace('Z',' UTC'))}</p>`;
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
			const clusters=d.clusters||[];
			const highPriority=clusters.filter(c=>(c.priority_band||'').toLowerCase()==='high').length;
			let html='<h4>Cluster risk summary</h4>';
			html+='<p class="hint">Operational triage view: ranked clusters for immediate review planning.</p>';
			html+=`<div class="kpi-strip">High-priority clusters: ${escapeHtml(highPriority)} | Top 5 average score: ${escapeHtml((clusters.slice(0,5).reduce((acc,c)=>acc+Number(c.priority_score||0),0)/(Math.min(clusters.length,5)||1)).toFixed(1))}</div>`;
			html+=_renderClusterRiskSummaryTable(clusters);
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
		const thresh=p.snpThreshold;
		let html=`<h4>SNP Distance Matrix (${escapeHtml(d.case_count)} cases)</h4><p class="hint">${escapeHtml(d.message||'')}</p>`;
		html+='<div style="display:flex;gap:1rem;align-items:center;margin-bottom:.5rem;flex-wrap:wrap;font-size:.78rem">';
		html+='<span style="display:inline-flex;align-items:center;gap:4px"><span style="width:14px;height:14px;background:#16a34a;border-radius:3px;display:inline-block"></span> Linked (≤'+escapeHtml(thresh)+' SNP)</span>';
		html+='<span style="display:inline-flex;align-items:center;gap:4px"><span style="width:14px;height:14px;background:#fef08a;border-radius:3px;border:1px solid #d97706;display:inline-block"></span> Marginal (≤25 SNP)</span>';
		html+='<span style="display:inline-flex;align-items:center;gap:4px"><span style="width:14px;height:14px;background:#fecaca;border-radius:3px;display:inline-block"></span> Distant (>25 SNP)</span>';
		html+='</div>';
		html+='<div class="analytics-table-wrap"><table class="data-table snp-matrix"><thead><tr><th>Case</th>';
		for(const sid of d.short_case_ids){ html+=`<th class="snp-col-hdr"><span>${escapeHtml(sid)}</span></th>`; }
		html+='</tr></thead><tbody>';
		for(let i=0;i<d.case_count;i++){
			html+=`<tr><th>${escapeHtml(d.short_case_ids[i])}</th>`;
			for(let j=0;j<d.case_count;j++){
				const val=d.matrix[i][j];
				const cls=val===0?'snp-self':(val<=thresh?'snp-close':(val<=25?'snp-mid':'snp-far'));
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

async function loadAdvancedFastaSummaryView(){
	const view=document.getElementById('analyticsPrimaryView');
	view.textContent='Loading advanced FASTA summary...';
	try{
		const d=await fetch(`${API}/analytics/advanced-fasta-summary`).then(r=>r.json());
		const stats=d.seqkit_stats||{};
		const agree=d.snp_matrix_agreement||{};
		const iq=d.iqtree||{};
		const clock=d.molecular_clock||{};
		const counts=d.artifact_counts||{};
		let html='<h4>Advanced FASTA Analysis</h4>';
		html+=`<div class="kpi-strip">Status: ${escapeHtml(d.status||'unknown')} | Samples: ${escapeHtml((d.input||{}).sample_count??stats.num_seqs??'n/a')} | SNP matrix: ${escapeHtml(agree.status||'n/a')}</div>`;
		if(Array.isArray(d.warnings)&&d.warnings.length){
			html+='<div class="result-panel" style="border-color:#f59e0b;background:#fffbeb"><strong>Review warnings</strong><ul>';
			for(const w of d.warnings.slice(0,6)) html+=`<li>${escapeHtml(w)}</li>`;
			html+='</ul></div>';
		}
		html+='<div class="analytics-grid">';
		html+='<div><h5>FASTA QC</h5><table class="data-table"><tbody>';
		for(const [label,key] of [['Sequences','num_seqs'],['Total length','sum_len'],['Min length','min_len'],['Average length','avg_len'],['Max length','max_len']]){
			html+=`<tr><th>${escapeHtml(label)}</th><td>${escapeHtml(stats[key]??'n/a')}</td></tr>`;
		}
		html+='</tbody></table></div>';
		html+='<div><h5>SNP Matrix Validation</h5><table class="data-table"><tbody>';
		html+=`<tr><th>Compared pairs</th><td>${escapeHtml(agree.compared_pairs??0)}</td></tr>`;
		html+=`<tr><th>Mismatches</th><td>${escapeHtml(agree.mismatch_count??0)}</td></tr>`;
		html+=`<tr><th>Max delta</th><td>${escapeHtml(agree.max_abs_delta??'n/a')}</td></tr>`;
		html+='</tbody></table></div>';
		html+='<div><h5>IQ-TREE</h5><table class="data-table"><tbody>';
		for(const [label,key] of [['Input','input_data'],['Model','model'],['Parsimony sites','parsimony_informative_sites'],['Log likelihood','log_likelihood'],['Tree length','total_tree_length']]){
			html+=`<tr><th>${escapeHtml(label)}</th><td>${escapeHtml(iq[key]??'n/a')}</td></tr>`;
		}
		html+='</tbody></table></div>';
		html+='<div><h5>TreeTime</h5><table class="data-table"><tbody>';
		html+=`<tr><th>Clock rate</th><td>${escapeHtml(clock.rate??'n/a')}</td></tr>`;
		html+=`<tr><th>Root-tip r^2</th><td>${escapeHtml(clock.r_squared??'n/a')}</td></tr>`;
		html+='</tbody></table></div>';
		html+='</div>';
		html+='<h5>Raw output counts</h5><table class="data-table"><thead><tr><th>Output</th><th>Count</th></tr></thead><tbody>';
		for(const [key,value] of Object.entries(counts)){
			html+=`<tr><td>${escapeHtml(key.replaceAll('_',' '))}</td><td>${escapeHtml(value)}</td></tr>`;
		}
		html+='</tbody></table>';
		if(Array.isArray(agree.mismatches)&&agree.mismatches.length){
			html+='<h5>SNP matrix mismatches</h5><table class="data-table"><thead><tr><th>Case A</th><th>Case B</th><th>Internal</th><th>snp-dists</th><th>Delta</th></tr></thead><tbody>';
			for(const m of agree.mismatches.slice(0,10)){
				html+=`<tr><td>${escapeHtml(m.case_a_short)}</td><td>${escapeHtml(m.case_b_short)}</td><td>${escapeHtml(m.internal_distance)}</td><td>${escapeHtml(m.external_distance)}</td><td>${escapeHtml(m.delta)}</td></tr>`;
			}
			html+='</tbody></table>';
		}
		view.innerHTML=html;
	}catch(e){
		view.textContent='Failed to load advanced FASTA summary: '+e;
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
		const _cb=Date.now();
		html+=`<img class="media-plot" src="${escapeAttr(API+(d.images?.outbreaker_tree||'')+'?t='+_cb)}" alt="outbreaker tree"/>`;
		html+=`<img class="media-plot" src="${escapeAttr(API+(d.images?.outbreaker_phylo||'')+'?t='+_cb)}" alt="phylogenetic tree"/>`;
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

// Simplified continent outline polygons as [lon, lat] arrays.
// Equirectangular projection matches _geoToSvg.
const _WORLD_CONTINENTS=[
	// North America
	[[-168,72],[-140,70],[-132,56],[-125,49],[-100,49],[-67,47],[-53,47],[-60,23],[-83,10],[-90,16],[-105,22],[-117,29],[-124,37],[-125,49],[-132,56],[-140,70]],
	// Greenland
	[[-73,83],[-20,85],[-17,76],[-42,75],[-73,83]],
	// South America
	[[-80,12],[-62,11],[-50,5],[-34,-4],[-35,-8],[-38,-13],[-40,-23],[-52,-33],[-72,-42],[-75,-52],[-68,-56],[-65,-42],[-57,-38],[-50,-29],[-48,-27],[-42,-22],[-38,-13],[-34,-4],[-50,5],[-62,11],[-80,12]],
	// Europe
	[[-10,36],[28,36],[30,46],[25,48],[20,54],[24,60],[15,69],[5,72],[0,62],[-5,48],[-10,44],[-10,36]],
	// Africa
	[[-18,15],[35,15],[50,12],[44,-2],[42,-12],[35,-18],[30,-30],[18,-35],[8,-40],[-18,-35],[-18,15]],
	// Asia (mainland + peninsula)
	[[26,36],[42,12],[55,12],[58,22],[72,22],[80,9],[100,1],[108,2],[120,22],[130,32],[140,43],[150,46],[168,70],[140,72],[100,73],[80,73],[60,73],[50,68],[40,65],[30,68],[24,60],[20,54],[25,48],[30,46],[26,36]],
	// Australia
	[[114,-22],[122,-18],[130,-12],[138,-16],[142,-10],[148,-18],[152,-24],[152,-28],[149,-38],[144,-38],[136,-35],[128,-32],[114,-22]],
	// New Zealand (simplified)
	[[166,-46],[172,-44],[172,-40],[174,-37],[172,-40],[170,-44],[166,-46]],
];

function _worldMapSvg(w,h){
	return _WORLD_CONTINENTS.map(poly=>{
		const pts=poly.map(([lon,lat])=>{
			const c=_geoToSvg(lon,lat,w,h);
			return `${c.x.toFixed(1)},${c.y.toFixed(1)}`;
		}).join(' ');
		return `<polygon points="${pts}" fill="#d4e8c2" stroke="#9ab88a" stroke-width="0.7" stroke-linejoin="round"/>`;
	}).join('');
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
			dots+=`<circle cx="${c.x.toFixed(1)}" cy="${c.y.toFixed(1)}" r="${r.toFixed(1)}" fill="#ef4444" fill-opacity="0.75" stroke="#991b1b" stroke-width="1"><title>${escapeHtml(p.region)}: ${escapeHtml(p.case_count)} cases</title></circle>`;
		}
		let html='<h4>Geography Map (region centroids)</h4>';
		html+=`<svg viewBox="0 0 ${w} ${h}" class="analytics-svg map-svg"><rect x="0" y="0" width="${w}" height="${h}" fill="#cce4f0"/>${_worldMapSvg(w,h)}${dots}</svg>`;
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

async function loadGeoMapView(){
	const view=document.getElementById('analyticsPrimaryView');
	view.innerHTML='<h4>Geography Map (region centroids)</h4><div id="geo-leaflet-map" style="height:400px;width:100%;border-radius:6px;border:1px solid #e2e8f0"></div><div id="geo-map-table"></div>';
	try{
		const d=await fetch(`${API}/analytics/geo-map`).then(r=>r.json());
		const points=d.points||[];
		// Initialise Leaflet map — destroy any previous instance first
		if(window._geoLeafletMap){ window._geoLeafletMap.remove(); window._geoLeafletMap=null; }
		const centre=points.length?[Number(points[0].lat),Number(points[0].lon)]:[54.6,-6.7];
		const map=L.map('geo-leaflet-map').setView(centre,7);
		window._geoLeafletMap=map;
		L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{
			maxZoom:19,
			attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
		}).addTo(map);
		const maxCount=Math.max(1,...points.map(p=>Number(p.case_count||0)));
		for(const p of points){
			const r=6+(Number(p.case_count||0)/maxCount)*14;
			L.circleMarker([Number(p.lat),Number(p.lon)],{
				radius:r, color:'#991b1b', fillColor:'#ef4444',
				fillOpacity:0.75, weight:1.5
			}).bindPopup(`<strong>${escapeHtml(p.region)}</strong><br>Cases: ${escapeHtml(p.case_count)}<br>Clusters: ${escapeHtml(p.cluster_count)}<br>Recent 90d: ${escapeHtml(p.recent_cases_90d)}`).addTo(map);
		}
		if(points.length>1){
			const lats=points.map(p=>Number(p.lat));
			const lons=points.map(p=>Number(p.lon));
			map.fitBounds([[Math.min(...lats),Math.min(...lons)],[Math.max(...lats),Math.max(...lons)]],{padding:[30,30]});
		}
		let tbl='<table class="data-table" style="margin-top:.75rem"><tr><th>Region</th><th>Cases</th><th>Clusters</th><th>Recent 90d</th></tr>';
		for(const p of points.slice(0,20)){
			tbl+=`<tr><td>${escapeHtml(p.region)}</td><td>${escapeHtml(p.case_count)}</td><td>${escapeHtml(p.cluster_count)}</td><td>${escapeHtml(p.recent_cases_90d)}</td></tr>`;
		}
		tbl+='</table>';
		document.getElementById('geo-map-table').innerHTML=tbl;
	}catch(e){
		view.innerHTML+='<p style="color:#dc2626">Failed to load map: '+escapeHtml(String(e))+'</p>';
	}
}

function _agreementBand(value, passThreshold, warnThreshold){
	if(value===null||value===undefined||Number.isNaN(Number(value))) return 'pending';
	const v=Number(value);
	if(v>=passThreshold) return 'pass';
	if(v>=warnThreshold) return 'warn';
	return 'fail';
}

function _safePct(value){
	if(value===null || value===undefined || Number.isNaN(Number(value))) return 'n/a';
	return `${(Number(value)*100).toFixed(1)}%`;
}

async function loadCalibrationView(){
	const view=document.getElementById('analyticsPrimaryView');
	view.textContent='Loading calibration dashboard...';
	try{
		const cid=_analyticsClusterId();
		const p=_analyticsParams();
		const base=`${API}/analytics/case-pair-calibration?max_cases=120&max_pairs=1500&snp_strong_threshold=5&snp_moderate_threshold=${encodeURIComponent(p.snpThreshold)}&temporal_window_days=${encodeURIComponent(p.epiWindowDays)}`;
		const url=cid?`${base}&cluster_id=${encodeURIComponent(cid)}`:base;
		const d=await fetch(url).then(r=>r.json());

		const summary=d.summary||{};
		const exact=summary.exact_agreement;
		const binary=summary.binary_agreement;
		const exactBand=_agreementBand(exact, 0.7, 0.5);
		const binaryBand=_agreementBand(binary, 0.8, 0.65);
		const exactStatus=exactBand==='pass'?'ready':(exactBand==='warn'?'warning':(exactBand==='pending'?'pending':'failed'));
		const binaryStatus=binaryBand==='pass'?'ready':(binaryBand==='warn'?'warning':(binaryBand==='pending'?'pending':'failed'));

		let html='<h4>Calibration Dashboard: Model vs Reviewer</h4>';
		html+=`<p class="hint">Coverage reflects the fraction of model-generated pairs that have reviewer classifications. Use this to track calibration and drift over time.</p>`;
		html+=`<div class="kpi-strip">
			Model pairs: ${escapeHtml(d.model_pair_count??0)} |
			Reviewed pairs: ${escapeHtml(d.reviewed_pair_count??0)} |
			Coverage: ${escapeHtml(_safePct(d.coverage))} |
			Exact agreement: ${escapeHtml(_safePct(exact))} ${renderStatusPill(exactStatus)} |
			Binary agreement: ${escapeHtml(_safePct(binary))} ${renderStatusPill(binaryStatus)}
		</div>`;

		if(exactBand==='pending' && binaryBand==='pending'){
			html+=`<p class="hint" style="color:#6b7280;">No reviewer comparisons have been submitted yet. Agreement metrics will appear once pairs are reviewed.</p>`;
		}else if(exactBand==='fail' || binaryBand==='fail'){
			html+=`<p class="hint" style="color:#9a3412;"><strong>Alert:</strong> Agreement has dropped below expected thresholds. Review recent contradictory pairs and re-check interpretation rules.</p>`;
		}else if(exactBand==='warn' || binaryBand==='warn'){
			html+=`<p class="hint" style="color:#92400e;"><strong>Watch:</strong> Agreement is moderate. Consider focused reviewer reconciliation and threshold review.</p>`;
		}else{
			html+=`<p class="hint" style="color:#166534;"><strong>Stable:</strong> Agreement is within expected range for current heuristics.</p>`;
		}

		const timeline=d.review_volume_by_month||[];
		if(timeline.length){
			html+='<h5>Reviewer volume trend</h5>';
			html+=_sparkline(timeline, 'count');
		}

		const byLabel=summary.by_label||{};
		const labels=Object.keys(byLabel);
		html+='<h5>Agreement by reviewer label</h5>';
		if(!labels.length){
			html+='<p class="hint">No reviewed pairs available for calibration yet.</p>';
		}else{
			html+='<table class="data-table"><thead><tr><th>Reviewer label</th><th>Pairs</th><th>Exact matches</th><th>Exact agreement</th></tr></thead><tbody>';
			for(const label of labels){
				const row=byLabel[label]||{};
				html+=`<tr><td>${escapeHtml(label)}</td><td>${escapeHtml(row.count??0)}</td><td>${escapeHtml(row.exact_matches??0)}</td><td>${escapeHtml(_safePct(row.exact_agreement))}</td></tr>`;
			}
			html+='</tbody></table>';
		}

		const confusion=summary.confusion||{};
		const allCols=new Set(d.reviewer_classification_options||[]);
		for(const reviewerLabel of Object.keys(confusion)){
			for(const modelLabel of Object.keys(confusion[reviewerLabel]||{})) allCols.add(modelLabel);
		}
		const colLabels=Array.from(allCols);
		html+='<h5>Confusion matrix (reviewer rows, model columns)</h5>';
		if(!Object.keys(confusion).length){
			html+='<p class="hint">Confusion matrix will appear once reviewer labels are saved.</p>';
		}else{
			html+='<div class="analytics-table-wrap"><table class="data-table"><thead><tr><th>Reviewer \ Model</th>';
			for(const col of colLabels) html+=`<th>${escapeHtml(col)}</th>`;
			html+='</tr></thead><tbody>';
			for(const reviewerLabel of Object.keys(confusion)){
				html+=`<tr><th>${escapeHtml(reviewerLabel)}</th>`;
				for(const col of colLabels){
					html+=`<td>${escapeHtml((confusion[reviewerLabel]||{})[col]??0)}</td>`;
				}
				html+='</tr>';
			}
			html+='</tbody></table></div>';
		}

		html+='<h5>Recent reviewed comparisons</h5>';
		html+='<table class="data-table"><thead><tr><th>Pair</th><th>Model</th><th>Reviewer</th><th>Match</th><th>Reviewed by</th><th>Reviewed at</th></tr></thead><tbody>';
		for(const item of (d.comparisons||[]).slice(0,30)){
			html+=`<tr><td>${escapeHtml(item.pair||'')}</td><td>${escapeHtml(item.model_label||'')}</td><td>${escapeHtml(item.reviewer_label||'')}</td><td>${item.match?'Y':'N'}</td><td>${escapeHtml(item.reviewer||'')}</td><td>${escapeHtml((item.reviewed_at||'').replace('T',' ').replace('Z',' UTC'))}</td></tr>`;
		}
		if(!(d.comparisons||[]).length){
			html+='<tr><td colspan="6">No reviewed pair comparisons available.</td></tr>';
		}
		html+='</tbody></table>';

		if(Array.isArray(d.notes)&&d.notes.length){
			html+=`<p class="hint">${escapeHtml(d.notes.join(' '))}</p>`;
		}
		view.innerHTML=html;
	}catch(e){
		view.textContent='Failed to load calibration dashboard: '+e;
	}
}

async function loadGenomicVsEpiView(){
	const view=document.getElementById('analyticsPrimaryView');
	view.textContent='Loading genomic vs epi comparison...';
	try{
		const p=_analyticsParams();
		const url=`${API}/analytics/genomic-vs-epi?snp_threshold=${encodeURIComponent(p.snpThreshold)}&epi_window_days=${encodeURIComponent(p.epiWindowDays)}&posterior_min=${encodeURIComponent(p.posteriorMin)}`;
		const d=await fetch(url).then(r=>r.json());
		const s=d.summary||{};
		let html='<h4>Genomic vs Epidemiological Link Comparison</h4>';
		html+=`<div class="kpi-strip">
			Total pairs: ${escapeHtml(s.total_pairs||0)} |
			Both supported: ${escapeHtml(s.both_supported||0)} |
			Genomic only: ${escapeHtml(s.genomic_only||0)} |
			Epi only: ${escapeHtml(s.epi_only||0)} |
			Neither: ${escapeHtml(s.neither||0)}
		</div>`;
		const params=d.parameters||{};
		html+=`<p class="hint">SNP threshold: ${escapeHtml(params.snp_threshold??p.snpThreshold)} | Epi window: ${escapeHtml(params.epi_window_days??p.epiWindowDays)} days | Posterior min: ${escapeHtml(params.posterior_min??p.posteriorMin)}</p>`;
		const pairs=d.pairs||[];
		const reliabilityNotes=(d.notes||[]).filter(note=>String(note).toLowerCase().includes('not assessable'));
		if(reliabilityNotes.length){
			html+=`<div class="callout callout-warn"><strong>Posterior confidence not assessable.</strong> ${escapeHtml(reliabilityNotes.join(' '))}</div>`;
		}
		if(!pairs.length){
			html+='<p class="hint">No transmission pairs available. Run Outbreaker2 analysis first.</p>';
		}else{
			html+='<div class="analytics-table-wrap"><table class="data-table"><thead><tr><th>Pair</th><th>Posterior</th><th>Confidence</th><th>SNP distance</th><th>Genomic</th><th>Epi</th><th>Category</th></tr></thead><tbody>';
			for(const row of pairs){
				const cat=row.category||'';
				const catColor=cat==='both_supported'?'#166534':cat==='genomic_only'?'#1e40af':cat==='epi_only'?'#92400e':'#6b7280';
				html+=`<tr>
					<td>${escapeHtml(row.pair||'')}</td>
					<td>${escapeHtml((row.posterior??'').toString())}</td>
					<td>${escapeHtml(row.confidence==='not_assessable'?'not assessable':(row.confidence||''))}</td>
					<td>${row.snp_distance!=null?escapeHtml(row.snp_distance.toString()):'n/a'}</td>
					<td>${row.genomic_supported?'Yes':'No'}</td>
					<td>${row.epi_supported?'Yes':'No'}</td>
					<td style="color:${catColor};font-weight:600">${escapeHtml(cat.replace(/_/g,' '))}</td>
				</tr>`;
			}
			html+='</tbody></table></div>';
		}
		if(Array.isArray(d.notes)&&d.notes.length){
			html+=`<p class="hint">${escapeHtml(d.notes.join(' '))}</p>`;
		}
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

// -----------------------------------------------------------------------------

function goToCaseEvidenceWorkflow(){
	const card=document.getElementById('cicCard');
	if(!card) return;
	card.scrollIntoView({behavior:'smooth', block:'start'});
	const list=document.getElementById('cicList');
	if(list && !list.textContent.trim()){
		loadClusterInvestigations();
	}
}

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
	const btn=document.getElementById('loadOutbreakerStatusBtn');
	if(!box) return;
	outbreakerStatusRefreshCount+=1;
	const refreshId=outbreakerStatusRefreshCount;
	if(btn){
		btn.disabled=true;
		btn.dataset.originalLabel=btn.dataset.originalLabel||btn.textContent||'Load outbreaker status';
		btn.textContent=`Loading outbreaker status (#${refreshId})...`;
	}
	box.textContent=`Loading outbreaker status (request #${refreshId})...`;
	try{
		let apiBase=API;
		let d;
		const ts=Date.now();
		try{
			d=await apiJson(`${apiBase}/cases/outbreaker-status?_ts=${encodeURIComponent(ts)}`);
		}catch(firstError){
			const alternateApiBase=(apiBase===API_FALLBACK)?'http://localhost:8000':API_FALLBACK;
			d=await apiJson(`${alternateApiBase}/cases/outbreaker-status?_ts=${encodeURIComponent(ts)}`);
			apiBase=alternateApiBase;
			API=alternateApiBase;
		}
		const checkedAtLocal=new Date().toLocaleString();
		const checkedAtIso=new Date().toISOString();
		const missing=(Array.isArray(d.missing_artifacts)?d.missing_artifacts:[]).map(v=>String(v));
		const ready=('artifact_ready' in d)?Boolean(d.artifact_ready):(('ready' in d)?Boolean(d.ready):(Boolean(d.cases_export)&&Boolean(d.dna_export)&&Boolean(d.results_rds)));
		const operationalReady=Boolean(d.operational_ready);
		const readinessLabel=operationalReady?'OPERATIONAL READY':(ready?'ARTIFACTS READY - EXPLORATORY':'NOT READY');
		const summaryUpdated=d.summary_updated_at?`Summary updated: ${escapeHtml(String(d.summary_updated_at))}`:'Summary updated: unknown';
		const missingLabel=missing.length?`Missing: ${escapeHtml(missing.join(', '))}`:'Missing: none';
		box.innerHTML=`<div class="kpi-strip"><strong>Outbreaker readiness: ${escapeHtml(readinessLabel)}</strong> | Cases export: ${d.cases_export?'available':'missing'} | DNA export: ${d.dna_export?'available':'missing'} | Results RDS: ${d.results_rds?'available':'missing'} | Provenance: ${escapeHtml(d.provenance||'unknown')} | Posterior reliable: ${escapeHtml(d.posterior_reliable)} | Mock: ${escapeHtml(d.is_mock)} | API: ${escapeHtml(apiBase)} | Last checked: ${escapeHtml(checkedAtLocal)} | Request #: ${escapeHtml(refreshId)}</div><p class="hint">${missingLabel} | ${summaryUpdated}</p><p class="hint">Refresh confirmation: request #${escapeHtml(refreshId)} completed at ${escapeHtml(checkedAtIso)}</p>`;
	}catch(e){
		box.textContent=`Failed to load outbreaker status (request #${refreshId}): ${e}`;
	}finally{
		if(btn){
			btn.textContent=btn.dataset.originalLabel||'Load outbreaker status';
			btn.disabled=false;
		}
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

function _shortId(value){
	return value ? String(value).slice(0,8) : '';
}

function _recordTable(rows, columns, empty){
	if(!Array.isArray(rows)||!rows.length) return `<p class="hint empty-records">${escapeHtml(empty)}</p>`;
	const header=columns.map(([label])=>`<th>${escapeHtml(label)}</th>`).join('');
	const body=rows.slice(0,10).map(row=>`<tr>${columns.map(([,key,formatter])=>`<td>${escapeHtml(formatter ? formatter(row[key], row) : row[key]??'')}</td>`).join('')}</tr>`).join('');
	return `<div class="epi-reference-table-wrap"><table class="data-table compact-record-table"><thead><tr>${header}</tr></thead><tbody>${body}</tbody></table></div>`;
}

async function loadExposureRecords(){
	const box=document.getElementById('exposureRecords');
	if(!box) return;
	box.innerHTML='<p class="hint">Loading exposures...</p>';
	try{
		const d=await apiJson(`${API}/epidemiology/exposures?limit=25`);
		box.innerHTML=_recordTable(d,[['ID','exposure_id',_shortId],['Type','exposure_type'],['Context','exposure_context'],['Confidence','confidence']], 'No exposures recorded.');
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
		box.innerHTML=_recordTable(d,[['ID','contact_id',_shortId],['Label','contact_label'],['Type','contact_type'],['Relationship','relationship_type']], 'No contacts recorded.');
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
		box.innerHTML=_recordTable(d,[['ID','location_id',_shortId],['Name','location_name'],['Type','location_type'],['Region','geographic_region'],['Postcode','postcode_prefix']], 'No locations recorded.');
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
		// Backend unavailable - leave placeholder only
	}
}
(async()=>{try{await fetch(`${API}/`);document.getElementById('status').innerHTML=renderSystemStatusItem('Backend','Running','status-pass','API responded successfully');}catch{document.getElementById('status').innerHTML=renderSystemStatusItem('Backend','Unavailable','status-fail','Unable to reach the API from this session');}refreshDemoModeStatus();loadRegions();loadKPIBanner();loadWorkflowStatus();loadDataSafety();loadDataReadiness();loadAnalyticsClusters();loadTransmissionSynthesisOverview();loadActionableReportSummary();loadFullKpis();loadOutbreakerStatus();loadResistanceValidationStatus();})();
