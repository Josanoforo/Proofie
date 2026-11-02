def test_lazy_registration():
 import inspect
 from copilot import app
 source=inspect.getsource(app.create_app);i=source.index('feature_enabled("explainable_queue")');assert source.index('from copilot.features.explainable_queue.routes',i)>i
