def test_lazy_registration():
 import inspect
 from copilot import app
 source=inspect.getsource(app.create_app);i=source.index('feature_enabled("local_autopilot")');assert source.index('from copilot.features.local_autopilot.routes',i)>i
