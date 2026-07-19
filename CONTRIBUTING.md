# Contributing

Keep changes surgical and evidence-backed. New provider features must include request/response parsing tests without real network calls. New configuration fields must be documented in `config.schema.json`, represented in the shipped default or an example, and either enforced by runtime code or explicitly labeled informational.

Before submitting a change:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q plugins/relentless-inception
python3 -m json.tool plugins/relentless-inception/config/default.json >/dev/null
python3 -m json.tool plugins/relentless-inception/schemas/config.schema.json >/dev/null
```

Do not commit credentials, `.env` files, runtime outputs, or user overrides. Do not weaken exact-hash gates, author/reviewer separation, default retention controls, or the external-seat/workspace boundary without an explicit security review.
