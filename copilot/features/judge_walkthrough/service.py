MANIFEST={'schema':'proofie-judge-walkthrough/1','version':'local-R3','app':'Proofie local walkthrough','python':'3.12','source':'bundled synthetic dispute plus temporary Proofie SQLite package/document records','network_required':False,'env_required':False,'entrypoint':'.venv\\Scripts\\python.exe -m copilot.features.judge_walkthrough.runner','steps':[{'id':'1','action':'Start from Proofie home and open All disputes','expected':'The ordinary /disputes inbox lists the bundled synthetic case using the normal app route','remedy':'Enable COPILOT_FEATURE_JUDGE_WALKTHROUGH, COPILOT_FEATURE_EVIDENCE_DOSSIER, and COPILOT_FEATURE_EVALUATION_WORKBENCH in this process only.'},{'id':'2','action':'Open the listed case, then its saved evidence package','expected':'The existing detail and package routes retain the same case ID and saved local DB records','remedy':'Use the package link shown under the case tasks; the runner seeds one temporary package and document.'},{'id':'3','action':'Review the connected dossier and evaluation manifest','expected':'Package view shows claim/source, original byte hash, attachment state and instrument provenance with human-readable sections','remedy':'Check the package record and original byte digest; missing provenance remains UNKNOWN.'},{'id':'4','action':'Check run boundaries','expected':'Fixture stays labelled synthetic; only injected GET transport is allowed; journey reports in-process ASGI TestClient and no listening server','remedy':'Use only the runner output. No .env, PayPal, model, or external provider operation is required.'}]}
FORMS={'Q1':{'status':'NOTRUN','participant':'','date':'','notes':''},'Q2':{'status':'NOTRUN','participant':'','date':'','notes':''}}

# Explicit provenance for the synthetic walkthrough. IDs are derived from the active
# fixture/DB at runtime; no historical receipt is read or updated by this guide.
SYNTHETIC_PROVENANCE = (
    {'step': 'Purchase and capture', 'source': 'Bundled synthetic dispute transaction',
     'status': 'Synthetic fixture only; no checkout or capture was performed'},
    {'step': 'Buyer report', 'source': 'Bundled synthetic dispute',
     'status': 'Synthetic fixture only; no buyer submitted a report'},
    {'step': 'Seller case', 'source': 'Injected read-only fixture transport',
     'status': 'Local replay; no PayPal read'},
    {'step': 'Evidence package', 'source': 'Temporary Proofie database and synthetic original',
     'status': 'Local package; seller review and approval are separate human actions'},
    {'step': 'Send and outcome', 'source': 'No provider operation in this walkthrough',
     'status': 'Not performed; no provider receipt or outcome is implied'},
)


def synthetic_identity():
    """Join only the bundled synthetic order and dispute by their exact capture ID."""
    import json
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[3]
    order_path = root / 'pilot/fixtures/order_confirmation.txt'
    dispute_path = root / 'pilot/fixtures/synthetic/dispute_PP-R-DEMO-0000001_waiting_for_seller.json'
    order = order_path.read_text(encoding='utf-8')
    dispute = json.loads(dispute_path.read_text(encoding='utf-8'))['body']
    return _identity_from_records(order, dispute)


def _identity_from_records(order, dispute):
    import re
    def field(label):
        match = re.search(r'^' + re.escape(label) + r': ([A-Za-z0-9-]+)\s*$', order, re.MULTILINE)
        return match.group(1) if match else None
    capture = field('PayPal capture id')
    matches = [row for row in dispute.get('disputed_transactions', [])
               if capture and row.get('seller_transaction_id') == capture]
    if len(matches) != 1:
        return None
    return {'order_id': field('PayPal order id'), 'store_order': field('Store order number'),
            'capture_id': capture, 'dispute_id': dispute.get('dispute_id'),
            'order_source': 'pilot/fixtures/order_confirmation.txt',
            'dispute_source': 'pilot/fixtures/synthetic/dispute_PP-R-DEMO-0000001_waiting_for_seller.json'}
