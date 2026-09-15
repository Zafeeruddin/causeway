# Storage setup — hosted Versity Gateway

The recordings store is `https://s3.example.com`, a Versity Gateway instance shared
across the org. It is not run by this project; this file records what this
application needs from it and what it must not be given.

## What the app needs

| Setting | Value |
|---|---|
| Endpoint | `https://s3.example.com` (internal address once available) |
| Addressing style | **path** (`https://s3.example.com/<bucket>/<key>`) |
| Region | `us-east-1` |
| Bucket | `cam-recordings` |
| Signature | SigV4 |

Path-style is not optional. Versity does not serve `<bucket>.s3.example.com`, and
boto3's default virtual-host addressing produces a DNS failure that reads like a
network outage rather than a configuration mistake. `S3_ADDRESSING_STYLE=path`
in `.env` is what pins it.

## Create the bucket and a scoped account

`storage-root` is a **root credential for an org-wide store**. It works, and
the app will run with it, but it means a bug in our retention sweep can reach
every other bucket on the gateway. Give this app its own account instead:

```bash
# Run against the gateway host, as an operator with admin rights.
versitygw admin create-user \
    --access cam-dashboard \
    --secret "$(openssl rand -base64 32)" \
    --role user

# Versity scopes access by bucket ownership: the owner of a bucket is the
# account that may use it.
versitygw admin change-bucket-owner \
    --bucket cam-recordings \
    --owner cam-dashboard
```

Then put `cam-dashboard` and its secret in `.env` as `AWS_ACCESS_KEY_ID` /
`AWS_SECRET_ACCESS_KEY` and keep the root credential out of the deployment
entirely.

Verify from the app host:

```bash
aws --endpoint-url https://s3.example.com s3 ls s3://cam-recordings/
```

## Reaching it from the app

Versity is on our own network, not the public internet, so the internal address
can be dropped straight into `S3_ENDPOINT_URL` when it is ready. Nothing else
changes.

It still is not reached from inside a VPN namespace. `CONTROL_CIDRS` could carry
a route there, but uploads deliberately run outside every namespace: an upload
started inside one dies with the tunnel and with the namespace, and a customer
VPN advertising `10.0.0.0/8` collides with a storage host at `10.x.x.x` in a way
that looks like a storage outage rather than a routing one. Recorders write
segments to the shared work volume; a shipper outside the namespaces uploads.

If the gateway stops answering, the dashboard says so before anything else
loads. `/api/health` asks it with a HeadBucket -- one attempt, five seconds at
most, and one answer shared by every caller for fifteen seconds -- and an
unavailable answer puts a "service unavailable" screen in front of sign-in and
the dashboard, naming the endpoint host so whoever sees it can pass that on.
The screen checks again on its own and clears when the gateway is back. The
public answer is only up or down and the host; the reason, including a missing
bucket or a refused credential, is in the API log as
`storage.health.unavailable`. `STORAGE_HEALTH_CHECK=false` turns it off.

## What Versity does not give us

Two server-side safety nets that MinIO and AWS both have are absent here, and
the application compensates for both. This matters if anyone later assumes they
exist.

**No lifecycle rules.** Nothing on the gateway will ever expire an object. All
deletion is done by the retention sweep in `app/storage/retention.py`. If that
job stops running, storage grows until the admission check refuses new
recordings — and then stays there until someone intervenes.

**No bucket quotas.** There is no server-side ceiling. The admission check that
runs before each recording starts is the only thing between a runaway job and
the org's storage, which is why it estimates generously and refuses up front
rather than discovering the limit mid-write.

Ask the gateway operators whether a filesystem-level quota can be applied to the
bucket's backing directory. That would restore the outer safety net without any
change on our side, and it is the single cheapest piece of protection available.

## Thresholds

Set in `.env`, enforced in `app/storage/retention.py`:

| Threshold | Bytes | Behaviour |
|---|---|---|
| Warn | 60 GB | Oldest recordings flagged "may be deleted in 7 days". Nothing removed. |
| Collect | 90 GB | Oldest-first sweep, down to the warn line. |
| Hard | 98 GB | New recordings refused. |

The sweep deliberately runs down to the *warn* line rather than just back under
the collect line — otherwise it would trigger again on the next recording, and
the one after that.
