# BentoML service template

A starting point for serving framework models with BentoML and letting the framework deploy to
it (`DEPLOYMENT_BACKEND=bentoml`).

```sh
cd templates/bentoml-service
bentoml serve service:OranModels --port 3000
# framework side:
#   DEPLOYMENT_BACKEND=bentoml
#   BENTOML_URL=http://<host>:3000        (the adapter appends /oran)
#   BENTOML_TOKEN=<secret>                (optional; the service reads ORAN_DEPLOY_TOKEN)
```

The service implements the deployment contract in `docs/adapters/deployment.md` under `/oran`.
It loads each version in the background and reports only what it has actually loaded, so the
framework's read-back is real. Replace `load_model` and `predict` with your model's code.

**Unverified:** this template has not been run against a real BentoML install. The contract
it implements is tested against a stdlib stub (`tests/unit/serving_stub.py`), and the `bentoml`
adapter passes the conformance suite against that stub.
