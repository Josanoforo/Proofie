def test_lazy_registration():
 import inspect
 from copilot import app
 source=inspect.getsource(app.create_app);i=source.index('feature_enabled("evaluation_workbench")');assert source.index('from copilot.features.evaluation_workbench.routes',i)>i
