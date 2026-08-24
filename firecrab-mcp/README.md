# FireCrab MCP

A small MCP adapter for FireCrab's existing HTTP management API.

The design uses the Jenkins MCP Server plugin as the reference shape:

- expose explicit product operations rather than a generic HTTP escape hatch;
- keep MCP transport concerns separate from product tool logic;
- return native product data with a small stable evidence envelope;
- let the underlying product API remain the source of truth;
- make dangerous capabilities structurally absent or operator-gated.

The implementation differs from Jenkins in one important way: every FireCrab
operation flows through a typed TalkPipe Pipe API pipeline before the HTTP call.

```text
MCP tool
  -> ApiRequest
  -> TalkPipe ValidateRequest
  -> TalkPipe EnforcePolicy
  -> TalkPipe CallFireCrab
  -> TalkPipe NormalizeResult / evidence
  -> MCP result
```

## Tools

Read tools are enabled by default:

- `getStatus`
- `listVMs`
- `getVM`
- `getVMLog`

Mutation tools are registered but default-deny at the TalkPipe policy stage:

- `createVM`
- `startVM`
- `stopVM`

Enable them explicitly with:

```bash
export FIRECRAB_MCP_ALLOW_MUTATIONS=1
```

VM deletion is intentionally not exposed in this first slice. The adapter also
rejects `DELETE` requests internally, so there is no hidden generic path to it.

`createVM` deliberately accepts the native FireCrab create payload as an
object. The MCP adapter does not fork or duplicate FireCrab's VM schema; the
FireCrab API remains authoritative for validation.

There is intentionally no `request(url, method, body)` tool.

## Evidence envelope

Every successful operation preserves the native FireCrab response and adds
execution evidence suitable for logs and CI:

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
with API/server logs instead of inventing a second identity system.

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

The focused CI workflow installs the current MCP 2.x and TalkPipe 0.14.x lines
and runs the adapter suite on every MCP change.

## Why TalkPipe

MCP is the capability boundary. TalkPipe is the orchestration/execution spine.
Keeping those separate means the same FireCrab operations can later be
composed into CI plans without teaching Gitflare or an MCP client FireCrab's
internal REST implementation. The policy and evidence stages also become
natural extension points for allowlists, checkpoints, validation and rollback
without growing one giant MCP tool implementation.
