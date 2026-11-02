def test_feature_registered_lazily():
    import inspect
    from copilot import app
    source=inspect.getsource(app.create_app)
    block=source.index('feature_enabled("evidence_dossier")')
    assert source.index('from copilot.features.evidence_dossier.routes',block) > block
