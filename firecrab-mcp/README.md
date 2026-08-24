# FireCrab MCP

A small MCP adapter for FireCrab's existing HTTP management API, with a
Jenkins-shaped build lifecycle layered on FireCrab's native VM and Shell
repository primitives.

The design uses the Jenkins MCP Server plugin as the reference shape:

- expose explicit product operations rather than a generic HTTP escape hatch;
- keep MCP transport concerns separate from product tool logic;
- publish read-only, mutating, and destructive tool semantics;
- return native product data with a small stable evidence envelope;
- let the underlying product API remain the source of truth;
- make dangerous capabilities structurally absent or operator-gated.

The implementation differs from Jenkins in one important way: every FireCrab
API operation flows through a typed TalkPipe Pipe API pipeline before the HTTP
call.

```text
MCP tool
  -> typed operation
  -> TalkPipe ValidateRequest
  -> TalkPipe EnforcePolicy
  -> TalkPipe CallFireCrab
  -> TalkPipe NormalizeResult / evidence
  -> FireCrab native API
```

## VM tools

Read tools are enabled by default:

- `getStatus`
- `listVMs`
- `getVM`
- `getVMLog`

Mutation tools are registered but default-deny at the TalkPipe policy stage:

- `createVM`
- `startVM`
- `stopVM`

Enable mutations explicitly with:

```bash
export FIRECRAB_MCP_ALLOW_MUTATIONS=1
```

VM deletion is intentionally not exposed. The adapter also rejects `DELETE`
requests internally, so there is no hidden generic path to it.

`createVM` deliberately accepts the native FireCrab create payload as an
object. The MCP adapter does not fork or duplicate FireCrab's VM schema; the
FireCrab API remains authoritative for validation.

There is intentionally no `request(url, method, body)` tool.

## Jenkins-shaped build tools

The build layer exposes four higher-level operations:

- `triggerBuild(label, command)` — create and start a build;
- `getBuild(buildId)` — read lifecycle and conclusion facts;
- `getBuildLog(buildId)` — read command output;
- `stopBuild(buildId)` — stop a build VM.

`triggerBuild` is a mutating tool. `getBuild` and `getBuildLog` are read-only
and idempotent. `stopBuild` is explicitly marked destructive because it
terminates live execution.

A build uses FireCrab's existing Shell repository instead of inventing SSH or a
guest agent:

```text
triggerBuild
  -> create versioned FireCrab Shell containing the command
  -> create fresh VM with that Shell revision pinned
  -> start VM
  -> FireCrab injects Shell into guest rootfs
  -> guest reaches network-ready
  -> FireCrab Shell runner executes command
  -> stdout/stderr + FIRECRAB_SHELL_* markers reach serial console

getBuild / getBuildLog
  -> read VM state + native serial log
  -> parse final Shell runner markers
  -> return Jenkins-like phase / conclusion / exit code / console evidence
```

The build command itself exports `CI=true`. Runner placement and capacity are
operator configuration rather than MCP arguments, analogous to Jenkins-owned
agent configuration:

```bash
export FIRECRAB_MCP_BUILD_TEMPLATE=ubuntu-26.04
export FIRECRAB_MCP_BUILD_NETWORK_ID=00000000-0000-0000-0000-000000000000

# Optional defaults shown:
export FIRECRAB_MCP_BUILD_CPU=2
export FIRECRAB_MCP_BUILD_RAM=2048
export FIRECRAB_MCP_BUILD_DISK_GB=20
export FIRECRAB_MCP_BUILD_EGRESS_POLICY=internet
# export FIRECRAB_MCP_BUILD_STORAGE_ROOT=fast
```

Both `FIRECRAB_MCP_ALLOW_MUTATIONS=1` and a valid build runner profile are
required before `triggerBuild` can create infrastructure.

Build VMs use a unique `ci-` name and pin a FireCrab Shell with the same build
name. Build-specific stop/read operations require both facts before treating a
VM as a build, so an ordinary VM cannot opt into build mutation merely by using
the prefix. The first build slice intentionally retains the VM and Shell
revision as evidence; garbage collection and retention policy are follow-on
work rather than silently adding delete authority to this MCP.

### Current build boundary

This slice executes a supplied command inside a FireCrab guest. It does **not**
yet perform source checkout. The next integration boundary is an authenticated
CI/Gitflare handoff that supplies a revision and short-lived source credential
without exposing FireCrab MCP remotely before an authentication boundary exists.

The `FIRECRAB_SHELL_*` serial protocol is reliable for normal trusted CI jobs
because FireCrab's runner writes its own terminal markers after the script
returns. It is not claimed as hostile multi-tenant attestation: a deliberately
malicious guest can write to its own console or leave background processes.
Strong adversarial result attestation would require a separate trusted channel.

## Evidence envelope

Every low-level successful operation preserves the native FireCrab response and
adds execution evidence suitable for logs and CI:

```json
{
  "operation": {
    "method": "GET",
    "path": "/api/vms",
    "risk": "read"
  },
  "statusCode": 200,
  "requestId": "...",
  "durationMs": 3.427,
  "data": []
}
```

`requestId` is FireCrab's own `x-request-id`, so MCP activity can be correlated
with API/server logs instead of inventing a second identity system. Build-level
results preserve the relevant FireCrab request IDs as lineage evidence.

## Run

Python 3.11+ is required by TalkPipe.

```bash
cd firecrab-mcp
python -m venv .venv
. .venv/bin/activate
pip install -e .
firecrab-mcp
```

The default transport is stdio and the default FireCrab API is
`http://127.0.0.1:5523`.

Configure a different FireCrab API endpoint with:

```bash
export FIRECRAB_API_URL=http://127.0.0.1:5523
```

The configured API URL must be an absolute HTTP(S) origin without embedded
credentials, a path, query string, or fragment. It is operator configuration,
not MCP tool input.

For local Streamable HTTP:

```bash
firecrab-mcp --transport streamable-http --host 127.0.0.1 --port 8000
```

The HTTP MCP transport refuses non-loopback binds in this MVP. Jenkins can
reuse Jenkins authentication; this adapter does not yet have an equivalent
authenticated MCP boundary, so remote exposure is intentionally blocked.

## Test

```bash
pip install -e '.[dev]'
pytest
```

The focused CI workflow installs the MCP 2.x and TalkPipe 0.14.x lines and runs
the adapter suite on every MCP change. Tests inspect the published MCP catalog
itself so the tool set, safety annotations, and structured output schemas cannot
silently regress.

## Why TalkPipe

MCP is the capability boundary. TalkPipe is the orchestration/execution spine.
Keeping those separate means the same FireCrab operations can later be
composed into CI plans without teaching Gitflare or an MCP client FireCrab's
internal REST implementation. The policy and evidence stages also become
natural extension points for allowlists, checkpoints, validation and rollback
without growing one giant MCP tool implementation.
