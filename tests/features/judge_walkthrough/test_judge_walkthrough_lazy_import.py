def test_lazy_registration():
 import inspect
 from copilot import app
 source=inspect.getsource(app.create_app);i=source.index('feature_enabled("judge_walkthrough")');assert source.index('from copilot.features.judge_walkthrough.routes',i)>i
