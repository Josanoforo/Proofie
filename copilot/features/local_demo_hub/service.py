import hashlib
from pathlib import Path
import json
from copilot import config
ROOT=Path(__file__).resolve().parents[3]
LANE_RECORDS=ROOT/'internal'/'lanes'/'mejoras10'   # the lane's records live under internal/ (not exported); absent in a kit, where they read as unavailable
VERSION='local-R3'
PAGES=[('Evidence dossier','evidence_dossier','Local source-backed dossier preview'),('Explainable queue','explainable_queue','Recorded snapshots and event timeline'),('Negotiation drafts','negotiation_drafts','Fact-bound proposals; never sent'),('Sale readiness','sale_readiness','Sale checklist and prepared-only tracking'),('Appeal workspace','appeal_workspace','Recorded outcome and evidence delta'),('Local MCP contracts','mcp_local_contracts','stdio local case tools'),('Evaluation workbench','evaluation_workbench','Manifest viewer; no evaluation run'),('Judge walkthrough','judge_walkthrough','Offline local walkthrough'),('Local autopilot','local_autopilot','SIMULATED — NOT SENT'),('Demo hub','local_demo_hub','Local navigation and claim index')]
CLAIMS=[{'claim':'Bundled sample source','artifact':'repository synthetic fixture','status':'synthetic; not a provider record'},{'claim':'Historical CUT72 R04 was 52/72','artifact':'recorded historical run','status':'historical only at 5a12f56e136b1f7e98b1e5b9f0387191f068be1e; not rerun'},{'claim':'External human rehearsals Q1/Q2','artifact':'blank rehearsal forms','status':'NOTRUN'}]
STORYBOARD=['Start at the existing /disputes inbox','Open one case and its saved package','Review integrated dossier, checklist, outcome or draft where enabled','Return to the same case using its case-aware navigation']
FLAGS={slug:'COPILOT_FEATURE_'+slug.upper() for _,slug,_ in PAGES}
DESTINATION_FLAGS={
 "negotiation_drafts":("resolution","negotiation_drafts"),
 "appeal_workspace":("outcome","appeal_workspace"),
}
ARTIFACTS=[('synthetic dossier sample','copilot/features/evidence_dossier/service.py'),('queue acceptance fixtures','copilot/features/explainable_queue/service.py'),('MCP local catalog','copilot/features/mcp_local_contracts/server.py'),('local walkthrough manifest','copilot/features/judge_walkthrough/service.py'),('evaluation workbench manifest','copilot/features/evaluation_workbench/service.py')]
ACCEPTANCE_LOGS = {
    "evidence_dossier": "01-evidence_dossier-final-acceptance.log",
    "explainable_queue": "02-explainable_queue-final-acceptance.log",
    "negotiation_drafts": "03-negotiation_drafts-final-acceptance.log",
    "sale_readiness": "04-sale_readiness-final-acceptance.log",
    "appeal_workspace": "05-appeal_workspace-final-acceptance.log",
    "mcp_local_contracts": "06-mcp_local_contracts-final-acceptance.log",
    "evaluation_workbench": "07-evaluation_workbench-final-acceptance.log",
    "judge_walkthrough": "08-judge_walkthrough-final-acceptance.log",
    "local_autopilot": "09-local_autopilot-final-acceptance.log",
    "local_demo_hub": "10-local_demo_hub-final-acceptance.log",
}
INTEGRATION_LOGS = {
    "evidence_dossier": "R3-01-package-integration-attempt6.log",
    "explainable_queue": "R3-02-inbox-integration-attempt5.log",
    "negotiation_drafts": "R3-03-resolution-draft-integration.log",
    "sale_readiness": "R3-04-case-fulfilment-package-integration-attempt4.log",
    "appeal_workspace": "R3-05-outcome-appeal-comparison-integration-attempt2.log",
    "mcp_local_contracts": "R3-06-protocol-case-detail-attempt1.log",
    "evaluation_workbench": "R3-07-package-manifest-attempt1.log",
    "judge_walkthrough": "R3-08-habitual-journey-attempt1.log",
    "local_autopilot": "R3-09-case-policy-integration-attempt3.log",
    "local_demo_hub": "R3-10-case-aware-hub-integration-attempt1.log",
}

