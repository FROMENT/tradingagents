# `deploy/` — intraday deployment socle (P3 scaffold)

Infrastructure to run the intraday stack headless on an Oracle Cloud **Ampere A1
(ARM64)** Always-Free host, provisioned with Terraform and deployed via GitHub
Actions, with **no plaintext secret in git**.

> ⚠️ **Scaffold, not a trading system.** The runner (`runner/heartbeat.py`)
> connects to IBKR (paper), reconciles positions/open orders against the broker,
> and logs a heartbeat. It **places no orders**. The order path (signal → sizer →
> interactive blocker → broker, with bracket/OCO protection resting at the
> broker) is added in later phases. See [`../docs/intraday_architecture.md`](../docs/intraday_architecture.md).
>
> This is technical/operational tooling, **not** investment, tax, or legal advice.

## What's here

| Path | Role |
|---|---|
| `Dockerfile.headless` | ARM64 app image; entrypoint is the scaffold runner |
| `docker-compose.prod.yml` | `ib-gateway` + `app`, no tty, secrets from tmpfs |
| `runner/heartbeat.py` | Connectivity + reconciliation + heartbeat (no orders) |
| `secrets/` | SOPS/age config, plaintext template — only `*.enc.env` is committed |
| `scripts/decrypt-secrets.sh` | SOPS → `/run/tradingagents/secrets.env` (tmpfs) |
| `systemd/tradingagents-intraday.service` | decrypt → compose up → shred on stop |
| `terraform/` | OCI VM (A1), VCN, SSH-only ingress, cloud-init |
| `../.github/workflows/deploy-intraday.yml` | build on self-hosted ARM runner → restart |

## Security model

- **CI deploys code; the host fetches secrets.** GitHub never sees a broker key.
- Secrets live SOPS-encrypted in git (`secrets/secrets.enc.env`) and are decrypted
  only at runtime into `/run/tradingagents/secrets.env` (tmpfs — never on disk),
  then shredded on stop. The scheme reduces to **one** bootstrap secret: the age
  private key at `/etc/tradingagents/age.key`.
- **No inbound trading port.** Only SSH (from your admin CIDR) is open; the app
  reaches IB Gateway over the internal Docker network; all broker/market/LLM
  traffic is outbound.
- **Self-hosted runner ⇒ private repo only.** Never attach one to a public repo.
- **IBKR credentials are full-account** (not a revocable key): a leak = full
  compromise. Rotate on any exposure.

## Fail-safe (mandatory before any real order, later phases)

The host has no SLA and can be reclaimed. Software guards die with it — so every
position must carry **bracket/OCO protection resting at IBKR**, and the runner
always reconciles from the broker on start. The scaffold already reconnects,
reconciles, and honours a kill switch (`~/.tradingagents/KILL`).

## Bring-up (once the repo is PRIVATE)

1. `cd deploy/terraform && cp terraform.tfvars.example terraform.tfvars` → fill in → `terraform init && terraform apply`.
2. `age-keygen -o age.key`; put the **public** key in `secrets/.sops.yaml`; copy the **private** key to the host at `/etc/tradingagents/age.key` (0600).
3. `cp secrets/secrets.env.example secrets/secrets.plain.env` → fill in → `sops --encrypt secrets/secrets.plain.env > secrets/secrets.enc.env` → commit `secrets.enc.env`, delete the plaintext.
4. Register a **self-hosted ARM64 runner** on the private repo (labels `self-hosted, ARM64`); grant its user passwordless sudo for `rsync`/`systemctl`.
5. Push → the workflow builds natively on ARM and `systemctl restart`s the stack.

## Ports caveat

`gnzsnz/ib-gateway` exposes the API inside the Docker network on **4003 (live) /
4004 (paper)**. `IB_GATEWAY_PORT` defaults to `4004`. Pin a specific arm64 image
tag and confirm its ports before going further.
