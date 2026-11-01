# Fork-only XZ experiment

This branch is an experiment on `nieyuanhong/z3`, based on native-cache PR
#11496 at bbc18646ccbc487b17d8747cd7df36f700b89536. It is not an upstream PR,
release format change, or Maven publication. No publishing credentials are used.

Compare the same current-branch Java loader and pinned official 5.3.2 native
libraries in platform JARs and one four-platform universal JAR. The baseline
explicitly applies DEFLATE level 9 to each ZIP entry (the fix in PR #11492).
The optional compact form stores a separate XZ preset-6/CRC64 stream per library
as a STORED JAR resource. It never decompresses another platform's libraries.

Compact JARs are standalone: XZ for Java 1.12 is built from the SHA-256-pinned
official source archive, compiling only Java 8 baseline sources with `--release
8`. Its package is relocated in source to `com.microsoft.z3.internal.xz` to
avoid the application's `org.tukaani.xz` version. Copyright, 0BSD license,
authors, source URL/hash and modification notice are embedded. No JDK 9
multi-release classes, bytecode rewriting tool or third-party build plugin is
needed. This measures the baseline decoder, omitting its JDK 9 optimizations.

The ordinary raw-resource path and conventional system loading remain available.
Only compact resources use the reflected embedded decoder (64 MiB decoder
memory limit). Cache identity binds both compressed content hashes and declared
raw SHA-256 values; extracted files are checked before loading and on reuse.
Warm hits read/hash compressed resources and cached raw files without decoding
XZ. Cold extraction verifies the decoded bytes against the declared raw hash.
Private fallback performs the same decoded hash check. This is an experimental
metadata format, not a new official cache/packaging contract.

The fork workflow produces the JARs once and transports those exact artifacts
to Linux x64/ARM64, Windows x64, and macOS ARM64 runners. It compiles no native
C++ code. Each actual runner tests both packaging forms and formats, version,
Context, SAT/UNSAT, 5 interleaved cold starts, 10 warm starts, and 5 private
extractions per form/format. Warm runs check native file hashes/size/mtime and
that no private fallback occurred. Existing cache failure/concurrency/classloader
regressions run on both compact JAR forms. Negative tests cover corruption,
wrong/missing raw hashes, missing decoder, excessive decoder memory and a
conflicting public decoder. All classes must have Java 8 bytecode.

Receipts record exact commit, loader/JAR hashes, workflow run/attempt, JVM,
individual timings and medians. Tests use an isolated writable user profile/cache instead of modifying the
developer's or runner's real profile. A private fallback fails a cache timing
sample, rather than being silently reported as a cache hit.
`load_ms` measures actual Native initialization;
`wall_ms` also includes JVM startup and the solver probe. OS filesystem caches
are not cleared. Native/JVM/antivirus differences prevent treating timings as
portable benchmarks. Platforms absent from a successful receipt are unverified.
The experiment must finish and its receipts be checked before discussing adoption.