def page_entries(case_id=None, package_id=None):
 package_url=f"/disputes/{case_id}/packages/{package_id}" if case_id and package_id else None
 case_url=f"/disputes/{case_id}" if case_id else None
 case_destinations={
  "evidence_dossier":f"{package_url}#evidence-dossier" if package_url else None,
  "explainable_queue":f"{case_url}/workspace/records#queue-explanation" if case_url else None,
  "negotiation_drafts":f"{case_url}/resolution" if case_url else None,
  "sale_readiness":f"{package_url}#sale-readiness" if package_url else None,
  "appeal_workspace":f"{case_url}/outcome" if case_url else None,
  "mcp_local_contracts":f"{case_url}/workspace/hackathon#local-mcp-notes" if case_url else None,
  "evaluation_workbench":f"{package_url}#evaluation-manifest" if package_url else None,
  "judge_walkthrough":f"/features/judge_walkthrough?case_id={case_id}" if case_id else None,
  "local_autopilot":f"{case_url}/workspace/hackathon#local-autopilot" if case_url else None,
  "local_demo_hub":f"/features/local_demo_hub?case_id={case_id}" if case_id else None,
  }
 entries=[]
 for title,slug,desc in PAGES:
  # Standalone feature pages have their own router flag; the extra dependency
  # applies only when this entry points at a case-aware route.
  required=DESTINATION_FLAGS.get(slug,(slug,)) if case_id else (slug,)
  missing=[FLAGS.get(flag,"COPILOT_FEATURE_"+flag.upper()) for flag in required
           if not config.feature_enabled(flag)]
  href=case_destinations.get(slug)
  if href is None:
   # These namespaces all register a standalone feature page; use it only when no case target applies.
   href=f"/features/{slug}"
  entries.append({"title":title,"slug":slug,"description":desc,"flag":FLAGS[slug],
                  "required_flags":[FLAGS.get(flag,"COPILOT_FEATURE_"+flag.upper()) for flag in required],
                  "missing_flags":missing,"enabled":not missing,"href":href if not missing else None})
 return entries

def evidence_index():
 rows=[]
 for title,slug,_ in PAGES:
  rel=f"copilot/features/{slug}/service.py"
  path=(ROOT/rel).resolve()
  if ROOT not in path.parents:raise ValueError('Artifact path outside checkout')
  try:sha=hashlib.sha256(path.read_bytes()).hexdigest();status='present; source file hash'
  except OSError:sha=None;status='unavailable'
  dossier=LANE_RECORDS/f"{len(rows)+1:02d}"/'DOSSIER.md'
  dossier_hash=hashlib.sha256(dossier.read_bytes()).hexdigest() if dossier.exists() else None
  acceptance=INTEGRATION_LOGS[slug]
  log_path=LANE_RECORDS/'logs'/acceptance
  code_status='focused acceptance log unavailable'
  if log_path.exists():
   log_text=log_path.read_text(encoding='utf-8')
   match=__import__('re').search(r'NATIVE_EXIT[=:]\s*(\d+)',log_text)
   if match:
    exit_code=match.group(1)
    code_status=f"focused acceptance group exit {exit_code}; see {acceptance}"
  rows.append({'claim':f'{title} local implementation and R3 case-flow connection','artifact':rel,'sha256':sha,'dossier':str(dossier.relative_to(ROOT)).replace('\\','/'),'dossier_sha256':dossier_hash,'acceptance_log':f'internal/lanes/mejoras10/logs/{acceptance}','run_status':code_status if sha else status,'version':VERSION})
 rows.extend([{'claim':'Historical CUT72 R04 52/72','artifact':'historical record','sha256':None,'run_status':'historical only at 5a12f56e136b1f7e98b1e5b9f0387191f068be1e; not rerun','version':VERSION},{'claim':'Human rehearsal Q1/Q2','artifact':'external forms','sha256':None,'run_status':'NOTRUN','version':VERSION}])
 return rows

def case_context(case_id, db_path=None):
 from copilot.db import repo
 packages=repo.list_packages(db_path or config.db_path(),case_id)
 documents=repo.list_documents(db_path or config.db_path(),case_id)
 latest=max(packages,key=lambda row:(row.get('version',0),row.get('id',0))) if packages else None
 return {'case_id':case_id,'source':'Existing Proofie local package/document records; no provider state read by this hub',
         'case_url':f'/disputes/{case_id}','latest_package_id':latest['id'] if latest else None,
         'package_url':f"/disputes/{case_id}/packages/{latest['id']}" if latest else None,
         'packages':[{'id':row['id'],'version':row['version'],'source':row['source'],'validation_ok':row['validation_ok']} for row in packages],
         'documents':[{'id':row['id'],'filename':row['filename'],'sha256':row.get('sha256'),
                       'digest_status':'recorded DB digest; hub does not reread original bytes'} for row in documents]}
