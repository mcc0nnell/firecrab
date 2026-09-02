# Tailcat transport

FireCrab can expose one MCP stdio session through an ephemeral Tailcat
capability without binding an MCP HTTP listener or changing host/guest routes.

```text
Shell / MCP client
  -> tailcat <capability>
  -> WireGuard-encrypted Tailcat byte pipe
  -> firecrab-mcp-tailcat
  -> FireCrab MCP stdio child
  -> TalkPipe policy / evidence pipeline
  -> FireCrab API
```

This is a transport adapter only. It does not widen the FireCrab MCP tool
surface and it does not bypass `FIRECRAB_MCP_ALLOW_MUTATIONS`. The existing
TalkPipe policy remains authoritative for side effects.

## Why stdio instead of remote HTTP

Tailcat's pipe mode already provides the primitive MCP needs: a bidirectional
ordered byte stream. Reusing it directly means this path needs no:

- non-loopback MCP bind;
- public TCP listener;
- SOCKS proxy;
- host route or DNS change;
- FireCrab micro-network change;
- guest image package;
- Tailcat Go API embedded in the Rust or Python runtime.

One launcher invocation creates one Tailcat server and one MCP stdio child.
Tailcat pipe mode accepts one session. When that session ends, the launcher
tears down the MCP child and the capability dies with the fresh server key.
The launcher also enforces a bounded capability lifetime even if nobody ever
connects.

## Tailcat version contract

The first adapter pins the Tailcat **v0.2.x CLI contract**. Tailcat explicitly
does not promise CLI/library/wire stability yet, so FireCrab fails closed on a
different Tailcat version rather than guessing at changed behavior.

The launcher always supplies:

```text
--key=new
--full-address
```

`--key=new` is important: Tailcat can otherwise silently reuse a saved
`default` server key if one exists. FireCrab never permits that for this
transport. `--full-address` makes the ephemeral capability self-contained so
the client does not need a separate DERP-map lookup to interpret it.

## Start a capability

Install `firecrab-mcp` and Tailcat v0.2.x on the FireCrab operator host, then:

```bash
firecrab-mcp-tailcat
```

For an interactive terminal, the launcher prints the capability once to the
terminal. It suppresses Tailcat's own startup output so the token is not
accidentally duplicated in ordinary logs.

For automation, use a private capability file instead:

```bash
mkdir -m 700 -p "$HOME/.local/run/firecrab"
firecrab-mcp-tailcat \
  --address-file "$HOME/.local/run/firecrab/mcp.tailcat"
```

The destination must not already exist. FireCrab creates it mode `0600` and
removes it when the Tailcat/MCP session ends. A non-interactive launch without
an address file is rejected rather than writing a bearer capability into logs.

By default the capability is valid for at most ten minutes from publication,
including time spent waiting for the first client. The allowed range is one
minute to one hour:

```bash
firecrab-mcp-tailcat --session-ttl 300
```

When the TTL expires, the launcher terminates both Tailcat and the MCP child,
removes the public address file if one was created, and exits with status 124.
This makes an abandoned or leaked-but-unused token self-revoking at the process
boundary instead of depending on an operator to remember cleanup.

Equivalent environment variables are available:

```bash
export FIRECRAB_MCP_TAILCAT_BIN=tailcat
export FIRECRAB_MCP_TAILCAT_ADDRESS_FILE="$HOME/.local/run/firecrab/mcp.tailcat"
export FIRECRAB_MCP_TAILCAT_STARTUP_TIMEOUT=15
export FIRECRAB_MCP_TAILCAT_SESSION_TTL=600
```

## Connect from an MCP client

The remote side can use Tailcat itself as the stdio transport command:

```text
command: tailcat
args: [<capability>]
```

Conceptually, instead of an MCP client spawning `firecrab-mcp` locally, it
spawns `tailcat <capability>`. Tailcat's stdin/stdout are the MCP byte stream;
the FireCrab-side launcher cross-connects them to the real MCP stdio child.

The capability is case-sensitive and secret. On a multi-user client machine,
remember that passing a bearer token as a process argument may expose it to
local process inspection. Client identity pinning below removes token-only
access and is the preferred mode for durable operator devices.

## Pin a client identity

Tailcat v0.2.x can authenticate the connecting client by its Tailcat node public
key. On the operator client, create a client key:

```bash
tailcat genkey --client --key=client-default
```

Take the printed `nodekey:...` public key and configure the FireCrab side:

```bash
firecrab-mcp-tailcat \
  --allow-client 'nodekey:...' \
  --address-file "$HOME/.local/run/firecrab/mcp.tailcat"
```

or:

```bash
export FIRECRAB_MCP_TAILCAT_ALLOW_CLIENT='nodekey:...'
```

With this set, possession of the connection token alone is insufficient; the
Tailcat client must also present the allowed client identity. Tailcat's normal
`client-default` behavior makes this practical for a trusted Shell device.

## Mutation policy still applies

Tailcat reachability does not grant FireCrab mutation authority. Reads remain
available under the normal MCP policy. VM lifecycle and build mutation tools
still require the existing explicit opt-in:

```bash
export FIRECRAB_MCP_ALLOW_MUTATIONS=1
```

That separation is intentional:

```text
Tailcat token / client key = may reach this MCP session
TalkPipe policy            = may perform this class of operation
FireCrab API                = authoritative product validation
```

## Capability handling

Treat the Tailcat connection token like a short-lived credential:

- do not commit it;
- do not put it in CI logs;
- do not publish it in an issue or PR;
- prefer `--address-file` for automation;
- prefer `--allow-client` for trusted long-lived operator devices;
- keep the TTL as short as the operation reasonably permits;
- destroy the session when the work is complete.

The launcher never places the token in the FireCrab MCP child environment or
arguments. It receives Tailcat's token through a private temporary file, then
deletes that internal copy after publishing the capability to the selected
operator channel.

## Deliberate first-slice limits

This first slice does not:

- expose arbitrary FireCrab guest ports;
- act as an exit node;
- use Tailcat's SOCKS mode;
- create a persistent Tailcat server identity;
- embed Tailcat's unstable Go API;
- add browser/WASM transport code.

The browser path can come later without changing the MCP capability model: a
Tailcat/WASM client only needs to present the same ordered byte stream to an MCP
client. Today Tailcat's browser implementation is experimental and DERP-only;
WebRTC/direct browser paths can be adopted independently when that layer is
ready.
