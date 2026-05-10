
let API='http://localhost:8000';const API_FALLBACK='http://127.0.0.1:8010';let activeJob=null;

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
		html+=`<p>Total: ${summary.total_cases} | Clustered: ${summary.clustered_cases} | Unclustered: ${summary.unclustered_cases}</p>`;
		
		// Outbreaker analysis
		html+='<h4>Outbreak Analysis</h4>';
		html+=`<p>Status: ${analysis.status}</p>`;
		if(analysis.summary){
			const posteriorSamples=(analysis.summary.n_samples??'n/a');
			const caseCount=(analysis.summary.case_count??'n/a');
			html+=`<div class="kpi-strip">Case count: ${caseCount} | Posterior samples (MCMC): ${posteriorSamples} | Mean Likelihood: ${analysis.summary.likelihood_mean?.toFixed(2)}</div>`;
		}

		if(analysis.transmission_network){
			const net=analysis.transmission_network;
			html+='<h4>Transmission Network Insights</h4>';
			html+=`<div class="kpi-strip">Nodes: ${net.node_count||0} | Links: ${net.edge_count||0} | Clusters: ${net.cluster_count||0} | High-confidence links: ${net.high_confidence_edges||0}</div>`;
			if(Array.isArray(net.key_nodes)&&net.key_nodes.length>0){
				html+='<p><strong>Potential priority spreaders</strong></p>';
				html+='<table class="data-table">';
				html+='<tr><th>Case</th><th>Cluster</th><th>Region</th><th>Risk</th><th>Band</th><th>Out</th><th>In</th></tr>';
				for(const n of net.key_nodes.slice(0,8)){
					html+=`<tr><td>${n.case_id}</td><td>${(n.cluster_id||'').toString().slice(0,8)}</td><td>${n.region||'Unknown'}</td><td>${n.risk_score??0}</td><td>${n.risk_band||'low'}</td><td>${n.outgoing_links??0}</td><td>${n.incoming_links??0}</td></tr>`;
				}
				html+='</table>';
			}
		}
		
		// Graphics
		if(analysis.graphics.length > 0){
			html+='<h4>Diagnostic Plots</h4>';
			for(const graphic of analysis.graphics){
				const fullUrl=graphic.url.startsWith('http')?graphic.url:`${API}${graphic.url}`;
				html+=`<img src="${fullUrl}" class="media-plot" alt="${graphic.type}"/>`;
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
		box.textContent=JSON.stringify(payload,null,2);
	}catch(e){
		box.textContent=`Failed to load lineage/DR validation: ${e}`;
	}
}
function downloadOutbreakReport(){
	window.open(`${API}/cases/outbreak-report`, '_blank');
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
	const box=document.getElementById('searchResults');
	box.textContent='Searching cases...';
	try{
		let url=`${API}/cases/search?`;
		const params=[];
		if(region) params.push(`region=${encodeURIComponent(region)}`);
		if(lineage) params.push(`lineage=${encodeURIComponent(lineage)}`);
		if(dateFrom) params.push(`date_from=${encodeURIComponent(dateFrom)}`);
		if(dateTo) params.push(`date_to=${encodeURIComponent(dateTo)}`);
		url+=params.join('&');
		const r=await fetch(url);
		const data=await r.json();
		let html=`<div class="result-panel"><h4>Search Results: ${data.total_results} cases found</h4>`;
		if(data.total_results>0){
			html+='<table class="data-table">';
			html+='<tr><th>Case ID</th><th>Date</th><th>Region</th><th>Lineage</th><th>Cluster</th><th>Action</th></tr>';
			for(const c of data.cases){
				html+=`<tr><td>${c.case_id}</td><td>${c.specimen_date}</td><td>${c.region}</td><td>${c.lineage}</td><td>${c.cluster_id||'-'}</td><td><button type="button" class="mini-btn" onclick="loadCaseHistory('${c.case_id}')">View history</button></td></tr>`;
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
		let html=`<div class="result-panel"><h4>Case ${data.case_id}</h4>`;
		html+=`<div class="kpi-strip">Related Cases: ${data.related_cases} | Observation Span: ${data.observation_span_days} days</div>`;
		if(data.history.length>0){
			html+='<table class="data-table">';
			html+='<tr><th>Specimen Date</th><th>Region</th><th>Lineage</th><th>Status</th><th>Index?</th></tr>';
			for(const h of data.history){
				html+=`<tr><td>${h.specimen_date}</td><td>${h.region}</td><td>${h.lineage||'-'}</td><td>${h.status}</td><td>${h.is_index_case?'Y':''}</td></tr>`;
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
(async()=>{try{await fetch(`${API}/`);document.getElementById('status').innerHTML='<li>✅ Backend running</li>';}catch{document.getElementById('status').innerHTML='<li>❌ Backend unavailable</li>';}loadRegions();loadKPIBanner();loadDataSafety();})();
