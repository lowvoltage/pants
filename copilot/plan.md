# Implementation Plan: Selective uv sync for PEX Builds

## Problem

When building a PEX from a uv lockfile, Pants always syncs the **entire** lockfile into a
venv (via `uv sync --frozen --all-extras`), even when the target only needs a small subset
of packages. For ML-heavy resolves with large packages (torch, CUDA libs), this causes
out-of-disk failures on CI agents.

The pex resolver doesn't have this issue — it subsets the lockfile natively per target. The
uv resolver regressed this capability.

## Proposed Approach

Use uv's `--only-group` and `--inexact` flags to selectively sync only the required
packages.

**Critical insight**: `uv sync --frozen --only-group` requires the dependency group to
exist in the lockfile at generation time. Simply adding an ephemeral group at sync time
and using `--frozen` will fail. However, since `generate_pyproject_toml()` is shared by
both lockfile generation (`generate_uv_lockfile` in `lockfile.py:344`) and sync
(`create_venv_repository_from_uv_lockfile` in `uv.py:164`), we can add per-requirement
dependency groups during lock generation, and they'll be baked into the lockfile.

**Strategy**: During lockfile generation, create one dependency group per top-level
requirement (e.g., `dep-torch`, `dep-requests`). During sync, use
`uv sync --frozen --only-group dep-requests --only-group dep-metatron --inexact` to
install just those packages and their transitive deps. The `--inexact` flag prevents
removing previously-installed packages, preserving the shared-venv caching model.

uv 0.11.6 (the version Pants pins) supports `--only-group` and `--inexact`.

## Data Flow (Current vs. Proposed)

### Current flow (always downloads everything):
```
target (2 deps) → PexRequirements(from_superset=Resolve)
  → _setup_pex_requirements → detects UV lockfile
  → build_pex → create_venv_repository_from_uv_lockfile
  → generate_pyproject_toml(ALL metadata.requirements)
  → uv sync --frozen --all-extras   ← installs ALL packages
  → pex --venv-repository=... --no-transitive <2 req strings>
```

### Proposed flow (downloads only what's needed):
```
Lockfile generation:
  → generate_pyproject_toml(ALL reqs, with per-req dependency groups)
  → uv lock   ← lockfile now contains group metadata

PEX build for target (2 deps):
  → _setup_pex_requirements → detects UV lockfile, stashes req_strings
  → build_pex → create_venv_repository_from_uv_lockfile(subset=["requests", "metatron"])
  → generate_pyproject_toml(ALL reqs, with per-req groups)
  → uv sync --frozen --only-group dep-requests --only-group dep-metatron --inexact
  ← installs only those 2 packages + transitive deps
  → pex --venv-repository=... --no-transitive <2 req strings>
```

### EntireLockfile case (tools, run_against_entire_lockfile):
No change — continues to sync everything via `uv sync --frozen --all-extras`.

## Implementation Steps

### 1. Modify `generate_pyproject_toml` to emit per-requirement dependency groups
**File**: `src/python/pants/backend/python/util_rules/uv.py`

Add per-requirement dependency groups to the generated pyproject.toml. Each top-level
requirement gets its own group named after the canonicalized package name:

```python
def generate_pyproject_toml(resolve: str, ics: InterpreterConstraints, reqs: Iterable[str]) -> str:
    # ... existing [project] section (unchanged) ...

    # Add per-requirement dependency groups for selective sync.
    # Group names use the canonicalized package name to enable
    # subset installs via `uv sync --only-group <name>`.
    groups: dict[str, str] = {}
    for r in sorted(reqs):
        parsed = Requirement(r)
        group_name = canonicalize_name(parsed.name)
        groups[group_name] = f'["{escape_double_quotes(r)}"]'

    if groups:
        content += "\n[dependency-groups]\n"
        for name, deps in groups.items():
            content += f'{name} = {deps}\n'

    return content
```

This change affects both lockfile generation and sync (they share the function), ensuring
groups are baked into the lockfile.

### 2. Modify `VenvFromUvLockfileRequest` to accept subset requirements
**File**: `src/python/pants/backend/python/util_rules/uv.py`

```python
@dataclass(frozen=True)
class VenvFromUvLockfileRequest:
    lockfile: LoadedLockfile
    python: PythonExecutable
    # If set, only these requirements (and their transitive deps) will be installed.
    # If None, the entire lockfile is synced.
    subset_req_strings: tuple[str, ...] | None = None
```

### 3. Modify `create_venv_repository_from_uv_lockfile` for selective sync
**File**: `src/python/pants/backend/python/util_rules/uv.py`

