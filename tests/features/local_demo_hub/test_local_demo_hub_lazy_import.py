def test_lazy_registration():
 import inspect
 from copilot import app
 source=inspect.getsource(app.create_app);i=source.index('feature_enabled("local_demo_hub")');assert source.index('from copilot.features.local_demo_hub.routes',i)>i
