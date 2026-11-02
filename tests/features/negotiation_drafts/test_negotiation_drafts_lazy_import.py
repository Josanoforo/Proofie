def test_lazy_registration():
 import inspect
 from copilot import app
 source=inspect.getsource(app.create_app);i=source.index('feature_enabled("negotiation_drafts")');assert source.index('from copilot.features.negotiation_drafts.routes',i)>i
