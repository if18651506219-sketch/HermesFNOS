# port-patches (runtime src patches)

Payload src patches applied to the official kernel tree after extracting app.tgz
(patch -p1 inside the payload root, before stamp_version.py / fnpack):

- adapter-create-retry.patch — gateway/run_adapters.py: adapter creation failed -> retry once in 3s
- feishu-video-shrink.patch — plugins/platforms/feishu/adapter.py: >30MB videos re-encoded to ~26MB before upload

sock_proxy keepalive and cmd/main watchdog seed are tracked directly in repo files.
verify.py L0 asserts all four fingerprints.
