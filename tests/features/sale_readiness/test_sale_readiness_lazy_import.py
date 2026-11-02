def test_lazy_registration():
 import inspect
 from copilot import app
 source=inspect.getsource(app.create_app);i=source.index('feature_enabled("sale_readiness")');assert source.index('from copilot.features.sale_readiness.routes',i)>i
