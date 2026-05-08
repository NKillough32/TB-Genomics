
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
		let html='<div style="margin:10px 0;">';
		
		// Summary stats
		html+='<h4>Case Summary</h4>';
		html+=`Total: ${summary.total_cases} | Clustered: ${summary.clustered_cases} | Unclustered: ${summary.unclustered_cases}<br/>`;
		
		// Outbreaker analysis
		html+='<h4>Outbreak Analysis</h4>';
		html+=`Status: ${analysis.status}<br/>`;
		if(analysis.summary){
			html+=`Samples: ${analysis.summary.n_samples} | Mean Likelihood: ${analysis.summary.likelihood_mean?.toFixed(2)}<br/>`;
		}
		
		// Graphics
		if(analysis.graphics.length > 0){
			html+='<h4>Diagnostic Plots</h4>';
			for(const graphic of analysis.graphics){
				const fullUrl=graphic.url.startsWith('http')?graphic.url:`${API}${graphic.url}`;
				html+=`<img src="${fullUrl}" style="max-width:100%; border:1px solid #ccc; margin:10px 0;" alt="${graphic.type}"/>`;
			}
		}
		
		html+='</div>';
		box.innerHTML=html;
	}catch(e){
		box.textContent=`Failed to load analysis: ${e}`;
	}
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
		let html=`<h4>Search Results: ${data.total_results} cases found</h4>`;
		if(data.total_results>0){
			html+='<table style="width:100%;font-size:0.85rem;border-collapse:collapse;">';
			html+='<tr style="border-bottom:1px solid #ccc;"><th>Case ID</th><th>Date</th><th>Region</th><th>Lineage</th><th>Cluster</th></tr>';
			for(const c of data.cases){
				html+=`<tr style="border-bottom:1px solid #eee;"><td>${c.case_id}</td><td>${c.specimen_date}</td><td>${c.region}</td><td>${c.lineage}</td><td>${c.cluster_id||'—'}</td></tr>`;
			}
			html+='</table>';
		}
		box.innerHTML=html;
	}catch(e){
		box.textContent=`Search failed: ${e}`;
	}
}
async function loadCaseHistory(){
	const caseId=document.getElementById('caseHistoryId').value;
	const box=document.getElementById('caseHistory');
	if(!caseId){box.textContent='Please enter a case ID';return;}
	box.textContent='Loading case history...';
	try{
		const r=await fetch(`${API}/cases/case-history/${encodeURIComponent(caseId)}`);
		const data=await r.json();
		if(data.error){box.textContent=`Case not found: ${data.error}`;return;}
		let html=`<h4>Case ${data.case_id}</h4>`;
		html+=`<p>Related Cases: ${data.related_cases} | Observation Span: ${data.observation_span_days} days</p>`;
		if(data.history.length>0){
			html+='<table style="width:100%;font-size:0.85rem;border-collapse:collapse;">';
			html+='<tr style="border-bottom:1px solid #ccc;"><th>Specimen Date</th><th>Region</th><th>Lineage</th><th>Status</th><th>Index?</th></tr>';
			for(const h of data.history){
				html+=`<tr style="border-bottom:1px solid #eee;"><td>${h.specimen_date}</td><td>${h.region}</td><td>${h.lineage||'—'}</td><td>${h.status}</td><td>${h.is_index_case?'✓':''}</td></tr>`;
			}
			html+='</table>';
		}
		box.innerHTML=html;
	}catch(e){
		box.textContent=`Failed to load case history: ${e}`;
	}
}
(async()=>{try{await fetch(`${API}/`);document.getElementById('status').innerHTML='<li>✅ Backend running</li>';}catch{document.getElementById('status').innerHTML='<li>❌ Backend unavailable</li>';}})();
