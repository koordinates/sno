Notes on Preparing a Sno Release
================================

This process only supports a single release branch (master). It'll need to be expanded over time.

### Prerequisites

1. Make sure you're on `master`.

2. Check tests are all passing.

3. Decide on your version number. We use [Semantic Versioning](https://semver.org/) with [PEP-440 version numbering](https://www.python.org/dev/peps/pep-0440/):
   * Version numbers are eg: `1.2.3` or `1.2.3b3` or `1.2.3rc1`
   * The Git tag version format is `v{Ver}`, eg: `v1.2.3` or `v1.2.3b3` or `v1.2.3rc1`

### Release

4. Update `sno/VERSION` to the new version number.

5. Update any version numbers in `README.md` to point to the new version.

6. Update `CHANGELOG.md` to refer to the new version (ie, make sure it doesn't say "UNRELEASED" next to all the latest changes.)

7. Commit with a message like "Release v1.2.3"

8. Tag the release with the Git tag version format and push it:
   ```console
   $ git tag v1.2.3
   $ git push origin v1.2.3
   ```

9. CI will build and sign the installers and packages, and create a [new draft release in github](https://github.com/koordinates/sno/releases). Check CI passes and the RPM/DEB/MSI/PKG archives are all attached.

10. Write the release notes. Use `CHANGELOG.md` as a starting point. Topic/section suggestions:
    * Overview
    * New features
    * Compatibility / Upgrading
    * Bugs fixed
    * External Contributors

11. Release the new release by clicking "Publish release".

12. If it's _not_ an alpha/beta/candidate release, update the Homebrew Tap:

    1. Get the SHA256 hash of the macOS PKG installer: `sha256 Sno-1.2.3.pkg`
    2. Pull [homebrew-sno](https://github.com/koordinates/homebrew-sno/)
    3. Edit `Casks/sno.rb` and update the `version` and `sha256` fields
    4. Commit with a message like "Update to release v1.2.3"
    5. Push

### Cleanup

13. Update `sno/VERSION` to the next [development Python version](https://www.python.org/dev/peps/pep-0440/#developmental-releases) eg: `1.2.4.dev0`. If it's an alpha/beta/rc release, then it should be set to the next _release version_ with `.dev0` appended.

14. Commit with a message like "Set development version as v1.2.4.dev0" and push
