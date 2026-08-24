# FireCrab MCP

A small MCP adapter for FireCrab's existing HTTP management API.

The design uses the Jenkins MCP Server plugin as the reference shape:

- expose explicit product operations rather than a generic HTTP escape hatch;
- keep MCP transport concerns separate from product tool logic;
- return native product data with a small stable envelope;
- let the underlying product API remain the source of truth.

The implementation differs from Jenkins in one important way: every FireCrab
operation flows through a typed TalkPipe Pipe API pipeline before the HTTP call.

```text
MCP tool
  -> ApiRequest
  -> TalkPipe ValidateRequest
  -> TalkPipe CallFireCrab
  -> TalkPipe NormalizeResult
  -> MCP result
```

## Tools

- `getStatus`
- `listVMs`
- `getVM`
- `createVM`
- `startVM`
- `stopVM`
- `deleteVM`
- `getVMLog`

`createVM` deliberately accepts the native FireCrab create payload as an
object. The MCP adapter does not fork or duplicate FireCrab's VM schema.

There is intentionally no `request(url, method, body)` tool.

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

## Why TalkPipe

MCP is the capability boundary. TalkPipe is the orchestration/execution spine.
Keeping those separate means the same FireCrab operations can later be
composed into CI plans without teaching Gitflare or an MCP client FireCrab's
internal REST implementation.
