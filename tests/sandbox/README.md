# tests/sandbox/

Tests in this folder hit the real PayPal sandbox (`api-m.sandbox.paypal.com`) over the network. They are
marked `@pytest.mark.sandbox` and **excluded by default** (`pytest.ini`'s `addopts = -m "not sandbox and not live_model"`), as the
project's rules require: sandbox tests are marked and never run by default.

Run them explicitly, from the repo root, with the project venv:

```
.venv\Scripts\python.exe -m pytest -m sandbox tests/sandbox
```

Each run appends real evidence to `evidence/*.jsonl` via `SandboxClient` (sanitised, as always) and should
be recorded in the project's private evidence log by file and line, same as any other sandbox call in this project -
never described as a substitute for the offline tests in `tests/local/`.
