# Private-network synchronization

Silo uses one codebase and keeps every instance self-contained. `standalone` never connects to another machine. `server` stores the authoritative event order and complete local data. `client` stores complete local data, queues offline changes, and synchronizes while `serve.py` is running.

## Start a server

Copy the complete populated Skill directory to the server, then run:

```bash
python3 scripts/serve.py --mode server --host 0.0.0.0 --port 8767
```

The first run creates `data/sync.json`, a device ID, a shared token, and baseline events for existing assets, tags, discovery sources, and interest rules. The token is printed to the server terminal. Keep it private. Ordinary browser and asset APIs remain limited to loopback requests; only authenticated `/api/sync/*` routes accept LAN requests.

## Start a client

On each client, use a fresh Silo copy or an existing local library and run once:

```bash
SILO_SYNC_TOKEN='token-from-server' python3 scripts/serve.py \
  --mode client --sync-url http://192.168.1.10:8767 --port 8767
```

Later starts read the saved role, URL, token, device ID, and 30-second interval from `data/sync.json`. The first client sync applies the complete server baseline. Records that existed only on the client are then queued for upload. For the same entity, the event received later by the server wins.

Use `asset_db.py sync-status` for machine-readable state and `sync-now` for an immediate foreground run. `sync-disable` returns the instance to standalone mode without deleting assets. `sync-config` accepts a JSON object with `mode`, optional `server_url`, optional `token`, and optional `interval_seconds` between 5 and 3600.

## Data behavior

Assets, archive status, tags, aliases, reports, attachments, rich-text embeds, discovery source definitions, and interest rules synchronize. Discovery candidates, feed cache metadata, FTS indexes, ports, and browser state remain local. Attachments are verified by SHA-256 and keep the configured 20 MiB per-file limit.

Synchronization records structured snapshots, never executable SQL. Operation IDs are idempotent, the server assigns a monotonic sequence, and clients retain a pull cursor. A client service pushes queued local work before each normal pull. Network or authentication failures leave local operations queued.

Personal-data exports include sync settings and logs. Restore pauses synchronization. Clearing the library disables synchronization. The empty Skill package contains no token, device identity, logs, or personal content.
