
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
async function runJob(job){document.getElementById('jobStatus').textContent='Starting '+job;const r=await fetch(`${API}/jobs/run/${job}`,{method:'POST'});const d=await r.json();activeJob=d.job_id;poll();}
async function poll(){if(!activeJob)return;const r=await fetch(`${API}/jobs/status/${activeJob}`);const d=await r.json();document.getElementById('jobStatus').textContent=JSON.stringify(d,null,2);const bar=document.getElementById('progressBar');bar.style.width=(d.progress||0)+'%';bar.textContent=(d.progress||0)+'%';if(d.status!=='completed'&&d.status!=='failed'){setTimeout(poll,1500);} }
async function loadCases(){const r=await fetch(`${API}/cases`);document.getElementById('cases').textContent=JSON.stringify(await r.json(),null,2);} 
async function loadOutbreakerResults(){document.getElementById('outbreakerResults').textContent='Results available after outbreaker2 completes.';}
(async()=>{try{await fetch(`${API}/`);document.getElementById('status').innerHTML='<li>✅ Backend running</li>';}catch{document.getElementById('status').innerHTML='<li>❌ Backend unavailable</li>';}})();
