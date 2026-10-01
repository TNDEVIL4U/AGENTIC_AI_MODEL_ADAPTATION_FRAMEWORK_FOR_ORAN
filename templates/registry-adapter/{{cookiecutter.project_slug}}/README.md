# {{ cookiecutter.project_name }}

A model registry adapter for oran-adapt, selected with `REGISTRY_BACKEND={{ cookiecutter.adapter_name }}`.

## Configuration

| Key | Type | Default | Required |
|---|---|---|---|
| `{{ cookiecutter.env_prefix }}ROOT` | path | none | yes |

Building the adapter at startup fails with a `ConfigurationError` that names any missing key.

## Develop

```sh
pip install -e ".[test]"   # into the virtual environment that holds oran-adapt
pytest                      # the oran-adapt registry conformance suite
```

As generated, `registry.py` is a working single-writer registry over a local directory. Port it
to your backend by replacing `_load`, `_save`, `_put_artifact` and `_get_artifact`, and add the
backend SDK to `dependencies` in `pyproject.toml`. Import the SDK only in `registry.py`: the
package `__init__` is loaded every time oran-adapt lists its adapters. The rules the
conformance suite enforces are in oran-adapt's `docs/adapters/registry.md`.
