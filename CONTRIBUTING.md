# Contributing to Forcelet

Thanks for your interest in contributing! Forcelet is a metadata-driven,
Salesforce-style CRM platform built by Suresh Itha.

## Local setup

```bash
git clone https://github.com/sureshbujji/forcelet.git
cd forcelet
pip install -r requirements.txt
python run.py
```

Open http://localhost:5000 — demo password for all users is `forcelet`
(change it after first login).

## Running tests

```bash
python -m pytest tests/ -q
```

All tests must pass before a PR is merged. If you add a feature, add tests
under `tests/` following the existing per-batch naming (e.g.
`tests/test_automotive.py`).

## Branch / PR conventions

- Branch from `main`: `feature/<short-name>` or `fix/<short-name>`.
- One logical change per PR; keep PRs small enough to review.
- PRs run the CI workflow (pytest on Python 3.12) — green CI is required.
- New standard objects, automation, or seed data go in `metadata/` as JSON,
  following the existing schema; wire any demo data through
  `forcelet/bootstrap.py`.

## Code style

- Follow the existing patterns in the codebase (module layout, docstrings,
  JSON metadata shape).
- Keep the demo-grade spirit: simple, readable, and explicit over clever.
- Never commit secrets, `.db` files, or `.forcelet.key` (all gitignored).
- Update `README.md` and `CHANGELOG.md` when behavior changes.
