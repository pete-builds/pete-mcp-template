# End-to-end fixture for the reusable workflow, run by this repo's CI.
# The "candidate" is the current release of a real server, so the reusable
# workflow resolves that same release on GHCR as its baseline (paging the tag
# list, confirming the manifest) and must find no difference. `latest` moves
# with each release, which keeps this a release-against-itself comparison.
FROM ghcr.io/pete-builds/mcp-searxng:latest
