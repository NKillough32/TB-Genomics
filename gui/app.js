
const API='http://localhost:8000';let activeJob=null;
async function uploadFile(){const f=document.getElementById('fileInput').files[0];if(!f)return;const fd=new FormData();fd.append('file',f);const r=await fetch(`${API}/ingest/file`,{method:'POST',body:fd});document.getElementById('uploadResult').textContent=JSON.stringify(await r.json());}
async function seedSyntheticData(){
	const caseCount=Number(document.getElementById('seedCaseCount').value||250);
	const seed=Number(document.getElementById('seedValue').value||42);
	const reset=document.getElementById('seedReset').checked;
	const resultBox=document.getElementById('seedResult');
	resultBox.textContent='Generating synthetic dataset...';
	try{
		const url=`${API}/ingest/seed-synthetic?case_count=${encodeURIComponent(caseCount)}&reset=${encodeURIComponent(reset)}&seed=${encodeURIComponent(seed)}`;
		const r=await fetch(url,{method:'POST'});
		const payload=await r.json();
		resultBox.textContent=JSON.stringify(payload,null,2);
	}catch(e){
		resultBox.textContent=`Failed to generate synthetic data: ${e}`;
	}
}
async function runJob(job){document.getElementById('jobStatus').textContent='Starting '+job;const r=await fetch(`${API}/jobs/run/${job}`,{method:'POST'});const d=await r.json();if(!d.job_id){document.getElementById('jobStatus').textContent=JSON.stringify(d,null,2);return;}activeJob=d.job_id;poll();}
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
			html+=`<div class="kpi-strip">Samples: ${analysis.summary.n_samples} | Mean Likelihood: ${analysis.summary.likelihood_mean?.toFixed(2)}</div>`;
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
(async()=>{try{await fetch(`${API}/`);document.getElementById('status').innerHTML='<li>✅ Backend running</li>';}catch{document.getElementById('status').innerHTML='<li>❌ Backend unavailable</li>';}})();
