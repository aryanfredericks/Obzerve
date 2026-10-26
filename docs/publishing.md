# Publishing the Python SDK

The `obzerve` package on PyPI is built from `sdk/python/` and published by GitHub Actions (`.github/workflows/release-sdk.yml`) when a tag `sdk-vX.Y.Z` is pushed. Uploads use [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/): PyPI trusts this repository's workflow directly, so no API token is stored anywhere.

Releases are permanent. A version number can never be uploaded twice, even after it is deleted, and the project name `obzerve` is claimed by the first successful upload. That's why every release goes to TestPyPI first.

## One-time setup (repository owner)

Do these steps once, before the first release.

**1. Register the publisher on TestPyPI and on PyPI.** The project doesn't exist yet, so add it as a *pending* publisher. Log in, then open *Your account → Publishing → Add a new pending publisher* (TestPyPI: https://test.pypi.org/manage/account/publishing/ , PyPI: https://pypi.org/manage/account/publishing/). Enter:

| Field | Value |
| --- | --- |
| PyPI project name | `obzerve` |
| Owner | `aryanfredericks` |
| Repository name | `Obzerve` |
| Workflow name | `release-sdk.yml` |
| Environment name | `testpypi` on TestPyPI, `pypi` on PyPI |

TestPyPI and PyPI are separate sites with separate accounts, so register on both.

**2. Create the two GitHub environments.** On GitHub, open *Settings → Environments → New environment*, and create `testpypi` and `pypi`. On `pypi`, add yourself under **Required reviewers**, so every release waits for your approval before it reaches PyPI. Optionally, limit both environments to tags matching `sdk-v*` (*Deployment branches and tags*).

## Releasing a version

1. Update `__version__` in `sdk/python/src/obzerve/_version.py`, and add a section to `sdk/python/CHANGELOG.md`.
2. Check locally: `make sdk-check`. It builds the wheel, checks the metadata as PyPI will, and runs the SDK tests against the installed wheel on Python 3.10 (oldest supported dependencies) and 3.13 (newest).
3. Merge to `main`. CI must pass.
4. Tag the merge commit and push the tag:

   ```bash
   git tag sdk-v0.1.0
   git push origin sdk-v0.1.0
   ```

5. Watch the **Release SDK** workflow under the repository's *Actions* tab:
   - It builds the package and checks that the tag matches `__version__`.
   - It tests the wheel, then publishes to TestPyPI.
   - It waits for your approval on the `pypi` environment, then publishes to PyPI.
   - Finally it creates a GitHub release with the files attached.
6. Verify that the release installs: `pip install obzerve==0.1.0`. For TestPyPI, use `pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ obzerve==0.1.0` (dependencies come from PyPI).

Running the workflow by hand (*Actions → Release SDK → Run workflow*) builds and tests without publishing.

## If something goes wrong

- **The tag doesn't match the version:** the build job stops before anything is uploaded. Delete the tag (`git push --delete origin sdk-vX.Y.Z`), fix `_version.py`, and tag again.
- **TestPyPI fails:** nothing has reached PyPI. Fix the problem and release a *new* version number if the TestPyPI upload already succeeded.
- **A broken version reached PyPI:** don't delete it; deleting doesn't free the number. Yank it on PyPI (*Manage → Releases → Yank*) and release a fixed version.
