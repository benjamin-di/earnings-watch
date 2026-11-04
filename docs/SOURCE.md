# Source and releases

[Earnings Watch](https://github.com/benjamin-di/earnings-watch) is the maintained
source. Open issues and pull requests there. Generic source, synthetic tests and
public documentation are accepted directly; accepted contributions remain in
the public history across releases. See [Contributing](../CONTRIBUTING.md).

The `.public-source.toml` contract identifies this source and its two public
images. It also prevents a release-export writer from replacing this tree.
Historical releases remain immutable. No private history is imported by the
source-home transition, and no application state migration is implied.

Releases build from a checked public commit and an immutable annotated version
tag. Package and registry versions must agree. Public images retain the names
`earnings-watch` and `earnings-watch-worker`; their source and revision labels
identify this repository and the exact tagged public commit. Package names,
CLI entry points, MCP names and data formats do not change with the source home.

The public Release workflow remains responsible for the tested distributions,
image architectures, release checksums, PyPI attestations and MCP Registry
publication. Consumers verify that publication and pin image digests. A source
commit or tag alone is not a published release.

Public CI runs the complete source gate and retains its JUnit report. It also
qualifies both image targets on amd64 and arm64, retaining phase receipts and
logs on failure. Pull requests use the same checks on hosted runners with public
read access. Release qualification requires the exact public main commit's
successful push checks and reports; a pull-request, manual or version-tag check
does not substitute for that source evidence.
