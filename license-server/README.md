# Licence proxy

The service that lets you distribute the desktop app without giving anyone your
Google credentials or your prompts.

It is optional. To run the agent yourself with your own API key you do not need
any of this; see the [root README](../README.md).

> `prompts.yaml` is published here as the working reference set, and the agent
> reads a copy of it when calling a provider directly. If you run this service
> for paying customers and want the templates to stay private, as the design
> below assumes, replace them with your own before deploying.

Customers run a compiled binary that holds one thing: a licence key. Every model
call goes through this service, which authenticates the key, books it against a
quota, substitutes the customer's variables into templates **it** holds, and
calls Vertex AI as **its own** service account. A customer machine therefore has
no Google credentials to steal and no prompt templates to copy.

```
Desktop app                    Cloud Run (this service)          your-vertex-project
  licence key   ──Bearer──▶  authenticate → quota → prompt  ──key──▶  Vertex AI
  {task, variables}          (prompts.yaml lives HERE)       ──key──▶  gs://your-bucket-name
```

## Where things live, and why

| | Project | Paid/owned by |
|---|---|---|
| Cloud Run, Cloud Build, Artifact Registry | `your-host-project` | the host account |
| Vertex AI (Gemini) calls | `your-vertex-project` | the billing account |
| All persisted data (`gs://your-bucket-name`) | `your-vertex-project` | the billing account |

The host project runs the container and holds **no customer data at all** — no
bucket, no database. Both Vertex AI and the bucket are reached with a mounted
service-account key for `your-vertex-project`, so model spend and stored data both
land there regardless of where the service happens to run.

This is enforced, not merely intended:

- There is **no Application Default Credentials path** in the code. If the key
  is not mounted, the service returns 503 rather than silently falling back to
  its own identity — which would be the host project.
- The project is read **from the key file itself**, not from an environment
  variable, so no config change can redirect spend or storage.
- The host project is on a permanent **forbidden list**; a key belonging to it
  is refused at startup.
- The runtime service account holds exactly one role,
  `secretmanager.secretAccessor` — no `aiplatform`, no `storage`, no
  `datastore`. Even if the code tried, Google would refuse.

`GET /healthz` reports the billing project and bucket so this can be verified
from outside the service.

## Deploy

Needs a principal with Owner on the project. The default compute service account
is **not** enough — it cannot enable APIs or grant roles.

`deploy.sh` ships with placeholder names. Set your own first:

```bash
export PROJECT_ID=your-host-project            # runs the container
export VERTEX_PROJECT_ID=your-vertex-project   # pays for the model calls
export STORAGE_BUCKET=your-bucket-name         # holds licences, usage and resumes

gcloud auth login              # the account that owns your-vertex-project
cd license-server
./deploy.sh
```

The script enables the APIs, creates a dedicated runtime service account with
only `secretmanager.secretAccessor`, asserts that identity has no `aiplatform`
role, and deploys. It prints the service URL. It is safe to re-run.

Then bake that URL into the app build:

```bash
cd ../job-agent
JOB_AGENT_API_URL=https://job-agent-proxy-xxxxx.a.run.app python build.py
```

## Licence keys

Minting is a local CLI, not an HTTP endpoint — there is deliberately no admin
surface on the public internet.

```bash
gcloud auth application-default login
./venv/bin/python admin.py mint --name "Acme Ltd" --email ops@acme.com
```

```
  KEY: JAA-4K2WP-9XQ7M-B3NHT-Z8RVD
```

**Shown once.** Only a SHA-256 of the key is stored, so a dump of the bucket
yields no working licences and a lost key must be reissued, not recovered.

Objects are laid out as:

```
gs://your-bucket-name/licences/<key_hash>.json           one record per licence
gs://your-bucket-name/usage/<key_hash>/<YYYY-MM-DD>.json that day's call count
gs://your-bucket-name/usage/<key_hash>/total.json        lifetime call count
```

GCS has no transactions, so quota is booked with a compare-and-swap loop on the
object's generation number. Verified: 20 parallel calls against a quota of 10
grant exactly 10.

| Command | Does |
|---|---|
| `admin.py mint --name X` | New key. `--days 365 --daily-quota 400 --devices 2` |
| `admin.py list` | Every licence with today's usage and status |
| `admin.py show <key>` | Full detail for one |
| `admin.py revoke <key> --reason "..."` | Stops working on the next request; the reason is shown to the customer |
| `admin.py reset-devices <key>` | Lets a customer move machines |
| `admin.py set <key> --days 30` | Extend or change quotas |

## Costs and controls

You pay for every call your customers make, so quota is the billing control, not
a nicety:

- `--daily-quota` (default 400) caps calls per key per day. A working day of job
  matching runs 100–250.
- `--total-quota` caps the licence's lifetime, for a trial.
- `--devices` (default 2) limits seat sharing. Soft — it stops a key being
  passed around an office, nothing more.
- `MAX_PROMPT_CHARS` (default 24000) stops a key being used to push large
  documents through your billing account.

Watch spend in Cloud Billing, and set a budget alert on the project.

## What a customer can and cannot see

Can: the variables their own app sent, and the model's answer.

Cannot: the prompt templates, your GCP project, the service account, or any
other customer's data. There is no passthrough endpoint that forwards a
caller-supplied prompt — adding one would turn a licence key into free
general-purpose model access billed to you, and would put the templates back on
the client.

## Adding a task

1. Add a section to `prompts.yaml` with `system` and `user_template`.
2. Register it in `TASKS` in `main.py` with its required variables.
3. Call it from the client with `llm.run_task_json("your_task", {...})`.

Both the system and user templates get the same variable substitution, so a
placeholder works in either.
