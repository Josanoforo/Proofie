def test_lazy_registration():
 import inspect
 from copilot import app
 source=inspect.getsource(app.create_app);i=source.index('feature_enabled("appeal_workspace")');assert source.index('from copilot.features.appeal_workspace.routes',i)>i