When `subset_req_strings` is provided:
- Derive group names from the canonicalized package names of the subset
- Add `--only-group <name>` for each subset requirement
- Add `--inexact` to prevent removing previously-installed packages
- Remove `--all-extras` (not applicable with `--only-group`)

When `subset_req_strings` is None (EntireLockfile case):
- Keep current behavior unchanged (`--all-extras`)

```python
if request.subset_req_strings is not None:
    group_args = []
    for req_str in request.subset_req_strings:
        parsed = Requirement(req_str)
        group_name = canonicalize_name(parsed.name)
        group_args.extend(["--only-group", group_name])
    uv_sync_args = ["--frozen", "--no-install-project", "--inexact", *group_args]
else:
    uv_sync_args = ["--frozen", "--no-install-project", "--all-extras"]
```

### 4. Pass requirement strings from `build_pex` to the venv request
**File**: `src/python/pants/backend/python/util_rules/pex.py`

In `build_pex()`, pass the available `req_strings` to `VenvFromUvLockfileRequest`:

```python
venv_repo = await create_venv_repository_from_uv_lockfile(
    VenvFromUvLockfileRequest(
        lockfile=requirements_setup.uv_lockfile,
        python=pex_python_setup.python,
        subset_req_strings=req_strings if req_strings else None,
    ),
    **implicitly(),
)
```

`req_strings` is already available at this point — populated from
`get_req_strings(request.requirements)` for `PexRequirements` (line 785), and is `()`
for `EntireLockfile` (line 792).

### 5. Handle lockfile digest in venv cache key for `--inexact` mode
**File**: `src/python/pants/backend/python/util_rules/uv.py`

The `--inexact` flag means stale packages from old lockfile versions could accumulate.
Include the lockfile content hash in the venv path when using `--inexact`:

```python
if request.subset_req_strings is not None:
    lock_hash = hashlib.sha256(uv_lock_contents[0].content).hexdigest()[:12]
    venv_path_suffix = os.path.join(
        buildroot_entropy, metadata.resolve, request.python.fingerprint, lock_hash
    )
else:
    venv_path_suffix = os.path.join(
        buildroot_entropy, metadata.resolve, request.python.fingerprint
    )
```

This ensures a new venv is created when the lockfile changes, preventing stale packages.

### 6. Regenerate existing lockfiles
Users must regenerate their uv lockfiles after upgrading to pick up the new dependency
group structure. Add a note in upgrade documentation. Existing lockfiles without groups
will continue to work (full sync, no subsetting).

### 7. Add tests
**File**: `src/python/pants/backend/python/util_rules/pex_test.py`

- **Integration test**: Generate a uv lockfile with multiple deps (one large, one small),
  build a PEX requiring only the small dep, verify only the small dep's packages are in
  the venv.
- **Unit test**: Verify `generate_pyproject_toml` produces correct `[dependency-groups]`
  section.
- **Stale cache test**: Generate lockfile v1, build with subset, update lockfile v2
  (removing a package), build again, verify old package isn't used.

**File**: `src/python/pants/backend/python/goals/lockfile_test.py`
- Verify generated uv lockfile contains dependency group metadata.

## Edge Cases and Considerations

1. **Group name collisions**: Requirement names are canonicalized (PEP 503) to produce
   group names. Two different requirement specifiers for the same package (e.g.,
   `torch>=2.0` and `torch==2.12.0`) map to the same group. This is correct — there
   should be only one version of each package in a resolve.

2. **Extras**: A requirement like `package[extra1,extra2]` gets a group named `package`.
   The group contains the full requirement string with extras, so uv will install the
   extras correctly.

3. **Venv accumulation with `--inexact`**: Within a single lockfile version, the venv
   grows monotonically as different targets add their deps. This is bounded by the lockfile
   size (worst case = full sync) and is correct since pex subsets from the venv anyway.

4. **Concurrency**: uv handles concurrent `uv sync` with locking (noted in existing code
   comments). The `--inexact` flag doesn't change this.

5. **EntireLockfile path**: Unchanged. Tools and `run_against_entire_lockfile` continue to
   sync everything with `--all-extras`.

6. **Cross-platform PEX**: Already errors out for uv lockfiles. No change needed.

7. **Backward compatibility**: Old lockfiles without groups will not have the group
   metadata. The `subset_req_strings` path should gracefully fall back to full sync when
   groups are not present in the lockfile (detected by catching uv's error or checking
   metadata).

8. **Number of groups**: For resolves with many requirements (100+), the pyproject.toml
   will have many groups. uv should handle this fine — groups are lightweight metadata.
