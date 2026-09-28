# Security

## Reporting a vulnerability

Please don't open a public issue. Report it privately through GitHub's
[security advisory form](https://github.com/jashansadioura32/dbt-sentinel/security/advisories/new)
for this repository.

Include what you found, how to reproduce it, and what an attacker could do with it.

## Scope

The parts worth attention:

- **The webhook** (`webhook.py`) verifies GitHub's HMAC-SHA256 signature over the raw
  body in constant time before doing anything else.
- **The GitHub App private key** is read from the environment only. Never commit it; see
  [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).
- **LLM output** never reaches the PR comment as Markdown: findings are schema-validated
  and rendered by template code, and fields that land in code spans reject backticks,
  pipes and newlines.
- **Secret scanning** (`security.py`) redacts every credential it reports. A report of a
  value appearing unredacted in a comment is a vulnerability.
