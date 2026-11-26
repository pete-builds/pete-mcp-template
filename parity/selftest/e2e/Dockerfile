# End-to-end fixture for the reusable workflow, run by this repo's CI.
# The "candidate" is a real release, mcp-searxng 0.6.0, pinned by digest. CI
# passes release-version 0.6.1, so the workflow resolves 0.6.0 on GHCR as the
# baseline (paging the tag list, confirming the manifest) and must find no
# difference. A later searxng release changes neither side.
FROM ghcr.io/pete-builds/mcp-searxng:0.6.0@sha256:ba2bbdb39a0424bc4280c25a31f77169352ef0b998175680f77f8c538a2b42b0
