# Shanghai runtime timezone default

The production six-service Compose, developer Compose, Python runtime image and native worker systemd examples now default the process timezone to `Asia/Shanghai`. The Python image explicitly installs zoneinfo data. Existing `.env`, credentials, mounts, images, schema and data are not rewritten by this source change; deployment requires a separately frozen plan and controlled restart.

The PostgreSQL container process and server log display use Shanghai, while `postgres -c timezone=UTC` explicitly preserves SQL/session interpretation for new and existing data volumes. This is intentional: changing an OS display timezone must not reinterpret timestamp inputs. Application `DateTime(timezone=True)`, UTC audit fields, aware UTC lease/deadline calculations, epoch values and historical rows remain unchanged. No database migration or batch timestamp conversion is included.

After deployment, verify each process `TZ`, readable `/usr/share/zoneinfo/Asia/Shanghai`, `date +%z` = `+0800`, the same epoch as UTC, and PostgreSQL `SHOW timezone` = `UTC` / `SHOW log_timezone` = `Asia/Shanghai`. Verify role/database session overrides separately; do not change them blindly. A host `timedatectl` change is a separate production operation and does not establish container timezone proof. No authorization expiry is extended.

Targeted checks cover all six services, explicit database clock separation and every mapped datetime column. Existing deployment tests and CI cover rendered Compose and image behavior. Source checks are not a claim of production deployment or database observation.


## Existing HTTPS project

The separate protected `/opt/cf-agent-gateway-https/compose.yaml` is site-created, not a tracked generic deployment template. Its observed service key is `https`. `deploy/https/compose.timezone.yaml` is a minimal merge input containing only that service's timezone. Include it when preparing a new HTTPS project, or merge this one environment field into the existing independently frozen Compose plan. Do not replace site TLS configuration, image, ports or mounts with a generic file. Existing return-site management expects its original single Compose path; persist the merged result there through the reviewed deployment transaction instead of silently changing its command-line file list.

Read-only observation by the parent executor at 2026-10-18 11:21:50 UTC: the CFserver host already uses Asia/Shanghai. Current running Gateway/Dispatch, PostgreSQL, FileBrowser, WeChat and HTTPS containers use UTC without a TZ environment entry; all have a readable Shanghai zonefile with SHA256 `64ffc2e43a94435a043c040d1d3af7e92d031adc78e7737af1861baa4eeef3e6`. PostgreSQL timezone and log_timezone were both Etc/UTC. The actual existing database Compose is `/opt/cf-agent-gateway/deploy/compose.yaml`, distinct from the future clean-install `deploy/postgres/compose.yml`; never replace it with the new-install template. No live change is implied by this source commit. The original private audit is retained locally; the paths and safe observations here are the shared subset.

Local targeted result: 39 tests passed (timezone/deployment/logging/runtime heartbeat). The first attempt used the system Python without cryptography and stopped during import before tests; the rerun reused the existing isolated dependency-complete environment. No full historical matrix was rerun locally.
