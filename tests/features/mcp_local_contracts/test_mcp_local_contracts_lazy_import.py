def test_lazy_registration():
 import inspect
 from copilot import app
 source=inspect.getsource(app.create_app);i=source.index('feature_enabled("mcp_local_contracts")');assert source.index('from copilot.features.mcp_local_contracts.routes',i)>i
